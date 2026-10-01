# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml", "requests"]
# ///
"""The F26 autoplanner: keeps the plan and the "F26 Plan" calendar current, no LLM.

One run is one cycle; systemd timers run it (deploy/). A cycle:

  1. reads the planner inputs, Zach's free/busy and every Could Do row with a Plan ID;
  2. rolls earlier days over once the date has turned, turning their blocks into history;
  3. if anything changed, replans from now (plan.py) and checks the result (verify.py);
  4. matches the new blocks to stable session ids and writes the differences.

    uv run service.py poll               # every minute: replan only if something changed
    uv run service.py sweep              # daily: also replan anyway, adopt new deliverable
                                         # rows, rewrite every block row, refresh the stats
    uv run service.py poll --dry-run     # plan and diff, write nothing

Once the Notion token is on the VM, Could Do is the only "done" signal: a block checked
off is done (and trimmed to the moment it was checked), a block still unchecked when its
day ends is missed and its minutes are planned again, and finished tasks teach the
planner Zach's real pace (learn.py). Without the token, a passed block is assumed done.

Inputs are deployed next to this file; state and outputs live in ~/.local/state/f26-planner
(SQLite plus a work directory the planner runs in).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import gcal
import learn
import notion
import notion_sync

APP = Path(__file__).resolve().parent
STATE = Path("~/.local/state/f26-planner").expanduser()
WORK = STATE / "work"
INPUTS = ["config.yaml", "tasks.yaml", "events.yaml", "plan.py", "verify.py"]
# everything that can change what ends up on the calendar or in Notion
WATCHED = INPUTS + ["service.py", "gcal.py", "gcal.json", "notion.py", "notion_sync.py",
                    "notion_map.py", "learn.py"]
TZ = ZoneInfo("America/Toronto")
ALERT_AFTER = 5            # consecutive failed runs before an alert event
ALERT_KEY = "alert"


def log(msg: str) -> None:
    print(msg, flush=True)


def local_now() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def round_up(t: dt.datetime, step: int = 5) -> dt.datetime:
    extra = (-t.minute) % step
    return t + dt.timedelta(minutes=extra)


# ---------- state ------------------------------------------------------------
SCHEMA = 6         # bump when a table changes; add the in-place step to MIGRATIONS
MIGRATIONS = {     # from version -> statements that bring it to the next one
    3: ["alter table sessions add column locked integer not null default 0",
        "alter table sessions add column user_min integer",
        "alter table rows add column w_start text",
        "alter table rows add column w_end text"],
    4: ["alter table finished add column block text"],
    5: ["alter table rows add column skip integer not null default 0"],
}


def open_db() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "state.db"
    if path.exists():
        old = sqlite3.connect(path)
        version = old.execute("pragma user_version").fetchone()[0]
        while version in MIGRATIONS:
            for stmt in MIGRATIONS[version]:
                old.execute(stmt)
            version += 1
            old.execute(f"pragma user_version = {version}")
            old.commit()
            log(f"state.db migrated to schema v{version}")
        old.close()
        if version != SCHEMA:
            kept = path.with_suffix(f".db.v{version}")
            path.rename(kept)
            log(f"state.db was schema v{version}, this build needs v{SCHEMA}: kept it as "
                f"{kept.name} and starting a fresh one")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        create table if not exists sessions (
            id          text primary key,  -- "<task>#<seq>", never reused
            task        text not null,
            seq         integer not null,
            date        text not null,
            start       text not null,
            "end"       text not null,     -- trimmed to the check time when done early
            planned_min integer not null,  -- the block as planned; what "done" counts
            status      text not null,     -- planned | done | missed | cancelled | skipped
                                           -- (Zach ticked Skip) | dropped (its task is)
            actual_min  integer,           -- measured, or planned when not measurable
            measured    integer not null default 0,
            off_plan    integer not null default 0,  -- (unused since v4; kept for old rows)
            locked      integer not null default 0,  -- Zach moved it: it stays where he put it
            user_min    integer,           -- Zach resized it: it keeps this length

            block       text not null      -- the schedule.json work block, as JSON
        );
        create table if not exists rows (      -- Could Do rows carrying a Plan ID
            plan_id text primary key, page_id text not null, hash text not null,
            done integer not null default 0, state text,
            w_start text, w_end text,      -- a block row's times as the service last wrote them
            skip integer not null default 0);  -- its Skip box, as last read or written
        create table if not exists finished (task text primary key, day text not null,
                                             actual integer,   -- total minutes, if typed in
                                             block text);      -- the block whose tick finished
                                                               -- it; null when a row did
        create table if not exists dropped (task text primary key, day text not null);
        create table if not exists kv (k text primary key, v text);
    """)
    db.execute(f"pragma user_version = {SCHEMA}")
    return db


def kv(db, k: str, default=None):
    row = db.execute("select v from kv where k = ?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_kv(db, k: str, v) -> None:
    db.execute("insert into kv values (?, ?) on conflict(k) do update set v = excluded.v",
               (k, json.dumps(v)))


def minutes(row) -> int:
    h = lambda s: int(s[:2]) * 60 + int(s[3:])
    return h(row["end"]) - h(row["start"])


def notion_on() -> bool:
    return notion.TOKEN_FILE.expanduser().exists()


def rollover(db, now: dt.datetime, commit: bool = True) -> bool:
    """Once the day turns, the blocks left unchecked on earlier days become history:
    missed, with their minutes planned again. Without Notion there is no done signal,
    so they are assumed done instead. Returns True if it ran."""
    today = str(now.date())
    if kv(db, "rolled_over") == today:
        return False
    if notion_on():
        n = db.execute("update sessions set status = 'missed' where status = 'planned' "
                       "and date < ?", (today,)).rowcount
        what = "missed (unchecked)"
    else:
        n = db.execute("update sessions set status = 'done', actual_min = planned_min "
                       "where status = 'planned' and date < ?", (today,)).rowcount
        what = "assumed done"
    set_kv(db, "rolled_over", today)
    if commit:
        db.commit()
        log(f"rollover {today}: {n} blocks from earlier days {what}")
    return True


def planner_state(db, now: dt.datetime) -> dict:
    """Everything the planner needs besides its input files. A block's size on the
    calendar is its time: "done" counts the ticked blocks at the size they have now."""
    today, hhmm = str(now.date()), f"{now:%H:%M}"
    done, last, wrapped = {}, {}, {}
    for r in db.execute("select * from sessions where status = 'done'"):
        done[r["task"]] = done.get(r["task"], 0) + minutes(r)
        last[r["task"]] = max(last.get(r["task"], r["date"]), r["date"])
    # a block Zach skipped: its time is dropped from the task, not planned again
    for r in db.execute("select * from sessions where status = 'skipped'"):
        done[r["task"]] = done.get(r["task"], 0) + minutes(r)
    # a task finished through its row, with no block of its own, dates from that row
    # (discussion-post spacing counts from it)
    for r in db.execute("select task, day from finished"):
        last.setdefault(r["task"], r["day"])
    # wrap-up minutes already spent or under way, so the 30-minute allowance is given once
    for r in db.execute("select * from sessions where status in ('done', 'missed', 'planned')"):
        if not json.loads(r["block"]).get("wrapup"):
            continue
        started = r["status"] != "planned" or (r["date"] < today or
                                               (r["date"] == today and r["start"] <= hhmm))
        if started:
            wrapped[r["task"]] = wrapped.get(r["task"], 0) + minutes(r)
    pinned, locked, sized = [], [], {}
    for r in db.execute("select * from sessions where status = 'planned' order by date, start"):
        block = {**json.loads(r["block"]), "start": r["start"], "end": r["end"], "session": r["id"]}
        if r["date"] == today and r["start"] < hhmm:
            pinned.append(block)                          # under way: history now
        elif r["locked"]:
            locked.append({**block, "date": r["date"]})   # he moved it: it stays there
        elif r["user_min"]:
            sized[r["id"]] = {"task": r["task"], "minutes": r["user_min"]}
    state = {"now": f"{now:%Y-%m-%dT%H:%M}", "done": done, "last_done": last,
             "pinned": pinned, "locked": locked, "sized": sized, "wrapped": wrapped,
             **notion_sync.state(db)}
    # learned pace, from the tasks the last good plan called finished
    base = {t["id"]: t["minutes"] for t in yaml.safe_load((APP / "tasks.yaml").read_text())["tasks"]}
    resolved = kv(db, "resolved", [])
    started = set(done) | {b["task"] for b in pinned + locked}
    mult = learn.multipliers(learn.evidence(db, resolved, base))
    tasks = [t for t in resolved if t["id"] in base]
    # a task keeps the multiplier it started with: changing it mid-task would leave slivers
    current, frozen = learn.per_task(tasks, mult, set()), kv(db, "frozen_multiplier", {})
    for tid in started:
        if tid not in frozen and tid in current:
            frozen[tid] = current[tid]
    state["multiplier"] = {**{t: m for t, m in current.items() if t not in started},
                           **{t: m for t, m in frozen.items() if t in started}}
    set_kv(db, "frozen_multiplier", frozen)
    set_kv(db, "learned", mult)
    return state


# ---------- planning ---------------------------------------------------------
def fingerprint(busy: list[dict], state: dict) -> str:
    h = hashlib.sha256()
    for name in WATCHED:
        h.update((APP / name).read_bytes())
    h.update(json.dumps(busy, sort_keys=True).encode())
    # what the plan depends on besides the inputs: the day, and which sessions are history
    h.update(json.dumps({k: state[k] for k in ("done", "finished", "dropped", "late_ok",
                                               "wrapped", "multiplier", "last_done",
                                               "locked", "sized", "hand_in")},
                        sort_keys=True).encode())
    h.update(state["now"][:10].encode())
    return h.hexdigest()


def run_planner(busy: list[dict], state: dict) -> tuple[list, list]:
    """Plan and verify in a fresh copy of the inputs. Raises on failure."""
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    for name in INPUTS:
        shutil.copy2(APP / name, WORK / name)
    (WORK / "busy.json").write_text(json.dumps({"busy": busy}, indent=1))
    (WORK / "state.json").write_text(json.dumps(state, indent=1))
    for script in ("plan.py", "verify.py"):
        # the service's own interpreter already has the planner's dependencies
        res = subprocess.run([sys.executable, script], cwd=WORK, capture_output=True, text=True)
        if res.returncode:
            tail = (res.stdout + res.stderr).strip().splitlines()[-12:]
            raise PlannerError(f"{script} failed:\n" + "\n".join(tail))
    return (json.loads((WORK / "schedule.json").read_text()),
            json.loads((WORK / "resolved_tasks.json").read_text()))


class PlannerError(RuntimeError):
    pass


def match_sessions(db, schedule: list[dict], now: dt.datetime) -> None:
    """Give each new future block a stable session id. A block Zach resized comes back
    carrying its own id; the rest of a task's future blocks, in time order, take over its
    other open sessions in time order. Leftover open sessions are cancelled; extra blocks
    get new, never-reused numbers. Blocks he moved are his and are left alone."""
    key = lambda date, start: (date, start)
    cut = key(str(now.date()), f"{now:%H:%M}")
    blocks: dict[str, list] = {}
    for day in schedule:
        for b in day["work"]:
            if b.get("pinned"):
                continue
            if b.get("session"):                  # a resized block: same session, new slot
                db.execute('update sessions set date = ?, start = ?, "end" = ?, block = ? '
                           "where id = ?", (day["date"], b["start"], b["end"], json.dumps(b),
                                            b["session"]))
                continue
            blocks.setdefault(b["task"], []).append((day["date"], b))
    placed_sized = {b["session"] for day in schedule for b in day["work"] if b.get("session")
                    and not b.get("pinned")}
    open_rows: dict[str, list] = {}
    for r in db.execute("select * from sessions where status = 'planned' and locked = 0 "
                        "order by date, start"):
        if key(r["date"], r["start"]) >= cut and r["id"] not in placed_sized:
            open_rows.setdefault(r["task"], []).append(r)
    top = {r["task"]: r["m"] for r in db.execute("select task, max(seq) m from sessions group by task")}
    for task in set(blocks) | set(open_rows):
        new = sorted(blocks.get(task, []), key=lambda x: (x[0], x[1]["start"]))
        old = [r for r in open_rows.get(task, []) if not r["user_min"]]
        for i, (date, b) in enumerate(new):
            length = minutes(b)
            if i < len(old):
                db.execute('update sessions set date = ?, start = ?, "end" = ?, planned_min = ?, '
                           "block = ? where id = ?",
                           (date, b["start"], b["end"], length, json.dumps(b), old[i]["id"]))
            else:
                top[task] = top.get(task, 0) + 1
                db.execute('insert into sessions (id, task, seq, date, start, "end", planned_min, '
                           "status, block) values (?, ?, ?, ?, ?, ?, ?, 'planned', ?)",
                           (f"{task}#{top[task]}", task, top[task], date, b["start"], b["end"],
                            length, json.dumps(b)))
        # open sessions the new plan has no block for, including a resized one it could
        # not fit any more
        for r in old[len(new):] + [r for r in open_rows.get(task, []) if r["user_min"]]:
            db.execute("update sessions set status = 'cancelled' where id = ?", (r["id"],))


# ---------- calendar ---------------------------------------------------------
def desired_events(db, resolved: list[dict], today: dt.date) -> dict:
    """What "F26 Plan" should hold: only warnings. The blocks and deadlines are Could Do
    rows, which Notion Calendar shows and Zach edits."""
    want: dict[str, dict] = {}
    covered = notion_sync.covered(db)
    all_day = lambda d: {"start": {"date": str(d)}, "end": {"date": str(d + dt.timedelta(days=1))},
                         "transparency": "transparent"}
    for t in resolved:
        due = dt.datetime.strptime(t["orig_due"], "%Y-%m-%d %H:%M")
        if t["status"] == "awaiting" and due <= dt.datetime.combine(today, dt.time(23, 59)):
            want[f"overdue|{t['id']}"] = gcal.keyed(f"overdue|{t['id']}", {
                "summary": f"⚠ Not checked off: {t['course']} {t['title']}",
                "description": f"Was due {t['orig_due']}. Its blocks are done; check it off in "
                               "Could Do once it is handed in.", **all_day(today)})
        if t["status"] == "overdue":
            fix = ("Set its row's Plan state to Late OK in Could Do to keep working on it, or "
                   "check the row off if it is done." if t["id"] in covered else
                   "It has no row of its own in Could Do: ask Claude to keep it in the plan "
                   "or drop it.")
            want[f"overdue|{t['id']}"] = gcal.keyed(f"overdue|{t['id']}", {
                "summary": f"⚠ Overdue, no longer scheduled: {t['course']} {t['title']}",
                "description": f"Was due {t['orig_due']}. {fix}", **all_day(today)})
    return want


def raise_alert(db, svc, now: dt.datetime, why: str) -> None:
    """Show one all-day "Planner stalled" event, dated today, and keep it current."""
    if kv(db, "alert") == [str(now.date()), why]:
        return
    body = gcal.keyed(ALERT_KEY, {
        "summary": "⚠ Planner stalled", "description": f"{why}\n\nSince {now:%a %b %d %H:%M}.",
        "start": {"date": str(now.date())},
        "end": {"date": str(now.date() + dt.timedelta(days=1))}, "transparency": "transparent"})
    gcal.alert(svc, calendar_id(), ALERT_KEY, body)
    set_kv(db, "alert", [str(now.date()), why])
    db.commit()
    log(f"alert raised: {why.splitlines()[0]}")


def clear_alert(db, svc) -> None:
    if kv(db, "alert") is None:
        return
    gcal.alert(svc, calendar_id(), ALERT_KEY, None)
    set_kv(db, "alert", None)
    db.commit()
    log("alert cleared")


def write_stats(db, nt, cfg, resolved: list[dict], state: dict) -> None:
    base = {t["id"]: t["minutes"] for t in yaml.safe_load((APP / "tasks.yaml").read_text())["tasks"]}
    ev = learn.evidence(db, resolved, base)
    started = set(state["done"]) | {b["task"] for b in state["pinned"]}
    tasks = [t for t in resolved if t["id"] in base]
    lines = learn.report(ev, kv(db, "learned", {}), tasks, started)
    if lines != kv(db, "stats_lines"):
        nt.set_page_text(cfg["notion"]["stats_page"], lines)
        set_kv(db, "stats_lines", lines)


def calendar_id() -> str:
    return json.loads((APP / "gcal.json").read_text())["calendar_id"]


# ---------- the cycle --------------------------------------------------------
def cycle(db, svc, dry_run: bool, sweep: bool = False) -> None:
    """One poll, or with sweep=True the daily full pass."""
    clock = local_now()
    # never round past midnight: today's sessions must stay today's
    now = min(round_up(clock), clock.replace(hour=23, minute=59))
    cfg = yaml.safe_load((APP / "config.yaml").read_text())
    busy = gcal.fetch_busy(svc, cfg["google_calendar"]["read_busy_from"], now.date(),
                           cfg["term"]["plan_until"] + dt.timedelta(days=1))
    nt = notion.Notion(cfg["notion"]["data_source"]) if notion_on() else None
    if nt:
        notion_sync.read(db, nt, cfg, clock)
    # after the ticks are read: a block ticked before the day turned is done, not missed,
    # and a block's tick is never read as a task's last while an earlier one is still
    # waiting to become missed (and be planned again)
    forced = rollover(db, now, commit=not dry_run)
    state = planner_state(db, now)
    fp = fingerprint(busy, state)
    if fp == kv(db, "fingerprint") and not (forced or sweep or dry_run):
        return                        # nothing changed; an alert, if any, still stands
    try:
        schedule, resolved = run_planner(busy, state)
    except PlannerError as e:
        # the last good plan stays on the calendar untouched; the alert is added beside it
        log(str(e))
        if not dry_run:
            raise_alert(db, svc, now, f"The plan could not be rebuilt:\n{e}")
            set_kv(db, "fingerprint", fp)       # do not retry the same failing input
            db.commit()
        return
    unplaced = [t for t in resolved if t["status"] == "unplaced"]
    if unplaced and not dry_run:
        raise_alert(db, svc, now, "These do not fit any more:\n" + "\n".join(
            f"· {t['course']} {t['title']} ({t['owed']} min short, due {t['orig_due']})"
            for t in unplaced[:8]))
    match_sessions(db, schedule, now)
    want = desired_events(db, resolved, now.date())
    counts = gcal.apply(svc, calendar_id(), want, now.date(), dry_run)
    log(f"{now:%a %H:%M} replanned: calendar add {counts[0]} · update {counts[1]} · "
        f"delete {counts[2]} · unchanged {counts[3]}" + (" (dry run)" if dry_run else ""))
    if dry_run:
        db.rollback()
        return
    if nt:
        if sweep:                            # deliverable rows Zach added since yesterday
            notion_sync.adopt(db, nt, {t["id"] for t in resolved})
        added, updated, trashed = notion_sync.write(db, nt, cfg, resolved, now, full=sweep)
        log(f"  notion rows: add {added} · update {updated} · trash {trashed}")
        if sweep:
            try:
                write_stats(db, nt, cfg, resolved, state)
            except Exception as e:           # the stats page is a report, never a blocker
                log(f"stats page not updated: {e}")
    if not unplaced:                         # the "do not fit" alert stands until it all fits
        clear_alert(db, svc)
    set_kv(db, "fingerprint", fp)
    set_kv(db, "resolved", resolved)
    db.commit()
    shutil.copy2(WORK / "calendar.html", STATE / "calendar.html")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["poll", "sweep"],
                    help="poll: replan if anything changed; sweep: the daily full pass")
    ap.add_argument("--dry-run", action="store_true", help="plan and diff, write nothing")
    args = ap.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / "run.lock", "w")
    try:
        # a sweep waits for a poll under way; a poll skips while anything else runs
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if args.mode == "sweep" else fcntl.LOCK_NB))
    except BlockingIOError:
        return
    db = open_db()
    svc = gcal.service()
    try:
        cycle(db, svc, args.dry_run, sweep=args.mode == "sweep")
        if kv(db, "failures", 0) and not args.dry_run:
            log("recovered")
        set_kv(db, "failures", 0)
        db.commit()
    except Exception as e:                      # counted, so a blip is not an alert
        db.rollback()
        n = kv(db, "failures", 0) + 1
        set_kv(db, "failures", n)
        db.commit()
        log(f"{args.mode} failed ({n} in a row): {e}\n{traceback.format_exc(limit=3)}")
        if n >= ALERT_AFTER and not args.dry_run:
            try:
                raise_alert(db, svc, local_now(), f"{n} failed runs in a row: {e}")
            except Exception as e2:
                log(f"could not raise the alert on the calendar either: {e2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
