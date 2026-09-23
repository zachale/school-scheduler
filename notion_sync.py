"""Reconcile the plan with the Could Do rows: read what Zach checked, write what changed.

read() turns a scan of the Plan ID rows into state the planner understands — minutes
actually spent, tasks finished, tasks dropped, blocks missed — and resizes a block to the
moment its box was ticked. write() then makes Could Do hold exactly the rows the current
plan calls for.

Nothing without a Plan ID is ever read as progress or written to.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import notion
import notion_map
from notion import date_range, edited_at, rich, title

MAX_VANISHED = 5           # more rows than this disappearing at once is a failure, not intent


def log(msg: str) -> None:
    print(msg, flush=True)


def _hash(props: dict) -> str:
    return hashlib.sha1(json.dumps(props, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _known(db) -> dict[str, dict]:
    return {r["plan_id"]: dict(r) for r in db.execute("select * from rows")}


# ---------- reading ----------------------------------------------------------
def read(db, nt: notion.Notion, cfg: dict, now: dt.datetime) -> None:
    """Fold Zach's checkboxes, Plan state choices and deletions into the service's state."""
    grace = dt.timedelta(minutes=cfg["notion"].get("grace_minutes", 15))
    nt.check_schema()
    scanned, known, bot = nt.scan(), _known(db), nt.me()

    # a row copied with Notion's Duplicate shares its Plan ID: keep the one we know
    rows = {}
    for plan_id, found in scanned.items():
        keep = next((r for r in found if r["page_id"] == known.get(plan_id, {}).get("page_id")),
                    found[0])
        for extra in found:
            if extra["page_id"] != keep["page_id"]:
                nt.trash(extra["page_id"])
                log(f"trashed a duplicate {plan_id} row")
        rows[plan_id] = keep

    # a scan that lost most rows is a glitch (or a revoked share), not Zach deleting them;
    # deleting one deliverable legitimately takes all its block rows with it
    gone = [pid for pid in known if pid not in rows]
    if len(gone) > MAX_VANISHED and len(gone) > len(known) // 2:
        raise notion.NotionError(f"{len(gone)} of {len(known)} planner rows vanished from Could "
                                 "Do at once; not treating that as deletions")

    imported = set()
    for plan_id, row in rows.items():
        kind, _, ident = plan_id.partition(":")
        if kind not in ("deadline", "session"):
            continue                          # not a row the service keeps
        before = known.get(plan_id)
        if before is None and kind == "deadline":
            for task in ident.split(","):     # back from the trash: the task is owed again
                if db.execute("delete from dropped where task = ?", (task,)).rowcount:
                    log(f"row restored: {task} is back in the plan")
        db.execute("insert into rows(plan_id, page_id, hash, done, state) "
                   "values (?, ?, '', ?, ?) on conflict(plan_id) do update set "
                   "page_id = excluded.page_id, state = excluded.state",
                   (plan_id, row["page_id"], int(bool(before and before["done"])), row["state"]))
        was_done = bool(before and before["done"])
        if kind == "session":
            if db.execute("select 1 from sessions where id = ?", (ident,)).fetchone() is None:
                imported.add(ident.rpartition("#")[0])
            _session_change(db, row, ident, was_done, now, grace, bot, before)
            if row["state"] == "Missed":
                db.execute("update sessions set status = 'missed' where id = ? "
                           "and status = 'planned'", (ident,))
        elif row["done"] != was_done:
            for task in ident.split(","):     # one deliverable can cover several tasks
                _task_change(db, task, row["done"], now)
        if kind == "deadline" and row["done"] and "," not in ident:
            # the whole task's time, typed into Actual min on its own row
            db.execute("update finished set actual = ? where task = ?",
                       (int(row["actual_min"]) if row["actual_min"] else None, ident))
        db.execute("update rows set done = ? where plan_id = ?", (int(row["done"]), plan_id))
    for task in imported:
        _refinish_rebuilt(db, task)

    for plan_id in gone:                      # confirm each one is really in the trash
        page_id = known[plan_id]["page_id"]
        if nt.get(page_id) is not None:
            continue                          # a glitch, not a deletion
        kind, _, ident = plan_id.partition(":")
        db.execute("delete from rows where plan_id = ?", (plan_id,))
        if kind == "session":
            db.execute("update sessions set status = 'cancelled' where id = ? "
                       "and status = 'planned'", (ident,))
            log(f"row deleted: block {ident} unscheduled, it will be planned again")
        elif kind == "deadline":
            for task in ident.split(","):
                db.execute("insert or replace into dropped values (?, ?)", (task, str(now.date())))
                db.execute("update sessions set status = 'cancelled' where task = ? "
                           "and status = 'planned' and (date > ? or (date = ? and start >= ?))",
                           (task, str(now.date()), str(now.date()), f"{now:%H:%M}"))
            log(f"row deleted: {ident} dropped from the plan")
    db.commit()


def _import_session(db, row, sid, now) -> None:
    """A block row the service has no record of (state.db was lost or reset): take it
    back in from Notion, so its id is never reused and a tick on it is not lost."""
    task, _, seq = sid.rpartition("#")
    if not task or not seq.isdigit() or not row["start"]:
        return
    local = lambda v: dt.datetime.fromisoformat(v).astimezone(notion.TZ).replace(tzinfo=None)
    start = local(row["start"])
    end = local(row["end"]) if row["end"] else start + dt.timedelta(minutes=row["planned_min"] or 60)
    planned = int((end - start).total_seconds() // 60)       # its size on the calendar
    course, _, rest = row["name"].partition(" · ")
    title_ = rest.rsplit(" — block", 1)[0]
    status = "done" if row["done"] else ("missed" if start < now else "planned")
    block = {"start": f"{start:%H:%M}", "end": f"{end:%H:%M}", "title": title_, "cat": course,
             "course": course, "task": task, "kind": "", "overflow": False, "due": "",
             "note": ""}
    db.execute('insert or ignore into sessions (id, task, seq, date, start, "end", planned_min, '
               "status, actual_min, measured, block) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (sid, task, int(seq), str(start.date()), f"{start:%H:%M}", f"{end:%H:%M}", planned,
                status, planned if row["done"] else None,   # its size is its time
                int(bool(row["done"])), json.dumps(block)))
    log(f"rebuilt block {sid} from Notion ({status})")


def _refinish_rebuilt(db, task: str) -> None:
    """After state.db was rebuilt from Notion: a task with no row of its own that has no
    block still planned and whose latest block is ticked was finished by that block (the
    service plans more blocks for anything still owed), so it stays finished even if that
    block ran short. A latest block that was missed means the task ran out of time instead."""
    if db.execute("select 1 from rows where plan_id = ?", (f"deadline:{task}",)).fetchone() or \
            db.execute("select 1 from sessions where task = ? and status = 'planned'", (task,)).fetchone():
        return
    last = db.execute("select id, date, status from sessions where task = ? and status in "
                      "('done', 'missed') order by date desc, \"end\" desc", (task,)).fetchone()
    if last and last["status"] == "done" and db.execute("insert into finished (task, day, block) values (?, ?, ?) "
                           "on conflict(task) do nothing", (task, last["date"], last["id"])).rowcount:
        log(f"rebuilt: {task} finished by its last block {last['id']}")


def _local(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value).astimezone(notion.TZ).replace(tzinfo=None, second=0,
                                                                          microsecond=0)


def _times(s) -> tuple[str, str]:
    return f"{s['date']}T{s['start']}", f"{s['date']}T{s['end']}"


def _set_times(db, sid: str, start: dt.datetime, end: dt.datetime) -> None:
    """Move a block, keeping its measured time equal to its size: the size is the time."""
    db.execute('update sessions set date = ?, start = ?, "end" = ?, actual_min = case when '
               "status = 'done' then ? else actual_min end where id = ?",
               (str(start.date()), f"{start:%H:%M}", f"{end:%H:%M}",
                int((end - start).total_seconds() // 60), sid))


def _session_change(db, row, sid, was_done, now, grace, bot, known_row) -> None:
    """One block row: Zach's drags and resizes first, then his tick. A block's size on the
    calendar is its time, before and after it is ticked."""
    s = db.execute("select * from sessions where id = ?", (sid,)).fetchone()
    if s is None:
        _import_session(db, row, sid, now)
        return
    start, end = _local(row["start"]), _local(row["end"])
    written = ((known_row or {}).get("w_start"), (known_row or {}).get("w_end"))
    if written[0] is None:                       # first scan since v4: we last wrote the DB times
        written = _times(s)
    edited = (row["edited_by"] != bot and start is not None and end is not None and end > start
              and (f"{start:%Y-%m-%dT%H:%M}", f"{end:%Y-%m-%dT%H:%M}") != written)
    if edited:
        size = int((end - start).total_seconds() // 60)
        moved = f"{start:%Y-%m-%dT%H:%M}" != written[0]
        _set_times(db, sid, start, end)
        # the size he gave it, and a start he chose, stand even if he unticks it later,
        # including when the tick and the drag reach this scan together
        db.execute("update sessions set user_min = ?, locked = max(locked, ?) where id = ?",
                   (size, int(moved), sid))
        if s["status"] == "done" or row["done"]:
            db.execute("update sessions set measured = 1 where id = ?", (sid,))
            log(f"edited: {sid} now {start:%a %H:%M}-{end:%H:%M} ({size} min)")
        elif moved:
            db.execute("update sessions set status = 'planned' where id = ?", (sid,))
            log(f"moved: {sid} locked at {start:%a %H:%M}-{end:%H:%M}")
        elif s["status"] == "missed":
            # its time was already planned again; the new size is what it records if ticked
            log(f"resized: missed block {sid} is now {size} min, counted only if ticked")
        else:
            log(f"resized: {sid} is now {size} min")
        db.execute("update rows set w_start = ?, w_end = ? where plan_id = ?",
                   (f"{start:%Y-%m-%dT%H:%M}", f"{end:%Y-%m-%dT%H:%M}", f"session:{sid}"))
        s = db.execute("select * from sessions where id = ?", (sid,)).fetchone()

    if not row["done"]:
        if was_done and s["status"] == "done":   # unticked: it is owed again
            end_ = dt.datetime.fromisoformat(_times(s)[1])
            # a slot already over cannot be planned in place: it becomes missed, and its
            # minutes go back into the plan
            status = "missed" if end_ <= now else "planned"
            db.execute("update sessions set status = ?, actual_min = null, measured = 0 "
                       "where id = ?", (status, sid))
            log(f"unchecked: {sid} is owed again" + (" (its slot has passed)" if status == "missed" else ""))
            if db.execute("delete from finished where task = ? and block = ?", (s["task"], sid)).rowcount:
                log(f"  {s['task']} is open again: that block had finished it")
        return
    if s["status"] == "done":
        return
    b_start, b_end = (dt.datetime.fromisoformat(t) for t in _times(s))
    length = b_end - b_start
    if s["status"] == "cancelled":
        # restored from the trash and ticked: the work happened, at the time the row shows
        size = int(length.total_seconds() // 60)
        db.execute("update sessions set status = 'done', actual_min = ?, measured = ? "
                   "where id = ?", (size, int(edited), sid))
        log(f"done: {sid} (restored and ticked), {size} min")
        _cancel_replacements(db, s["task"], size, sid)
        _finish_by_last_block(db, s["task"], sid)
        return
    # Notion's own edit time, unless the service made that edit; then it is only as
    # precise as this cycle
    checked = edited_at(row) if row["edited_by"] != bot else now
    measured = int(edited)
    if not edited:
        if checked < b_start:
            # done ahead of its slot: it moves to end when he ticked it
            b_start, b_end = checked - length, checked
        elif checked <= b_end + grace:
            b_end, measured = max(b_start + dt.timedelta(minutes=5), min(checked, b_end)), 1
        _set_times(db, sid, b_start, b_end)
    size = int((b_end - b_start).total_seconds() // 60)
    db.execute("update sessions set status = 'done', actual_min = ?, measured = ? where id = ?",
               (size, measured, sid))
    if s["status"] == "missed":
        # a missed block ticked late: its time was already re-planned, so give that back
        _cancel_replacements(db, s["task"], size, sid)
    log(f"done: {sid}, {size} min ({b_start:%a %H:%M}-{b_end:%H:%M})")
    _finish_by_last_block(db, s["task"], sid)


def _finish_by_last_block(db, task: str, sid: str) -> None:
    """A task with no Could Do row of its own (a discussion post, a weekly review) is
    finished by ticking its last block, even one that ran short: with no row to tick,
    that block is its hand-in. A task with its own row waits for that row. The block is
    the last only if none of the task is still planned and no missed block comes after
    it (a task that ran out of time keeps its missed blocks, never planned again)."""
    if db.execute("select 1 from rows where plan_id = ?", (f"deadline:{task}",)).fetchone():
        return
    b = db.execute("select date, start from sessions where id = ?", (sid,)).fetchone()
    if db.execute("select 1 from sessions where task = ? and id <> ? and (status = 'planned' or "
                  "(status = 'missed' and (date > ? or (date = ? and start > ?))))",
                  (task, sid, b["date"], b["date"], b["start"])).fetchone():
        return
    if db.execute("insert into finished (task, day, block) values (?, ?, ?) "
                  "on conflict(task) do nothing", (task, b["date"], sid)).rowcount:
        log(f"finished: {task}, its last block ticked")


def _cancel_replacements(db, task: str, minutes: int, sid: str) -> None:
    """Cancel up to `minutes` of the task's not-yet-done blocks, soonest first, including
    one already under way: the work they were planned for turned out to be done."""
    left = minutes
    for r in db.execute("select * from sessions where task = ? and status = 'planned' and id <> ? "
                        "order by date, start", (task, sid)).fetchall():
        if left <= 0:
            break
        db.execute("update sessions set status = 'cancelled' where id = ?", (r["id"],))
        left -= r["planned_min"]
        log(f"  {r['id']} no longer needed")


def _task_change(db, task: str, done: bool, now: dt.datetime) -> None:
    if done:
        db.execute("insert into finished (task, day) values (?, ?) on conflict(task) do nothing",
                   (task, str(now.date())))
        # a block under way when it was handed in closes now; later ones are not needed
        today, hhmm = str(now.date()), f"{now:%H:%M}"
        for s in db.execute("select * from sessions where task = ? and status = 'planned' "
                            "and date = ? and start <= ? and \"end\" > ?",
                            (task, today, hhmm, hhmm)).fetchall():
            start = dt.datetime.fromisoformat(f"{s['date']}T{s['start']}")
            actual = max(5, int((now - start).total_seconds() // 60))
            db.execute("update sessions set status = 'done', actual_min = ?, measured = 1, "
                       "\"end\" = ? where id = ?", (actual, hhmm, s["id"]))
        n = db.execute("update sessions set status = 'cancelled' where task = ? "
                       "and status = 'planned' and (date > ? or (date = ? and start > ?))",
                       (task, today, today, hhmm)).rowcount
        log(f"finished in Notion: {task}" + (f", {n} planned blocks cancelled" if n else ""))
    else:
        if db.execute("delete from finished where task = ? and block is null", (task,)).rowcount:
            log(f"unfinished in Notion: {task} is owed again")


def covered(db) -> set[str]:
    """Tasks with a deliverable row of Zach's: their own, or one they share."""
    return {t for r in db.execute("select plan_id from rows where plan_id like 'deadline:%'")
            for t in r["plan_id"].split(":", 1)[1].split(",")}


def state(db) -> dict:
    """The parts of the planner's state.json that come from Notion."""
    return {
        "finished": [r["task"] for r in db.execute("select task from finished")],
        "dropped": [r["task"] for r in db.execute("select task from dropped")],
        # tasks handed in by ticking their own row: past their estimate they wait for it
        "hand_in": [r["plan_id"].split(":", 1)[1] for r in db.execute(
            "select plan_id from rows where plan_id like 'deadline:%' and plan_id not like '%,%'")],
        "late_ok": [t for r in db.execute("select plan_id from rows where state = 'Late OK'")
                    for t in r["plan_id"].split(":", 1)[1].split(",")
                    if not r["plan_id"].startswith("session:")],
    }


# ---------- writing ----------------------------------------------------------
def _edited_since_scan(nt: notion.Notion, row: dict) -> bool:
    """Whether Zach changed a block row after this cycle's scan. Writing it now would put
    back the old times and make the service its last editor, so his edit would never be
    read; left alone, the next cycle reads it. Notion has no conditional update, so this
    reads the row just before writing it."""
    live = nt.get(row["page_id"])
    if live is None:
        return True                               # trashed meanwhile: the next scan sees it
    start, end = _local(live["start"]), _local(live["end"])
    shown = (start and f"{start:%Y-%m-%dT%H:%M}", end and f"{end:%Y-%m-%dT%H:%M}")
    changed = shown != (row["w_start"], row["w_end"]) or live["done"] != bool(row["done"])
    if changed and live["edited_by"] != nt.me():
        log(f"{row['plan_id']} changed in Notion during this cycle; the next one reads it")
        return True
    return False


def write(db, nt: notion.Notion, cfg: dict, resolved: list[dict], now: dt.datetime) -> tuple:
    """Make Could Do hold a row for every planned block, under Zach's deliverable row for
    its task where there is one. His rows are the only task-level rows: ticking one hands
    in its task(s), and a task with none is finished through its blocks."""
    tags = cfg["notion"]["course_tags"]
    if db.execute("select v from kv where k = 'adopted'").fetchone() is None or \
            json.loads(db.execute("select v from kv where k = 'adopted'").fetchone()[0]) != str(now.date()):
        adopt(db, nt, {t["id"] for t in resolved})     # daily, so a partial run catches up
        db.execute("insert into kv values ('adopted', ?) on conflict(k) do update set v = "
                   "excluded.v", (json.dumps(str(now.date())),))
        db.commit()
    known, added, updated, trashed = _known(db), 0, 0, 0
    by_task = {t["id"]: t for t in resolved}
    parent_of = {tid: row["page_id"] for plan_id, row in known.items()
                 if plan_id.startswith("deadline:") for tid in plan_id.split(":", 1)[1].split(",")}

    def remember(plan_id, page_id, props, times=(None, None)):
        # committed at once: Notion has already changed, so a later failure in this cycle
        # must not roll the service's record of it back
        db.execute("insert into rows(plan_id, page_id, hash, done, w_start, w_end) "
                   "values (?, ?, ?, 0, ?, ?) on conflict(plan_id) do update set "
                   "page_id = excluded.page_id, hash = excluded.hash, "
                   "w_start = coalesce(excluded.w_start, rows.w_start), "
                   "w_end = coalesce(excluded.w_end, rows.w_end)",
                   (plan_id, page_id, _hash(props), *times))
        db.commit()

    # one row per planned block
    for s in db.execute("select * from sessions where status = 'planned' and date >= ? "
                        "order by date, start", (str(now.date()),)):
        t = by_task.get(s["task"])
        if t is None:
            continue
        plan_id = f"session:{s['id']}"
        start = dt.datetime.fromisoformat(f"{s['date']}T{s['start']}")
        end = dt.datetime.fromisoformat(f"{s['date']}T{s['end']}")
        parent_page = parent_of.get(s["task"])
        props = {
            "Name": title(f"{t['course']} · {t['title']} — block {s['seq']}"),
            "Plan ID": rich(plan_id),
            "Due Date": date_range(start, end),
            "Planned min": {"number": int((end - start).total_seconds() // 60)},
            "Tags": {"multi_select": [{"name": "School"}, {"name": tags[t["course"]]}]},
            "Plan state": {"select": {"name": "Planned"}},
            # a weekly review or catch-up has no deliverable row: its blocks stand alone
            "Parent item": {"relation": [{"id": parent_page}] if parent_page else []},
        }
        row = known.get(plan_id)
        times = _times(s)
        if row is None:
            remember(plan_id, nt.create(props), props, times)
            added += 1
        elif row["hash"] != _hash(props) or (row["w_start"], row["w_end"]) != times:
            if _edited_since_scan(nt, row):
                continue
            nt.update(row["page_id"], props)
            remember(plan_id, row["page_id"], props, times)
            updated += 1

    # a block left unchecked when its day ended stays on its slot, marked Missed
    for s in db.execute("select id from sessions where status = 'missed'"):
        row = known.get(f"session:{s['id']}")
        if row and row["state"] != "Missed":
            nt.update(row["page_id"], {"Plan state": {"select": {"name": "Missed"}}})
            db.execute("update rows set state = 'Missed' where plan_id = ?", (f"session:{s['id']}",))
            db.commit()
            updated += 1

    # a done block shows when it really happened (trimmed at the tick, moved to the tick
    # when done early, or wherever Zach dragged it), so Notion alone holds the record
    for s_ in db.execute("select * from sessions where status = 'done'"):
        plan_id = f"session:{s_['id']}"
        row = known.get(plan_id)
        times = _times(s_)
        if row is None or (row["done"] and (row["w_start"], row["w_end"]) == times):
            continue
        if _edited_since_scan(nt, row):
            continue
        start, end = (dt.datetime.fromisoformat(t) for t in times)
        nt.update(row["page_id"], {"Done": {"checkbox": True}, "Due Date": date_range(start, end)})
        db.execute("update rows set done = 1, w_start = ?, w_end = ? where plan_id = ?",
                   (*times, plan_id))
        db.commit()
        updated += 1

    # blocks that are no longer planned lose their row
    live = {f"session:{r['id']}" for r in db.execute(
        "select id from sessions where status in ('planned', 'done', 'missed')")}
    for plan_id, row in known.items():
        if plan_id.startswith("session:") and plan_id not in live:
            nt.trash(row["page_id"])
            db.execute("delete from rows where plan_id = ?", (plan_id,))
            db.commit()
            trashed += 1
    return added, updated, trashed


def adopt(db, nt: notion.Notion, task_ids: set[str]) -> None:
    """First run: give the deliverable rows Zach already has a Plan ID, so checking one
    means its task is handed in. Rows whose name maps to no planner task are left alone."""
    body = {"page_size": 100, "filter": {"and": [
        {"property": "Tags", "multi_select": {"contains": "School"}},
        {"property": "Plan ID", "rich_text": {"is_empty": True}}]}}
    cursor, adopted = None, 0
    while True:
        if cursor:
            body["start_cursor"] = cursor
        res = nt.call("POST", f"/data_sources/{nt.ds}/query", body)
        for page in res["results"]:
            row = notion.parse_row(page)
            try:
                ids = notion_map.targets(row["name"])
            except (SystemExit, Exception):
                continue                      # not one of the F26 deliverables
            ids = [i for i in ids if i in task_ids]
            if not ids:
                continue
            plan_id = "deadline:" + ",".join(sorted(ids))
            nt.update(row["page_id"], {"Plan ID": rich(plan_id)})
            # recorded as not done, so a row he ticked before the service started is
            # seen as a change on the next scan and its task finishes
            db.execute("insert or replace into rows(plan_id, page_id, hash, done, state) "
                       "values (?, ?, '', 0, ?)", (plan_id, row["page_id"], row["state"]))
            db.commit()
            adopted += 1
        cursor = res.get("next_cursor")
        if not res.get("has_more"):
            break
    if adopted:
        log(f"adopted {adopted} deliverable rows that were already in Could Do")
