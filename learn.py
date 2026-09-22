"""Learned speed: how long each kind of work really takes Zach, from finished tasks.

Evidence is a finished task, never a single block, since one block does not say how much
of an assignment is done. A task counts only when most of its blocks were measured
(checked off during the block, or given an Actual min), and its ratio is measured time
over the unmultiplied tasks.yaml estimate. Dividing by the multiplied estimate would
drag a correct multiplier back towards 1.

    multiplier = (sum actual + 2·mean estimate) / (sum estimate + 2·mean estimate)

The two phantom on-estimate tasks keep one odd result from swinging the plan; the result
is clamped to 0.5–2×. A course with fewer than two finished assignments borrows the
all-assignments figure.
"""
from __future__ import annotations

PRIOR = 2
LO, HI = 0.5, 2.0
MIN_MEASURED = 0.5            # share of a task's blocks (by planned minutes) that must be measured
READ_WPM, READ_BUFFER = 220, 1.3   # how the reading estimates were built


def group(task: dict) -> str | None:
    kind = task["kind"]
    if kind == "work":
        return f"work:{task['course']}"
    return kind if kind in ("reading", "discussion", "ongoing", "prep") else None


def evidence(db, resolved: list[dict], base: dict[str, int]) -> dict[str, list[tuple]]:
    """(actual, estimate, task id) per group, from finished tasks that were measured."""
    out: dict[str, list[tuple]] = {}
    for t in resolved:
        if t["status"] != "finished" or t["id"] not in base or not group(t):
            continue
        rows = db.execute("select planned_min, actual_min, measured from sessions "
                          "where task = ? and status = 'done'", (t["id"],)).fetchall()
        planned = sum(r["planned_min"] for r in rows)
        if not rows or sum(r["planned_min"] for r in rows if r["measured"]) < MIN_MEASURED * planned:
            continue
        actual = sum(r["actual_min"] for r in rows)
        out.setdefault(group(t), []).append((actual, base[t["id"]], t["id"]))
    return out


def multipliers(ev: dict[str, list[tuple]]) -> dict[str, float]:
    def fit(pairs):
        a = sum(p[0] for p in pairs)
        e = sum(p[1] for p in pairs)
        ebar = e / len(pairs)
        return min(HI, max(LO, (a + PRIOR * ebar) / (e + PRIOR * ebar)))
    out = {g: fit(p) for g, p in ev.items() if p}
    work = [p for g, ps in ev.items() if g.startswith("work:") for p in ps]
    if work:
        out["work"] = fit(work)
    for g, ps in ev.items():                  # thin evidence for one course: use all courses
        if g.startswith("work:") and len(ps) < 2:
            out[g] = out["work"]
    return out


def per_task(tasks: list[dict], mult: dict[str, float], started: set[str]) -> dict[str, float]:
    """Multipliers for the tasks that have not started; started ones keep their estimate."""
    out = {}
    for t in tasks:
        g = group(t)
        m = mult.get(g) if g else None
        if m is None and g and g.startswith("work:"):
            m = mult.get("work")
        if m is not None and t["id"] not in started and abs(m - 1) > 0.005:
            out[t["id"]] = round(m, 3)
    return out


def report(ev: dict[str, list[tuple]], mult: dict[str, float], tasks: list[dict],
           started: set[str]) -> list[str]:
    names = {"reading": "Readings", "discussion": "Discussion posts",
             "ongoing": "Weekly reviews and practice", "prep": "Exam prep"}
    lines = []
    if not ev:
        return ["Nothing measured yet. Check a block off in Could Do while you are in it, or "
                "fill in Actual min, and finished tasks start teaching the planner your pace."]
    for g in sorted(mult):
        if g == "work":
            continue
        n = len(ev.get(g, []))
        label = names.get(g) or f"{g.split(':', 1)[1]} assignments"
        line = f"{label}: {mult[g]:.2f}× the estimate"
        line += f" (from {n} finished task{'s' if n != 1 else ''})" if n else " (borrowed from all assignments)"
        if g == "reading":
            line += f" — about {READ_WPM / (READ_BUFFER * mult[g]):.0f} words a minute"
        lines.append(line)
    shift = sum(round(t["minutes"] * (m - 1)) for t in tasks
                for m in [per_task([t], mult, started).get(t["id"], 1)])
    lines.append(f"Hours moved in the plan by these: {shift / 60:+.1f} h")
    return lines
