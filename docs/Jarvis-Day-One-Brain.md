# JARVIS — Day-One Brain

*The starting knowledge base Jarvis runs from. Built with Paul, ongoing. Last updated: this session.*

---

## How this brain is organised (storage & update design for Fable)

Everything Jarvis knows is filed into **rooms** (namespaces). Each fact is tagged so Jarvis retrieves the right things and never quotes stale info:

- **`STABLE`** — facts that rarely change (names, roles, brand rules). Stored as documents, embedded for semantic recall.
- **`LIVING`** — facts that change often (weight, villa balance, deadlines, who owes what). Stored as **structured records Jarvis updates in place** — never duplicated, so it can't contradict itself.
- **`PRIVATE`** — sensitive (sobriety, health meds). Stored in a **walled-off, encrypted room** that never enters business context or the Kiefer report.
- **Scope tags** — each item is tagged by room (company / personal / people) so Jarvis can narrow retrieval (e.g. "only Derma EU regulatory").

Rooms: **1) You · 2) The four companies · 3) Health/body/training · 4) Finances & villa · 5) People · 6) Private.**

---

## ROOM 1 — YOU

### Identity `STABLE`
- **Name:** Paul Lunny
- **DOB:** 22 August 1989 (turns 37 on 22 Aug 2026) — *confirmed*
- **Nationality:** British

### Living situation & active personal projects `LIVING`
- **Nottingham** — current rental. ⚠️ **Must move out by 20 Oct 2026**, and clear/dispose of all belongings first. → *Jarvis tracks this as a hard deadline with a countdown + a declutter plan.*
- **House co-owned with ex, Frankie** — needs to be **sold**. Big project. → *Jarvis tracks as an ongoing project with next-actions.*
- **Dubai apartment** — Paul owns; girlfriend **Steph** lives there. (Separate from the Sobha Elwood villa being built.)
- Home bases day-to-day: **UK (Nottingham for now) ↔ Dubai**, moving roughly weekly. Timezone auto-switches.

### ADHD operating manual `STABLE` — *how Jarvis handles Paul*
- **The tell for a bad day:** he missed his run. If the run didn't happen, assume a harder day and lean in.
- **Core failure mode:** *inaction / paralysis.* He knows exactly what to do, knows it's urgent, knows skipping it causes pain — and still freezes. "Can't see the wood for the trees." **This is the enemy Jarvis exists to beat.**
- **What breaks the freeze:** getting the run in, **music** (big lever), a clear single next-action, and *urgency*. Jarvis should shrink the mountain to one tiny concrete step and create pressure.
- **When he's avoiding:** come in **HARD** — "Paul, stop, do this now." He *wants* the direct push, not gentle coaxing, when stuck.
- **Energy pattern:** rough in the **first hour** (coffee, slow wake — no heavy asks). Often does his **best work in the evenings.** Daytime is fragmented by the **kids**, especially in **school holidays** (kids off the coming week).
- **Wants:** to sit with Jarvis and go through Trello card-by-card to teach it everything (onboarding session).
- **Mood ↔ productivity link:** a bad day = a day where nothing got done; he always feels better after a productive day. → *Jarvis protects at least one real win per day.*

### Non-negotiables `STABLE` — *never schedule over or let slide*
- Daily **5km run** (keystone)
- **Sobriety** (private track)
- **Diet** — sacred
- **Supplements:** vitamins, minerals, **testosterone (TRT)**, **ADHD medication** — never miss.

### The deeper why `STABLE` — *use to re-motivate at the 2pm dip*
- **Long-term financial stability** — reach the point where work is optional and passive income covers life.
- **Pay off the Dubai villa** — a permanent geographical base, somewhere that's his.
- **Physical proof** — get a **six-pack**, be in the **best shape of his life** within 12 months.
- **The 12-month mission:** make *every* area — work, health, finances, mental health — as close to perfect as possible, so that in a year he can **settle down, start his family, and be happy with the right person.**

---

## ROOM 5 — PEOPLE  *(captured early — Paul introduced them in Room 1)*

### Inner circle & team `STABLE` *(contact details `LIVING` — add as available)*

| Person | Role | Based | Notes |
|---|---|---|---|
| **Kiefer Brindle** | CFO — ops, IT & finance | Manchester | Right-hand man; receives the nightly summary. Paul wants to spend more time with him. |
| **Adriana** | Project manager — a bit of everything, all projects | Calgary | Works ~11:30am UK → evening. (Appears as "Adrianna" on Trello.) |
| **Harry** | Operations & warehouse manager | Northampton | Helps with everything. Brother of Paul's ex, Frankie. |
| **Alicia** | International business development mgr (Prodermis) | Dubai | Manages overseas distributor markets *(confirm regions)*. |
| **Kenny** | Head of customer service — Derma Direct UK + more | Málaga, Spain | |
| **Mohammed** | Web developer | — | |
| **Marco** | CRM developer | — | Building the new CRM. |
| **Vadim** | Video creation | — | |
| **Ella** | Social media — all companies | — | |
| **Jane** & **Karen** | UK distributors — Prodermis | UK | Karen linked to the "5-for-5 boosters" task. |
| **John** | Friend; former employee, now pensioned off (£6k/mo) | Northampton | Talks most days; wants to find long-term value from his arrangement. |

### Family & personal `PRIVATE`
- **Steph** — girlfriend, Brazilian, lives in the Dubai apartment. Paul is currently financially supporting her. *(Complicated — details later.)*
- **Eva** — daughter, **8**, birthday **28 Sep**. Loves K-pop, baking, arts & crafts, dancing, reading.
- **Jack** — son, **7**, birthday **2 Oct**. Loves Minecraft, Super Zings, Pokémon.
- **Jade** — ex-wife; **mother of Eva & Jack**; receives £1,150/mo child support.
- **Frankie** — different ex-partner; co-owns the UK house (to be sold); **Harry's sister**.

---

## ROOM 2 — THE FOUR COMPANIES

*Note: task-level detail is deliberately NOT here — Paul will walk Jarvis through the Trello board card-by-card in a dedicated onboarding session. These are the company profiles.*

### Cross-company facts `STABLE`
- **Sector:** aesthetics — supply/distribution of practitioner products, plus an own-brand manufacturing arm (Prodermis).
- **Sourcing:** Derma Direct UK, Derma Direct EU and Aesthetics Supply all buy product from **Korea and the EU**.
- **Shared blocker — Dutch company formation** `LIVING`: until the Dutch entity is formed, EU stock (fillers + skincare) is stuck in a **bonded warehouse in the Netherlands**. This blocks both Derma Direct EU trading and Prodermis EU distribution. *Major, recurring sticking point — Jarvis tracks to resolution.*

### Derma Direct UK `STABLE` · *metrics* `LIVING`
- **What:** UK online wholesaler / 100% e-commerce, selling to doctors, nurses & aesthetic practitioners — **~90% "non-medics."** Open till **10pm, 7 days a week.**
- **Sells:** dermal fillers, skin boosters, gloves — everything a practitioner needs to run their business.
- **History:** established **6 years**; formerly **Filler Direct**, rebranded to Derma Direct after Paul's divorce.
- **Size:** turnover **~£1M/month** (down from ~£1.5M) · **~10 staff.** Currently **stable but shrinking slightly** — priority is to stop the decline and return to growth.
- **Growth plays underway:** new account manager hired, ads running, **website relaunch imminent**, big **retention-marketing push through Jul–Aug**.
- **Who runs it:** Paul + **Harry (~90% of day-to-day ops)** · **Marketing → Paul** · **Finance → Kiefer**.
- **Own/key brands & products:** **Nexfill** (own product, Paul is distributor), **LumiEyes**, **DermaN** (brand owned by **Acmedy**, Korea), **Revitrain** (by **BR Farm**), + others.

### Derma Direct EU `STABLE`
- Same model as Derma Direct UK, but sells **EU + US only — never the UK (Brexit).** Essentially a "photocopy" of the UK business once live.
- **Not yet trading** — company still being formed, tied to the **Dutch company formation** blocker above.

### Aesthetics Supply UK `STABLE`
- Sells **"grey" products** — not illegal in the UK, but a legal grey area.
- **~40 registered accounts, effectively no customer base yet** — needs marketing to activate. Same Korea/EU sourcing.

### Prodermis `STABLE` · *targets* `LIVING`
- **What:** Paul's **own manufacturing brand** (dermal fillers + boosters + mesotherapy + skincare).
- **Manufacturers:**
  - **BMI** — makes the **dermal fillers only.** ⚠️ **High-pressure contract:** very large **MOQs**, they demand growth (UK first, then EU), and there are **document issues.** Key relationship to manage.
  - **Michael @ Pyway Medical** (agent) — makes the **eye booster, skin booster + 6 mesotherapy products**; subcontracts out by product.
  - **Barcelona** — the **skincare range.**
- **Distribution:** **Alicia** (overseas distributor markets) · **Jane & Karen** (UK distributors).
- **Priorities:** ① get **UK sales to £5,000/month**; ② obtain **product registration documents for every distributor country** so distributors can register with their Ministry of Health (currently struggling — needs BMI's help).
- ⚠️ **URGENT (this weekend):** need **3 surveys from doctors for BMI.** `LIVING`
- Also affected by the **Dutch company formation** blocker (stock in NL bonded warehouse).

## ROOM 3 — HEALTH, BODY & TRAINING

### Current stats `LIVING` — *baselines to establish*
- **Weight:** 13 st 13.5 lb (**~88.7 kg**).
- **Body fat / muscle %:** unknown → **book a body scan** for a baseline. *(action)*
- **5km time:** no benchmark yet → **set one on an early run.** *(action)*
- **Lifts:** no baselines yet → **capture from the first live-coached sessions.** *(action)*

### Background `STABLE`
- Training on/off since **16**; now 36. Historically **intermittent** — consistency is the goal.
- **Bariatric history (important):** had a **gastric sleeve**; previously **~24 st (~152 kg)**, has lost a large amount. Long-standing struggle with weight & eating habits. → *Jarvis keeps nutrition **supportive, sustainable and protein-forward**; small stomach capacity is a hard constraint, and it flags if intake drops too low.*

### Training setup `STABLE` — *equipment varies by location*
- **Trains every day.** Access points: UK **home gym** (only until the move ~20 Oct), **gym next to the warehouse**, **Dubai building gym**, and **hotel gyms** when travelling.
- Likes **all exercises**, happy to swap based on what's available. → *Jarvis keeps an **equipment profile per location** and auto-adapts each session.*
- **Split:** Push / Pull / Legs / rest. **Run:** daily, **prefers outdoor**, treadmill fine.

### Nutrition `STABLE` · targets `LIVING`
- Doesn't track macros yet — wants to. Likely tool: **MyFitnessPal** (connect / log via Jarvis).
- **Eating pattern (sleeve):** **6–8 small meals a day**; likes to **cook once in the morning and weigh out portions** across the day. Completely happy eating the **same macro-perfect meal daily** ("cookie-cutter").
- **Likes:** chicken, beef, eggs, most vegetables, quick/easy repeatable food. **Dislikes:** fish.
- → *Chef role: a **repeatable, protein-forward daily menu** split into 6–8 small portions that hits macros and respects the sleeve — cook-at-home (UK) with order-in equivalents (Dubai).*

### Goals `STABLE`
- Target: **~11–11.5 st (70–73 kg), lean, with serious muscle** — a **recomposition**, plus the **six-pack / best shape of his life within 12 months.**
- Frame: healthy and sustainable given the bariatric history; protein-priority; consistency over intensity.

### Health admin `LIVING` *(actions Jarvis tracks)*
- Book **body scan** (baseline) · Book **blood test** (on Trello) · Set **5km benchmark** · Establish **lift baselines** · Set up **macro tracking**.

## ROOM 4 — FINANCES & THE VILLA

### The Dubai villa — payment plan `LIVING`
- **Sobha Elwood SEL-V 202** · price AED 11,638,800 · **~27% paid** (~AED 3.10M incl. the unrecorded 170k). Chase: log the 170k; dispute the AED 7,601 late fee.
- **Next demand: ~AED 1.2M, due ~7 Jan 2027** (6 months from 7 Jul).
- **Strategy — pre-fund monthly:** set aside a chunk each month (~AED 200k) so the 6-monthly demand isn't a big hit. → *Jarvis runs a monthly villa savings tracker + warns ahead of each demand.*
- **Flow:** Paul is paid personally, then pays the villa from that.

### Banking `STABLE`
- **UK:** main UK business account · **Revolut** (Derma Direct UK) · **Tide** (UK business — *confirm*; direct debits managed with Kiefer).
- **Derma Direct EU:** no accounts yet (company unregistered).
- **UAE personal:** Emirates NBD · FAB · Mashreq · "MBD" *(confirm exact bank)*.

### Regular commitments `LIVING` — *Jarvis watches so nothing surprises him*
- **Jade** (ex-wife, mother of Eva & Jack): **£1,150 / month** child support.
- **Steph:** ongoing financial support (Dubai).
- **John:** £6,000 / month.
- Business direct debits (with Kiefer, from Tide).

### Money goals `STABLE`
- **Freedom number:** **~£50,000 / month after tax** = the point where work becomes optional. (Doesn't spend it all — it's the target earn.)
- **Personal finances need a full reorganisation** → a dedicated project Paul + Jarvis tackle together. *(project)*

## ROOM 6 — PRIVATE TRACK (sobriety) `PRIVATE`

*Walled-off, encrypted room. Never enters business context, the Daily 12, or the Kiefer report. Supportive-coach tone only — never shame.*

### Known triggers & how Jarvis gets ahead of each
- **Flying / travel days** → check in **before and after flights**; pre-plan the journey; extra presence on travel days.
- **Work events — especially trade shows** → **high-risk**; prep him beforehand, stay close during, decompress after.
- **Loneliness — a major trigger** → watch for the signs (quiet evenings, time away from the kids/Steph, low engagement) and **reach out first** — nudge a call to John, Kiefer or Steph. Don't let him sit alone in it.
- **Not feeling happy / fulfilled** → reconnect him to the **deeper why** and the day's wins; when he's flat, surface progress and meaning.
- **Overwhelm, especially around the kids** (worst in school holidays) → **keep work light**, offer decompression and small breaks, normalise that it's hard. Gentle, never guilt.

### Approach
Proactive and pattern-aware — cross-references **sleep, mood, isolation, the travel/event calendar** to anticipate hard days. **SOS on demand.** Celebrates milestones. Always "I've got you," never a scoreboard.

### To add when Paul's ready *(optional)*
- A **"who to call in a tough moment"** shortlist.
- A preferred **professional / helpline resource** on file, so support is one tap away if ever needed.
