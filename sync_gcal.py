# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "pyyaml"]
# ///
"""Mirror the term plan into a dedicated Google Calendar, "F26 Plan".

The calendar belongs to a service account in the your-gcp-project GCP project and is
shared read-only with Zach, so this script can never see or change any of Zach's
own calendars. It authenticates by impersonating that service account with the
gcloud login already on this machine: no key file or client secret exists.

    uv run sync_gcal.py --dry-run   # read the calendar, report what would change
    uv run sync_gcal.py             # push the current plan

Reads schedule.json and resolved_tasks.json, so run plan.py (and verify.py) first.
Every event carries a stable key and a content hash in its private extended
properties; a re-run adds, updates and deletes only what changed. Events before
the plan's first day are left alone as history.
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

# Google Calendar's event colours as the web UI draws them, by colorId
PALETTE = {"1": "#7986cb", "2": "#33b679", "3": "#8e24aa", "4": "#e67c73",
           "5": "#f6bf26", "6": "#f4511e", "7": "#039be5", "8": "#616161",
           "9": "#3f51b5", "10": "#0b8043", "11": "#d50000"}


def colour_id(hex_colour: str) -> str:
    """The event colour closest to a planner colour."""
    rgb = lambda h: tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    target = rgb(hex_colour)
    return min(PALETTE, key=lambda k: sum((a - b) ** 2 for a, b in zip(rgb(PALETTE[k]), target)))


def service():
    token = subprocess.run(["gcloud", "auth", "print-access-token", OWNER],
                           capture_output=True, text=True, check=True).stdout.strip()
    creds = impersonated_credentials.Credentials(
        source_credentials=google.oauth2.credentials.Credentials(token),
        target_principal=SERVICE_ACCOUNT, target_scopes=[SCOPE], lifetime=900)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def desired_events(schedule: list[dict], tasks: list[dict], colours: dict) -> dict[str, dict]:
    """Every event the calendar should hold from the plan start on, by stable key."""
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
                "colorId": colour_id(colours[b["course"]]),
                "transparency": "opaque",
            }
    last = {b["task"]: day["date"] for day in schedule for b in day["work"]}
    for t in tasks:
        due = dt.datetime.strptime(t["orig_due"], "%Y-%m-%d %H:%M")
        if t["kind"] != "work" or str(due.date()) < start:
            continue
        done = dt.date.fromisoformat(last[t["id"]])
        out[f"due|{t['id']}"] = {
            "summary": f"⚑ Due {due:%H:%M}: {t['course']} {t['title']}",
            "description": f"Real deadline. Last planned session {done:%a %b %d}, "
                           f"{(due.date() - done).days} days before.",
            "start": {"date": str(due.date())},
            "end": {"date": str(due.date() + dt.timedelta(days=1))},
            "colorId": colour_id(colours[t["course"]]),
            "transparency": "transparent",
        }
    for key, ev in out.items():
        digest = hashlib.sha1(json.dumps(ev, sort_keys=True).encode()).hexdigest()[:16]
        ev["extendedProperties"] = {"private": {"f26": key, "f26hash": digest}}
    return out


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = ap.parse_args()

    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    schedule = json.loads((HERE / "schedule.json").read_text())
    tasks = json.loads((HERE / "resolved_tasks.json").read_text())
    want = desired_events(schedule, tasks, cfg["colours"])

    svc = service()
    cid = ensure_calendar(svc, args.dry_run)
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
    if args.dry_run:
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


if __name__ == "__main__":
    main()
