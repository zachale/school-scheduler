# Fall 2026 term planner

Time-blocks every deliverable across the term, respecting classes, travel, meals and
ad-hoc commitments. Deterministic: same inputs always give the same plan, so adding a
constraint and re-running reflows everything predictably.

Since 2026-09-22 it runs by itself on the VM (`ssh vm`) as the **autoplanner service**
([PRD](PRD-autoplanner.md)). Every minute it reads Zach's free/busy; when anything changed it
replans from now, verifies, and writes only the differences to the "F26 Plan" Google
Calendar. No LLM is involved.

```bash
deploy/deploy.sh                                   # push code + inputs to the VM, restart
ssh vm journalctl --user -u f26-planner -f         # watch it work
ssh vm cat .local/state/f26-planner/calendar.html > /tmp/plan.html   # latest week grid
ssh vm 'cd f26-planner && ~/.local/bin/uv run service.py --once --dry-run'   # plan + diff only
```

## Files

| File | What it is |
|---|---|
| `config.yaml` | Constraints: work hours, meals, travel, classes, term dates |
| `tasks.yaml` | The 94 work items with hours, earliest-start and due |
| `events.yaml` | **Ad-hoc commitments — the file that changes** |
| `plan.py` | The scheduler and HTML renderer; plans from `state.json`'s "now" |
| `verify.py` | Independent constraint checker; exits non-zero on violation |
| `service.py` | The always-on loop on the VM: rollover, free/busy, replan, verify, calendar writes |
| `gcal.py` | Google Calendar library used by the service (free/busy, batched keyed writes) |
| `gcal.json` | The "F26 Plan" calendar's id |
| `notion.py` | Notion client for Could Do (the F26 Planner connection) |
| `notion_sync.py` | Reads ticks, deletions and Missed/Late OK from Could Do; writes a row per task and block |
| `notion_map.py` | Maps the existing deliverable rows to planner tasks (used once, and daily after) |
| `learn.py` | Learned pace: speed multipliers per kind of work, from finished measured tasks |
| `deploy/` | systemd user unit and `deploy.sh` |
| `_gen_tasks.py` | One-off that built `tasks.yaml`; kept for provenance |
| `PRD-autoplanner.md` | PRD for the no-LLM service that replans on its own |

## Changing the plan in plain English

Tell Claude what changed and it edits the right file and runs `deploy/deploy.sh`; the
service replans within a minute. The translations are mechanical:

| You say | What changes |
|---|---|
| "I have a thing Thursday 6–9pm" | nothing, if it is a Busy event on your calendar; otherwise a row in `events.yaml` |
| "I did this block" | tick it in Could Do; ticking during it trims the block to the tick, ticking before its time moves it to end at the tick |
| "that took longer / happened earlier" | drag or resize the ticked block in Notion Calendar: its size is the time spent |
| "make this block shorter" | resize it (same start): it keeps that length, and the planner may still move it |
| "I'll do this block then" | drag it to a new start: it is locked there and never moved again |
| "I handed it in" | tick the deliverable row; its remaining blocks are cancelled |
| "I didn't do today's blocks" | nothing: a block still unticked when the day ends becomes Missed and its time is planned again |
| "drop this task" | delete its row in Could Do; restoring it from the trash brings it back |
| "keep working on it after the deadline" | set its Plan state to Late OK |
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

Since 2026-09-22 18:15 the blocks and deadlines live only in Notion (Could Do rows, shown and
edited in Notion Calendar). **"F26 Plan" holds only all-day ⚠ warnings**: overdue work, work
that no longer fits, and planner failures. Each block has a stable session id (`<task>#<n>`, never reused),
and each event carries that key and a content hash, so a replan only adds, updates or
deletes what changed, in batches. Days before today are left alone as history.

How it connects (set up 2026-09-21):

- GCP project `your-gcp-project` on you@example.com, with the Calendar and IAM Credentials
  APIs enabled.
- Service account `planner-bot@your-project.iam.gserviceaccount.com` owns the calendar and
  shares it read-only with you@example.com. It has no access to any of Zach's own
  calendars.
- On the VM the service authenticates with the robot's key file,
  `~/.config/f26-planner/google-key.json` (mode 0600, created 2026-09-22, never on the Mac).
  The earlier token-creator grant that let the Mac impersonate the robot is unused now.

Reading Zach's own calendar (built 2026-09-21). Zach shared his main calendar with the robot
as **free/busy only**. The service reads its busy time every minute into `busy.json`, and `plan.py`
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

## Notion tracking

Could Do is the only "done" signal once the connection token is on the VM
(`~/.config/f26-planner/notion-token`, mode 0600). Without it, the service assumes a block that
has passed was done.

Rows the planner manages carry a `Plan ID`; nothing else in Could Do is read or written.

| Plan ID | Row | Ticking it means |
|---|---|---|
| `deadline:<task>[,<task>…]` | a deliverable Zach already had (51 of them, adopted on the first run) | handed in: its tasks are finished |
| `task:<task>` | a planner task with no deliverable of its own, or one of several under a deliverable | that task is finished |
| `session:<task>#<n>` | one calendar block, a sub-item of its task or deliverable | that block is done |

Every cycle scans all Plan ID rows and diffs them against the service's SQLite record, because
a "changed since" query cannot see deleted rows and Notion rounds edit times to the minute.
Deleting a block's row replans it; deleting a task's or deliverable's row drops the task;
restoring it from the trash brings it back. Notion holds the whole record (the service writes
its own ticks and measured minutes back), so a lost `state.db` is rebuilt from a scan.

**A block's size is its time.** Ticked blocks count at the size they have on the calendar,
and Zach may drag or resize a block after ticking it to match what really happened. Before
it is ticked, a resize keeps the block at that length (it can still move), and changing its
start locks it where he put it. The service tells his edits from its own writes by the
row's last editor and by the times it last wrote (`rows.w_start`/`w_end`). Task rows are
all-day, so only blocks carry times.

**Learned pace** (`learn.py`): a finished task whose blocks were mostly measured is evidence;
each kind of work (readings, posts, reviews, exam prep, assignments per course) gets a
multiplier once it has two such tasks, damped by two phantom on-estimate tasks and clamped to
0.5–2×. It applies only to tasks not yet started. The **Planner stats** page in Notion shows it.

**When the plan does not fit**: the planner first releases a hand-in's 3-day early-finish
target, then its learned pace and post spacing; whatever still does not fit is flagged in a
⚠ calendar alert while everything else keeps reflowing.

The original name-based mapping of rows to tasks, and why it is not 1:1, is in
`notion_map.py` (checked 2026-09-21 against `notion_rows.tsv`: 44 one-to-one, 7 one-to-many,
20 quiz rows with no planner task, 31 planner-only tasks).

## Current state

**The autoplanner service has been live on the VM since 2026-09-22 09:35.** It holds 216 work
blocks and 23 ⚑ deadlines on "F26 Plan", each keyed by a session id, and re-runs every minute:
a changed input or a new busy event reflows the plan within about two minutes. ENVS Reading
wk1 passed its deadline unchecked, so by the rule Zach chose it is no longer scheduled and
carries an all-day ⚠ event instead.

The plan itself: 394 h from Tue Sep 22 to Dec 18, Oct 5–10 written off, hand-ins finishing
3 days early, about 20 h of evening work, busiest day 8.7 h.

Two adversarial reviews ran before it went live and found 20 defects, all fixed: a verify
check that would have frozen the planner from mid-October, double-scheduled work in the
minutes around midnight, an early-finish target that behaved as a hard deadline, alerts that
could delete the whole calendar, and retried calendar writes that could duplicate an event.

Still to come (see [the PRD](PRD-autoplanner.md)): Notion rows for every block and deadline
(M2), checkbox tracking with resized blocks (M3), and learned estimates (M4).

Hours come from [[fall-2026-effort-estimates]], which carries a +30% buffer.
