# school-scheduler

A deterministic term planner. It time-blocks every reading, assignment, discussion post
and exam prep session around your Google Calendar, puts the blocks in a Notion database
(which Notion Calendar shows), and replans on its own when your week changes. No LLM calls.

![A week planned in Notion Calendar](docs/calendar-week.png)

## What it does

- **Plans the term.** `plan.py` places each task's estimated time before its deadline,
  inside your work hours, around classes, meals, travel and anything Busy on your calendar.
  Hand-ins finish 3 days early; sessions are capped at 3 h, with breaks.
- **Checks itself.** `verify.py` fails the plan if any rule is broken, so a bad plan never
  replaces a good one.
- **Tracks progress in Notion.** Every block is a row. Ticking a block records the time
  spent (its size on the calendar is its time); ticking your own deadline row hands the
  task in. Drag a block to lock it there; resize it to change its length.
- **Replans around change.** A new calendar event, a missed block or a finished task
  reflows everything after now.
- **Learns your pace.** Measured time on finished tasks recalibrates the estimates for
  similar work (`learn.py`).

## Layout

| File | Role |
|---|---|
| `service.py` | Runs a cycle: read Notion and free/busy, replan, verify, write |
| `plan.py`, `verify.py` | The planner and its rule checker |
| `notion.py`, `notion_sync.py`, `notion_map.py` | Notion client and row sync |
| `gcal.py` | Google free/busy and the warnings calendar |
| `learn.py` | Learned pace multipliers |
| `PRD-autoplanner.md` | Design notes |

## Setup

Your inputs stay out of git (see `.gitignore`): `config.yaml` (term, classes, work hours),
`tasks.yaml` (deliverables and estimates), `events.yaml`, `gcal.json`. Secrets live on the
host at `~/.config/f26-planner/` with mode 600: a Notion connection token and a Google
service-account key. See [TODO.md](TODO.md) for what is in progress.
