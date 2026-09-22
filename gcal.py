"""Google Calendar access for the autoplanner, as the f26-plan-sync robot.

The robot is a service account in the your-gcp-project GCP project. Zach's own calendars
are shared with it as free/busy only, so it sees when he is busy but never what the
event is. "F26 Plan" belongs to the robot and is shared read-only with Zach.

It authenticates with a service-account key that lives only on the VM
(~/.config/f26-planner/google-key.json, mode 0600, override with F26_GOOGLE_KEY).

Every event the planner writes carries a stable key and a content hash in its private
extended properties, so apply() adds, updates and deletes only what changed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPE = "https://www.googleapis.com/auth/calendar"
TZ = "America/Toronto"
KEY = Path(os.environ.get("F26_GOOGLE_KEY", "~/.config/f26-planner/google-key.json")).expanduser()
BATCH = 50                     # Calendar's recommended ceiling per batch request
RETRIES = 6


class CalendarError(RuntimeError):
    """A Google failure the service should treat as a failed cycle."""


def service():
    creds = service_account.Credentials.from_service_account_file(str(KEY), scopes=[SCOPE])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def fetch_busy(svc, calendars: list[str], start: dt.date, end: dt.date) -> list[dict]:
    """Busy intervals on the given calendars, split at midnight into local days.
    A per-calendar error (for example a revoked share) raises CalendarError rather than
    returning an empty list, which would read as a free calendar."""
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
                raise CalendarError(f"free/busy for {cid}: {cal['errors']}")
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


def keyed(key: str, body: dict) -> dict:
    """Stamp an event body with its stable key and a hash of its content."""
    digest = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return {**body, "extendedProperties": {"private": {"f26": key, "f26hash": digest}}}


def existing(svc, cid: str, since: dt.date) -> dict[str, list[dict]]:
    """Planner events from `since` on, by key. Events before it are history."""
    time_min = dt.datetime.combine(since, dt.time(), ZoneInfo(TZ)).isoformat()
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


def _batch(svc, calls: list, label: str) -> None:
    """Run request factories in batches, retrying rate-limited and server errors with
    backoff. A delete of an event that is already gone counts as done."""
    pending, delay = list(calls), 2
    for _ in range(RETRIES):
        retry = []
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            errors: dict[str, HttpError] = {}
            batch = svc.new_batch_http_request(
                callback=lambda rid, _resp, exc: errors.__setitem__(rid, exc) if exc else None)
            for j, (make, _) in enumerate(chunk):
                batch.add(make(), request_id=str(j))
            batch.execute()
            for j, (make, is_delete) in enumerate(chunk):
                exc = errors.get(str(j))
                if exc is None:
                    continue
                status = exc.resp.status
                if is_delete and status in (404, 410):
                    continue
                if status in (403, 429, 500, 502, 503):
                    retry.append((make, is_delete))
                else:
                    raise CalendarError(f"{label}: {status} {exc}")
        if not retry:
            return
        pending = retry
        time.sleep(delay)
        delay *= 2
    raise CalendarError(f"{label}: {len(pending)} writes still failing after retries")


def apply(svc, cid: str, want: dict[str, dict], since: dt.date, dry_run: bool) -> tuple:
    """Make the calendar hold exactly `want` (key -> body from keyed()) from `since` on."""
    have = existing(svc, cid, since)
    h = lambda ev: ev["extendedProperties"]["private"].get("f26hash")
    add = [k for k in want if k not in have]
    update = [k for k in want if k in have and h(have[k][0]) != h(want[k])]
    delete = [ev for k, evs in have.items() for ev in (evs if k not in want else evs[1:])]
    if not dry_run:
        ev = svc.events()
        _batch(svc, [(lambda k=k: ev.insert(calendarId=cid, body=want[k]), False) for k in add]
               + [(lambda k=k: ev.update(calendarId=cid, eventId=have[k][0]["id"], body=want[k]),
                   False) for k in update]
               + [(lambda e=e: ev.delete(calendarId=cid, eventId=e["id"]), True) for e in delete],
               "calendar writes")
    return len(add), len(update), len(delete), len(want) - len(add) - len(update)
