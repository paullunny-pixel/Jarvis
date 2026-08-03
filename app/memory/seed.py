"""The Day-One Brain seed (source: docs/Jarvis-Day-One-Brain.md), curated into
memory chunks and living facts. Idempotent — loads once, versioned so future
seed updates can top up without duplicating.
"""
from __future__ import annotations

import logging

from app.core.store import SettingsStore
from app.memory.store import LivingFacts, MemoryStore

logger = logging.getLogger(__name__)

SEED_VERSION = "4"

# (content, room, type, tags)
STABLE_CHUNKS: list[tuple[str, str, str, list[str]]] = [
    # --- ROOM 1: YOU ---
    ("Paul Lunny, born 22 August 1989, British. Turns 37 on 22 Aug 2026.", "you", "STABLE", ["identity"]),
    ("Paul's home bases: UK (Nottingham, for now) and Dubai, moving roughly weekly. His timezone auto-switches between the two.", "you", "STABLE", ["living-situation", "travel"]),
    ("Paul owns a Dubai apartment where his girlfriend Steph lives — separate from the Sobha Elwood villa being built.", "you", "STABLE", ["living-situation", "dubai"]),
    ("ADHD operating manual: Paul's core failure mode is inaction/paralysis — he knows exactly what to do, knows it's urgent, and still freezes ('can't see the wood for the trees'). This is the enemy Jarvis exists to beat.", "you", "STABLE", ["adhd", "operating-manual"]),
    ("The tell for a bad Paul day: he missed his run. If the run didn't happen, assume a harder day and lean in more.", "you", "STABLE", ["adhd", "operating-manual", "run"]),
    ("What breaks Paul's freeze: getting the run in, music (a big lever), one clear single next-action, and urgency. When he's avoiding, come in hard — he wants the direct push, not gentle coaxing.", "you", "STABLE", ["adhd", "operating-manual"]),
    ("Paul's energy pattern: rough in the first hour of the day (coffee, slow wake — no heavy asks). Best work in the evenings. Daytime is fragmented by his kids, especially in school holidays.", "you", "STABLE", ["adhd", "rhythm"]),
    ("Paul's mood and productivity are linked: a bad day is a day nothing got done; he always feels better after a productive day. Jarvis protects at least one real win per day.", "you", "STABLE", ["adhd", "operating-manual"]),
    ("Paul's non-negotiables, never scheduled over and never let slide: the daily 5km run (keystone), sobriety, his diet, and his supplements — vitamins, minerals, testosterone (TRT), ADHD medication.", "you", "STABLE", ["non-negotiables"]),
    ("Paul's deeper why: long-term financial stability (work optional, passive income covers life), paying off the Dubai villa as a permanent base, getting a six-pack and into the best shape of his life, and using the 12-month mission to get every area right so he can settle down, start his family, and be happy with the right person.", "you", "STABLE", ["why", "motivation"]),
    ("Paul wants an onboarding session going through Trello card-by-card with Jarvis to teach it everything about his work.", "you", "STABLE", ["onboarding", "trello"]),
    # --- ROOM 2: COMPANIES ---
    ("Paul runs four companies in aesthetics: Derma Direct UK, Derma Direct EU, Aesthetics Supply UK, and Prodermis (own manufacturing brand). Sector: supply/distribution of practitioner products plus own-brand manufacturing.", "companies", "STABLE", ["overview"]),
    ("Sourcing: Derma Direct UK, Derma Direct EU and Aesthetics Supply all buy product from Korea and the EU.", "companies", "STABLE", ["sourcing"]),
    ("Shared blocker — Dutch company formation: until the Dutch entity is formed, EU stock (fillers and skincare) is stuck in a bonded warehouse in the Netherlands. This blocks both Derma Direct EU trading and Prodermis EU distribution. Major recurring sticking point tracked to resolution.", "companies", "STABLE", ["derma-eu", "prodermis", "blocker", "dutch-formation"]),
    ("Derma Direct UK: UK online wholesaler, 100% e-commerce, selling to doctors, nurses and aesthetic practitioners (~90% non-medics). Open till 10pm, 7 days a week. Sells dermal fillers, skin boosters, gloves — everything a practitioner needs.", "companies", "STABLE", ["derma-uk"]),
    ("Derma Direct UK history: established 6 years, formerly Filler Direct, rebranded after Paul's divorce. About 10 staff.", "companies", "STABLE", ["derma-uk"]),
    ("Derma Direct UK who runs it: Paul + Harry (~90% of day-to-day ops); marketing is Paul; finance is Kiefer.", "companies", "STABLE", ["derma-uk", "people"]),
    ("Derma Direct UK growth plays underway: new account manager hired, ads running, website relaunch imminent, big retention-marketing push through July–August.", "companies", "STABLE", ["derma-uk", "growth"]),
    ("Derma Direct UK key brands/products: Nexfill (own product, Paul is distributor), LumiEyes, DermaN (brand owned by Acmedy, Korea), Revitrain (by BR Farm), plus others.", "companies", "STABLE", ["derma-uk", "products"]),
    ("Derma Direct EU: same model as Derma Direct UK but sells EU + US only, never the UK (Brexit). Essentially a photocopy of the UK business once live. Not yet trading — company still being formed, tied to the Dutch formation blocker.", "companies", "STABLE", ["derma-eu"]),
    ("Aesthetics Supply UK: sells 'grey' products — not illegal in the UK but a legal grey area. ~40 registered accounts, effectively no customer base yet; needs marketing to activate. Same Korea/EU sourcing.", "companies", "STABLE", ["aesthetics-supply"]),
    ("Prodermis: Paul's own manufacturing brand — dermal fillers, boosters, mesotherapy and skincare. Distribution: Alicia handles overseas distributor markets; Jane and Karen are UK distributors.", "companies", "STABLE", ["prodermis"]),
    ("Prodermis manufacturers: BMI makes the dermal fillers only — a high-pressure contract with very large MOQs, growth demands (UK first, then EU) and document issues; key relationship to manage. Michael at Pyway Medical (agent) makes the eye booster, skin booster and 6 mesotherapy products, subcontracting by product. A Barcelona manufacturer makes the skincare range.", "companies", "STABLE", ["prodermis", "bmi", "manufacturing"]),
    ("Prodermis priorities: get UK sales to £5,000/month, and obtain product registration documents for every distributor country so distributors can register with their Ministry of Health (currently struggling — needs BMI's help).", "companies", "STABLE", ["prodermis", "priorities"]),
    # --- ROOM 3: HEALTH ---
    ("Paul has trained on and off since age 16; historically intermittent — consistency is the goal. Trains every day. Split: Push/Pull/Legs/rest. Daily run, prefers outdoor, treadmill fine.", "health", "STABLE", ["training"]),
    ("Bariatric history (important): Paul had a gastric sleeve; he was previously ~24 st (~152 kg) and has lost a large amount. Long-standing struggle with weight and eating habits. Nutrition must be supportive, sustainable and protein-forward; small stomach capacity is a hard constraint; flag if intake drops too low.", "health", "STABLE", ["bariatric", "nutrition"]),
    ("Training access points: UK home gym (only until the move ~20 Oct), the gym next to the warehouse, the Dubai building gym, and hotel gyms when travelling. Paul likes all exercises and is happy to swap based on available equipment — keep an equipment profile per location and auto-adapt each session.", "health", "STABLE", ["training", "equipment"]),
    ("Eating pattern (gastric sleeve): 6–8 small meals a day. Paul likes to cook once in the morning and weigh out portions across the day, and is completely happy eating the same macro-perfect 'cookie-cutter' meal daily. Likes chicken, beef, eggs, most vegetables, quick easy repeatable food. Dislikes fish. Cook-at-home in the UK, order-in equivalents in Dubai.", "health", "STABLE", ["nutrition"]),
    ("Paul's body goal: ~11–11.5 st (70–73 kg), lean with serious muscle — a recomposition, plus the six-pack / best shape of his life within 12 months. Healthy and sustainable given the bariatric history; protein priority; consistency over intensity.", "health", "STABLE", ["goals"]),
    ("Health baselines still to establish: book a body scan, book a blood test, set a 5km benchmark, capture lift baselines from first live-coached sessions, set up macro tracking (likely MyFitnessPal).", "health", "STABLE", ["actions", "baselines"]),
    # --- ROOM 4: FINANCES ---
    ("Dubai villa: Sobha Elwood SEL-V 202, price AED 11,638,800. Payment strategy: pre-fund monthly (~AED 200k set aside) so the 6-monthly demands aren't a big hit. Paul is paid personally, then pays the villa from that.", "finances", "STABLE", ["villa"]),
    ("Villa actions: get Sobha to log the 170,000 AED payment that isn't recorded, and dispute the AED 7,601 late fee.", "finances", "STABLE", ["villa", "actions"]),
    ("UK banking: main UK business account, Revolut (Derma Direct UK), Tide (UK business — to confirm; direct debits managed with Kiefer). Derma Direct EU has no accounts yet (company unregistered). UAE personal: Emirates NBD, FAB, Mashreq, 'MBD' (confirm exact bank).", "finances", "STABLE", ["banking"]),
    ("Paul's freedom number: ~£50,000/month after tax — the point where work becomes optional. He doesn't spend it all; it's the target earn. Personal finances need a full reorganisation — a dedicated project Paul and Jarvis tackle together.", "finances", "STABLE", ["goals"]),
    # --- ROOM 5: PEOPLE ---
    ("Kiefer Brindle — CFO (ops, IT and finance), based in Manchester. Paul's right-hand man; receives the friendly nightly summary. Paul wants to spend more time with him.", "people", "STABLE", ["team", "kiefer"]),
    ("Adriana — project manager across all projects ('a bit of everything'), based in Calgary; works ~11:30am UK time into the evening. Appears as 'Adrianna' on Trello.", "people", "STABLE", ["team", "adriana"]),
    ("Harry — operations and warehouse manager, Northampton. Helps with everything; runs ~90% of Derma Direct UK day-to-day. Brother of Paul's ex, Frankie.", "people", "STABLE", ["team", "harry"]),
    ("Alicia — international business development manager for Prodermis, based in Dubai. Manages overseas distributor markets (regions to confirm).", "people", "STABLE", ["team", "alicia"]),
    ("Kenny — head of customer service for Derma Direct UK and more, based in Málaga, Spain.", "people", "STABLE", ["team", "kenny"]),
    ("Mohammed — web developer. Marco — CRM developer, building the new CRM. Vadim — video creation. Ella — social media across all companies.", "people", "STABLE", ["team"]),
    ("Jane and Karen — UK distributors for Prodermis. Karen is linked to the '5-for-5 boosters' task.", "people", "STABLE", ["team", "prodermis"]),
    ("John — friend and former employee, now pensioned off at £6k/month, based in Northampton. Talks with Paul most days; wants to find long-term value from his arrangement.", "people", "STABLE", ["john"]),
    # --- Active personal projects (Room 1, living situation) ---
    ("Nottingham rental: Paul must move out by 20 Oct 2026 and clear/dispose of all belongings first. Tracked as a hard deadline with countdown and a declutter plan.", "you", "STABLE", ["nottingham", "deadline"]),
    ("Paul co-owns a UK house with his ex, Frankie, which needs to be sold. A big ongoing project with next-actions.", "you", "STABLE", ["house-sale", "frankie"]),
]

# Family lives in the PEOPLE room: Jarvis needs it to plan Paul's real life
# (birthdays, custody rhythm, school holidays). It still never reaches outbound
# reports — the Kiefer note composes from task/streak data only, by construction.
FAMILY_CHUNKS: list[tuple[str, str, str, list[str]]] = [
    ("Steph — Paul's girlfriend, Brazilian, lives in his Dubai apartment. Paul currently supports her financially. (Complicated — details to come.)", "people", "STABLE", ["steph", "family"]),
    ("Eva — Paul's daughter, 8, birthday 28 September. Loves K-pop, baking, arts & crafts, dancing, reading.", "people", "STABLE", ["eva", "family"]),
    ("Jack — Paul's son, 7, birthday 2 October. Loves Minecraft, Super Zings, Pokémon.", "people", "STABLE", ["jack", "family"]),
    ("Jade — Paul's ex-wife, mother of Eva and Jack; receives £1,150/month child support.", "people", "STABLE", ["jade", "family"]),
    ("Frankie — a different ex-partner; co-owns the UK house being sold; Harry's sister.", "people", "STABLE", ["frankie", "family"]),
]

# v3 (3 Aug 2026): Paul's year of ChatGPT memory, imported from his export
# ('What ChatGPT Knows About Paul'). Only what the Day-One Brain didn't already
# hold. Sensitive lines go to the private room — encrypted, support-space only.
CHATGPT_CHUNKS: list[tuple[str, str, str, list[str]]] = [
    ("How Paul likes to be answered (a year of his own ChatGPT habits): detailed, practical, commercially focused. He often asks for an expert lens — think like a CEO, lawyer, doctor, strategist, regulator, even a theologian — and he prefers explanations over excuses, in both directions.", "you", "STABLE", ["working-style", "chatgpt-import"]),
    ("Paul's interests beyond work: entrepreneurship, medical aesthetics, psychology, neuroscience, addiction science, aviation, Roman history, Catholic theology, AI and technology, and luxury cars. Real doors for real conversation — use them.", "you", "STABLE", ["interests", "chatgpt-import"]),
    ("Paul spends considerable time trying to understand himself — psychology and self-knowledge genuinely matter to him. He responds to honest observations about his own patterns, not platitudes.", "you", "STABLE", ["self-knowledge", "chatgpt-import"]),
    ("Travel Paul has planned or discussed at length: South Korea (his supplier country), Italy, and Dubai — business meetings, hotels, restaurants and aesthetics clinics all in scope.", "you", "STABLE", ["travel", "chatgpt-import"]),
    ("Paul often asks for Brazilian Portuguese translations so Steph can understand important topics — language is one of the ways he looks after her.", "people", "STABLE", ["steph", "language", "chatgpt-import"]),
]

# v4 (3 Aug 2026): Paul told the engineer he has dyslexia — this governs how
# every part of the system reads his words.
DYSLEXIA_CHUNKS: list[tuple[str, str, str, list[str]]] = [
    ("Paul has dyslexia. Spellings wobble, autocorrect mangles words ('quite day' means 'quiet day'), and voice transcripts garble. ALWAYS read his messages for meaning, never take an odd spelling literally, and NEVER comment on or correct his spelling — just understand him.", "you", "STABLE", ["dyslexia", "operating-manual"]),
]

CHATGPT_PRIVATE_CHUNKS: list[tuple[str, str, str, list[str]]] = [
    ("From Paul's year with ChatGPT: chronic loneliness, feeling misunderstood, and often not feeling emotionally safe are his deepest recurring themes. Not feeling safe is his biggest drinking trigger; he knows he cannot drink moderately, and shame follows relapses. Emotional safety is the ground everything else stands on.", "private", "PRIVATE", ["sobriety", "emotional-safety", "chatgpt-import"]),
    ("Paul has explored a previous or possible diagnosis of Borderline Personality Disorder alongside his ADHD, plus childhood trauma and emotional-regulation work. Support-space territory only — never raised unprompted, never in business context.", "private", "PRIVATE", ["mental-health", "chatgpt-import"]),
]

PRIVATE_CHUNKS: list[tuple[str, str, str, list[str]]] = [
    ("Sobriety triggers and how Jarvis gets ahead of each: flying/travel days (check in before and after flights, pre-plan the journey, extra presence); work events especially trade shows (high-risk — prep beforehand, stay close during, decompress after); loneliness, a major trigger (watch for quiet evenings, time away from the kids/Steph, low engagement — reach out first, nudge a call to John, Kiefer or Steph); not feeling happy/fulfilled (reconnect to the deeper why and the day's wins); overwhelm around the kids, worst in school holidays (keep work light, offer decompression, normalise that it's hard — gentle, never guilt).", "private", "PRIVATE", ["sobriety", "triggers"]),
    ("Sobriety approach: proactive and pattern-aware — cross-reference sleep, mood, isolation and the travel/event calendar to anticipate hard days. SOS on demand. Celebrate milestones. Always 'I've got you', never a scoreboard. Optional additions when Paul's ready: a who-to-call shortlist and a preferred professional/helpline resource on file.", "private", "PRIVATE", ["sobriety", "approach"]),
]

# (key, value, room)
LIVING_SEED: list[tuple[str, str, str]] = [
    ("health.weight", "13 st 13.5 lb (~88.7 kg) — baseline at day one", "health"),
    ("health.body_fat", "unknown — body scan to be booked for baseline", "health"),
    ("health.5km_time", "no benchmark yet — set one on an early run", "health"),
    ("villa.price", "AED 11,638,800 (Sobha Elwood SEL-V 202)", "finances"),
    ("villa.paid", "~27% paid (~AED 3.10M including the unrecorded 170k)", "finances"),
    ("villa.next_demand", "~AED 1.2M due ~7 Jan 2027 (6 months from 7 Jul 2026)", "finances"),
    ("villa.flags", "get Sobha to log the 170,000 AED; dispute the AED 7,601 late fee", "finances"),
    ("commitments.jade", "£1,150/month child support to Jade", "finances"),
    ("commitments.john", "£6,000/month to John", "finances"),
    ("commitments.steph", "ongoing financial support for Steph (Dubai)", "finances"),
    ("companies.derma_uk.turnover", "~£1M/month (down from ~£1.5M) — stable but shrinking slightly; priority is stopping the decline and returning to growth", "companies"),
    ("companies.prodermis.uk_sales_target", "grow UK sales to £5,000/month", "companies"),
    ("companies.dutch_formation", "in progress — blocks Derma Direct EU trading and Prodermis EU distribution (stock in NL bonded warehouse)", "companies"),
    ("deadlines.nottingham_moveout", "move out of Nottingham rental by 20 Oct 2026 (clear belongings first)", "you"),
    ("deadlines.bmi_surveys", "URGENT: 3 doctor surveys for BMI due this weekend", "companies"),
]


FAMILY_NAMES = ("Steph —", "Eva —", "Jack —", "Jade —", "Frankie —")


async def _migrate_v1_to_v2(memory: MemoryStore) -> int:
    """v1 filed family under the private room; v2 moves it to people so the
    planner brain can use it. Old private rows are marked superseded."""
    moved = 0
    for row in await memory.audit(room="private", include_private=True):
        if row["source"] == "day-one-brain" and row["content"].startswith(FAMILY_NAMES):
            entry = next((e for e in FAMILY_CHUNKS if row["content"] == e[0]), None)
            content, room, type_, tags = entry if entry else (row["content"], "people", "STABLE", ["family"])
            await memory.supersede(row["id"], content, room=room, type_=type_, tags=tags, source="day-one-brain")
            moved += 1
    logger.info("Seed migration v1→v2: %d family facts moved to the people room", moved)
    return moved


async def load_day_one_brain(
    memory: MemoryStore, living: LivingFacts, settings_store: SettingsStore
) -> int:
    """Load the seed once (versioned). Returns number of chunks written/moved."""
    current = await settings_store.get("seed_version")
    if current == SEED_VERSION:
        return 0
    count = 0
    if not current:
        for content, room, type_, tags in STABLE_CHUNKS + FAMILY_CHUNKS + PRIVATE_CHUNKS:
            await memory.add_chunk(content, room=room, type_=type_, source="day-one-brain", tags=tags)
            count += 1
        for key, value, room in LIVING_SEED:
            await living.set(key, value, room=room)
    elif current == "1":
        count += await _migrate_v1_to_v2(memory)
    # Versioned top-ups (each also part of any fresh load).
    if not current or current in ("1", "2"):
        # v3: the ChatGPT memory import.
        for content, room, type_, tags in CHATGPT_CHUNKS + CHATGPT_PRIVATE_CHUNKS:
            await memory.add_chunk(content, room=room, type_=type_, source="chatgpt-import", tags=tags)
            count += 1
    if not current or current in ("1", "2", "3"):
        # v4: dyslexia — read for meaning, everywhere.
        for content, room, type_, tags in DYSLEXIA_CHUNKS:
            await memory.add_chunk(content, room=room, type_=type_, source="engineer-note", tags=tags)
            count += 1
    await settings_store.set("seed_version", SEED_VERSION)
    logger.info("Brain seed at v%s: %d chunks written/moved", SEED_VERSION, count)
    return count
