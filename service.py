# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml"]
# ///
"""The F26 autoplanner: keeps the plan and the "F26 Plan" calendar current, no LLM.

Runs on the VM under systemd (deploy/f26-planner.service). Every cycle it:

  1. rolls yesterday over at 00:05, turning its sessions into history;
  2. reads the planner inputs and Zach's free/busy;
  3. if anything changed, replans from now (plan.py) and checks the result (verify.py);
  4. matches the new blocks to stable session ids and writes the calendar differences.

    uv run service.py                    # loop forever
    uv run service.py --once             # one cycle, then exit
    uv run service.py --once --dry-run   # plan and diff, write nothing

Until Notion tracking arrives (PRD milestone M3), a session that has passed is assumed
done. Inputs are deployed next to this file; state and outputs live in
~/.local/state/f26-planner (SQLite plus a work directory the planner runs in).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import gcal

APP = Path(__file__).resolve().parent
STATE = Path("~/.local/state/f26-planner").expanduser()
WORK = STATE / "work"
INPUTS = ["config.yaml", "tasks.yaml", "events.yaml", "plan.py", "verify.py"]
TZ = ZoneInfo("America/Toronto")
CYCLE_SECONDS = 60
ROLLOVER = dt.time(0, 5)
ALERT_AFTER = 5            # consecutive failed cycles before an alert event


def log(msg: str) -> None:
    print(msg, flush=True)


def local_now() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def round_up(t: dt.datetime, step: int = 5) -> dt.datetime:
    extra = (-t.minute) % step
    return t + dt.timedelta(minutes=extra)


# ---------- state ------------------------------------------------------------
def open_db() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / "state.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        create table if not exists sessions (
            id     text primary key,     -- "<task>#<seq>", never reused
            task   text not null,
            seq    integer not null,
            date   text not null,
            start  text not null,
            "end"  text not null,
            status text not null,        -- planned | done | cancelled
            block  text not null         -- the schedule.json work block, as JSON
        );
        create table if not exists kv (k text primary key, v text);
    """)
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


def rollover(db, now: dt.datetime) -> bool:
    """At 00:05, yesterday's sessions become history. Returns True if it ran."""
    today = str(now.date())
    if kv(db, "rolled_over") == today or now.time() < ROLLOVER:
        return False
    # no done signal yet (Notion arrives in M3): a passed session is assumed done
    n = db.execute("update sessions set status = 'done' where status = 'planned' and date < ?",
                   (today,)).rowcount
    set_kv(db, "rolled_over", today)
    db.commit()
    log(f"rollover {today}: {n} sessions from earlier days recorded as done")
    return True


def planner_state(db, now: dt.datetime) -> dict:
    today, hhmm = str(now.date()), f"{now:%H:%M}"
    done, last = {}, {}
    for r in db.execute("select * from sessions where status = 'done'"):
        done[r["task"]] = done.get(r["task"], 0) + minutes(r)
        last[r["task"]] = max(last.get(r["task"], r["date"]), r["date"])
    pinned = [{**json.loads(r["block"]), "session": r["id"]}
              for r in db.execute("select * from sessions where status = 'planned' "
                                  "and date = ? and start < ? order by start", (today, hhmm))]
    return {"now": f"{now:%Y-%m-%dT%H:%M}", "done": done, "last_done": last,
            "pinned": pinned, "finished": [], "dropped": [], "late_ok": []}


# ---------- planning ---------------------------------------------------------
def fingerprint(busy: list[dict], state: dict) -> str:
    h = hashlib.sha256()
    for name in INPUTS:
        h.update((APP / name).read_bytes())
    h.update(json.dumps(busy, sort_keys=True).encode())
    # what the plan depends on besides the inputs: the day, and which sessions are history
    h.update(json.dumps({k: state[k] for k in ("done", "finished", "dropped", "late_ok")},
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
    """Give each new future block a stable session id: a task's future blocks, in time
    order, take over its open sessions in time order. Leftover open sessions are
    cancelled; extra blocks get new, never-reused numbers."""
    key = lambda date, start: (date, start)
    cut = key(str(now.date()), f"{now:%H:%M}")
    blocks: dict[str, list] = {}
    for day in schedule:
        for b in day["work"]:
            if not b.get("pinned"):
                blocks.setdefault(b["task"], []).append((day["date"], b))
    open_rows: dict[str, list] = {}
    for r in db.execute("select * from sessions where status = 'planned' order by date, start"):
        if key(r["date"], r["start"]) >= cut:
            open_rows.setdefault(r["task"], []).append(r)
    top = {r["task"]: r["m"] for r in db.execute("select task, max(seq) m from sessions group by task")}
    for task in set(blocks) | set(open_rows):
        new = sorted(blocks.get(task, []), key=lambda x: (x[0], x[1]["start"]))
        old = open_rows.get(task, [])
        for i, (date, b) in enumerate(new):
            if i < len(old):
                db.execute('update sessions set date = ?, start = ?, "end" = ?, block = ? '
                           "where id = ?", (date, b["start"], b["end"], json.dumps(b), old[i]["id"]))
            else:
                top[task] = top.get(task, 0) + 1
                db.execute("insert into sessions values (?, ?, ?, ?, ?, ?, 'planned', ?)",
                           (f"{task}#{top[task]}", task, top[task], date, b["start"], b["end"],
                            json.dumps(b)))
        for r in old[len(new):]:
            db.execute("update sessions set status = 'cancelled' where id = ?", (r["id"],))


# ---------- calendar ---------------------------------------------------------
def desired_events(db, resolved: list[dict], today: dt.date, alerts: list[str]) -> dict:
    want: dict[str, dict] = {}
    last: dict[str, str] = {}
    for r in db.execute("select * from sessions where status in ('planned', 'done') "
                        "and date >= ? order by date, start", (str(today),)):
        b = json.loads(r["block"])
        last[r["task"]] = r["date"]
        lines = [f"Really due {b['due']}"]
        if b.get("note"):
            lines.append(b["note"])
        if b.get("overflow"):
            lines.append("Evening overflow: the deadline needs it.")
        want[r["id"]] = gcal.keyed(r["id"], {
            "summary": f"{b['course']} · {b['title']}",
            "description": "\n".join(lines),
            "start": {"dateTime": f"{r['date']}T{r['start']}:00", "timeZone": gcal.TZ},
            "end": {"dateTime": f"{r['date']}T{r['end']}:00", "timeZone": gcal.TZ},
            "transparency": "opaque",
        })
    all_day = lambda d: {"start": {"date": str(d)}, "end": {"date": str(d + dt.timedelta(days=1))},
                         "transparency": "transparent"}
    for t in resolved:
        due = dt.datetime.strptime(t["orig_due"], "%Y-%m-%d %H:%M")
        if t["kind"] == "work" and due.date() >= today and t["status"] != "dropped":
            note = (f"Last planned session {dt.date.fromisoformat(last[t['id']]):%a %b %d}, "
                    f"{(due.date() - dt.date.fromisoformat(last[t['id']])).days} days before."
                    if t["id"] in last else "No session left to plan.")
            want[f"due|{t['id']}"] = gcal.keyed(f"due|{t['id']}", {
                "summary": f"⚑ Due {due:%H:%M}: {t['course']} {t['title']}",
                "description": f"Real deadline. {note}", **all_day(due.date())})
        if t["status"] == "overdue":
            want[f"overdue|{t['id']}"] = gcal.keyed(f"overdue|{t['id']}", {
                "summary": f"⚠ Overdue, no longer scheduled: {t['course']} {t['title']}",
                "description": f"Was due {t['orig_due']}. Mark it Late OK in Notion to keep "
                               "working on it, or check it off if it is done.", **all_day(today)})
    for i, a in enumerate(alerts):
        want[f"alert|{i}"] = gcal.keyed(f"alert|{i}", {
            "summary": "⚠ Planner stalled", "description": a, **all_day(today)})
    return want


def calendar_id() -> str:
    return json.loads((APP / "gcal.json").read_text())["calendar_id"]


# ---------- the cycle --------------------------------------------------------
def cycle(db, svc, dry_run: bool) -> None:
    now = round_up(local_now())
    forced = rollover(db, now) if not dry_run else False
    cfg = yaml.safe_load((APP / "config.yaml").read_text())
    busy = gcal.fetch_busy(svc, cfg["google_calendar"]["read_busy_from"], now.date(),
                           cfg["term"]["plan_until"] + dt.timedelta(days=1))
    state = planner_state(db, now)
    fp = fingerprint(busy, state)
    if fp == kv(db, "fingerprint") and not forced and not dry_run:
        return
    try:
        schedule, resolved = run_planner(busy, state)
    except PlannerError as e:
        # a plan that fails its checks is never written; say so on the calendar now
        log(str(e))
        if not dry_run:
            # keep the last good plan on the calendar and add the alert beside it
            gcal.apply(svc, calendar_id(), desired_events(db, kv(db, "resolved", []),
                                                          now.date(), [str(e)]), now.date(), False)
            set_kv(db, "fingerprint", fp)       # do not retry the same failing input
            db.commit()
        return
    match_sessions(db, schedule, now)
    want = desired_events(db, resolved, now.date(), [])
    counts = gcal.apply(svc, calendar_id(), want, now.date(), dry_run)
    log(f"{now:%a %H:%M} replanned: calendar add {counts[0]} · update {counts[1]} · "
        f"delete {counts[2]} · unchanged {counts[3]}" + (" (dry run)" if dry_run else ""))
    if dry_run:
        db.rollback()
        return
    set_kv(db, "fingerprint", fp)
    set_kv(db, "resolved", resolved)
    db.commit()
    shutil.copy2(WORK / "calendar.html", STATE / "calendar.html")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="plan and diff, write nothing")
    args = ap.parse_args()
    db = open_db()
    svc = gcal.service()
    log(f"f26 autoplanner started ({'dry run' if args.dry_run else 'live'})")
    while True:
        started = time.monotonic()
        try:
            cycle(db, svc, args.dry_run)
            if kv(db, "failures", 0) and not args.dry_run:
                log("recovered")
            set_kv(db, "failures", 0)
            db.commit()
        except Exception as e:                  # never exit on an API error
            db.rollback()
            n = kv(db, "failures", 0) + 1
            set_kv(db, "failures", n)
            db.commit()
            log(f"cycle failed ({n} in a row): {e}\n{traceback.format_exc(limit=3)}")
            if n == ALERT_AFTER and not args.dry_run:
                try:
                    now = local_now()
                    gcal.apply(svc, calendar_id(), desired_events(
                        db, kv(db, "resolved", []), now.date(), [f"{n} failed cycles: {e}"]),
                        now.date(), False)
                except Exception as e2:
                    log(f"could not raise the alert on the calendar either: {e2}")
        if args.once:
            return
        time.sleep(max(5, CYCLE_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
