"""§9 — send everything that changes the answer, strip everything that
doesn't. Two layers, deliberately: a regex backstop for the structured,
catastrophic leaks (bank details, card numbers, tax/company-registration
ids) that must NEVER slip through regardless of what an LLM decides, and
an LLM contextual pass for the judgement call regex can't make (a salary
tied to a name vs. a salary alone, a home address vs. a city).

The Private room never reaches this module at all — the context assembly
in service.py never queries room='private', so there is nothing here to
redact FROM it. That wall is enforced upstream, not by this file.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# --- Layer 1: regex backstop, always runs, never skippable ---

SORT_CODE = re.compile(r"\b\d{2}[-\s]?\d{2}[-\s]?\d{2}\b")
UK_ACCOUNT = re.compile(r"\b(?:account\s*(?:no\.?|number)?[:\s]*)(\d{8})\b", re.IGNORECASE)
IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")
VAT_NUMBER = re.compile(r"\b(?:VAT\s*(?:No\.?|reg(?:istration)?)?[:\s]*)?GB\s?\d{9}\b", re.IGNORECASE)
COMPANY_REG = re.compile(r"\bcompany\s*(?:no\.?|number|reg(?:istration)?)[:\s]*\d{6,8}\b", re.IGNORECASE)
UK_PHONE = re.compile(r"\b(?:\+44|0)\s?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}\b")


def _redact_pattern(text: str, pattern: re.Pattern, label: str) -> str:
    return pattern.sub(f"[REDACTED {label}]", text)


def regex_backstop(text: str) -> str:
    text = _redact_pattern(text, IBAN, "IBAN")
    text = _redact_pattern(text, VAT_NUMBER, "VAT NO")
    text = _redact_pattern(text, COMPANY_REG, "COMPANY NO")
    text = _redact_pattern(text, UK_ACCOUNT, "ACCOUNT NO")
    text = _redact_pattern(text, CARD_NUMBER, "CARD NO")
    text = _redact_pattern(text, SORT_CODE, "SORT CODE")
    text = _redact_pattern(text, UK_PHONE, "PHONE")
    return text


LLM_REDACTION_SYSTEM = """\
You prepare Paul's business context to be sent to three external AI \
vendors for a strategic review. Rewrite the text below, keeping every \
business fact that could change the answer — revenue, margin, cash \
position, P&L, runway, stock levels, debtor days, supplier/customer terms \
(pricing, volumes, notice periods, exclusivity, contract clauses), \
headcount, capacity, who does what, prior decisions, constraints.

Strip ONLY:
- Named individuals' personal data — home addresses, dates of birth, \
personal phone numbers, a salary tied to a specific name. Keep the FACT, \
drop the IDENTITY: "warehouse manager, £48k" is fine; "Harry Smith, 12 \
Acacia Ave, £48k" is not — rewrite it as the role-based version.
- Customer PII — individual customer names, addresses, order histories. \
Aggregate figures pass through untouched.

Do not remove or soften any financial figure, business term or company \
name. Return ONLY the rewritten text, nothing else.\
"""


async def redact(claude, text: str, *, skip_llm_pass: bool = False) -> str:
    """The regex backstop always runs. The LLM pass is skipped only when
    Paul explicitly says 'send it unredacted' for a session (§9 override) —
    even then the regex backstop still runs, because a bank account number
    is never a judgement call."""
    text = regex_backstop(text)
    if skip_llm_pass or not text.strip():
        return text
    try:
        rewritten = await claude.quick(text, system=LLM_REDACTION_SYSTEM, max_tokens=4000)
    except Exception:
        logger.exception("LLM redaction pass failed — regex backstop still applied")
        return text
    return rewritten.strip() or text
