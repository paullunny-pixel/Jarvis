# Code brief — Mac app v2: feedback, unified sync, sidebar hub (staged)

Builds on the working Phase-A Mac app. Additions below, built in stages. Keep the existing app working.

## 1. Recording feedback (do first — quick)
- Play a subtle **chime when recording starts** (wake word fires, or button/record), and a distinct **sound when it stops / sends**.
- Add a clear **visible recording indicator** (pulsing dot / red state) so it's obvious when the mic is live.
- **Fix "didn't hear anything":** start capturing audio the **instant** the wake word fires, with a short pre/trailing buffer so the first words aren't clipped.

## 2. Desktop notifications + spoken announcements (NOT conversation sync — Paul dropped that)
- Proactive messages (briefs, nudges, reminders — "call Harry", "time to hydrate", "leave now") currently go to **Telegram**. **Keep Telegram** as the always-on / away-from-desk channel. **Add the Mac app as a desktop channel** for when Paul's at his computer.
- **When the Mac app is running, proactive messages also arrive there**, delivered as:
  - a **native macOS notification** (banner) on the desktop; and
  - for **actionable / time-sensitive** ones, a **spoken announcement in Jarvis's voice** — *"Paul, it's time to call Harry." / "Sir, it's time to hydrate."* — preceded by a short chime.
- **Announce vs silent:** each proactive message is flagged **announce-aloud** (spoken) or **silent** (banner only) — actionable reminders speak; ambient/FYI ones stay silent so he isn't talked at all day. Tunable.
- **Dismiss:** a small **notifications queue** in the app (recent items) Paul can dismiss / mark done; native banners are dismissable too.
- **Guardrails:** respect quiet hours + quiet-day; **don't announce mid-call/meeting**; a **mute/volume** control in the app. *(Optional later: when the Mac is active, suppress the duplicate Telegram buzz.)*
- **Voice:** use Jarvis's **ElevenLabs voice** for announcements (consistent); native macOS `say` is an acceptable instant fallback.

## 3. Sidebar reference panels (ADHD memory aids)
- **"Adding to Trello" cheat-sheet** — a visible sidebar panel. **Render this content verbatim:**

  > **What I need per card — in order of importance**
  >
  > **Essential (I can't file it properly without these)**
  > 1. **Title** — what the task actually is, in your words. Short and specific: "Email BMI full sales plan" not "BMI stuff".
  > 2. **Domain** — which company it belongs to: Derma Direct UK, Derma Direct EU, Aesthetics Supply UK, Prodermis, or Personal. If it's a name I haven't got a ruling for (Revolax, AMS, BMI, LPG, JD Bio), I'll ask you once and remember your answer forever.
  > 3. **List** — where it lives: Paul Today, This Week, Brain Dump, Blocked/Waiting On, or Inbox.
  >
  > **Strongly worth giving me**
  > 4. **Priority** — P1 to P5. P1 and P2 auto-flag as Urgent.
  > 5. **Due date** — plain words are fine: "today", "friday", "next Tuesday".
  > 6. **Owner** — you, Sarah, Adriana, Harry, Kiefer, whoever's actually holding it.
  >
  > **Optional but useful**
  > 7. **Description** — any context you'll want in three weeks when you've forgotten why. Links, amounts, names.
  > 8. **Checklist** — the sub-steps. You can give each item its own owner and due date.
  > 9. **Board** — Master Board by default; say so if it's the Paul x Harry board.

- **Card-creation behaviour Jarvis must support (implied by the cheat-sheet):**
  - **Domain resolution with learn-once memory:** for ambiguous names (Revolax, AMS, BMI, LPG, JD Bio…), ask Paul the domain **once**, store the ruling in the brain, and never ask again.
  - **Priority P1–P5**, with **P1/P2 auto-flagged Urgent**.
  - **Natural-language due dates** ("today", "friday", "next Tuesday").
  - **Owner** from the People room (Paul, Sarah, Adriana, Harry, Kiefer…).
  - **Checklist** items each able to carry their **own owner + due date**.
  - **Board:** default **Master Board**; support the **Paul × Harry** board when Paul says so.
  - Jarvis still **asks for any missing Essential field** conversationally — but the visible script stays regardless.
- **"Commands" tab** — a live list of every Jarvis keyword/command (override, goodnight, wake me at, water Xml, moved, park:, focus sprint, hound me, test alarm, start a new zoom meeting, catch me up on [group], I'm in [city]…). **Generate it from the backend's command registry** so it auto-updates as new commands are added.

## 4. The hub — panels Paul has chosen (build these, in this order)
The app becomes Paul's desktop command centre. Every panel is **toggle-able** (show/hide) and reads from the existing backend — **no new brains, no duplicated logic**, these are windows onto data Jarvis already holds. Build them in the order below.

### 4a. Today's Focus (build first)
- Live view of the day's tasks — the **same** Today's Focus the cockpit and Telegram use (Paul Today + Paul Personal, max 3 per company + 3 personal). One source of truth; do not recompute it in the app.
- **Tickable in-app:** clicking a task marks it done → writes straight back to Trello and updates the streak/momentum figures everywhere else. It must feel instant (optimistic tick, reconcile on the server response, revert with a small error toast if the write fails).
- Group by company, exactly as the cockpit does, so it's visually familiar.
- Show the count remaining ("4 of 12 done") at the panel head.

### 4b. Next up — Google Calendar
- Shows the **next event**: title, time, and a countdown ("Harry call — 14:30, in 42 min"). Below it, the **rest of today** in a compact list.
- Uses the existing calendar connection. When calendar write-back (OAuth) lands, this panel gets a **"move it"** action; until then it's read-only.
- If the event has a video link, show a **Join** button that opens it directly.
- Blank state matters: "Nothing else in the diary today" reads better than an empty box.

### 4c. Portuguese lessons
- A **Brazil countdown** at the top ("22 days to Brazil — 28 Aug") plus the **readiness gauge** (speech mastery % + basics %) already specced in the Portuguese brief.
- A big **"Start today's lesson"** button that opens the voice lesson straight in the app — Paul speaks, Jarvis corrects, no typing. This is the point of it being on the desktop: one click, no phone.
- Shows today's status (done / not done) and the current Portuguese daily streak.
- After 28 Aug this panel keeps working as a general Portuguese practice launcher; the countdown just retires.

### 4d. Drag a file onto the app → into the second brain
- **Drop zone:** dragging any file (PDF, Word, image, spreadsheet) onto the app window uploads it to the **document library** and files it into the second brain — same pipeline as the existing document upload, no separate store.
- On drop, Jarvis **reads it, suggests the room and tags** (You / Companies / Health / Finances / People / Private) and shows a one-line confirmation Paul can correct in a single click — *"Filed under Companies → Prodermis. Wrong? change it."*
- If it's clearly actionable (an invoice, a signed contract, a demand notice), Jarvis says so and offers to **create a Trello card** from it.
- Show a small **recently filed** list (last 5) so Paul can see it landed and click through.

### 4e. WhatsApp groups panel — **build it now; it will sit empty until the SIM is connected**
> **Scope note:** the group *logic* — reading the groups, extracting actions, auto-promoting @-mentions, generating the missed-summary, dismiss state — is **backend work specced in `Jarvis-Build-Slice-WhatsApp-1to1.md` (Part 2)**. Build it there, not in the app. This panel is the client that renders those endpoints.
>
> **Important — do NOT defer this panel.** Paul has not yet set up the second phone/SIM, so **no group data will flow yet and the panel will look empty. That is expected, not a bug.** Build the panel **fully and completely now**, wired to the real endpoints, so that the moment the SIM is connected it lights up with **zero further code**. Don't stub it, don't leave it half-built, don't wait for live data to finish it.

- Render the **per-group "what's going on" lines** (group name, message count, one-line gist).
- Render the **actionable items list**, with **tagged items visually distinct and higher up** ("You were tagged"), showing who asked and when. Each row wires to the endpoints: **"Add to Trello"** and **"Ignore"**.
- Render the **AI missed-summary** and a **Dismiss button** wired to the dismiss endpoint. Dismiss state is server-side and shared — clearing here clears everywhere.
- Show the **uncleared badge count** from the backend on the sidebar item, so Paul can tell at a glance whether anything's waiting.
- **Two distinct empty states — get these right, they're how Paul knows what's happening:**
  - **Not connected yet** (no ingest configured — today's state): say so plainly on the panel — *"WhatsApp group reading isn't connected yet. Set up the second number and this fills in automatically."* Not a blank box, not an error, not a spinner.
  - **Connected and caught up** (ingest live, nothing outstanding): *"You're caught up — nothing needs you."*
- **Test it with fake data before shipping:** run the panel against sample group/action/tagged/summary payloads so every state (tagged item, action row, summary, dismiss, badge count) is proven working **before** the SIM exists. When real data arrives it should be a non-event.

### Later panels (not now)
- **Momentum** (streaks + water pace at a glance)
- **Goals** (villa, body — cockpit tiles)
- **"Leave now"** travel alerts (needs the maps/traffic key)
- **Deadline radar** (countdowns)
- **Privacy:** keep the **sobriety/private panel hidden or behind a toggle** — the desktop may be visible to others. This rule applies to every panel: nothing from the Private room appears on this screen by default.

## Done =
Recording chime + indicator with no clipped first words; **desktop notifications + spoken announcements** on the Mac (with silent/announce flags, a dismissable queue, and quiet-hours/mid-call guardrails), Telegram kept as the away channel; a Trello-fields cheat-sheet and an auto-updating Commands tab in the sidebar; and the five chosen hub panels — **Today's Focus (tickable), Next up (calendar), Portuguese lessons, drag-a-file-to-brain, and the WhatsApp groups panel (display only)** — each toggle-able and reading from the existing backend. Build sections 1–3 first, then section 4 panels in the order listed (4a → 4e).

**Scope rule for this whole brief:** the Mac app is a **client**. It renders and it captures — it does not hold logic that belongs in the backend. Anything that needs new server-side thinking (group intelligence, calendar write-back, lesson scoring) is specced in its own brief and built there, once, so every surface gets it.
