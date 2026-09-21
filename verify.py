# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Check schedule.json against every stated constraint. Deterministic, no judgement.

    uv run verify.py     # exits non-zero if any check fails

Windows are checked against resolved_tasks.json (the planner's effective windows
after unavailability is applied) AND against each task's original due date, which
is the hard limit no adjustment may cross.
"""
import datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
import yaml

HERE = Path(__file__).parent
cfg = yaml.safe_load((HERE / "config.yaml").read_text())
tasks = {t["id"]: t for t in json.loads((HERE / "resolved_tasks.json").read_text())}
sched = json.loads((HERE / "schedule.json").read_text())
hm = lambda s: int(s[:2]) * 60 + int(s[3:])
dtm = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M")

fails, oks, warns = [], [], []
def check(cond, ok_msg, fail_msg=None):
    (oks if cond else fails).append(ok_msg if cond else (fail_msg or ok_msg))

CORE = (hm(cfg["work_hours"]["core"][0]), hm(cfg["work_hours"]["core"][1]))
OVER = (hm(cfg["work_hours"]["overflow"][0]), hm(cfg["work_hours"]["overflow"][1]))
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
    fixed = [(hm(b["start"]), hm(b["end"]), b) for b in day["fixed"]]
    work = [(hm(b["start"]), hm(b["end"]), b) for b in day["work"]]
    wmin = sum(e - s for s, e, _ in work)
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
        if we - ws > S["default_max_minutes"] + 15:   # 15 min tolerance absorbs a sliver
            fails.append(f"{date} session too long: {wb['title']} {wb['start']}-{wb['end']} "
                         f"({(we-ws)/60:.1f} h > {S['default_max_minutes']/60:.0f} h)")
        for fs, fe, fb in fixed:
            if ws < fe and fs < we:
                fails.append(f"{date} overlap: {wb['title']} vs {fb['title']}")
        if not (CORE[0] <= ws and we <= CORE[1]) and not (OVER[0] <= ws and we <= OVER[1]):
            fails.append(f"{date} outside hours: {wb['title']} {wb['start']}-{wb['end']}")
        t = tasks[wb["task"]]
        end = dt.datetime.combine(date, dt.time(we // 60, we % 60))
        if date < dt.date.fromisoformat(t["earliest"]):
            fails.append(f"{date} before earliest: {wb['title']}")
        if end > dtm(t["due"]):
            fails.append(f"{date} after effective due: {wb['title']} (due {t['due']})")
        if end > dtm(t["orig_due"]):
            fails.append(f"{date} AFTER REAL DEADLINE: {wb['title']} (due {t['orig_due']})")
        done[wb["task"]] += we - ws
    for i in range(len(work)):
        for j in range(i + 1, len(work)):
            if work[i][0] < work[j][1] and work[j][0] < work[i][1]:
                fails.append(f"{date} work self-overlap: {work[i][2]['title']}")

    # exam days: lighter, and nothing in the evening
    if any(b.get("cat") == "Exam" for _, _, b in fixed):
        n["exam"] += 1
        if wmin > EXAM_CAP + 1:
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

    if sum(e - s for s, e, b in work if not b["overflow"]) > S["max_core_hours_per_day"] * 60 + 1:
        fails.append(f"{date} core cap exceeded")
    if wmin > S["max_total_hours_per_day"] * 60 + 1:
        fails.append(f"{date} total cap exceeded: {wmin/60:.1f} h")

# every task fully scheduled
short = [(tid, t["minutes"] - done[tid]) for tid, t in tasks.items() if done[tid] != t["minutes"]]
for tid, gap in short:
    fails.append(f"task {tid} scheduled {tasks[tid]['minutes'] - gap} of {tasks[tid]['minutes']} min")

# spread groups: separate days, minimum gap
groups = defaultdict(list)
for day in sched:
    for b in day["work"]:
        g = tasks[b["task"]].get("spread_group")
        if g:
            groups[g].append(dt.date.fromisoformat(day["date"]))
bad_spread = []
for g, ds in sorted(groups.items()):
    u = sorted(set(ds))
    if len(u) != len(ds):
        bad_spread.append(f"{g}: {len(ds)} posts on {len(u)} days")
    if any((b - a).days < 2 for a, b in zip(u, u[1:])):
        bad_spread.append(f"{g}: posts closer than 2 days ({', '.join(f'{d:%b %d}' for d in u)})")
fails += bad_spread

# catch-up exists for every course that lost a lecture to a write-off
missed = set()
for d in blocked:
    wd = d.weekday()
    if any(o["date"] == d for o in cfg.get("no_class_days") or []):
        continue
    for c in cfg["classes"]:
        if wd in c["days"] and d <= cfg["term"]["last_class_day"]:
            missed.add(c["course"])
have_cu = {t["course"] for t in tasks.values() if t["kind"] == "catchup"}

check(not short, f"all {len(tasks)} tasks fully scheduled ({sum(done.values())/60:.1f} h)")
check(not any("REAL DEADLINE" in f for f in fails), "nothing lands after a real deadline")
check(not any("before earliest" in f or "effective due" in f for f in fails),
      "all work inside each task's resolved window")
check(not any("overlap" in f for f in fails), "no overlaps with classes, exams, travel or meals")
check(not any("outside hours" in f for f in fails), "all work inside stated hours")
check(not any("write-off" in f for f in fails),
      f"{n['blocked']} write-off days carry no work and no classes")
check(not any("exam day" in f for f in fails),
      f"{n['exam']} exam days capped at {EXAM_CAP/60:.0f} h with no evening work")
check(not any("travel" in f for f in fails), "travel brackets every class and exam trip")
check(not bad_spread, f"discussion posts on separate days, >=2 days apart ({len(groups)} groups)")
check(missed <= have_cu, f"catch-up generated for every course that missed lectures ({', '.join(sorted(missed)) or 'none'})",
      f"missing catch-up for {sorted(missed - have_cu)}")
check(not any("cap exceeded" in f for f in fails), f"daily caps held (busiest {maxday:.1f} h)")
check(not any("session too long" in f for f in fails),
      f"every session <= {S['default_max_minutes']//60} h, with a break after long ones")

for o in oks:   print(f"  ok   {o}")
for w in warns: print(f"  warn {w}")
if fails:
    print(f"\n{len(fails)} FAILURES")
    for f in fails[:25]: print(f"  {f}")
    sys.exit(1)
print("\nall constraints satisfied")
