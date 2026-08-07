# Code brief — Watch-wearing chaser + Smart move reminder

Two linked features. Additions on top of the existing repo; keep everything working. Build A first — B depends on it.

## A. Watch-wearing chaser
**Detection:** no fresh heart-rate samples via the Apple Health pipe for ~1 hour during the waking day = watch not on wrist.
- ⚠️ **Verify first:** confirm the Apple Health export posts heart-rate **frequently enough** that a 1-hour gap reliably means "off wrist," not "slow export." (The five-minute check before building.)

**The chase (Paul's rules — chase, never harass):**
1. **One phone call ever, per incident** — Jarvis rings once: "Your watch isn't on your wrist, sir." Never a second call for the same gap.
2. **No answer → one Telegram message** explaining the missed call: "Rang you just now — no heart-rate since 10:15, so the watch is off. Stick it on." (A missed call is never a mystery.)
3. **Re-check an hour later, silently.** Readings flowing again (he was in the bath) → say nothing, incident closed. Still nothing → the chase continues **Telegram-only**.
4. **Paul's word pauses it** — any reply like "I'm at dinner" stands Jarvis down **completely: no calls for ≥1 hour**, even if the watch still isn't detected. His reply is context, not something to argue with.

**Inherits for free:** quiet-day suppression, identical-message dedupe, **wake-hours only (never overnight)** — tunable if sleep-tracking means the watch stays on — and `override`.

## B. Smart move reminder
Replace the flat hourly "move" nudge with a pace-vs-goal model, exactly like the water pacing.
- **Only nudge if Paul is behind his Apple Health move goal** (pulled via the health pipe). **Silent when on track — silence means he's winning.**
- **Suppress entirely if the watch is off** (reuse the watch-detection from A — never nag him to move when there's no data / the watch isn't on).
- **Never while asleep**; wake-hours only; quiet-day + `override` respected.
- When behind: **one honest line with real numbers**, at most once per hour.

## Done =
Watch chaser: one call → Telegram-explains → silent re-check → Telegram-only chase, and Paul's word stands it down; wake-hours only. Smart move reminder: speaks only when behind the move goal, silent on track, never when the watch is off or he's asleep. Give a `test` trigger for each.
