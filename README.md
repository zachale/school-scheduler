# Fall 2026 term planner

Time-blocks every deliverable across the term, respecting classes, travel, meals and
ad-hoc commitments. Deterministic: same inputs always give the same plan, so adding a
constraint and re-running reflows everything predictably.

```bash
uv run plan.py                  # replan + render
uv run verify.py                # check the result against every stated constraint
uv run plan.py --from 2026-10-08   # freeze the past, replan from a date
```

## Files

| File | What it is |
|---|---|
| `config.yaml` | Constraints: work hours, meals, travel, classes, term dates |
| `tasks.yaml` | The 84 work items with hours, earliest-start and due |
| `events.yaml` | **Ad-hoc commitments — the file that changes** |
| `plan.py` | The scheduler and HTML renderer |
| `verify.py` | Independent constraint checker; exits non-zero on violation |
| `calendar.html` | Week-grid view, Google-Calendar style |
| `schedule.json` | Machine-readable output |
| `_gen_tasks.py` | One-off that built `tasks.yaml`; kept for provenance |

## Changing the plan in plain English

Tell Claude what changed and it edits the right file, re-runs, and re-verifies. The
translations are mechanical:

| You say | What changes |
|---|---|
| "I have a thing Thursday 6–9pm" | a row in `events.yaml` |
| "no weekends" | `work_hours.include_weekends: false` |
| "I can work till 11 on weeknights" | `work_hours.overflow` |
| "CIS 4020's project is a group project, halve it" | the `minutes` on `cis4020-proj` |
| "Antonie posted the project spec" | a revised `cis4020-*` block |
| "I'm dropping MATH 4310 A5" | delete `math4310-a5` |
| "assignment 2 moved to the 16th" | the `due` on that task |
| "I'm sick this week" | an all-day event per affected day |

## How it schedules

Sessions are 1–3 hours. Tasks are ordered by **latest feasible start** — deadline minus
the days the remaining work needs — rather than by deadline alone, so a large item due
late still starts early enough. Core hours (09:00–17:00) fill first; evening overflow
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
- Max 7 core hours and 9.5 total hours in a day

## Current state

370.1 h across 76 working days, **entirely inside core hours** — zero evening overflow
needed. Busiest day is 7.0 h. `verify.py` passes every check.

Hours come from [[fall-2026-effort-estimates]], which carries a +30% buffer.
