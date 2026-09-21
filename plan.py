# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Time-block every Fall 2026 deliverable across the term.

Reads  config.yaml (constraints) · tasks.yaml (work) · events.yaml (ad-hoc commitments)
Writes schedule.json (machine-readable) · calendar.html (week grids)

    uv run plan.py                 # replan and render
    uv run plan.py --from 2026-10-08   # freeze the past, replan from a date

Rescheduling is not incremental: the planner is deterministic, so adding an event
to events.yaml and re-running reflows everything around it. Use --from to keep
already-completed days fixed.
"""
from __future__ import annotations

import argparse
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


# ---------- the day model ----------------------------------------------------
class Day:
    def __init__(self, date: dt.date, cfg: dict, events: list[dict]):
        self.date = date
        self.cfg = cfg
        self.fixed: list[dict] = []          # classes, travel, meals, ad-hoc
        self.blocks: list[dict] = []         # scheduled work
        self._build(events)

    # -- fixed commitments
    def _class_weekday(self) -> int | None:
        for o in self.cfg.get("schedule_overrides") or []:
            if o["date"] == self.date:
                return o["runs_schedule_of"]
        for n in self.cfg.get("no_class_days") or []:
            if n["date"] == self.date:
                return None
        if self.date > self.cfg["term"]["last_class_day"]:
            return None
        return self.date.weekday()

    def _build(self, events: list[dict]) -> None:
        trav = self.cfg["travel"]["minutes_each_way"]

        # classes for this day, merged into on-campus trips
        wd = self._class_weekday()
        classes = []
        if wd is not None:
            for c in self.cfg["classes"]:
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
                self.fixed.append({"start": cs_[0], "end": cs_[1],
                                   "title": f"{c['course']} {c['kind']} - {c['room']}",
                                   "cat": "Class", "course": c["course"]})
            self.fixed.append({"start": e, "end": e + trav, "title": "Travel home",
                               "cat": "Travel"})

        # ad-hoc events
        for ev in events:
            if ev["date"] != self.date:
                continue
            s, e = hm(ev["start"]), hm(ev["end"])
            if ev.get("travel"):
                self.fixed.append({"start": s - trav, "end": s, "title": "Travel", "cat": "Travel"})
                self.fixed.append({"start": e, "end": e + trav, "title": "Travel", "cat": "Travel"})
            self.fixed.append({"start": s, "end": e, "title": ev["title"], "cat": "Personal"})

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
        w = self.cfg["work_hours"]
        if not overflow and not w.get("include_weekends", True) and self.date.weekday() >= 5:
            return []
        rng = w["overflow"] if overflow else w["core"]
        free = [(hm(rng[0]), hm(rng[1]))]
        for b in self.fixed + self.blocks:
            free = subtract(free, (b["start"], b["end"]))
        return [(s, e) for s, e in free if e - s >= 20]

    def worked(self, overflow: bool | None = None) -> int:
        if overflow is None:
            return sum(b["end"] - b["start"] for b in self.blocks)
        return sum(b["end"] - b["start"] for b in self.blocks if b["overflow"] is overflow)


# ---------- scheduler --------------------------------------------------------
def schedule(cfg: dict, tasks: list[dict], events: list[dict], start_from: dt.date):
    term = cfg["term"]
    d0, d1 = max(term["start"], start_from), term["plan_until"]
    days = [Day(d0 + dt.timedelta(days=i), cfg, events) for i in range((d1 - d0).days + 1)]

    S = cfg["sessions"]
    core_cap = int(S["max_core_hours_per_day"] * 60)
    total_cap = int(S["max_total_hours_per_day"] * 60)

    for t in tasks:
        t["remaining"] = t["minutes"]
        t["_due"] = dt.datetime.strptime(t["due"], "%Y-%m-%d %H:%M")
        t["_earliest"] = dt.date.fromisoformat(str(t["earliest"]))

    def latest_start(t, today):
        """Date by which this task must begin to still finish, at ~5h/day."""
        days_needed = max(1, math.ceil(t["remaining"] / 300))
        return t["_due"].date() - dt.timedelta(days=days_needed)

    unplaced: list[dict] = []
    group_days: dict[str, list[dt.date]] = {}

    for day in days:
        for overflow in (False, True):
            while True:
                slots = day.slots(overflow)
                if not slots:
                    break
                cap = total_cap if overflow else core_cap
                used = day.worked() if overflow else day.worked(False)
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
                # only dip into overflow for work that is actually pressed
                if overflow:
                    live = [t for t in live if latest_start(t, day.date) <= day.date]
                    if not live:
                        break
                live.sort(key=lambda t: (latest_start(t, day.date), t["_due"], -t["remaining"]))

                placed = False
                for t in live:
                    for si, (s, e) in enumerate(sorted(slots)):
                        room = int(min(e - s, cap - used))
                        chunk = int(min(t["remaining"], t["max_minutes"], room))
                        if chunk < min(t["min_minutes"], t["remaining"]):
                            continue
                        # never leave a sliver of a task behind
                        if 0 < t["remaining"] - chunk < 20:
                            chunk = t["remaining"]
                            if chunk > room:
                                continue
                        day.blocks.append({
                            "start": s, "end": s + chunk, "title": t["title"],
                            "cat": t["course"], "course": t["course"], "task": t["id"],
                            "kind": t["kind"], "overflow": overflow,
                            "due": t["due"], "note": t.get("note", ""),
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
            if merged and merged[-1]["task"] == b["task"] and merged[-1]["end"] == b["start"]:
                merged[-1]["end"] = b["end"]
                merged[-1]["overflow"] = merged[-1]["overflow"] or b["overflow"]
            else:
                merged.append(b)
        day.blocks = merged

    for t in tasks:
        if t["remaining"] > 0:
            unplaced.append(t)
    return days, unplaced


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

def render(days, unplaced, cfg, tasks) -> str:
    colours = cfg["colours"]
    T0, T1 = hm("07:00"), hm("22:00")
    PX = 42 / 60  # one hour = 42px

    def block_html(b, fixed):
        top = (b["start"] - T0) * PX
        h = max((b["end"] - b["start"]) * PX, 12)
        col = colours.get(b.get("cat"), "#5f6368")
        cls = "ev fixed" if fixed else "ev"
        if b.get("overflow"):
            cls += " of"
        if b.get("cat") in ("Meal", "Travel"):
            cls += " meal"
        label = f'{b["course"]} · {b["title"]}' if not fixed and b.get("course") else b["title"]
        sub = f"{fmt(b['start'])}–{fmt(b['end'])}"
        return (f'<div class="{cls}" style="top:{top:.1f}px;height:{h:.1f}px;background:{col}" '
                f'title="{label} · {sub}"><b>{label}</b><span class="t">{sub}</span></div>')

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
               f'{days[0].date:%b %d} – {days[-1].date:%b %d}</div>'
               '<div class="legend">'
               + "".join(f'<span><i class="sw" style="background:{c}"></i>{k}</span>'
                         for k, c in colours.items())
               + '<span><i class="sw" style="background:#5f6368;background-image:'
                 'repeating-linear-gradient(45deg,transparent,transparent 3px,'
                 'rgba(255,255,255,.5) 3px,rgba(255,255,255,.5) 6px)"></i>'
                 'hatched = evening overflow</span>'
               '</div></header><div class="wrap">')

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
        out.append('<div class="daycol axis"><div class="dayhd"></div><div class="body">')
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
                       f'<div class="dow">{DAYS[i]}</div><div class="num">{date.day}</div></div>'
                       '<div class="body">')
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=None,
                    help="replan from this date, leaving earlier days alone")
    args = ap.parse_args()

    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    tasks = yaml.safe_load((HERE / "tasks.yaml").read_text())["tasks"]
    events = (yaml.safe_load((HERE / "events.yaml").read_text()) or {}).get("events") or []

    start_from = dt.date.fromisoformat(args.start) if args.start else cfg["term"]["start"]
    days, unplaced = schedule(cfg, tasks, events, start_from)

    (HERE / "calendar.html").write_text(render(days, unplaced, cfg, tasks))
    (HERE / "schedule.json").write_text(json.dumps([{
        "date": str(d.date),
        "fixed": [{**b, "start": fmt(b["start"]), "end": fmt(b["end"])} for b in d.fixed],
        "work": [{**b, "start": fmt(b["start"]), "end": fmt(b["end"])} for b in d.blocks],
    } for d in days], indent=1))

    tot = sum(sum(b["end"] - b["start"] for b in d.blocks) for d in days)
    ovf = sum(d.worked(True) for d in days)
    busiest = max(days, key=lambda d: d.worked())
    print(f"scheduled {tot/60:.1f} h of {sum(t['minutes'] for t in tasks)/60:.1f} h "
          f"across {sum(1 for d in days if d.blocks)} working days")
    print(f"  overflow (after 17:00): {ovf/60:.1f} h")
    print(f"  busiest day: {busiest.date:%a %b %d} at {busiest.worked()/60:.1f} h")
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
