# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Time-block every Fall 2026 deliverable across the term.

Reads  config.yaml (constraints) · tasks.yaml (work) · events.yaml (ad-hoc commitments)
       busy.json (busy time on Zach's own calendars)
       state.json (now, minutes done, blocks already started; written by service.py)
Writes schedule.json · resolved_tasks.json · calendar.html

    uv run plan.py                 # replan from state.json's "now" and render

Rescheduling is not incremental: the planner is deterministic, so any changed input
reflows everything after "now". Blocks that have already started ("pinned") are
history: they stay where they are and count toward that day's caps.

state.json:
    now        Toronto wall-clock time the plan starts from, e.g. "2026-09-22T09:05"
    done       {task id: minutes finished in earlier sessions}
    last_done  {task id: date of its latest finished session}  (spacing discussion posts)
    pinned     [started blocks today, as schedule.json work blocks, each with "session"]
    finished   [task ids whose Notion row is checked]
    dropped    [task ids whose Notion row was deleted]
    late_ok    [task ids to keep scheduling after their deadline has passed]
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import yaml

HERE = Path(__file__).parent
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------- small helpers ----------------------------------------------------
def hm(s: str) -> int:
    """'09:30' -> minutes from midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def pretty(mins: int) -> str:
    h, m = divmod(int(round(mins)), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def subtract(free: list[tuple[int, int]], busy: tuple[int, int]) -> list[tuple[int, int]]:
    bs, be = busy
    out = []
    for s, e in free:
        if be <= s or bs >= e:
            out.append((s, e))
            continue
        if s < bs:
            out.append((s, bs))
        if be < e:
            out.append((be, e))
    return [(s, e) for s, e in out if e > s]


# ---------- unavailability ---------------------------------------------------
def blocked_dates(cfg: dict) -> dict[dt.date, str]:
    """Every whole day with no work and no classes, mapped to its reason."""
    out: dict[dt.date, str] = {}
    for u in cfg.get("unavailable") or []:
        d = u["from"]
        while d <= u["to"]:
            out[d] = u.get("reason", "Unavailable")
            d += dt.timedelta(days=1)
    return out


def class_weekday(cfg: dict, date: dt.date) -> int | None:
    for o in cfg.get("schedule_overrides") or []:
        if o["date"] == date:
            return o["runs_schedule_of"]
    for n in cfg.get("no_class_days") or []:
        if n["date"] == date:
            return None
    if date > cfg["term"]["last_class_day"]:
        return None
    return date.weekday()


def skipped_labs(cfg: dict, date: dt.date) -> list[dict]:
    """Skipped labs that would run on this date."""
    wd = class_weekday(cfg, date)
    return [c for c in cfg["classes"] if c.get("skip") and wd is not None and wd in c["days"]]


def attended(cfg: dict) -> list[dict]:
    """Classes actually gone to; skipped labs are listed in config only so their
    calendar slots are recognised."""
    return [c for c in cfg["classes"] if not c.get("skip")]


def missed_lectures(cfg: dict, blocked: dict) -> dict[str, list[dt.date]]:
    """Lectures that would have run on a blocked day, by course."""
    out: dict[str, list[dt.date]] = {}
    for d in sorted(blocked):
        wd = class_weekday(cfg, d)
        if wd is None:
            continue
        for c in attended(cfg):
            if wd in c["days"]:
                out.setdefault(c["course"], []).append(d)
    return out


# ---------- the day model ----------------------------------------------------
class Day:
    def __init__(self, date: dt.date, cfg: dict, events: list[dict], blocked: dict,
                 busy: list[dict]):
        self.date = date
        self.cfg = cfg
        self.fixed: list[dict] = []          # classes, travel, meals, ad-hoc
        self.blocks: list[dict] = []         # scheduled work
        self.blocked = blocked.get(date)
        self.not_before = 0                  # minutes; set on today to "now"
        if self.blocked:
            self.fixed.append({"start": hm("07:00"), "end": hm("22:00"),
                               "title": self.blocked, "cat": "Personal"})
        else:
            self._build(events, [b for b in busy if b["date"] == str(date)])

    # -- fixed commitments
    def _class_weekday(self) -> int | None:
        return class_weekday(self.cfg, self.date)

    def _build(self, events: list[dict], busy: list[dict]) -> None:
        trav = self.cfg["travel"]["minutes_each_way"]

        # classes for this day, merged into on-campus trips
        wd = self._class_weekday()
        classes = []
        if wd is not None:
            for c in attended(self.cfg):
                if wd in c["days"]:
                    classes.append((hm(c["start"]), hm(c["end"]), c))
        classes.sort()

        trips: list[list] = []
        for s, e, c in classes:
            if trips and s - trips[-1][1] <= 60:     # same trip to campus
                trips[-1][1] = max(trips[-1][1], e)
                trips[-1][2].append(c)
            else:
                trips.append([s, e, [c]])
        for s, e, cs in trips:
            self.fixed.append({"start": s - trav, "end": s, "title": "Travel to campus",
                               "cat": "Travel"})
            for c in cs:
                cs_ = next(x for x in classes if x[2] is c)
                ex = next((x for x in self.cfg.get("exams") or []
                           if x["date"] == self.date and x["course"] == c["course"]
                           and x.get("in_class")), None)
                self.fixed.append({"start": cs_[0], "end": cs_[1],
                                   "title": (f"{c['course']} {ex['title']} - {c['room']}" if ex
                                             else f"{c['course']} {c['kind']} - {c['room']}"),
                                   "cat": "Exam" if ex else "Class", "course": c["course"]})
            self.fixed.append({"start": e, "end": e + trav, "title": "Travel home",
                               "cat": "Travel"})

        # exams outside lecture time
        for ex in self.cfg.get("exams") or []:
            if ex["date"] != self.date or ex.get("in_class"):
                continue
            s, e = hm(ex["start"]), hm(ex["end"])
            if ex.get("travel"):
                self.fixed.append({"start": s - trav, "end": s, "title": "Travel to exam",
                                   "cat": "Travel"})
                self.fixed.append({"start": e, "end": e + trav, "title": "Travel home",
                                   "cat": "Travel"})
            self.fixed.append({"start": s, "end": e, "cat": "Exam", "course": ex["course"],
                               "title": f"{ex['course']} {ex['title']} - {ex.get('room', '')}"})

        # ad-hoc events
        for ev in events:
            if ev["date"] != self.date:
                continue
            s, e = hm(ev["start"]), hm(ev["end"])
            if ev.get("travel"):
                self.fixed.append({"start": s - trav, "end": s, "title": "Travel", "cat": "Travel"})
                self.fixed.append({"start": e, "end": e + trav, "title": "Travel", "cat": "Travel"})
            self.fixed.append({"start": s, "end": e, "title": ev["title"], "cat": "Personal"})

        # busy time on Zach's own calendars, minus anything already accounted for above
        # (his calendar carries the same classes and exams). A skipped lab's own calendar
        # event is ignored only when free/busy returns exactly its slot: free/busy merges
        # overlapping events, so a longer interval may hide a real commitment and stays busy.
        known = [(b["start"], b["end"]) for b in self.fixed]
        lab_slots = {(hm(c["start"]), hm(c["end"])) for c in skipped_labs(self.cfg, self.date)}
        buf = self.cfg["google_calendar"]["buffer_minutes"]
        added: list[tuple[int, int]] = []
        for b in busy:
            span = (hm(b["start"]), hm(b["end"]))
            if span in lab_slots:
                continue
            parts = [span]
            for k in known:
                parts = subtract(parts, k)
            added += parts
        for s, e in added:
            self.fixed.append({"start": s, "end": e, "title": "Busy (Google Calendar)",
                               "cat": "Personal"})
            pad = [(max(0, s - buf), s), (e, min(24 * 60, e + buf))]
            for k in known + added:
                pad = subtract(pad, k)
            for ps, pe in pad:
                self.fixed.append({"start": ps, "end": pe, "title": "Buffer", "cat": "Travel"})

        # meals, slid to avoid whatever is already fixed
        for meal in self.cfg["meals"]:
            self._place_meal(meal)

        self.fixed.sort(key=lambda b: b["start"])

    def _place_meal(self, meal: dict) -> None:
        want, dur = hm(meal["around"]), meal["minutes"]
        lo, hi = hm(meal["window"][0]), hm(meal["window"][1])
        busy = [(b["start"], b["end"]) for b in self.fixed]
        free = [(lo, hi)]
        for b in busy:
            free = subtract(free, b)
        cands = [(s, e) for s, e in free if e - s >= dur]
        if not cands:
            return                                   # genuinely no gap; skip rather than lie
        # nearest slot to the preferred time
        best = min(cands, key=lambda se: abs(max(se[0], min(want, se[1] - dur)) - want))
        start = max(best[0], min(want, best[1] - dur))
        self.fixed.append({"start": start, "end": start + dur, "title": meal["name"], "cat": "Meal"})

    # -- capacity
    def slots(self, overflow: bool) -> list[tuple[int, int]]:
        if self.blocked:
            return []
        w = self.cfg["work_hours"]
        if not w.get("include_weekends", True) and self.date.weekday() >= 5:
            return []
        rng = w["overflow"] if overflow else w["core"]
        free = [(max(hm(rng[0]), self.not_before), hm(rng[1]))]
        free = [(a, b) for a, b in free if b > a]
        for b in self.fixed + self.blocks:
            free = subtract(free, (b["start"], b["end"]))
        return [(s, e) for s, e in free if e - s >= 20]

    def run_before(self, t: int, gap: int) -> int:
        """Minutes of work running up to time t without a gap of `gap` minutes or more."""
        run, cursor = 0, t
        for b in sorted(self.blocks, key=lambda b: b["end"], reverse=True):
            if b["end"] > cursor:
                continue
            if cursor - b["end"] >= gap:
                break
            run += b["end"] - b["start"]
            cursor = b["start"]
        return run

    def worked(self, overflow: bool | None = None) -> int:
        if overflow is None:
            return sum(b["end"] - b["start"] for b in self.blocks)
        return sum(b["end"] - b["start"] for b in self.blocks if b["overflow"] is overflow)


# ---------- task preparation -------------------------------------------------
def parse_tasks(tasks: list[dict]) -> None:
    for t in tasks:
        t["_orig_due"] = dt.datetime.strptime(t["due"], "%Y-%m-%d %H:%M")
        t["_due"] = t["_orig_due"]
        t["_earliest"] = dt.date.fromisoformat(str(t["earliest"]))


def add_catchups(cfg: dict, tasks: list[dict], blocked: dict) -> list[str]:
    """Missed lectures become catch-up work, replacing that week's review. Runs on the
    tasks.yaml windows, before progress or overdue handling can move them."""
    changes: list[str] = []
    avail = lambda d: d not in blocked
    per = cfg.get("catchup", {}).get("minutes_per_missed_lecture", 117)
    span = cfg.get("catchup", {}).get("days_to_complete", 7)
    for course, dates in missed_lectures(cfg, blocked).items():
        back = max(dates) + dt.timedelta(days=1)
        while not avail(back):
            back += dt.timedelta(days=1)
        due = dt.datetime.combine(back + dt.timedelta(days=span - 1), dt.time(22, 0))
        tasks.append({
            "id": f"catchup-{course.lower()}", "course": course,
            "title": f"Catch up {len(dates)} missed lectures", "minutes": len(dates) * per,
            "earliest": str(back), "due": f"{due:%Y-%m-%d %H:%M}", "kind": "catchup",
            "min_minutes": 60, "max_minutes": 180,
            "note": "missed " + ", ".join(f"{d:%a %b %d}" for d in dates),
            "_earliest": back, "_due": due, "_orig_due": due,
        })
        changes.append(f"{course}: +{pretty(len(dates) * per)} catch-up for "
                       f"{len(dates)} missed lectures ({back:%b %d}-{due:%b %d})")
        for t in list(tasks):
            if (t["kind"] == "ongoing" and t["course"] == course
                    and any(t["_earliest"] <= d <= t["_due"].date() for d in dates)):
                tasks.remove(t)
                changes.append(f"{course} {t['title']} ({t['_earliest']:%b %d} week): "
                               f"dropped, covered by catch-up")
    return changes


WRAPUP_MINUTES = 30


def apply_state(tasks: list[dict], state: dict):
    """Apply learned speed, take finished and started minutes off each task, and set
    aside what needs no more time. Returns the tasks still owed time and the closed ones
    (each with _status finished | dropped | awaiting, and _finished_on).

    A hand-in ("work") is finished when Zach checks it off, not when its minutes run
    out: past its estimate it gets one short wrap-up session, then waits ("awaiting")."""
    done, last = state.get("done") or {}, state.get("last_done") or {}
    finished_ids = set(state.get("finished") or [])
    dropped_ids = set(state.get("dropped") or [])
    wrapped = state.get("wrapped") or {}       # wrap-up minutes already spent or under way
    speed = state.get("multiplier") or {}
    today = dt.datetime.fromisoformat(state["now"]).date()
    pinned: dict[str, int] = {}
    for b in state.get("pinned") or []:
        pinned[b["task"]] = pinned.get(b["task"], 0) + hm(b["end"]) - hm(b["start"])
    owed, closed = [], []
    for t in tasks:
        t["_base"] = t["minutes"]
        if t["id"] in speed:
            t["minutes"] = max(5, round(t["minutes"] * speed[t["id"]]))
        t["_done"] = int(done.get(t["id"], 0))
        t["_pinned"] = pinned.get(t["id"], 0)
        left = t["minutes"] - t["_done"] - t["_pinned"]
        status = None
        if t["id"] in dropped_ids:
            status = "dropped"
        elif t["id"] in finished_ids:
            status = "finished"
        elif left <= 0:
            spare = WRAPUP_MINUTES - int(wrapped.get(t["id"], 0))
            if t["kind"] != "work":
                status = "finished"            # its minutes are the whole job
            elif spare <= 0:
                status = "awaiting"            # wrapped up; waiting to be handed in
            else:
                t["minutes"] = t["_done"] + t["_pinned"] + spare
                t["_wrapup"] = True
        if status:
            t["_status"] = status
            t["_finished_on"] = (today if t["_pinned"]
                                 else dt.date.fromisoformat(last.get(t["id"], str(today))))
            closed.append(t)
        else:
            owed.append(t)
    return owed, closed


def resolve_windows(cfg: dict, tasks: list[dict], blocked: dict, now: dt.datetime,
                    late_ok: set[str], seeded: dict[str, list[dt.date]]):
    """Resolve each task's real window against the days actually available and the
    current time. Returns the tasks to schedule, the overdue tasks left unscheduled,
    and a human-readable list of what changed."""
    changes: list[str] = []
    avail = lambda d: d not in blocked
    today = now.date()

    def last_avail_on_or_before(d: dt.date) -> dt.date:
        while not avail(d):
            d -= dt.timedelta(days=1)
        return d

    end_of_day = hm(cfg["work_hours"]["overflow"][1])

    def soonest_finish(start: dt.date, minutes: int) -> dt.datetime:
        """The earliest a task could be done if begun on `start`, at ~5 h a day. Today
        counts only the hours that are actually left."""
        d, left = start, minutes
        if d == today:
            hours_left = max(0, end_of_day - (now.hour * 60 + now.minute))
            left -= min(left, hours_left)
            if left <= 0:
                return dt.datetime.combine(d, dt.time(end_of_day // 60, end_of_day % 60))
            d += dt.timedelta(days=1)
        while not avail(d):
            d += dt.timedelta(days=1)
        while left > 300:
            left -= 300
            d += dt.timedelta(days=1)
            while not avail(d):
                d += dt.timedelta(days=1)
        return dt.datetime.combine(d, dt.time(22, 0))

    live, overdue = [], []
    for t in tasks:
        owed = t["minutes"] - t["_done"] - t["_pinned"]
        # the real deadline has passed with work still owed: stop scheduling it,
        # unless Zach said it is worth finishing late
        if t["_orig_due"] <= now:
            if t["id"] not in late_ok:
                overdue.append(t)
                continue
            t["_overdue"] = True
            t["_earliest"] = today
            t["_due"] = soonest_finish(today, owed)
            t.pop("spread_group", None)          # late posts are not spaced out
            changes.append(f"{t['course']} {t['title']}: late (was due "
                           f"{t['_orig_due']:%a %b %d}), now by {t['_due']:%a %b %d}")
            live.append(t)
            continue

        # a deadline inside a blocked stretch moves to the evening before it, unless
        # that evening has already passed; then only the real deadline is left
        if not avail(t["_due"].date()):
            d = last_avail_on_or_before(t["_due"].date())
            pulled = dt.datetime.combine(d, dt.time(22, 0))
            if pulled > now:
                t["_due"] = pulled
                changes.append(f"{t['course']} {t['title']}: finish by {d:%a %b %d} "
                               f"(really due {t['_orig_due']:%a %b %d})")

        # a window that is now empty opens up the week before
        if t["_earliest"] > t["_due"].date():
            new = max(today, t["_due"].date() - dt.timedelta(days=6))
            changes.append(f"{t['course']} {t['title']}: moved earlier, now "
                           f"{new:%b %d}-{t['_due']:%b %d}")
            t["_earliest"] = new
        live.append(t)

    # early finish: aim hand-in work N days ahead of its real deadline, but never
    # earlier than it can physically be completed after it is released
    ef = cfg.get("early_finish") or {}
    N, kinds = int(ef.get("days", 0)), set(ef.get("kinds", ["work"]))
    ef_from = ef.get("from")
    if N:
        for t in live:
            if t["kind"] not in kinds or t.get("_overdue"):
                continue
            if ef_from and t["_orig_due"].date() < ef_from:
                continue
            target = t["_orig_due"] - dt.timedelta(days=N)
            if target.date() in blocked:
                target = dt.datetime.combine(last_avail_on_or_before(target.date()),
                                             dt.time(22, 0))
            floor = soonest_finish(max(t["_earliest"], today),
                                   t["minutes"] - t["_done"] - t["_pinned"])
            new = max(target, floor)
            if new < t["_due"]:
                t["_hard_due"] = t["_due"]        # fall back to this if N days early cannot fit
                t["_due"] = new
            t["_buffer_days"] = (t["_orig_due"].date() - t["_due"].date()).days

    # spread groups: split each group evenly across its available days
    groups: dict[str, list[dict]] = {}
    for t in live:
        if t.get("spread_group"):
            groups.setdefault(t["spread_group"], []).append(t)
    for gid, members in groups.items():
        members.sort(key=lambda t: t["id"])
        gap = max((t.get("min_gap_days", 1) for t in members), default=1)
        lo = max(min(t["_earliest"] for t in members), today)
        if seeded.get(gid):          # posts already done hold the next one back
            lo = max(lo, max(seeded[gid]) + dt.timedelta(days=gap))
        hi = max(t["_due"] for t in members)
        days = [lo + dt.timedelta(days=i) for i in range((hi.date() - lo).days + 1)]
        days = [d for d in days if avail(d)]
        n = len(members)
        if not days:
            continue
        for j, t in enumerate(members):
            a = days[min(len(days) - 1, (j * len(days)) // n)]
            b = days[min(len(days) - 1, ((j + 1) * len(days)) // n - 1)]
            t["_earliest"] = a
            t["_due"] = hi if j == n - 1 else dt.datetime.combine(b, dt.time(22, 0))
    return live, overdue, changes


# ---------- scheduler --------------------------------------------------------
def schedule(cfg: dict, tasks: list[dict], events: list[dict], busy: list[dict],
             state: dict):
    term = cfg["term"]
    now = dt.datetime.fromisoformat(state["now"])
    d0, d1 = max(term["start"], now.date()), term["plan_until"]
    blocked = blocked_dates(cfg)
    parse_tasks(tasks)
    changes = add_catchups(cfg, tasks, blocked)
    tasks, finished = apply_state(tasks, state)
    seeded: dict[str, list[dt.date]] = {}
    for t in finished:               # a post that was dropped never happened
        if t.get("spread_group") and t["_status"] == "finished":
            seeded.setdefault(t["spread_group"], []).append(t["_finished_on"])
    live, overdue, more = resolve_windows(cfg, tasks, blocked, now,
                                          set(state.get("late_ok") or []), seeded)
    changes += more
    tasks = live
    def build_days():
        days = [Day(d0 + dt.timedelta(days=i), cfg, events, blocked, busy)
                for i in range((d1 - d0).days + 1)]
        # today: nothing new before now, and the blocks already started stay put
        if days and days[0].date == now.date():
            days[0].not_before = now.hour * 60 + now.minute
            for b in state.get("pinned") or []:
                days[0].blocks.append({**b, "start": hm(b["start"]), "end": hm(b["end"]),
                                       "pinned": True})
            brk, brk_after = S.get("break_minutes", 15), S.get("break_after_minutes", 90)
            for b in days[0].blocks:         # a long started session still earns its break
                if days[0].run_before(b["end"], brk) >= brk_after:
                    days[0].fixed.append({"start": b["end"], "end": b["end"] + brk,
                                          "title": "Break", "cat": "Break"})
        return days

    S = cfg["sessions"]
    core_cap = int(S["max_core_hours_per_day"] * 60)
    total_cap = int(S["max_total_hours_per_day"] * 60)
    exam_cap = int(S.get("exam_day_max_hours", S["max_total_hours_per_day"]) * 60)

    def attempt(days):
        for t in tasks:
            t["remaining"] = t["minutes"] - t["_done"] - t["_pinned"]

        def latest_start(t, today):
            """Latest date this task can begin and still finish at ~5 h/day,
            counting only days that are actually available."""
            need = max(1, math.ceil(t["remaining"] / 300))
            d = t["_due"].date()
            while need > 0 and d > today:
                d -= dt.timedelta(days=1)
                if d not in blocked:
                    need -= 1
            return d

        unplaced: list[dict] = []
        # discussion posts already finished still count toward the spacing rule
        group_days: dict[str, list[dt.date]] = {k: list(v) for k, v in seeded.items()}

        # Per day, two passes: core hours with any live task, then evening overflow
        # for pressed work only. An exam day is capped lower and never runs into the
        # evening. (A lower "comfortable target" tier was tried and removed: every
        # target below the core cap pushed work into evenings and worsened peak days.)
        for day in days:
            exam_day = any(b.get("cat") == "Exam" for b in day.fixed)
            passes = [(False, core_cap, False), (True, total_cap, True)]
            for overflow, cap, pressed_only in passes:
                if exam_day:
                    if overflow:
                        continue
                    cap = min(cap, exam_cap)
                while True:
                    slots = day.slots(overflow)
                    if not slots:
                        break
                    used = day.worked()
                    if used >= cap:
                        break

                    def spread_ok(t):
                        gid = t.get("spread_group")
                        if not gid:
                            return True
                        gap = t.get("min_gap_days", 1)
                        return all(abs((day.date - d).days) >= gap
                                   for d in group_days.get(gid, []))

                    live = [t for t in tasks
                            if t["remaining"] > 0
                            and t["_earliest"] <= day.date
                            and t["_due"].date() >= day.date
                            and spread_ok(t)]
                    if not live:
                        break
                    # past the comfortable target, only work that is actually pressed
                    if pressed_only:
                        live = [t for t in live if latest_start(t, day.date) <= day.date]
                        if not live:
                            break
                    live.sort(key=lambda t: (latest_start(t, day.date), t["_due"], -t["remaining"]))

                    placed = False
                    for t in live:
                        # on the due date itself, nothing may run past the due time
                        cutoff = (t["_due"].hour * 60 + t["_due"].minute
                                  if t["_due"].date() == day.date else 24 * 60)
                        for si, (s, e) in enumerate(sorted(slots)):
                            e = min(e, cutoff)
                            if e - s < 20:
                                continue
                            room = int(min(e - s, cap - used))
                            chunk = int(min(t["remaining"], t["max_minutes"], room))
                            if chunk < min(t["min_minutes"], t["remaining"]):
                                continue
                            # never leave a sliver of a task behind: absorb a leftover under
                            # 20 min into this session, allowing up to 15 min past the max
                            if 0 < t["remaining"] - chunk < 20:
                                if t["remaining"] <= min(room, t["max_minutes"] + 15):
                                    chunk = t["remaining"]
                                else:
                                    chunk = t["remaining"] - 20   # leave a real session instead
                            brk_after = S.get("break_after_minutes", 90)
                            brk = S.get("break_minutes", 15)
                            if day.run_before(s, brk) + chunk >= brk_after:
                                day.fixed.append({"start": s + chunk, "end": s + chunk + brk,
                                                  "title": "Break", "cat": "Break"})
                            day.blocks.append({
                                "start": s, "end": s + chunk, "title": t["title"],
                                "cat": t["course"], "course": t["course"], "task": t["id"],
                                "kind": t["kind"], "overflow": overflow,
                                "due": t["due"], "note": t.get("note", ""),
                                **({"wrapup": True} if t.get("_wrapup") else {}),
                            })
                            t["remaining"] -= chunk
                            if t.get("spread_group") and t["remaining"] == 0:
                                group_days.setdefault(t["spread_group"], []).append(day.date)
                            placed = True
                            break
                        if placed:
                            break
                    if not placed:
                        break
            day.blocks.sort(key=lambda b: b["start"])
            # merge back-to-back sessions of the same task into one readable block
            merged: list[dict] = []
            for b in day.blocks:
                if (merged and merged[-1]["task"] == b["task"] and merged[-1]["end"] == b["start"]
                        and merged[-1]["overflow"] == b["overflow"]
                        and not merged[-1].get("pinned") and not b.get("pinned")
                        and b["end"] - merged[-1]["start"] <= S["default_max_minutes"]):
                    merged[-1]["end"] = b["end"]
                else:
                    merged.append(b)
            day.blocks = merged

        return [t for t in tasks if t["remaining"] > 0]

    def drop_unplaceable_wrapups(unplaced):
        """The wrap-up is an allowance, not owed work: if it does not fit, let it go."""
        keep = []
        for t in unplaced:
            if t.get("_wrapup") and t["remaining"] <= WRAPUP_MINUTES:
                t["_status"], t["_finished_on"] = "awaiting", now.date()
                finished.append(t)
                tasks.remove(t)
            else:
                keep.append(t)
        return keep

    days = build_days()
    unplaced = drop_unplaceable_wrapups(attempt(days))
    if unplaced:
        # an early-finish target that cannot be met is a preference, not a deadline
        relaxed = False
        for t in unplaced:
            if t.get("_hard_due") and t["_hard_due"] > t["_due"]:
                changes.append(f"{t['course']} {t['title']}: cannot finish "
                               f"{t['_buffer_days']} days early, using its real deadline")
                # the early-finish promise is released for this one: verify expects no buffer
                t["_due"], t["_buffer_days"], relaxed = t["_hard_due"], 0, True
        if relaxed:
            days = build_days()
            unplaced = drop_unplaceable_wrapups(attempt(days))
    if unplaced:
        # last resort: give back the learned speed and the post spacing rather than
        # leave work unscheduled and the whole plan frozen
        for t in unplaced:
            t["minutes"] = t.get("_base", t["minutes"])
            t.pop("spread_group", None)
            if t.get("_hard_due") and t["_hard_due"] > t["_due"]:
                t["_due"], t["_buffer_days"] = t["_hard_due"], 0
            changes.append(f"{t['course']} {t['title']}: tight, so its learned pace and post "
                           "spacing are set aside to fit it in")
        days = build_days()
        unplaced = drop_unplaceable_wrapups(attempt(days))
    for t in unplaced:
        t["_status"] = "unplaced"
    return days, unplaced, tasks, overdue, finished, changes


# ---------- rendering --------------------------------------------------------
CSS = """
:root{--line:#dadce0;--txt:#3c4043;--mut:#70757a;--bg:#fff}
*{box-sizing:border-box}
body{margin:0;font:13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     color:var(--txt);background:#f1f3f4}
header{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
       padding:14px 20px}
h1{margin:0 0 3px;font-size:19px;font-weight:500}
.sub{color:var(--mut);font-size:12px}
.wrap{padding:18px 20px 60px;max-width:1500px;margin:0 auto}
.week{background:var(--bg);border:1px solid var(--line);border-radius:8px;margin-bottom:22px;
      overflow:hidden}
.wk-hd{padding:9px 14px;border-bottom:1px solid var(--line);font-weight:500;
       display:flex;justify-content:space-between;align-items:baseline}
.wk-hd .load{font-weight:400;color:var(--mut);font-size:12px}
.grid{display:grid;grid-template-columns:52px repeat(7,1fr);position:relative}
.daycol{border-left:1px solid var(--line);position:relative}
.dayhd{height:44px;border-bottom:1px solid var(--line);text-align:center;padding-top:5px}
.dayhd .dow{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.7px}
.dayhd .num{font-size:17px;line-height:1.3}
.dayhd.today .num{background:#1a73e8;color:#fff;border-radius:50%;width:26px;height:26px;
                  display:inline-block;line-height:26px}
.dayhd.off{background:#f8f9fa}
.axis .dayhd{border-left:none}
.hr{height:42px;border-bottom:1px solid #eceff1;font-size:10px;color:var(--mut);
    text-align:right;padding-right:6px}
.body{position:relative}
.ev{position:absolute;left:2px;right:2px;border-radius:4px;padding:2px 4px;overflow:hidden;
    color:#fff;font-size:10px;line-height:1.25;border-left:3px solid rgba(0,0,0,.22)}
.ev b{font-weight:500;display:block}
.ev .t{opacity:.85;font-size:9px}
.ev.fixed{opacity:.93}
.ev.meal,.ev.travel{font-size:9px}
.ev.of{background-image:repeating-linear-gradient(45deg,transparent,transparent 5px,
       rgba(255,255,255,.14) 5px,rgba(255,255,255,.14) 10px)}
.dl{height:30px;border-bottom:1px solid var(--line);padding:2px 3px;overflow:hidden;
    display:flex;flex-direction:column;gap:2px;background:#fafafa}
.dl .pill{font-size:9px;line-height:12px;border-radius:3px;padding:0 4px;color:#fff;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dl .more{font-size:9px;color:var(--mut)}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0 0;font-size:11px;align-items:center}
.sw{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;
    vertical-align:-1px}
table.sum{border-collapse:collapse;font-size:12px;margin-top:6px}
table.sum td,table.sum th{border:1px solid var(--line);padding:4px 9px;text-align:right}
table.sum th:first-child,table.sum td:first-child{text-align:left}
.warn{background:#fce8e6;border:1px solid #f5c6c2;border-radius:6px;padding:10px 14px;
      margin:12px 0;font-size:12px}
.ok{background:#e6f4ea;border:1px solid #b7e1c4;border-radius:6px;padding:10px 14px;
    margin:12px 0;font-size:12px}
"""

def render(days, unplaced, cfg, tasks, overdue, now) -> str:
    colours = cfg["colours"]
    T0, T1 = hm("07:00"), hm("22:00")
    PX = 42 / 60  # one hour = 42px

    def block_html(b, fixed):
        s, e = max(b["start"], T0), min(b["end"], T1)
        if e <= s:
            return ""
        top = (s - T0) * PX
        h = max((e - s) * PX, 12)
        col = colours.get(b.get("cat"), "#5f6368")
        cls = "ev fixed" if fixed else "ev"
        if b.get("overflow"):
            cls += " of"
        if b.get("cat") in ("Meal", "Travel"):
            cls += " meal"
        label = f'{b["course"]} · {b["title"]}' if not fixed and b.get("course") else b["title"]
        sub = f"{fmt(b['start'])}–{fmt(b['end'])}"
        tip = f"{label} · {sub}"
        if not fixed and b.get("due"):
            tip += f" · really due {b['due']}"
        return (f'<div class="{cls}" style="top:{top:.1f}px;height:{h:.1f}px;background:{col}" '
                f'title="{tip}"><b>{label}</b><span class="t">{sub}</span></div>')

    # real deadlines of hand-in work, flagged on the day they are actually due
    ef_days = int((cfg.get("early_finish") or {}).get("days", 0))
    deadlines: dict[dt.date, list] = {}
    for tk in tasks:
        if tk["kind"] != "work":
            continue
        deadlines.setdefault(tk["_orig_due"].date(), []).append(tk)

    # group days into weeks (Mon-start)
    weeks: dict[dt.date, list] = {}
    for d in days:
        mon = d.date - dt.timedelta(days=d.date.weekday())
        weeks.setdefault(mon, []).append(d)

    out = [f"<!doctype html><meta charset=utf-8><title>Fall 2026 plan</title><style>{CSS}</style>"]
    total = sum(sum(b["end"] - b["start"] for b in d.blocks) for d in days)
    of = sum(d.worked(True) for d in days)
    out.append('<header><h1>Fall 2026 — time-blocked plan</h1>'
               f'<div class="sub">{len(tasks)} tasks · {total/60:.0f} h scheduled '
               f'({of/60:.0f} h in evening overflow) · '
               f'planned from {now:%a %b %d %H:%M} to {days[-1].date:%b %d}'
               + (f' · hand-ins finish <b>{ef_days} days</b> before their real deadline (⚑)'
                  if ef_days else "") + '</div>'
               '<div class="legend">'
               + "".join(f'<span><i class="sw" style="background:{c}"></i>{k}</span>'
                         for k, c in colours.items())
               + '<span><i class="sw" style="background:#5f6368;background-image:'
                 'repeating-linear-gradient(45deg,transparent,transparent 3px,'
                 'rgba(255,255,255,.5) 3px,rgba(255,255,255,.5) 6px)"></i>'
                 'hatched = evening overflow</span>'
               '</div></header><div class="wrap">')

    if overdue:
        out.append('<div class="warn"><b>Past the deadline and no longer scheduled</b> '
                   '(mark Late OK in Notion to keep working on it):<br>' + "<br>".join(
            f'{t["course"]} {t["title"]} — was due {t["_orig_due"]:%a %b %d %H:%M}'
            for t in overdue) + "</div>")
    if unplaced:
        out.append('<div class="warn"><b>Could not place:</b><br>' + "<br>".join(
            f'{t["course"]} {t["title"]} — {pretty(t["remaining"])} short (due {t["due"]})'
            for t in unplaced) + "</div>")
    else:
        out.append('<div class="ok"><b>Everything fits.</b> Every task lands fully before its '
                   'deadline within the stated working hours.</div>')

    today = dt.date.today()
    for mon, ds in sorted(weeks.items()):
        load = sum(sum(b["end"] - b["start"] for b in d.blocks) for d in ds)
        cor = sum(d.worked(False) for d in ds)
        ovf = sum(d.worked(True) for d in ds)
        out.append('<div class="week"><div class="wk-hd"><span>Week of '
                   f'{mon:%B %d}</span><span class="load">{load/60:.1f} h '
                   f'({cor/60:.1f} core + {ovf/60:.1f} overflow)</span></div><div class="grid">')
        # time axis
        out.append('<div class="daycol axis"><div class="dayhd"></div>'
                   '<div class="dl" style="background:none"></div><div class="body">')
        for t in range(T0, T1, 60):
            out.append(f'<div class="hr">{fmt(t)}</div>')
        out.append('</div></div>')
        by_date = {d.date: d for d in ds}
        for i in range(7):
            date = mon + dt.timedelta(days=i)
            d = by_date.get(date)
            off = "" if d else " off"
            tod = " today" if date == today else ""
            out.append(f'<div class="daycol"><div class="dayhd{off}{tod}">'
                       f'<div class="dow">{DAYS[i]}</div><div class="num">{date.day}</div></div>')
            dls = deadlines.get(date, [])
            out.append('<div class="dl">')
            for tk in dls[:2]:
                col = colours.get(tk["course"], "#5f6368")
                out.append(f'<div class="pill" style="background:{col}" '
                           f'title="REAL DEADLINE {tk["_orig_due"]:%a %b %d %H:%M} · '
                           f'{tk["course"]} {tk["title"]}">⚑ {tk["course"]} {tk["title"]}</div>')
            if len(dls) > 2:
                out.append(f'<div class="more">+{len(dls) - 2} more due</div>')
            out.append('</div><div class="body">')
            for t in range(T0, T1, 60):
                out.append('<div class="hr"></div>')
            if d:
                for b in d.fixed:
                    out.append(block_html(b, True))
                for b in d.blocks:
                    out.append(block_html(b, False))
            out.append('</div></div>')
        out.append('</div></div>')

    # per-course summary
    per: dict[str, float] = {}
    for d in days:
        for b in d.blocks:
            per[b["course"]] = per.get(b["course"], 0) + (b["end"] - b["start"]) / 60
    out.append('<div class="week"><div class="wk-hd">Scheduled hours by course</div>'
               '<div style="padding:10px 14px"><table class="sum"><tr><th>Course</th>'
               '<th>Hours</th></tr>')
    for k, v in sorted(per.items(), key=lambda kv: -kv[1]):
        out.append(f'<tr><td><i class="sw" style="background:{colours.get(k,"#999")}"></i>'
                   f'{k}</td><td>{v:.1f}</td></tr>')
    out.append(f'<tr><th>Total</th><th>{sum(per.values()):.1f}</th></tr></table></div></div>')
    out.append("</div>")
    return "".join(out)


# ---------- entry ------------------------------------------------------------
def main() -> None:
    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    tasks = yaml.safe_load((HERE / "tasks.yaml").read_text())["tasks"]
    events = (yaml.safe_load((HERE / "events.yaml").read_text()) or {}).get("events") or []
    busy = json.loads((HERE / "busy.json").read_text())["busy"]
    state = json.loads((HERE / "state.json").read_text())
    now = dt.datetime.fromisoformat(state["now"])

    days, unplaced, tasks, overdue, finished, changes = schedule(cfg, tasks, events, busy, state)

    (HERE / "calendar.html").write_text(render(days, unplaced, cfg, tasks, overdue, now))
    row = lambda t, status: {
        "id": t["id"], "course": t["course"], "title": t["title"], "kind": t["kind"],
        "minutes": t["minutes"], "base_minutes": t.get("_base", t["minutes"]),
        "done": t["_done"], "owed": t["minutes"] - t["_done"],
        "status": status, "late": bool(t.get("_overdue")), "earliest": str(t["_earliest"]),
        "due": f"{t['_due']:%Y-%m-%d %H:%M}", "orig_due": f"{t['_orig_due']:%Y-%m-%d %H:%M}",
        "spread_group": t.get("spread_group"), "note": t.get("note", ""),
        "buffer_days": t.get("_buffer_days"),
        "finished_on": str(t["_finished_on"]) if "_finished_on" in t else None,
    }
    (HERE / "resolved_tasks.json").write_text(json.dumps(
        [row(t, "unplaced" if t.get("_status") == "unplaced" else "scheduled")
         for t in tasks] + [row(t, "overdue") for t in overdue]
        + [row(t, t["_status"]) for t in finished], indent=1))
    (HERE / "schedule.json").write_text(json.dumps([{
        "date": str(d.date),
        "fixed": [{**b, "start": fmt(b["start"]), "end": fmt(b["end"])} for b in d.fixed],
        "work": [{**b, "start": fmt(b["start"]), "end": fmt(b["end"])} for b in d.blocks],
    } for d in days], indent=1))

    if changes:
        print("adjusted:")
        for c in changes:
            print(f"  · {c}")
    tot = sum(sum(b["end"] - b["start"] for b in d.blocks) for d in days)
    ovf = sum(d.worked(True) for d in days)
    busiest = max(days, key=lambda d: d.worked())
    print(f"planning from {now:%a %b %d %H:%M}")
    print(f"scheduled {tot/60:.1f} h of {sum(t['minutes'] - t['_done'] for t in tasks)/60:.1f} h "
          f"across {sum(1 for d in days if d.blocks)} working days")
    print(f"  overflow (after 17:00): {ovf/60:.1f} h")
    print(f"  busiest day: {busiest.date:%a %b %d} at {busiest.worked()/60:.1f} h")
    for t in overdue:
        print(f"  OVERDUE, not scheduled: {t['course']} {t['title']} (was due {t['due']})")
    if unplaced:
        print(f"  UNPLACED: {len(unplaced)}")
        for t in unplaced:
            print(f"    {t['course']:9} {t['title'][:44]:46} {pretty(t['remaining'])} short "
                  f"(due {t['due']})")
    else:
        print("  everything fits")
    print("  -> calendar.html, schedule.json")


if __name__ == "__main__":
    main()
