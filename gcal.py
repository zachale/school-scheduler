# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml"]
# ///
"""The planner's two links to Google Calendar, both through the f26-plan-sync robot.

    uv run gcal.py busy             # snapshot busy time on Zach's calendars -> busy.json
    uv run gcal.py sync --dry-run   # report what pushing the plan would change
    uv run gcal.py sync             # push the plan into the "F26 Plan" calendar

The robot is a service account in the your-gcp-project GCP project. Zach's own calendars
are shared with it as free/busy only, so `busy` sees when he is busy but never what
the event is. "F26 Plan" belongs to the robot and is shared read-only with Zach. The
script authenticates by impersonating the robot with the gcloud login already on this
machine: no key file or client secret exists.

`sync` reads schedule.json and resolved_tasks.json, so run plan.py and verify.py
first. Every event carries a stable key and a content hash in its private extended
properties; a re-run adds, updates and deletes only what changed. Events before the
plan's first day are left alone as history.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import google.oauth2.credentials
import yaml
from google.auth import impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERE = Path(__file__).parent
STATE = HERE / "gcal.json"            # the calendar id; not a secret
OWNER = "you@example.com"       # gcloud account that impersonates, and who sees the calendar
SERVICE_ACCOUNT = "planner-bot@your-project.iam.gserviceaccount.com"
SCOPE = "https://www.googleapis.com/auth/calendar"
NAME = "F26 Plan"
TZ = "America/Toronto"
RETRIES = 6


def service():
    token = subprocess.run(["gcloud", "auth", "print-access-token", OWNER],
                           capture_output=True, text=True, check=True).stdout.strip()
    creds = impersonated_credentials.Credentials(
        source_credentials=google.oauth2.credentials.Credentials(token),
        target_principal=SERVICE_ACCOUNT, target_scopes=[SCOPE], lifetime=900)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def desired_events(schedule: list[dict], tasks: list[dict]) -> dict[str, dict]:
    """Every event the calendar should hold from the plan start on, by stable key.
    No per-event colours: Google shows an event's colour only to the calendar owner,
    so on Zach's side every event takes the calendar's colour."""
    start = schedule[0]["date"]
    out: dict[str, dict] = {}
    for day in schedule:
        seen: Counter = Counter()
        for b in day["work"]:
            key = f"{b['task']}|{day['date']}|{seen[b['task']]}"
            seen[b["task"]] += 1
            lines = [f"Really due {b['due']}"]
            if b.get("note"):
                lines.append(b["note"])
            if b["overflow"]:
                lines.append("Evening overflow: the deadline needs it.")
            out[key] = {
                "summary": f"{b['course']} · {b['title']}",
                "description": "\n".join(lines),
                "start": {"dateTime": f"{day['date']}T{b['start']}:00", "timeZone": TZ},
                "end": {"dateTime": f"{day['date']}T{b['end']}:00", "timeZone": TZ},
                "transparency": "opaque",
            }
    last = {b["task"]: day["date"] for day in schedule for b in day["work"]}
    for t in tasks:
        due = dt.datetime.strptime(t["orig_due"], "%Y-%m-%d %H:%M")
        if t["kind"] != "work" or str(due.date()) < start:
            continue
        if t["id"] in last:
            done = dt.date.fromisoformat(last[t["id"]])
            note = f"Last planned session {done:%a %b %d}, {(due.date() - done).days} days before."
        else:
            note = "No session is planned for it: check the planner's warnings."
        out[f"due|{t['id']}"] = {
            "summary": f"⚑ Due {due:%H:%M}: {t['course']} {t['title']}",
            "description": f"Real deadline. {note}",
            "start": {"date": str(due.date())},
            "end": {"date": str(due.date() + dt.timedelta(days=1))},
            "transparency": "transparent",
        }
    for key, ev in out.items():
        digest = hashlib.sha1(json.dumps(ev, sort_keys=True).encode()).hexdigest()[:16]
        ev["extendedProperties"] = {"private": {"f26": key, "f26hash": digest}}
    return out


def fetch_busy(svc, calendars: list[str], start: dt.date, end: dt.date) -> list[dict]:
    """Busy intervals on the given calendars, split at midnight into local days."""
    tz = ZoneInfo(TZ)
    seen: set[tuple[str, str, str]] = set()
    d = start
    while d < end:                                   # freebusy caps the range; go by month
        stop = min(end, d + dt.timedelta(days=30))
        res = svc.freebusy().query(body={
            "timeMin": dt.datetime.combine(d, dt.time(), tz).isoformat(),
            "timeMax": dt.datetime.combine(stop, dt.time(), tz).isoformat(),
            "timeZone": TZ, "items": [{"id": c} for c in calendars],
        }).execute(num_retries=RETRIES)
        for cid, cal in res["calendars"].items():
            if cal.get("errors"):
                raise SystemExit(f"cannot read free/busy for {cid}: {cal['errors']}. "
                                 f"Is it shared with {SERVICE_ACCOUNT}?")
            for b in cal.get("busy", []):
                # step in UTC: aware datetimes sharing one ZoneInfo compare by wall clock,
                # which misorders times inside the repeated hour when DST ends
                s = dt.datetime.fromisoformat(b["start"]).astimezone(dt.timezone.utc)
                e = dt.datetime.fromisoformat(b["end"]).astimezone(dt.timezone.utc)
                while s < e:
                    local = s.astimezone(tz)
                    midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(),
                                                   tz).astimezone(dt.timezone.utc)
                    cut = min(e, midnight)
                    seen.add((str(local.date()), f"{local:%H:%M}",
                              "24:00" if cut == midnight else f"{cut.astimezone(tz):%H:%M}"))
                    s = cut
        d = stop
    return [{"date": a, "start": b, "end": c} for a, b, c in sorted(seen)]


def ensure_calendar(svc, dry: bool) -> str | None:
    cid = json.loads(STATE.read_text())["calendar_id"] if STATE.exists() else None
    if cid:
        try:
            svc.calendars().get(calendarId=cid).execute(num_retries=RETRIES)
            return cid
        except HttpError as e:
            if e.resp.status != 404:
                raise
    if dry:
        return None
    cal = svc.calendars().insert(body={
        "summary": NAME, "timeZone": TZ,
        "description": "Fall 2026 time-blocked study plan. Generated by plan.py; "
                       "edits here are overwritten on the next sync.",
    }).execute(num_retries=RETRIES)
    svc.acl().insert(calendarId=cal["id"], sendNotifications=True, body={
        "role": "reader", "scope": {"type": "user", "value": OWNER},
    }).execute(num_retries=RETRIES)
    STATE.write_text(json.dumps({"calendar_id": cal["id"]}, indent=1) + "\n")
    print(f"created calendar {NAME} and shared it with {OWNER}")
    return cal["id"]


def existing_events(svc, cid: str, start: str) -> dict[str, list[dict]]:
    time_min = dt.datetime.fromisoformat(start).replace(tzinfo=ZoneInfo(TZ)).isoformat()
    out: dict[str, list[dict]] = {}
    page = None
    while True:
        res = svc.events().list(calendarId=cid, timeMin=time_min, singleEvents=True,
                                maxResults=2500, pageToken=page).execute(num_retries=RETRIES)
        for ev in res.get("items", []):
            key = ev.get("extendedProperties", {}).get("private", {}).get("f26")
            if key:
                out.setdefault(key, []).append(ev)
        page = res.get("nextPageToken")
        if not page:
            return out


def busy() -> None:
    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    progress = yaml.safe_load((HERE / "progress.yaml").read_text())
    calendars = cfg["google_calendar"]["read_busy_from"]
    start = max(cfg["term"]["start"], progress["through"] + dt.timedelta(days=1))
    out = fetch_busy(service(), calendars, start, cfg["term"]["plan_until"] + dt.timedelta(days=1))
    (HERE / "busy.json").write_text(json.dumps({"calendars": calendars, "busy": out}, indent=1) + "\n")
    print(f"{len(out)} busy blocks on {', '.join(calendars)} from {start:%a %b %d} -> busy.json")


def sync(dry_run: bool) -> None:
    schedule = json.loads((HERE / "schedule.json").read_text())
    tasks = json.loads((HERE / "resolved_tasks.json").read_text())
    want = desired_events(schedule, tasks)

    svc = service()
    cid = ensure_calendar(svc, dry_run)
    have = existing_events(svc, cid, schedule[0]["date"]) if cid else {}

    add = [k for k in want if k not in have]
    update = [k for k in want if k in have
              and have[k][0]["extendedProperties"]["private"].get("f26hash")
              != want[k]["extendedProperties"]["private"]["f26hash"]]
    delete = [ev for k, evs in have.items() for ev in (evs if k not in want else evs[1:])]

    print(f"plan from {schedule[0]['date']}: {len(want)} events "
          f"({sum(1 for k in want if not k.startswith('due|'))} work blocks, "
          f"{sum(1 for k in want if k.startswith('due|'))} deadlines)")
    print(f"  add {len(add)} · update {len(update)} · delete {len(delete)} · "
          f"unchanged {len(want) - len(add) - len(update)}")
    if dry_run:
        if not cid:
            print("  (calendar does not exist yet; a real run creates it)")
        return

    events = svc.events()
    for k in add:
        events.insert(calendarId=cid, body=want[k]).execute(num_retries=RETRIES)
    for k in update:
        events.update(calendarId=cid, eventId=have[k][0]["id"], body=want[k]).execute(num_retries=RETRIES)
    for ev in delete:
        events.delete(calendarId=cid, eventId=ev["id"]).execute(num_retries=RETRIES)
    print("  synced")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("busy", help="snapshot busy time on Zach's calendars into busy.json")
    sp = sub.add_parser("sync", help="push the plan into the F26 Plan calendar")
    sp.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = ap.parse_args()
    if args.cmd == "busy":
        busy()
    else:
        sync(args.dry_run)


if __name__ == "__main__":
    main()
