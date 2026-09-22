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
    rows, known, bot = nt.scan(), _known(db), nt.me()
    gone = [pid for pid in known if pid not in rows]
    if len(gone) > MAX_VANISHED:
        raise notion.NotionError(f"{len(gone)} planner rows vanished from Could Do at once; "
                                 "not treating that as deletions")

    for plan_id, row in rows.items():
        db.execute("insert into rows(plan_id, page_id, hash, done, state) "
                   "values (?, ?, '', ?, ?) on conflict(plan_id) do update set "
                   "page_id = excluded.page_id, state = excluded.state",
                   (plan_id, row["page_id"], int(row["done"]), row["state"]))
        kind, _, ident = plan_id.partition(":")
        was_done = bool(known.get(plan_id, {}).get("done"))
        if kind == "session":
            _session_change(db, cfg, row, ident, was_done, now, grace, bot)
        elif row["done"] != was_done and kind in ("task", "deadline"):
            for task in ident.split(","):        # one deliverable can cover several tasks
                _task_change(db, task, row["done"], now)
        if row["state"] == "Missed" and kind == "session":
            db.execute("update sessions set status = 'missed' where id = ? and status = 'planned'",
                       (ident,))
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
                           "and status = 'planned'", (task,))
            log(f"row deleted: {ident} dropped from the plan")


def _session_change(db, cfg, row, sid, was_done, now, grace, bot) -> None:
    s = db.execute("select * from sessions where id = ?", (sid,)).fetchone()
    if s is None:
        return
    if not row["done"]:
        if was_done:                          # unticked: it is owed again
            db.execute("update sessions set status = 'planned', actual_min = null "
                       "where id = ?", (sid,))
            log(f"unchecked: {sid} is back in the plan")
        return
    if s["status"] == "done":
        return
    start = dt.datetime.fromisoformat(f"{s['date']}T{s['start']}")
    end = dt.datetime.fromisoformat(f"{s['date']}T{s['end']}")
    planned = s["planned_min"]
    # Notion's own edit time, unless the service made that edit; then it is only as
    # precise as this cycle
    checked = edited_at(row) if row["edited_by"] != bot else now
    if row["actual_min"]:
        actual, resize, measured = int(row["actual_min"]), None, 1
    elif start <= checked <= end + grace:
        actual = max(5, int((min(checked, end) - start).total_seconds() // 60))
        resize, measured = min(checked, end), 1   # the block ends when he finished
    else:
        actual, resize, measured = planned, None, 0
    db.execute("update sessions set status = 'done', actual_min = ?, measured = ?, "
               "\"end\" = ? where id = ?",
               (actual, measured, f"{resize:%H:%M}" if resize else s["end"], sid))
    log(f"done: {sid} took {actual} min" + (f", block trimmed to {resize:%H:%M}" if resize else ""))


def _task_change(db, task: str, done: bool, now: dt.datetime) -> None:
    if done:
        db.execute("insert or replace into finished values (?, ?)", (task, str(now.date())))
        n = db.execute("update sessions set status = 'cancelled' where task = ? "
                       "and status = 'planned'", (task,)).rowcount
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
    if not any(k.startswith("deadline:") for k in _known(db)):
        adopt(db, nt, {t["id"] for t in resolved})
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
        db.execute("insert into rows(plan_id, page_id, hash, done) values (?, ?, ?, 0) "
                   "on conflict(plan_id) do update set page_id = excluded.page_id, "
                   "hash = excluded.hash", (plan_id, page_id, _hash(props)))

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
            updated += 1

    # blocks that are no longer planned lose their row
    live = {f"session:{r['id']}" for r in db.execute(
        "select id from sessions where status in ('planned', 'done', 'missed')")}
    for plan_id, row in known.items():
        if plan_id.startswith("session:") and plan_id not in live:
            nt.trash(row["page_id"])
            db.execute("delete from rows where plan_id = ?", (plan_id,))
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
            except SystemExit:
                continue                      # not one of the F26 deliverables
            ids = [i for i in ids if i in task_ids]
            if not ids:
                continue
            plan_id = "deadline:" + ",".join(sorted(ids))
            nt.update(row["page_id"], {"Plan ID": rich(plan_id)})
            db.execute("insert or replace into rows(plan_id, page_id, hash, done, state) "
                       "values (?, ?, '', ?, ?)",
                       (plan_id, row["page_id"], int(row["done"]), row["state"]))
            adopted += 1
        cursor = res.get("next_cursor")
        if not res.get("has_more"):
            break
    log(f"adopted {adopted} deliverable rows that were already in Could Do")
