# Jarvis — Build Slice: the Team Radar (bird's-eye view of Harry, Adriana & Kiefer)

**What Paul asked for, in his words:** *"a section of the application where it pulls in and automatically updates my birds eye view of what Harry, Adriana and Kiefer are all working, whats delayed, whats taking to long, based on their trello boards."*

**Size:** medium. One sync worker, one derivation layer, one endpoint, two front-ends. No new vendor, no new key — it runs entirely on the Trello connection that is already live.

**Where it lives:** the **Mac app hub** (a full panel) and the **web cockpit** (the same panel, same endpoint). Paul chose both. Build the logic once behind an API and render it twice — the Mac app is a client and holds no logic of its own.

**It is also the watcher for the War Room.** Cards created and assigned from a War Room session register here (see `Jarvis-Build-Slice-War-Room.md` §7b). **One watcher, one set of thresholds, two entry points.** Do not build a second one.

---

## 1. The honesty problem — read this before anything else

**Trello activity is not the same thing as work.** Harry can spend three days on a supplier negotiation and touch Trello exactly zero times. If this panel renders that as **"Harry — 3 cards stalled"**, it is telling Paul something false about a person, in a permanent-looking dashboard, and Paul will act on it.

That is the failure mode that would make this feature worse than not having it. Everything below is shaped to avoid it:

- **The panel reports card states, never people's performance.** The unit is the card. There is no productivity score, no ranking, no per-person league table, no "Harry: 62%".
- **Language is about signal, not effort.** "No update in 6 days" — **not** "not working on it". "Overdue" — **not** "late". The distinction is small on the page and enormous in what Paul concludes from it.
- **Every flag carries its evidence** — the actual date and the actual field it came from — so Paul can see *why* something is amber and judge it himself.
- **Cards Paul owns are shown on exactly the same terms as everyone else's.** He is a column in this view like the other three. A dashboard that only measures other people is a different, worse product.

If this brief is ever trimmed, **do not trim this section.**

---

## 2. Coverage — and saying out loud what it can't see

At the last system check, Jarvis's Trello connection could see **3 boards, 64 cards**. Whether that is all of Harry's, Adriana's and Kiefer's work is **unknown, and the panel must not pretend otherwise.**

- On first run, **enumerate every board the token can reach** and list them in the panel's footer: *"Reading 3 boards: Master Board, X, Y. Last sync 18:40."*
- For each of the four people, show **which boards they appear on**. If Kiefer appears on none, the panel says **"No boards visible for Kiefer"** in his column — not an empty, healthy-looking column. An empty column reads as "nothing to worry about"; that is the exact wrong message.
- **A stale sync is a visible state.** If the last successful sync is more than 2 hours old, grey the panel and stamp it. Silently showing yesterday's picture as though it were live is the second-worst thing this feature could do.
- **Paul action, flagged now:** confirm whether the three work on any board Jarvis can't currently reach, and add it to the token's access if so. Until that's confirmed, the panel's footer should carry a one-line *"this may not be everything"* note.

---

## 3. What it pulls, and how

**Source:** the existing Trello connection. **Poll boards, not cards** — Trello rate-limits per key and per token, and a per-card fan-out over 64+ cards will hit it. One `/boards/{id}/cards` call per board with the fields below gets everything in a handful of requests.

Per card, store: `id`, `name`, `idBoard`, `idList` (+ list name), `idMembers`, `due`, `dueComplete`, `dateLastActivity`, `labels`, checklist counts, comment count, and the War Room `session_id` if it has one.

**Sync cadence:** every 15 minutes is ample, plus an immediate sync on demand when Paul opens the panel or asks a question of it. Use Trello **webhooks** on each board if they're straightforward on the current connection — they turn 15-minute lag into seconds — but **poll as the backstop regardless**; webhooks miss deliveries and a radar that quietly stops updating is worse than one that's a few minutes behind.

> **Keep your own history.** Write a snapshot row per card per sync (list, due, owner, last activity). **Do not depend on Trello's own action history** for cycle times — retention and paging are limited and outside your control. Owning the timeline is what makes §5 possible, and it costs almost nothing. **Start writing snapshots on day one even though the derived views need weeks of data to be any good** — the data can't be backfilled later.

---

## 4. The four card states

Every card sits in exactly one state. These are the only states; resist adding more.

| State | Definition | Colour |
|---|---|---|
| **On track** | Has movement, no due date breach | green |
| **Due soon** | Due inside 48h, not yet complete | blue |
| **Overdue** | Past `due`, `dueComplete` false | red |
| **No update** | No `dateLastActivity` change for **N days** while sitting in an active list | amber |

**"Active list"** means a list that represents work in progress — *Paul Today*, *This Week*, *Doing*, and their equivalents. **Backlog and Brain Dump are not active lists.** A card can sit in Backlog for six months without that meaning anything; flagging it is pure noise and will train Paul to ignore the panel within a week.

**N (the no-update threshold) is per list, configurable, and sensible by default:** 2 days for *Paul Today*, 5 days for *This Week*, never for Backlog. One global "7 days" number would be wrong at both ends.

**Overdue beats no-update** when both are true — show the harder fact.

---

## 5. "Taking too long" — the harder one Paul asked for

Overdue is easy: there's a date and it's passed. *"Taking too long"* is the question of a card that has **no due date, or a due date that keeps moving**, and is simply ageing. That's most cards, and it's the thing Paul actually notices in his gut and can't currently evidence.

Three signals, in order of how trustworthy they are:

1. **Due-date churn.** The due date has been pushed **twice or more**. This is the strongest signal in the whole feature and it is nearly impossible to see by eye on a board. *"This card has moved its due date three times since 12 July."* Available only because §3 keeps its own snapshots.
2. **Age in list against that list's own norm.** Once there are several weeks of snapshots, compute the **median days-in-list** for completed cards, per list, and flag a card that has passed roughly twice that. Comparing a card to how that list normally behaves is far more honest than a number picked out of the air. **Until there's enough history, show nothing here rather than a made-up threshold** — say *"learning what normal looks like"* in the panel and mean it.
3. **Ping-pong.** A card that has moved backwards between lists more than twice — *This Week → Doing → This Week*. Usually means blocked or badly defined, and it is nearly always worth Paul's attention.

**No signal fires on a card younger than a week.** Everything is "taking too long" if the bar is low enough.

---

## 6. The view

Three parts, top to bottom. **The order matters — what needs Paul comes first, and the pretty overview comes last.**

### 6a. "Needs you" — the top strip
The **five** things most worth Paul's attention right now, across everyone, ranked: overdue-and-assigned first, then due-date churn, then no-update, then due-soon. Each row: the card, the person, one line of evidence with the actual date, and **one action — open it in Trello**.

**Hard cap at five, and the cap is the feature.** If eleven things are wrong, showing eleven guarantees none get done. Show five and a quiet *"+6 more"*. A radar that is always fully red is a radar Paul stops reading, and then this whole build was for nothing.

### 6b. The four columns — Harry · Adriana · Kiefer · Paul
Per person: a count per state (on track / due soon / overdue / no update), then the cards themselves, **grouped by company** (Derma Direct UK · Derma Direct EU · Prodermis · Aesthetics Supply UK · Personal) so Paul can read down one company across all four people. Same colour language as the card script panel — company gold, due blue — so nothing new has to be learned.

Each card row: title, company, list, due date or age, and a state dot. Click opens it in Trello. **Read-only, in this build.** No dragging, no reassigning from here — reassignment is deliberate and lives at the War Room approval step (§7a there), not in a dashboard where it can happen with a stray click.

### 6c. The company rollup — one line each
Per company: how many cards are moving, how many are stuck, and the single oldest thing. This is the actual bird's-eye layer — the one that answers *"is Prodermis moving this week?"* without reading a single card.

---

## 7. When Paul gets told — and it is only ever Paul

**The panel is passive.** It updates in place and never notifies. Paul looking at it is the interaction.

**One push, once a day.** A single morning line alongside the existing Today's Focus: *"Team: two overdue — Harry's supplier quote (4 days), Adriana's landing page copy (2 days). Nothing else has slipped."* One line. Not a report, not a digest with sections. If nothing has slipped, **it says nothing at all** — silence is information, and a daily "all clear" trains him to ignore it.

**Interrupt only for a genuine change of state**, and at most once a day: a card crossing into overdue that Paul had personally approved, or a project going from moving to fully stalled. Nothing else earns an interrupt.

### The hard rule
> **Nothing goes to Harry, Adriana or Kiefer. Ever, in this build.** No Trello comment, no @-mention, no message, no email, no "gentle nudge". Jarvis tells **Paul**, and Paul decides what happens next. This is Paul's explicit instruction and it is not a default to be helpfully flipped, not a setting to be exposed in a preferences panel, and not something to add "just in case". If chasing the team is ever wanted, it is a separate build that Paul asks for explicitly.
>
> **Test it on the outbound side, not the intent side** — see test 9.

---

## 8. Voice

- *"How's the team doing?"* → the rollup, spoken in three sentences.
- *"What's Harry working on?"* → his active cards by company.
- *"What's slipping?"* / *"What's overdue?"* → the "needs you" list.
- *"What's taking too long?"* → the §5 signals, with the evidence — *"the BMI proposal has moved its due date three times."*
- *"How's the Google Ads project going?"* → the War Room session's cards, by state.

Spoken answers **lead with the number and the name**: *"Two things. Harry's supplier quote is four days over…"*. Paul is usually walking when he asks.

---

## 9. Tests

1. A card in Backlog with no activity for a month is **not** flagged. (The single most likely source of noise.)
2. A card in *Paul Today* untouched for 3 days shows **no update**, with the actual last-activity date visible.
3. A card overdue **and** untouched shows as **overdue**, not both.
4. A card whose due date has been pushed twice appears under "taking too long" with the churn history — verified from your own snapshots, with Trello's action history unavailable.
5. Cycle-time comparison shows **"learning what normal looks like"**, not a number, until there is enough history — and never invents a threshold.
6. A person with no visible boards renders **"No boards visible for [name]"** — not an empty healthy column.
7. Sync older than 2 hours greys the panel and stamps the time.
8. "Needs you" never shows more than five rows, and the overflow count is accurate.
9. **Nothing outbound:** run a full week of fixture data with cards overdue on all three people → assert that **zero** Trello comments, mentions, messages or emails were sent to anyone but Paul. This is a hard rule; the test asserts on the wire, not on config.
10. War Room cards from a session appear here automatically, tagged to their session, and *"how's the [project] going"* answers from them.
11. Mac app and web cockpit render from the **same endpoint** and show identical states for the same data.
12. Read-only: nothing in this panel can move, edit, reassign or delete a Trello card.

---

## 10. Done =

Paul opens the Mac app or the cockpit and sees, without asking, what Harry, Adriana and Kiefer have on, what's overdue, and what's quietly ageing — grouped by company, with the evidence attached, and honest about the boards it can't see. Once a day he gets one line about anything that slipped. **The three of them get nothing at all** — every conversation still comes from Paul. And when he says *"how's the Google Ads project going?"*, the answer comes back from the same place.
