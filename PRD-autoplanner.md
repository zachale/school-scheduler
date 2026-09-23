---
title: F26 autoplanner — PRD
date: 2026-09-21
status: built — M1 live 2026-09-22; M2–M4 built, go-live pending the Notion token
tags: [school, planner, notion, google-calendar]
related: [[fall-2026-deliverables]], [[fall-2026-effort-estimates]]
---

# F26 autoplanner — PRD

## Problem

The plan changes only when Zach asks Claude. New calendar events, finished work and faster or
slower progress never reach it on their own, so the calendar drifts from reality within a day.

## Goal

A service on the Azure VM, with no LLM calls, that keeps the plan true by itself:

1. New busy time on Zach's calendar → the plan reflows around it.
2. A checkbox in Notion `Could Do` is the only "done" signal.
3. Measured time recalibrates the estimates for similar future work.

## Non-goals

- Editing the plan by moving events in Google Calendar. "F26 Plan" stays read-only.
- Creating tasks from Notion. `tasks.yaml` stays the catalog; Claude edits it.
- Scheduling the CIS*3210 in-lecture quizzes. They stay Notion-only rows.

## The loop

A cycle starts 60 s after the previous cycle's writes finish, so cycles never overlap.

1. **Read.**
   - Planner inputs.
   - Google free/busy.
   - A full scan of every `Could Do` row that has a `Plan ID` (~350 rows, 4 paged
     requests). The scan is diffed against the service's SQLite state, which catches checks,
     unchecks, deletions and restores. A "changed since the last poll" query would miss
     deletions and any check made later in the same minute as the poll, because Notion rounds
     `last_edited_time` down to the minute.
2. **Decide.** If nothing changed, go back to sleep. A forced run at 00:05 Toronto time rolls
   the day over.
3. **Record** completions and misses, then update the estimates.
4. **Replan from now.** Now is Toronto wall-clock time rounded up to 5 min; the VM runs in
   UTC, so the unit sets `TZ=America/Toronto`. Every block that has started is pinned as
   history: it counts toward today's caps and is never moved or deleted. Only time after now
   is replanned. A block starting within the next 2 h also stays put unless it now conflicts.
5. **Verify.** Run `verify.py`. If it fails, keep the last good plan and raise an alert.
6. **Write** the differences, Google first, then Notion rows soonest first.
   - Google writes go in batch requests.
   - Notion writes are rate-limited (about 3 requests/s, shared with Claude's Notion
     connector) and retried with backoff.

**Latency target:** Google and the next 48 h of Notion rows within 2 min of a change; every row
within 5 min.

## Notion model (`Could Do`)

| Row | Represents | Checking it means |
|---|---|---|
| Deliverable (Zach's 51 original rows, dates untouched) | a due date: assignment, discussion close, exam | submitted: every task it covers is finished and its open sessions cancelled |
| Session (new sub-item of its deliverable, ~220 over the term) | one calendar block; `Due Date` = block start–end | that block is done |

- Zach's original rows are the only task-level rows (revised 2026-09-22: an earlier build
  added a row per planner task, which duplicated them). A task with a deliverable to itself
  is finished by that row. Every other task (ENVS posts and midterm parts, CIS*4020 analysis
  work under the Project Report, weekly reviews, catch-up) is finished when its minutes are
  spent or its last planned session is checked, even short; unchecking that session reopens
  it. Weekly reviews are one session each.
- Every row the planner manages carries a `Plan ID`. Rows without one are Zach's and are never
  touched.
- **Session identity** is the task plus a sequence number that is never reused.
  - Checked, missed and started rows are frozen.
  - Each new plan matches a task's future sessions, in time order, to its open rows. Extra
    open rows are archived; a shortfall gets new numbers.
  - Google events are keyed by the same session ID, so a moved session updates its event
    instead of deleting and recreating it.
- **New properties:**
  - `Plan ID` (text).
  - `Planned min` and `Actual min` (numbers). `Actual min` is an optional manual override.
  - `Plan state` (select: Planned, Missed, Late OK).
  - The unnamed checkbox is renamed `Done`. The service addresses every property by ID, and
    M2 updates the `tracking-could-dos` skill so Claude's could-do commands keep working and
    its daily brief leaves session rows out.

## Done tracking

**Check time** is the row's `last_edited_time`, unless the service itself made that edit. In
that case the check time is the start of the first poll that saw the box checked. The service
never writes to a session row between its start and the end of its grace window.

| Session checked | Actual time recorded | Calendar block |
|---|---|---|
| During the block, or up to 15 min after it ends | check time − block start | ends at the check time, capped at the next item's start; the freed time reflows |
| Later the same day | `Actual min` if filled, else planned minutes | unchanged |
| Before the block starts | `Actual min` if filled, else planned minutes | removed; the slot is freed |

- **Missed:** at the 00:05 rollover, an unchecked block from the previous day is set to
  Missed. It stays on its slot, and its minutes go to new session rows. Checking a Missed row
  later counts it as done late and cancels that many replacement minutes.
- **Submitting mid-session:** checking a deliverable row while one of its sessions is
  running first closes that session at the check time.
- **Unchecking** a row reverts it on the next cycle.
- **Past its estimate,** a task with its own deliverable row gets at most one 30-min wrap-up
  session, then waits for that row to be checked.
- **Deadline passed, row unchecked:** the task stops being scheduled and an alert is raised.
  Setting `Plan state` to Late OK keeps scheduling it as overdue work. A task with no row of
  its own cannot be marked Late OK in Notion; the alert says to ask Claude.

## Learning estimates

- **Evidence is a finished task,** not a single session, since one session does not say how
  much of an assignment is done.
  - A task counts only if most of its minutes were measured: checked within the grace
    window, or `Actual min` filled.
  - Ratio = actual minutes ÷ the unmultiplied `tasks.yaml` estimate. Dividing by the
    multiplied estimate would drag a correct multiplier back to 1.
- **Groups:** readings (learns words per minute), assignments per course, discussion posts,
  weekly reviews, exam prep.
- **Multiplier** = (Σ actual + 2·Ē) ÷ (Σ estimate + 2·Ē), where Ē is the group's mean estimate.
  - The two phantom on-estimate tasks damp early noise, so estimates move toward the measured
    rate rather than jumping to it.
  - Clamped to 0.5×–2×.
  - A course with fewer than two finished assignments uses the all-assignments multiplier.
- **Scope:** the multiplier applies to tasks in the group that have not started.
- **Stats page:** a "Planner stats" page in Notion shows reading speed, each group's
  multiplier and the hours it moved. Zach creates the empty page, because an internal
  connection cannot create top-level pages. The service rewrites its contents.

## Deletions and failures

- **Session row deleted:** the block is unscheduled and recreated with the next plan. A
  logged actual, if the row was checked, is kept.
- **Deliverable row deleted:** its tasks are dropped from the plan, like "I'm dropping
  A5". Restoring the row from Notion's trash brings the task back.
- **Many rows disappear at once:** if more than 5 known rows vanish in one cycle, or a known
  row returns 404 instead of being in the trash, the service treats it as a failure and drops
  nothing.
- **A Google or Notion error,** including a free/busy response carrying a per-calendar error
  such as a revoked share, fails the cycle:
  - It keeps the last good inputs and writes nothing.
  - The failure count lives in SQLite; 5 in a row raises an alert.
  - The process never exits on an API error.
- **Alerts** go out on whichever channel still works:
  - a Notion alert row for Google or `verify.py` failures;
  - an all-day "Planner stalled" event on F26 Plan for Notion failures;
  - journald for everything.

  Alerts clear themselves when the service recovers.

## Progress-model requirements from the code review

An adversarial review of the current planner (2026-09-21) found five defects that all live in
the manual `progress.yaml` model this service replaces. The new model must close them:

- Generated catch-up tasks can be completed and can go overdue like any other task.
- A weekly review dropped because a catch-up covers it stays dropped after its week passes.
- Discussion-post spacing counts posts already done, not only posts still planned.
- Two overdue posts in the same discussion do not block each other.
- "Overdue" compares date and time, so a task due at 09:00 today is overdue for a plan that
  starts after 09:00.

## Deployment

- **Host:** Azure VM `vm` (Ubuntu 24.04, 4 vCPU, 2.8 GB RAM, always on). The service is a uv
  script under a systemd service with `Restart=always`, logging to journald.
- **Who owns what:**
  - The Mac repo owns the inputs: `config.yaml`, `tasks.yaml`, `events.yaml`.
  - The VM owns state (SQLite: Notion IDs, completions, estimates) and the outputs.
  - Deploying is an rsync of the inputs over SSH; the service notices on its next cycle.
  - Completions live in Notion, so a lost SQLite file can be rebuilt from a full scan.
- **Secrets** live only on the VM, mode 0600, never in git: the Notion connection token and
  the Google credential for the `f26-plan-sync` robot.

## Setup Zach does (~15 min)

1. In the Notion workspace that holds `Could Do` (he must be its owner), create an internal
   connection, share `Could Do` with it, and put the token on the VM.
2. Create an empty "Planner stats" page, favorite it, and share it with the connection.
3. Give the VM a Google credential for `f26-plan-sync`.

## Milestones (each ships a working product)

| # | Ships | Estimate |
|---|---|---|
| M1 | Service on the VM: replan from now with pinned history, reflow around calendar changes, Google sync keyed by session ID | ~5 h |
| M2 | Notion rows for every deliverable, task and session; full-scan diff; deletion-safe; skill updated | ~5 h |
| M3 | Done tracking: actuals, resized blocks, missed and late rows, wrap-up rule | ~4 h |
| M4 | Learned multipliers and the stats page | ~3 h |

## Success criteria

- A new Busy event reflows the calendar within 2 minutes, with no Claude involved.
- Checking a session within its block resizes its Google block within 2 minutes.
- A cycle with no changes writes 0 Notion rows and 0 events.
- After 3 finished readings, reading estimates have moved toward the measured reading speed.

## Open decisions (proposed default first)

1. Deleted deliverable row: **drop the task** · recreate it.
2. Unchecked task past its deadline: **stop scheduling it and alert** · keep scheduling it as
   overdue.
3. When an unchecked block counts as missed: **at the nightly rollover** · 15 min after it
   ends, which reschedules the same day but races late checks.
4. Google credential on the VM: **service-account key file** · keyless federation from the
   VM's Azure identity (no secret on disk, ~1 h more setup).
