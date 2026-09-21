# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Check schedule.json against every stated constraint. Deterministic, no judgement.

    uv run verify.py     # exits non-zero if any check fails
"""
import datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
import yaml

HERE = Path(__file__).parent
cfg = yaml.safe_load((HERE / "config.yaml").read_text())
tasks = {t["id"]: t for t in yaml.safe_load((HERE / "tasks.yaml").read_text())["tasks"]}
sched = json.loads((HERE / "schedule.json").read_text())
hm = lambda s: int(s[:2]) * 60 + int(s[3:])

fails, notes, displaced = [], [], []
def check(cond, msg):
    (notes if cond else fails).append(msg)

CORE = (hm(cfg["work_hours"]["core"][0]), hm(cfg["work_hours"]["core"][1]))
OVER = (hm(cfg["work_hours"]["overflow"][0]), hm(cfg["work_hours"]["overflow"][1]))
TRAV = cfg["travel"]["minutes_each_way"]
S = cfg["sessions"]

done = defaultdict(int)
overlaps = outside = early = late = 0
missing_meal = []
bad_travel = []
maxday = 0.0

for day in sched:
    date = dt.date.fromisoformat(day["date"])
    fixed = [(hm(b["start"]), hm(b["end"]), b) for b in day["fixed"]]
    work = [(hm(b["start"]), hm(b["end"]), b) for b in day["work"]]

    # 1. work never collides with a fixed commitment, or with itself
    for ws, we, wb in work:
        for fs, fe, fb in fixed:
            if ws < fe and fs < we:
                overlaps += 1
                fails.append(f"{date} overlap: {wb['title']} vs {fb['title']}")
        if not (CORE[0] <= ws and we <= CORE[1]) and not (OVER[0] <= ws and we <= OVER[1]):
            outside += 1
            fails.append(f"{date} outside hours: {wb['title']} {wb['start']}-{wb['end']}")
    for i in range(len(work)):
        for j in range(i + 1, len(work)):
            if work[i][0] < work[j][1] and work[j][0] < work[i][1]:
                overlaps += 1
                fails.append(f"{date} work self-overlap: {work[i][2]['title']}")

    # 2. windows honoured
    for ws, we, wb in work:
        t = tasks[wb["task"]]
        if date < dt.date.fromisoformat(str(t["earliest"])):
            early += 1; fails.append(f"{date} before earliest: {wb['title']}")
        due = dt.datetime.strptime(t["due"], "%Y-%m-%d %H:%M")
        if dt.datetime.combine(date, dt.time(we // 60, we % 60)) > due:
            late += 1; fails.append(f"{date} after due: {wb['title']} (due {t['due']})")
        done[wb["task"]] += we - ws

    # 3. meals - absent is only a failure if there was actually room for one
    meals = {b["title"] for b in day["fixed"] if b.get("cat") == "Meal"}
    for m in cfg["meals"]:
        if m["name"] in meals:
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
            missing_meal.append(f"{date} {m['name']} - room existed but none placed")
        else:
            blocker = next((b["title"] for _, _, b in fixed
                            if hm(b["start"]) < hi and lo < hm(b["end"])
                            and b.get("cat") == "Personal"), "a fixed commitment")
            displaced.append(f"{date} {m['name']} displaced by {blocker}")

    # 4. travel brackets every class cluster
    cls = sorted([(hm(b["start"]), hm(b["end"])) for b in day["fixed"] if b.get("cat") == "Class"])
    trav = sorted([(hm(b["start"]), hm(b["end"])) for b in day["fixed"] if b.get("cat") == "Travel"])
    clusters = []
    for s, e in cls:
        if clusters and s - clusters[-1][1] <= 60:
            clusters[-1][1] = max(clusters[-1][1], e)
        else:
            clusters.append([s, e])
    for cs, ce in clusters:
        if not any(abs(te - cs) < 2 and (te - ts) == TRAV for ts, te in trav):
            bad_travel.append(f"{date} no {TRAV}m travel before class at {cs//60:02d}:{cs%60:02d}")
        if not any(abs(ts - ce) < 2 and (te - ts) == TRAV for ts, te in trav):
            bad_travel.append(f"{date} no {TRAV}m travel after class ending {ce//60:02d}:{ce%60:02d}")

    # 5. daily caps
    core_m = sum(we - ws for ws, we, b in work if not b["overflow"])
    tot_m = sum(we - ws for ws, we, b in work)
    maxday = max(maxday, tot_m / 60)
    if core_m > S["max_core_hours_per_day"] * 60 + 1:
        fails.append(f"{date} core cap exceeded: {core_m/60:.1f} h")
    if tot_m > S["max_total_hours_per_day"] * 60 + 1:
        fails.append(f"{date} total cap exceeded: {tot_m/60:.1f} h")

# 6. every task fully scheduled
for tid, t in tasks.items():
    if done[tid] != t["minutes"]:
        fails.append(f"task {tid} scheduled {done[tid]}m of {t['minutes']}m")

# 7. Friday labs really are absent
fri_lab = [d["date"] for d in sched for b in d["fixed"]
           if b.get("cat") == "Class" and "LAB" in b["title"]]
check(not fri_lab, f"no lab blocks scheduled ({len(fri_lab)} found)" if fri_lab else
      "no lab blocks scheduled (Friday labs correctly skipped)")

# 8. discussion posts land on distinct days, spread out
disc = defaultdict(list)
for day in sched:
    for b in day["work"]:
        if b["kind"] == "discussion":
            disc[b["task"].rsplit("-p", 1)[0]].append(dt.date.fromisoformat(day["date"]))
for k, ds in sorted(disc.items()):
    u = sorted(set(ds))
    check(len(u) == 3, f"{k}: {len(u)} distinct days")
    if len(u) >= 2:
        check((u[-1] - u[0]).days >= 4, f"{k}: spread {(u[-1]-u[0]).days} days "
              f"({u[0]:%b %d} to {u[-1]:%b %d})")

check(not missing_meal, (f"meals present every day"
      + (f" ({len(displaced)} displaced by all-day commitments)" if displaced else ""))
      if not missing_meal else f"{len(missing_meal)} missing meals")
check(not bad_travel, "travel brackets every class cluster" if not bad_travel
      else f"{len(bad_travel)} travel problems")
check(overlaps == 0, "no overlaps")
check(outside == 0, "all work inside stated hours")
check(early == 0 and late == 0, "all work inside each task's earliest..due window")
notes.append(f"busiest day {maxday:.1f} h (cap {S['max_total_hours_per_day']} h)")
notes.append(f"{sum(done.values())/60:.1f} h scheduled across {len(tasks)} tasks")

for n in notes:
    print(f"  ok   {n}")
for d in displaced: print(f"  warn {d}")
if missing_meal: print("\n".join(f"  FAIL {m}" for m in missing_meal[:5]))
if bad_travel:   print("\n".join(f"  FAIL {m}" for m in bad_travel[:5]))
if fails:
    print(f"\n{len(fails)} FAILURES")
    for f in fails[:25]:
        print(f"  {f}")
    sys.exit(1)
print("\nall constraints satisfied")
