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
RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
                 "backendError", "internalError", "variableTermLimitExceeded"}


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


def event_id(key: str) -> str:
    """A stable Calendar event id for a key: base32hex characters only, so a retried
    insert that Google already committed comes back as 409 instead of duplicating."""
    return "f26" + hashlib.sha1(key.encode()).hexdigest()[:26]


def keyed(key: str, body: dict) -> dict:
    """Stamp an event body with its stable key and a hash of its content."""
    digest = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return {**body, "id": event_id(key),
            "extendedProperties": {"private": {"f26": key, "f26hash": digest}}}


def _reason(exc: HttpError) -> str:
    try:
        errors = json.loads(exc.content)["error"].get("errors") or [{}]
        return errors[0].get("reason", "")
    except Exception:
        return ""


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
    """Run request factories in batches. Rate limits and server errors are retried with
    backoff; a delete of an event that is already gone, and an insert of one that already
    exists (the same write, retried), both resolve without duplicating anything."""
    pending, delay = list(calls), 2
    for _ in range(RETRIES):
        retry = []
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            errors: dict[str, HttpError] = {}
            batch = svc.new_batch_http_request(
                callback=lambda rid, _resp, exc: errors.__setitem__(rid, exc) if exc else None)
            for j, call in enumerate(chunk):
                batch.add(call["make"](), request_id=str(j))
            batch.execute()
            for j, call in enumerate(chunk):
                exc = errors.get(str(j))
                if exc is None:
                    continue
                status, reason = exc.resp.status, _reason(exc)
                if call["kind"] == "delete" and status in (404, 410):
                    continue
                if call["kind"] == "insert" and status == 409 and call.get("instead"):
                    retry.append({"make": call["instead"], "kind": "update"})
                elif status in (429, 500, 502, 503) or (status == 403 and reason in RETRY_REASONS):
                    retry.append(call)
                else:
                    raise CalendarError(f"{label}: {status} {reason or exc}")
        if not retry:
            return
        pending = retry
        time.sleep(delay)
        delay = min(delay * 2, 30)
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
        _batch(svc,
               [{"kind": "insert", "make": lambda k=k: ev.insert(calendarId=cid, body=want[k]),
                 "instead": lambda k=k: ev.update(calendarId=cid, eventId=event_id(k),
                                                  body=want[k])} for k in add]
               + [{"kind": "update",
                   "make": lambda k=k: ev.update(calendarId=cid, eventId=have[k][0]["id"],
                                                 body=want[k])} for k in update]
               + [{"kind": "delete", "make": lambda e=e: ev.delete(calendarId=cid,
                                                                   eventId=e["id"])}
                  for e in delete], "calendar writes")
    return len(add), len(update), len(delete), len(want) - len(add) - len(update)


def alert(svc, cid: str, key: str, body: dict | None) -> None:
    """Put one alert event on the calendar, or take it away, without touching anything
    else. Used when the plan itself could not be rebuilt."""
    ev = svc.events()
    found = ev.list(calendarId=cid, privateExtendedProperty=f"f26={key}", showDeleted=False,
                    maxResults=10).execute(num_retries=RETRIES).get("items", [])
    try:
        if body is None:
            for e in found:
                ev.delete(calendarId=cid, eventId=e["id"]).execute(num_retries=RETRIES)
        elif found:
            ev.update(calendarId=cid, eventId=found[0]["id"], body=body).execute(num_retries=RETRIES)
        else:
            ev.insert(calendarId=cid, body=body).execute(num_retries=RETRIES)
    except HttpError as e:
        if e.resp.status not in (404, 409, 410):
            raise
