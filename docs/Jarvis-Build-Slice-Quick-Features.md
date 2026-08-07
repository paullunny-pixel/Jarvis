# Code brief — Quick ADHD wins (deadline radar · smart scale · meds refill · follow-up chaser)

Four small, high-value features. Additions on top of the existing repo; keep everything working. Wake-hours/quiet-day/`override` respected where relevant.

## 1. Deadline radar
- Jarvis holds a list of **key dates** from Google Calendar, Trello due dates, and ones Paul tells it (kids' birthdays — Eva 28 Sep, Jack 2 Oct; villa demand ~7 Jan; Nottingham move-out 20 Oct; visa/passport/insurance renewals).
- **Counts down proactively** with reminders at tunable lead times (e.g. 4 weeks / 1 week / 3 days / day-of) so a date never ambushes him. Paul can add a date by just telling Jarvis.

## 2. Smart scale auto-logging (Withings)
- Connect **Withings (Health Mate API)** so **weight + body-fat auto-log every morning** into the health stats / body metrics — no manual entry, no forgetting. (If the scale already writes to Apple Health, the existing pipe may cover it — check that first.)

## 3. Meds refill tracking
- Track **run-out / refill dates** for **TRT** and **ADHD meds** (Paul sets quantity + start date, or the refill date). Jarvis warns ahead of time ("ADHD meds run out in 5 days — reorder"). Ties into the existing non-suppressible med reminders.

## 4. Follow-up chaser
- Jarvis tracks **outbound items awaiting a reply** — ones Paul flags ("chase this"), plus important ones it detects — and **chases**: "You emailed BMI 4 days ago, no reply — want to chase?" Tunable threshold (default ~3 days). Stops promises and asks from vanishing.

## Connect
- **Withings** account/API (feature 2). Everything else uses existing pipes (Calendar, Trello, Gmail, the brain).

## Done =
Deadline radar counts down key dates; the Withings scale auto-logs weight/BF each morning; meds refills warn in advance; the follow-up chaser surfaces unanswered items. Give a test path for each.
