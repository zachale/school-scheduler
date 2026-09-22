# Fall 2026 term planner

Time-blocks every deliverable across the term, respecting classes, travel, meals and
ad-hoc commitments. Deterministic: same inputs always give the same plan, so adding a
constraint and re-running reflows everything predictably.

```bash
uv run gcal.py busy             # snapshot busy time on Zach's Google Calendar
uv run plan.py                  # replan + render
uv run verify.py                # check the result against every stated constraint
uv run gcal.py sync             # mirror it into the "F26 Plan" Google Calendar
```

## Files

| File | What it is |
|---|---|
| `config.yaml` | Constraints: work hours, meals, travel, classes, term dates |
| `tasks.yaml` | The 94 work items with hours, earliest-start and due |
| `events.yaml` | **Ad-hoc commitments — the file that changes** |
| `progress.yaml` | What is done: last day accounted for, minutes completed per task |
| `plan.py` | The scheduler and HTML renderer |
| `verify.py` | Independent constraint checker; exits non-zero on violation |
| `calendar.html` | Week-grid view, Google-Calendar style |
| `schedule.json` | Machine-readable output |
| `gcal.py` | Google Calendar link: `busy` reads Zach's busy time, `sync` pushes the plan |
| `busy.json` | Snapshot of busy time on Zach's calendars (times only, no titles) |
| `gcal.json` | The "F26 Plan" calendar's id, written by the first sync |
| `_gen_tasks.py` | One-off that built `tasks.yaml`; kept for provenance |

## Changing the plan in plain English

Tell Claude what changed and it edits the right file, re-runs, re-verifies, and syncs the
calendar. The translations are mechanical:

| You say | What changes |
|---|---|
| "I have a thing Thursday 6–9pm" | nothing, if it is a Busy event on your calendar; otherwise a row in `events.yaml` |
| "I didn't do anything today" | `through:` in `progress.yaml` moves to today |
| "I got 2 h of the MATH 3240 assignment done" | `math3240-a1: 120` under `done:`, and `through:` |
| "no weekends" | `work_hours.include_weekends: false` |
| "I can work till 11 on weeknights" | `work_hours.overflow` |
| "CIS 4020's project is a group project, halve it" | the `minutes` on `cis4020-proj` |
| "Antonie posted the project spec" | a revised `cis4020-*` block |
| "I'm dropping MATH 4310 A5" | delete `math4310-a5` |
| "assignment 2 moved to the 16th" | the `due` on that task |
| "I'm sick this week" / "write off the 5th–10th" | a range under `unavailable` in `config.yaml` |
| "the CIS 3210 final is Dec 14 at 2pm" | a row under `exams` in `config.yaml` |
| "finish assignments a week early" | `early_finish.days: 7` |

## Write-offs

A range under `unavailable` means no work and no classes. The planner then, on its own:

1. pulls any deadline that falls inside it back to the evening before it starts,
2. moves work whose whole window is blocked to the week before,
3. re-spreads discussion posts across the days actually available,
4. **generates catch-up for every lecture missed** (1.5 h each, +30%), due within a week of
   returning, and drops that week's review since the catch-up covers it.

Nothing is hand-moved, so a later "I'm sick Tuesday" gets identical treatment. `verify.py`
checks every adjusted item against its **real** deadline, not just the adjusted one.

## Finishing early

`early_finish.days` aims every hand-in item that many days before its real deadline.
Exam prep, readings and discussions are deliberately excluded. An item is never pulled
earlier than it can physically be done after release; where that bites, the achieved
buffer is reported in `resolved_tasks.json` as `buffer_days`. `early_finish.from` phases
it in from a date.

Explored 2026-09-21 (total hours are fixed, so finishing early moves work, it does not
create time):

| Scenario | Evenings | Busiest | 8 h+ days | This week | Exam week |
|---|---:|---:|---:|---:|---:|
| Off (current) | 0.3 h | 7.0 h | 0 | 35.6 h | 45.2 h |
| 3 days early | 10.6 h | 8.2 h | 1 | 35.5 h | 35.1 h |
| 7 days early | 24.2 h | 9.5 h | 4 | 41.7 h | 17.9 h |
| 7 days early, from Oct 1 | 23.0 h | 9.5 h | 5 | 35.6 h | 17.9 h |

The greedy scheduler is not monotonic in this setting: 5 days early fails where 3 and 7
pass, because the CIS*4020 presentation lands squarely in the Nov 12–18 crunch.

## How it schedules

Sessions are 1–3 hours, and any session of 90 minutes or more is followed by a
15-minute break before the next. Tasks are ordered by **latest feasible start** — deadline minus
the *available* days the remaining work needs — rather than by deadline alone, so a large
item due late still starts early enough, and a blocked stretch makes the work around it
start sooner.

Exam preparation opens 21 days out. That is a pacing allowance, not a release date: the
planner only uses the early part when there is spare time, which is what lets it put prep
into the days before a write-off. Exam days are capped at 4 hours with no evening work.

A "comfortable daily target" below the 7-hour cap was tried and removed. A sweep of 5.5,
6.0 and 6.5 hours pushed 20–35 hours of work into evenings and worsened the peak days,
because class days cannot reach those targets in core hours. Core hours (09:00–17:00) fill first; evening overflow
(17:00–22:00) is used only for work whose latest start has already arrived.

Discussion posts carry staggered internal deadlines and a 2-day minimum gap, because
ENVS*2210 penalises clustering posts into the final 24 hours.

## Constraints encoded

- Work 09:00–17:00, overflow 17:00–22:00 only when a deadline demands it
- 20 min lunch near noon, 20 min dinner near 18:00, both slid to avoid class
- 50 min travel before the first class of the day and after the last, not between
  back-to-back classes
- Friday labs omitted — unmarked, done online
- No classes Oct 12–13; Dec 3 runs a Tuesday schedule and Dec 4 a Monday schedule
- Max 7 core hours and 9.5 total hours in a day; exam days max 4 hours, no evenings
- Oct 5–10 written off (away)

## Google Calendar sync

`gcal.py sync` mirrors the plan into a dedicated **"F26 Plan"** calendar: every work block,
plus an all-day ⚑ event on each hand-in's real deadline. Each event carries a stable key and a
content hash, so a re-run only adds, updates or deletes what changed. Days before the plan
start are left alone as history. `--dry-run` reports the changes without writing.

How it connects (set up 2026-09-21):

- GCP project `your-gcp-project` on you@example.com, with the Calendar and IAM Credentials
  APIs enabled.
- Service account `planner-bot@your-project.iam.gserviceaccount.com` owns the calendar and
  shares it read-only with you@example.com. It has no access to any of Zach's own
  calendars.
- The script impersonates the service account using the gcloud login already on this machine,
  so no key file or client secret is stored anywhere. That rests on one grant (Zach ran it
  2026-09-21):
  `gcloud iam service-accounts add-iam-policy-binding planner-bot@your-project.iam.gserviceaccount.com --project your-gcp-project --member user:you@example.com --role roles/iam.serviceAccountTokenCreator`

Reading Zach's own calendar (built 2026-09-21). Zach shared his main calendar with the robot
as **free/busy only**. `gcal.py busy` snapshots its busy time into `busy.json`, and `plan.py`
blocks each busy stretch with a 30-minute buffer either side, after removing time it already
schedules itself: classes, the skipped Friday labs (now listed in `config.yaml` with
`skip: true`), exams and `events.yaml` rows. `verify.py` fails any work on busy time.

Free/busy has two blind spots, both found on Sep 26: events on other calendars (the wedding
is on "Family") and events marked **Free** (the reception was auto-created from Gmail, which
defaults to Free). Fixes: share those calendars too and add their ids to
`google_calendar.read_busy_from`, mark such events Busy, or add them to `events.yaml`.
Upgrading the share to full event details would also expose Free events, at the cost of the
robot reading titles.

Options considered: free/busy (chosen: least access), full details (catches Free events, reads
everything), chat only (works only when remembered).

Why a synced calendar rather than a subscribed ICS feed: Google Calendar refreshes URL
subscriptions every 12-24 h with no manual refresh, so a same-day reflow would show the old plan
exactly when it matters. A synced calendar updates in seconds.

Why a service account rather than an OAuth sign-in client: an External OAuth app in Testing
gets refresh tokens that expire after 7 days, and publishing it to production requires a home
page and privacy-policy link on an authorized domain. The first attempt left an unused OAuth
consent screen and desktop client ("F26 plan sync (Mac)") in the project.

Sources: [Calendars: insert](https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert),
[ICS refresh rates](https://calfeed.ai/learn/ics-refresh-rate-apple-google),
[7-day testing tokens](https://dev.to/ko-hi/googles-oauth-testing-mode-expires-refresh-tokens-in-7-days-publish-the-consent-screen-before-24hm).

## Mapping to Notion `Could Do`

Checked 2026-09-21 with `notion_map.py` against a snapshot of the 71 F26 rows
(`notion_rows.tsv`). It is **not 1:1**, but every row and every task is accounted for:

| Relationship | Notion rows | Planner tasks |
|---|---:|---:|
| 1:1 — hand-ins, ENVS weekly readings, Respondus practice | 35 | 35 |
| 1:1 — exam row ↔ its prep task (5 midterms, 4 finals) | 9 | 9 |
| 1:3 — ENVS discussion ↔ its three posts | 5 | 15 |
| 1:2 — ENVS midterm ↔ prep + sitting the 24 h window | 2 | 4 |
| Notion only — CIS*3210 in-lecture quizzes (no prep scheduled) | 20 | 0 |
| Planner only — weekly reviews/practice, CIS*4020 project analysis | 0 | 31 |
| **Total** | **71** | **94** |

The generated catch-up tasks (one per course after the Oct 5–10 write-off) have no Notion
row either. Deadlines agree on every mapped pair except three, two of them deliberate: ENVS
midterm prep and sitting are planned for the window's first day, not its close, and the
CIS*4020 presentation is prepared by the Nov 24 start of its window. The one real
disagreement is ENVS week 1 reading: Notion ends it Sun Sep 20, the planner Mon Sep 21.

## Current state

Progress logged through Mon Sep 21 with nothing done, so the plan runs from Tue Sep 22:
394.8 h across 76 working days with Oct 5–10 written off and **hand-ins finishing 3 days
early**. Every hand-in lands at least 3 days before its real deadline. Cost: 19.1 h of
evening work across the term, busiest day 8.2 h. `verify.py` passes every check.

Sat Sep 26 is blocked 11:10 onward for a wedding and reception (in `events.yaml`, since
free/busy cannot see either). Losing that afternoon moved 4.5 h into evenings on Oct 16 and
Oct 20, the run-up to the Oct 21 midterms. Losing Monday Sep 21 earlier cost 4 h of evenings
on Oct 22. The scheduler only opens evenings for work that is pressed, so lost time surfaces
in the pre-midterm crunch.

Calendar sync is live: "F26 Plan" holds 242 events (219 work blocks, 23 deadlines) under Other
calendars in you@example.com's Google Calendar. Events take the calendar's own colour;
Google only shows per-event colours to the calendar owner.

On the calendar, ⚑ flags sit on each hand-in's **real** deadline, so the gap between the
last work block and the flag is the buffer.

Hours come from [[fall-2026-effort-estimates]], which carries a +30% buffer.
