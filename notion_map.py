# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Map the Notion Could Do F26 rows onto tasks.yaml and compare their deadlines.

    uv run notion_map.py notion_rows.tsv

notion_rows.tsv is a snapshot of the 71 F26 rows (name, due start, due end as Notion
stores them) pulled through the Notion MCP on 2026-09-21. Fails loudly on any row it
cannot place or any planner task claimed twice.
"""
import datetime as dt, re, sys, yaml
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/Toronto")
PLANNER = __import__("pathlib").Path(__file__).parent / "tasks.yaml"


def load_tasks():
    return {t["id"]: t for t in yaml.safe_load(open(PLANNER))["tasks"]}
CODE = {"CIS 3210": "cis3210", "CIS 4020": "cis4020", "MATH 3240": "math3240",
        "MATH 4310": "math4310", "ENVS 2210": "envs"}

def et(s):
    if not s: return None
    if len(s) == 10: return dt.datetime.fromisoformat(s)          # all-day, already ET
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET).replace(tzinfo=None)

def targets(name):
    m = re.match(r"ENVS Wk (\d+)", name)
    if m: return [f"envs-read-w{int(m[1]):02d}"]
    course, _, rest = name.partition(" — ")
    c = CODE[course]
    if rest.startswith("Final exam"): return [f"{c}-fin"]
    if rest.startswith("Quiz"): return []
    if c == "envs":
        if m := re.match(r"Discussion (\d)", rest): return [f"envs-d{m[1]}-p{i}" for i in (1, 2, 3)]
        if m := re.match(r"Midterm (\d)", rest): return [f"envs-mt{m[1]}prep", f"envs-mt{m[1]}sit"]
        if rest.startswith("Written"): return ["envs-written"]
        if rest.startswith("Respondus"): return ["envs-practice"]
    if c == "cis4020":
        for key, tid in [("Assignment 1", "a1"), ("Assignment 2", "a2"), ("Project Proposal", "prop"),
                         ("Tutorial Notebook", "nb"), ("Project Presentation", "pres"),
                         ("Project Report", "rep"), ("Midterm", "mt")]:
            if rest.startswith(key): return [f"cis4020-{tid}"]
    if m := re.match(r"Assignment #?(\d)", rest): return [f"{c}-a{m[1]}"]
    if m := re.match(r"Midterm #(\d)", rest): return [f"{c}-mt{m[1]}"]
    if rest.startswith("Midterm"): return [f"{c}-mt"]
    raise SystemExit(f"unmapped name: {name}")

def main(path):
    tasks = load_tasks()
    rows = [l.rstrip("\n").split("\t") for l in open(path) if l.strip()]
    used, one, many, none, datediff = {}, [], [], [], []
    for name, start, end in rows:
        ts = targets(name)
        for t in ts:
            if t not in tasks: raise SystemExit(f"{name} -> missing planner task {t}")
            if t in used: raise SystemExit(f"{t} claimed twice: {used[t]} / {name}")
            used[t] = name
        (none if not ts else one if len(ts) == 1 else many).append((name, ts))
        # compare the Notion deadline (end of range, or the date) with the planner's due
        n_due = et(end) or et(start)
        for t in ts:
            p_due = dt.datetime.strptime(tasks[t]["due"], "%Y-%m-%d %H:%M")
            if n_due and n_due.date() != p_due.date():
                datediff.append((name, t, n_due, p_due))
    print(f"Notion rows: {len(rows)}  -> 1:1 {len(one)} · 1:many {len(many)} · no planner task {len(none)}")
    print(f"Planner tasks: {len(tasks)} -> mapped {len(used)} · planner-only {len(tasks) - len(used)}")
    for n, ts in many: print("  1:many", n, "->", ts)
    from collections import Counter
    print("  Notion-only:", Counter(re.sub(r"\d+.*", "N", n) for n, _ in none))
    print("  planner-only kinds:", Counter(t["kind"] for i, t in tasks.items() if i not in used),
          [i for i, t in tasks.items() if i not in used and t["kind"] != "ongoing"])
    print("date differences (Notion vs planner):")
    for n, t, a, b in datediff: print(f"  {t:16} Notion {a:%a %b %d %H:%M}  planner {b:%a %b %d %H:%M}   ({n})")


if __name__ == "__main__":
    main(sys.argv[1])
