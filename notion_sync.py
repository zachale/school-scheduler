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

    for plan_id, row in rows.items():
        kind, _, ident = plan_id.partition(":")
        before = known.get(plan_id)
        if before is None and kind in ("task", "deadline"):
            for task in ident.split(","):     # back from the trash: the task is owed again
                if db.execute("delete from dropped where task = ?", (task,)).rowcount:
                    log(f"row restored: {task} is back in the plan")
        db.execute("insert into rows(plan_id, page_id, hash, done, state) "
                   "values (?, ?, '', ?, ?) on conflict(plan_id) do update set "
                   "page_id = excluded.page_id, state = excluded.state",
                   (plan_id, row["page_id"], int(bool(before and before["done"])), row["state"]))
        was_done = bool(before and before["done"])
        if kind == "session":
            _session_change(db, row, ident, was_done, now, grace, bot)
            if row["state"] == "Missed":
                db.execute("update sessions set status = 'missed' where id = ? "
                           "and status = 'planned'", (ident,))
        elif row["done"] != was_done and kind in ("task", "deadline"):
            for task in ident.split(","):     # one deliverable can cover several tasks
                _task_change(db, task, row["done"], now)
        if kind in ("task", "deadline") and row["done"] and "," not in ident:
            # the whole task's time, typed into Actual min on its own row
            db.execute("update finished set actual = ? where task = ?",
                       (int(row["actual_min"]) if row["actual_min"] else None, ident))
        db.execute("update rows set done = ? where plan_id = ?", (int(row["done"]), plan_id))

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
        else:
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
    planned = int(row["planned_min"] or (end - start).total_seconds() // 60)
    course, _, rest = row["name"].partition(" · ")
    title_ = rest.rsplit(" — block", 1)[0]
    status = "done" if row["done"] else ("missed" if start < now else "planned")
    block = {"start": f"{start:%H:%M}", "end": f"{end:%H:%M}", "title": title_, "cat": course,
             "course": course, "task": task, "kind": "", "overflow": False, "due": "",
             "note": ""}
    db.execute('insert or ignore into sessions (id, task, seq, date, start, "end", planned_min, '
               "status, actual_min, measured, block) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (sid, task, int(seq), str(start.date()), f"{start:%H:%M}", f"{end:%H:%M}", planned,
                status, int(row["actual_min"] or planned) if row["done"] else None,
                int(bool(row["actual_min"])), json.dumps(block)))
    log(f"rebuilt block {sid} from Notion ({status})")


def _session_change(db, row, sid, was_done, now, grace, bot) -> None:
    s = db.execute("select * from sessions where id = ?", (sid,)).fetchone()
    if s is None:
        _import_session(db, row, sid, now)
        return
    if not row["done"]:
        if was_done and s["status"] == "done":   # unticked: it is owed again
            start = dt.datetime.fromisoformat(f"{s['date']}T{s['start']}")
            end = start + dt.timedelta(minutes=s["planned_min"])
            # a slot already over cannot be planned in place: it becomes missed, and its
            # minutes go back into the plan
            status = "missed" if end <= now else "planned"
            db.execute("update sessions set status = ?, actual_min = null, measured = 0, "
                       "off_plan = 0, \"end\" = ? where id = ?", (status, f"{end:%H:%M}", sid))
            log(f"unchecked: {sid} is owed again" + (" (its slot has passed)" if status == "missed" else ""))
        return
    if s["status"] == "done":
        # Actual min typed after the tick still counts
        if row["actual_min"] and int(row["actual_min"]) != s["actual_min"]:
            db.execute("update sessions set actual_min = ?, measured = 1 where id = ?",
                       (int(row["actual_min"]), sid))
        return
    if s["status"] == "cancelled":
        # restored from the trash and ticked: the work happened, even though its slot is
        # gone; count it, without putting a block back on the calendar
        actual = int(row["actual_min"] or s["planned_min"])
        db.execute("update sessions set status = 'done', actual_min = ?, measured = ?, "
                   "off_plan = 1 where id = ?", (actual, int(bool(row["actual_min"])), sid))
        log(f"done: {sid} (restored and ticked), {actual} min")
        _cancel_replacements(db, s["task"], s["planned_min"], sid)
        return
    start = dt.datetime.fromisoformat(f"{s['date']}T{s['start']}")
    end = dt.datetime.fromisoformat(f"{s['date']}T{s['end']}")
    # Notion's own edit time, unless the service made that edit; then it is only as
    # precise as this cycle
    checked = edited_at(row) if row["edited_by"] != bot else now
    off_plan = 0
    if row["actual_min"]:
        actual, resize, measured = int(row["actual_min"]), None, 1
        off_plan = int(checked < start)
    elif checked < start:
        # done off-plan, before its block: record the estimate and take the block away
        actual, resize, measured, off_plan = s["planned_min"], None, 0, 1
    elif checked <= end + grace:
        actual = max(5, int((checked - start).total_seconds() // 60))   # the real time spent
        resize, measured = min(checked, end), 1
    else:
        actual, resize, measured = s["planned_min"], None, 0
    db.execute("update sessions set status = 'done', actual_min = ?, measured = ?, "
               "off_plan = ?, \"end\" = ? where id = ?",
               (actual, measured, off_plan, f"{resize:%H:%M}" if resize else s["end"], sid))
    if s["status"] == "missed":
        # a missed block ticked late: its time was already re-planned, so give that back
        _cancel_replacements(db, s["task"], s["planned_min"], sid)
    log(f"done: {sid} took {actual} min" + (", block removed (done off-plan)" if off_plan else
        (f", block trimmed to {resize:%H:%M}" if resize else "")))


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
        db.execute("insert or replace into finished (task, day) values (?, ?)", (task, str(now.date())))
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
        db.execute("delete from finished where task = ?", (task,))
        log(f"unfinished in Notion: {task} is owed again")


def state(db) -> dict:
    """The parts of the planner's state.json that come from Notion."""
    return {
        "finished": [r["task"] for r in db.execute("select task from finished")],
        "dropped": [r["task"] for r in db.execute("select task from dropped")],
        "late_ok": [t for r in db.execute("select plan_id from rows where state = 'Late OK'")
                    for t in r["plan_id"].split(":", 1)[1].split(",")
                    if not r["plan_id"].startswith("session:")],
    }


# ---------- writing ----------------------------------------------------------
def write(db, nt: notion.Notion, cfg: dict, resolved: list[dict], now: dt.datetime) -> tuple:
    """Make Could Do hold a row for every task and every planned block."""
    tags = cfg["notion"]["course_tags"]
    if db.execute("select v from kv where k = 'adopted'").fetchone() is None or \
            json.loads(db.execute("select v from kv where k = 'adopted'").fetchone()[0]) != str(now.date()):
        adopt(db, nt, {t["id"] for t in resolved})     # daily, so a partial run catches up
        db.execute("insert into kv values ('adopted', ?) on conflict(k) do update set v = "
                   "excluded.v", (json.dumps(str(now.date())),))
        db.commit()
    known, added, updated, trashed = _known(db), 0, 0, 0
    by_task = {t["id"]: t for t in resolved}
    # a deliverable row Zach already had stands in for the task(s) it covers
    parent_of, solo = {}, set()
    for plan_id, row in known.items():
        if plan_id.startswith("deadline:"):
            ids = plan_id.split(":", 1)[1].split(",")
            for tid in ids:
                parent_of[tid] = row["page_id"]
            if len(ids) == 1:
                solo.add(ids[0])

    def remember(plan_id, page_id, props):
        # committed at once: Notion has already changed, so a later failure in this cycle
        # must not roll the service's record of it back
        db.execute("insert into rows(plan_id, page_id, hash, done) values (?, ?, ?, 0) "
                   "on conflict(plan_id) do update set page_id = excluded.page_id, "
                   "hash = excluded.hash", (plan_id, page_id, _hash(props)))
        db.commit()

    # one row per task that still owes time (its parent deliverable row, where there is one)
    for t in resolved:
        if t["status"] not in ("scheduled", "overdue"):
            continue
        if t["id"] in solo:            # its own deliverable row already is its task row
            continue
        plan_id = f"task:{t['id']}"
        due = dt.datetime.strptime(t["orig_due"], "%Y-%m-%d %H:%M")
        props = {
            "Name": title(f"{t['course']} — {t['title']}"),
            "Plan ID": rich(plan_id),
            "Due Date": date_range(due),
            "Tags": {"multi_select": [{"name": "School"}, {"name": tags[t["course"]]}]},
            "Plan state": {"select": {"name": "Late OK" if t["late"] else "Planned"}},
        }
        if t["id"] in parent_of:
            props["Parent item"] = {"relation": [{"id": parent_of[t["id"]]}]}
        row = known.get(plan_id)
        if row is None:
            remember(plan_id, nt.create(props), props)
            added += 1
        elif row["hash"] != _hash(props):
            nt.update(row["page_id"], props)
            remember(plan_id, row["page_id"], props)
            updated += 1

    known = _known(db)                  # includes the task rows just made, as parents
    # one row per planned block, under its task's row
    for s in db.execute("select * from sessions where status = 'planned' and date >= ? "
                        "order by date, start", (str(now.date()),)):
        t = by_task.get(s["task"])
        if t is None:
            continue
        plan_id = f"session:{s['id']}"
        start = dt.datetime.fromisoformat(f"{s['date']}T{s['start']}")
        end = dt.datetime.fromisoformat(f"{s['date']}T{s['end']}")
        task_row = known.get(f"task:{s['task']}")
        parent_page = task_row["page_id"] if task_row else parent_of.get(s["task"])
        props = {
            "Name": title(f"{t['course']} · {t['title']} — block {s['seq']}"),
            "Plan ID": rich(plan_id),
            "Due Date": date_range(start, end),
            "Planned min": {"number": int((end - start).total_seconds() // 60)},
            "Tags": {"multi_select": [{"name": "School"}, {"name": tags[t["course"]]}]},
            "Plan state": {"select": {"name": "Planned"}},
        }
        if parent_page:
            props["Parent item"] = {"relation": [{"id": parent_page}]}
        row = known.get(plan_id)
        if row is None:
            remember(plan_id, nt.create(props), props)
            added += 1
        elif row["hash"] != _hash(props):
            nt.update(row["page_id"], props)
            remember(plan_id, row["page_id"], props)
            updated += 1

    # a block left unchecked when its day ended stays on its slot, marked Missed
    for s in db.execute("select id from sessions where status = 'missed'"):
        row = known.get(f"session:{s['id']}")
        if row and row["state"] != "Missed":
            nt.update(row["page_id"], {"Plan state": {"select": {"name": "Missed"}}})
            db.execute("update rows set state = 'Missed' where plan_id = ?", (f"session:{s['id']}",))
            db.commit()
            updated += 1

    # a block the service closed (handed in mid-block) or measured is written back, so
    # Notion alone can rebuild the record
    for s_ in db.execute("select * from sessions where status = 'done'"):
        plan_id = f"session:{s_['id']}"
        row = known.get(plan_id)
        stamp = f"done:{s_['actual_min'] if s_['measured'] else ''}"
        if row is None or (row["done"] and row["hash"] == stamp):
            continue
        props = {"Done": {"checkbox": True}}
        if s_["measured"]:
            props["Actual min"] = {"number": s_["actual_min"]}
        nt.update(row["page_id"], props)
        db.execute("update rows set done = 1, hash = ? where plan_id = ?", (stamp, plan_id))
        db.commit()
        updated += 1

    # a task row mirrors its task when it was finished or dropped through another row
    status = {t["id"]: t["status"] for t in resolved}
    dropped = {r["task"] for r in db.execute("select task from dropped")}
    for plan_id, row in list(known.items()):
        if not plan_id.startswith("task:"):
            continue
        tid = plan_id.split(":", 1)[1]
        if tid in dropped:
            nt.trash(row["page_id"])
            db.execute("delete from rows where plan_id = ?", (plan_id,))
            db.commit()
            trashed += 1
        elif status.get(tid) == "finished" and not row["done"]:
            nt.update(row["page_id"], {"Done": {"checkbox": True}})
            db.execute("update rows set done = 1 where plan_id = ?", (plan_id,))
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
