# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Check schedule.json against every stated constraint. Deterministic, no judgement.

    uv run verify.py     # exits non-zero if any check fails

Windows are checked against resolved_tasks.json (the planner's effective windows
after unavailability is applied) AND against each task's original due date, which
is the hard limit no adjustment may cross. Work Zach marked Late OK is the one
exception. Blocks already started, and blocks Zach moved himself ("pinned"; moved ones
are also "locked"), are his: they count toward totals and caps, but are not re-judged.
A block he resized ("sized") keeps whatever length he gave it.
"""
import datetime as dt, itertools, json, sys
from collections import defaultdict
from pathlib import Path
import yaml

HERE = Path(__file__).parent
cfg = yaml.safe_load((HERE / "config.yaml").read_text())
resolved = json.loads((HERE / "resolved_tasks.json").read_text())
# a task that only partly fits ("unplaced") still has blocks, and they obey every rule
tasks = {t["id"]: t for t in resolved if t["status"] in ("scheduled", "unplaced")}
sched = json.loads((HERE / "schedule.json").read_text())
busy = json.loads((HERE / "busy.json").read_text())["busy"]
STATE = json.loads((HERE / "state.json").read_text())
NOW = dt.datetime.fromisoformat(STATE["now"])
hm = lambda s: int(s[:2]) * 60 + int(s[3:])
dtm = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M")

def class_weekday(date):
    """Which weekday's classes run on this date (None = no classes), as plan.py decides."""
    for o in cfg.get("schedule_overrides") or []:
        if o["date"] == date:
            return o["runs_schedule_of"]
    if any(n["date"] == date for n in cfg.get("no_class_days") or []):
        return None
    if date > cfg["term"]["last_class_day"]:
        return None
    return date.weekday()


fails, oks, warns = [], [], []
def check(cond, ok_msg, fail_msg=None):
    (oks if cond else fails).append(ok_msg if cond else (fail_msg or ok_msg))



# mirrors plan.py
def day_hours(cfg: dict, date: dt.date) -> dict:
    """Working hours and daily caps for one date: the usual ones, or those of a stretch
    under work_hours.stretches that covers it (a crunch week, say). A stretch replaces
    whichever of core, overflow, max_core_hours and max_total_hours it gives."""
    w, S = cfg["work_hours"], cfg["sessions"]
    h = {"core": w["core"], "overflow": w["overflow"],
         "max_core_hours": S["max_core_hours_per_day"],
         "max_total_hours": S["max_total_hours_per_day"]}
    for st in w.get("stretches") or []:
        if st["from"] <= date <= st["to"]:
            h.update({k: st[k] for k in h if k in st})
    return h

TRAV = cfg["travel"]["minutes_each_way"]
S = cfg["sessions"]
EXAM_CAP = S.get("exam_day_max_hours", S["max_total_hours_per_day"]) * 60

blocked = set()
for u in cfg.get("unavailable") or []:
    d = u["from"]
    while d <= u["to"]:
        blocked.add(d); d += dt.timedelta(days=1)

done = defaultdict(int)
n = defaultdict(int)          # counters for the summary
maxday = 0.0

for day in sched:
    date = dt.date.fromisoformat(day["date"])
    H = day_hours(cfg, date)
    CORE, OVER = ((hm(H[k][0]), hm(H[k][1])) for k in ("core", "overflow"))
    fixed = [(hm(b["start"]), hm(b["end"]), b) for b in day["fixed"]]
    allwork = [(hm(b["start"]), hm(b["end"]), b) for b in day["work"]]
    for ws, we, wb in allwork:
        if wb.get("pinned"):
            if wb["task"] in tasks:
                done[wb["task"]] += we - ws
        elif dt.datetime.combine(date, dt.time(ws // 60, ws % 60)) < NOW:
            fails.append(f"{date} {wb['start']} {wb['title']} is before the plan start {NOW}")
    # judged against today's rules: what the planner added. Work under way and blocks Zach
    # placed (both pinned) count toward a day's totals, but a day they alone fill past a cap
    # is his call; the planner fails a cap only by adding to it
    work = [x for x in allwork if not x[2].get("pinned")]
    wmin = sum(e - s for s, e, _ in allwork)
    maxday = max(maxday, wmin / 60)

    # blocked days carry nothing at all
    if date in blocked:
        if work:
            fails.append(f"{date} is a write-off day but has {wmin} min of work")
        if any(b.get("cat") in ("Class", "Exam") for _, _, b in fixed):
            fails.append(f"{date} is a write-off day but has a class")
        n["blocked"] += 1
        continue

    for ws, we, wb in work:
        if we - ws > S["default_max_minutes"] + 15 and not wb.get("sized"):   # 15 min: a sliver
            fails.append(f"{date} session too long: {wb['title']} {wb['start']}-{wb['end']} "
                         f"({(we-ws)/60:.1f} h > {S['default_max_minutes']/60:.0f} h)")
        for fs, fe, fb in fixed:
            if ws < fe and fs < we:
                fails.append(f"{date} overlap: {wb['title']} vs {fb['title']}")
        if not (CORE[0] <= ws and we <= CORE[1]) and not (OVER[0] <= ws and we <= OVER[1]):
            fails.append(f"{date} outside hours: {wb['title']} {wb['start']}-{wb['end']}")
        if not cfg["work_hours"].get("include_weekends", True) and date.weekday() >= 5:
            fails.append(f"{date} outside hours: weekend work {wb['title']}")
        t = tasks[wb["task"]]
        end = dt.datetime.combine(date, dt.time(we // 60, we % 60))
        if date < dt.date.fromisoformat(t["earliest"]):
            fails.append(f"{date} before earliest: {wb['title']}")
        if end > dtm(t["due"]):
            fails.append(f"{date} after effective due: {wb['title']} (due {t['due']})")
        if end > dtm(t["orig_due"]) and not t["late"]:
            fails.append(f"{date} AFTER REAL DEADLINE: {wb['title']} (due {t['orig_due']})")
        done[wb["task"]] += we - ws
    # busy time on Zach's calendars: no work on it. The one exemption is free/busy
    # returning exactly a skipped lab's slot, which the planner deliberately leaves free.
    wd = class_weekday(date)
    labs = {(hm(c["start"]), hm(c["end"])) for c in cfg["classes"]
            if c.get("skip") and wd is not None and wd in c["days"]}
    for b in busy:
        if b["date"] != day["date"]:
            continue
        bs, be = hm(b["start"]), hm(b["end"])
        if (bs, be) in labs:
            continue
        for ws, we, wb in work:
            if ws < be and bs < we:
                fails.append(f"{date} work on busy calendar time: {wb['title']} "
                             f"{wb['start']}-{wb['end']} vs busy {b['start']}-{b['end']}")
        n["busy"] += 1
    # breaks: once continuous work reaches break_after_minutes, the next block must wait
    # at least break_minutes (a shorter gap does not count as a break)
    BRK, BRK_AFTER = S.get("break_minutes", 15), S.get("break_after_minutes", 90)
    run, prev_end = 0, None
    for ws, we, wb in sorted(allwork, key=lambda x: x[0]):
        if prev_end is not None and ws - prev_end < BRK:
            if run >= BRK_AFTER and not wb.get("pinned"):   # his own blocks are his call
                fails.append(f"{date} no break: {wb['title']} starts {wb['start']} after "
                             f"{run} min of continuous work")
            run += we - ws
        else:
            run = we - ws
        prev_end = we
    for i in range(len(work)):
        for j in range(i + 1, len(work)):
            if work[i][0] < work[j][1] and work[j][0] < work[i][1]:
                fails.append(f"{date} work self-overlap: {work[i][2]['title']}")

    # exam days: lighter, and nothing in the evening
    if any(b.get("cat") == "Exam" for _, _, b in fixed):
        n["exam"] += 1
        if work and wmin > EXAM_CAP + 1:
            fails.append(f"{date} exam day carries {wmin/60:.1f} h (cap {EXAM_CAP/60:.1f})")
        if any(b["overflow"] for _, _, b in work):
            fails.append(f"{date} exam day runs into the evening")

    # meals: absent is only a failure if there was room for one
    have = {b["title"] for _, _, b in fixed if b.get("cat") == "Meal"}
    for m in cfg["meals"]:
        if m["name"] in have:
            continue
        lo, hi = hm(m["window"][0]), hm(m["window"][1])
        free = [(lo, hi)]
        for fs, fe, _ in fixed:
            nf = []
            for s, e in free:
                if fe <= s or fs >= e: nf.append((s, e)); continue
                if s < fs: nf.append((s, fs))
                if fe < e: nf.append((fe, e))
            free = [(s, e) for s, e in nf if e > s]
        if any(e - s >= m["minutes"] for s, e in free):
            fails.append(f"{date} {m['name']} missing though there was room")
        else:
            warns.append(f"{date} {m['name']} displaced - no free {m['minutes']} min in its window")

    # travel brackets every on-campus trip (classes and standalone exams)
    oncampus = sorted((hm(b["start"]), hm(b["end"])) for b in day["fixed"]
                      if b.get("cat") in ("Class", "Exam"))
    trav = [(hm(b["start"]), hm(b["end"])) for b in day["fixed"] if b.get("cat") == "Travel"]
    trips = []
    for s, e in oncampus:
        if trips and s - trips[-1][1] <= 60:
            trips[-1][1] = max(trips[-1][1], e)
        else:
            trips.append([s, e])
    for cs, ce in trips:
        if not any(te == cs and te - ts == TRAV for ts, te in trav):
            fails.append(f"{date} no {TRAV} min travel before {cs//60:02d}:{cs%60:02d}")
        if not any(ts == ce and te - ts == TRAV for ts, te in trav):
            fails.append(f"{date} no {TRAV} min travel after {ce//60:02d}:{ce%60:02d}")

    if any(not b["overflow"] for _, _, b in work) and \
            sum(e - s for s, e, b in allwork if not b["overflow"]) > H["max_core_hours"] * 60 + 1:
        fails.append(f"{date} core cap exceeded")
    if work and wmin > H["max_total_hours"] * 60 + 1:
        fails.append(f"{date} total cap exceeded: {wmin/60:.1f} h")

# every task's remaining minutes fully scheduled
owed = {tid: t["minutes"] - t["done"] for tid, t in tasks.items()}
short = [tid for tid in tasks if done[tid] != owed[tid] and tasks[tid]["status"] == "scheduled"]
for tid in short:
    fails.append(f"task {tid} scheduled {done[tid]} of {owed[tid]} min still owed")
for t in resolved:
    if t["status"] == "unplaced":
        warns.append(f"does not fit: {t['course']} {t['title']} ({t['owed'] - t['done']} min "
                     f"short, due {t['orig_due']})")
    if t["status"] == "overdue":
        warns.append(f"overdue, no longer scheduled: {t['course']} {t['title']} "
                     f"(was due {t['orig_due']})")
    elif t.get("late"):
        warns.append(f"late OK: {t['course']} {t['title']} was due {t['orig_due']}, "
                     f"now finishing by {t['due']}")

# spread groups: every post on days of its own, min_gap_days from each other post of its
# group. A post's days are those its blocks sit on, under way and moved by Zach included,
# the day it was last worked on, and for a finished post the day it finished. The planner
# is judged on the days it chose; two days that are both history or Zach's own placing
# are his call.
posts = {t["id"]: t for t in resolved if t["status"] in ("scheduled", "unplaced", "finished")
         and t.get("spread_group")}
groups = defaultdict(lambda: defaultdict(set))   # group -> post -> {(day, planner chose it)}
for day in sched:
    for b in day["work"]:
        if b["task"] in posts:
            groups[posts[b["task"]]["spread_group"]][b["task"]].add(
                (dt.date.fromisoformat(day["date"]), not b.get("pinned")))
last_done = STATE.get("last_done") or {}
for t in posts.values():
    if t["id"] in last_done:
        groups[t["spread_group"]][t["id"]].add((dt.date.fromisoformat(last_done[t["id"]]), False))
    if t["status"] == "finished":
        groups[t["spread_group"]][t["id"]].add((dt.date.fromisoformat(t["finished_on"]), False))
bad_spread = []
for g, members in sorted(groups.items()):
    gap = max(posts[p].get("min_gap_days") or 1 for p in members)
    for p, q in itertools.combinations(sorted(members), 2):
        for (a, pa), (b, pb) in itertools.product(sorted(members[p]), sorted(members[q])):
            if abs((a - b).days) < gap and (pa or pb):
                bad_spread.append(f"{g}: {p} {a:%b %d} and {q} {b:%b %d} closer than {gap} days")
fails += bad_spread

# catch-up exists for every course that lost a lecture to a write-off, when catch-up is on
missed = set()
for d in blocked if cfg.get("catchup") else []:
    wd = d.weekday()
    if any(o["date"] == d for o in cfg.get("no_class_days") or []):
        continue
    for c in cfg["classes"]:
        if not c.get("skip") and wd in c["days"] and d <= cfg["term"]["last_class_day"]:
            missed.add(c["course"])
have_cu = {t["course"] for t in resolved if t["kind"] == "catchup"}

check(not short, f"all {len(tasks)} tasks fully scheduled ({sum(done.values())/60:.1f} h)")
check(not any("before the plan start" in f for f in fails),
      f"nothing new scheduled before {NOW:%a %b %d %H:%M}")
check(not any("REAL DEADLINE" in f for f in fails), "nothing lands after a real deadline")
check(not any("before earliest" in f or "effective due" in f for f in fails),
      "all work inside each task's resolved window")
check(not any("overlap" in f for f in fails), "no overlaps with classes, exams, travel or meals")
check(not any("busy calendar time" in f for f in fails),
      f"no work on busy time from Google Calendar ({n['busy']} busy blocks checked)")
check(not any("outside hours" in f for f in fails), "all work inside stated hours")
check(not any("write-off" in f for f in fails),
      f"{n['blocked']} write-off days carry no work and no classes")
check(not any("exam day" in f for f in fails),
      f"{n['exam']} exam days capped at {EXAM_CAP/60:.0f} h with no evening work")
check(not any("travel" in f for f in fails), "travel brackets every class and exam trip")
check(not bad_spread, f"discussion posts on separate days, >= min_gap_days apart, moved ones "
                      f"included ({len(groups)} groups)")
check(missed <= have_cu, f"catch-up generated for every course that missed lectures ({', '.join(sorted(missed)) or 'none'})",
      f"missing catch-up for {sorted(missed - have_cu)}")
check(not any("cap exceeded" in f for f in fails), f"daily caps held (busiest {maxday:.1f} h)")
# early finish: every hand-in's last session lands >= N days before its real deadline
ef = cfg.get("early_finish") or {}
EF, EF_FROM = int(ef.get("days", 0)), ef.get("from")
if EF:
    last = {}
    for day in sched:
        for b in day["work"]:
            if not b.get("locked"):           # where he put a block himself is his call
                last[b["task"]] = max(last.get(b["task"], day["date"]), day["date"])
    short_buf = []
    for tid, tk in tasks.items():
        if tk["kind"] not in set(ef.get("kinds", ["work"])) or tid not in last or tk["late"]:
            continue
        real = dtm(tk["orig_due"]).date()
        if EF_FROM and real < EF_FROM:
            continue
        got = (real - dt.date.fromisoformat(last[tid])).days
        cap = tk.get("buffer_days")
        want = EF if cap is None else min(EF, cap)   # release may cap it, even to 0
        if got < want:
            short_buf.append(f"{tid}: finishes {got} d early, expected {want}")
    fails += short_buf
    n_items = sum(1 for tid, tk in tasks.items() if tk["kind"] in set(ef.get("kinds", ["work"])))
    check(not short_buf, f"all {n_items} hand-ins finish >= {EF} days before their real deadline",
          f"{len(short_buf)} hand-ins short of the {EF}-day buffer")

check(not any("session too long" in f for f in fails),
      f"every session <= {S['default_max_minutes']//60} h")
check(not any("no break" in f for f in fails),
      f"a {S.get('break_minutes', 15)}-min break after every {S.get('break_after_minutes', 90)} min of continuous work")

for o in oks:   print(f"  ok   {o}")
for w in warns: print(f"  warn {w}")
if fails:
    print(f"\n{len(fails)} FAILURES")
    for f in fails[:25]: print(f"  {f}")
    sys.exit(1)
print("\nall constraints satisfied")
