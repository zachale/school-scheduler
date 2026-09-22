"""Notion access for the autoplanner: the Could Do rows are where "done" lives.

Three kinds of row carry a `Plan ID`, and nothing else in Could Do is ever touched:

    deadline:<task>   an existing deliverable row — the real due date. Checking it means
                      the whole task is handed in.
    task:<task>       one planner task. Where a deliverable covers several tasks (the
                      three posts of a discussion, prep and sitting a midterm) these are
                      new child rows under it; planner-only work gets a standalone row.
    session:<id>      one calendar block, a child of its task row. Checking it means that
                      block is done.

The connection token lives only on the VM (~/.config/f26-planner/notion-token, mode 0600).
Changes are found by scanning every Plan ID row each cycle and diffing against the
service's own state, because a "changed since" query cannot see a deleted row and Notion
rounds its edit times down to the minute.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
TOKEN_FILE = Path(os.environ.get("F26_NOTION_TOKEN", "~/.config/f26-planner/notion-token"))
TZ = ZoneInfo("America/Toronto")
RETRIES = 5
PAGE_SIZE = 100


class NotionError(RuntimeError):
    """A Notion failure the service should treat as a failed cycle."""


class Notion:
    def __init__(self, data_source: str, token: str | None = None):
        self.ds = data_source
        self.token = token or TOKEN_FILE.expanduser().read_text().strip()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })
        self._me: str | None = None

    # ---- transport
    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(RETRIES):
            try:
                r = self.session.request(method, f"{API}{path}", json=body, timeout=30)
            except requests.RequestException as e:
                if attempt == RETRIES - 1:
                    raise NotionError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == RETRIES - 1:
                    raise NotionError(f"{method} {path}: {r.status_code} {r.text[:200]}")
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 400:
                raise NotionError(f"{method} {path}: {r.status_code} {r.text[:300]}")
            return r.json()
        raise NotionError(f"{method} {path}: out of retries")

    def me(self) -> str:
        """The connection's own bot id, so its own edits are not read as Zach's."""
        if self._me is None:
            self._me = self.call("GET", "/users/me")["id"]
        return self._me

    # ---- reading
    def scan(self) -> dict[str, dict]:
        """Every row carrying a Plan ID, by Plan ID. One pass, a few requests."""
        out, cursor = {}, None
        while True:
            body = {"page_size": PAGE_SIZE,
                    "filter": {"property": "Plan ID", "rich_text": {"is_not_empty": True}}}
            if cursor:
                body["start_cursor"] = cursor
            res = self.call("POST", f"/data_sources/{self.ds}/query", body)
            for page in res["results"]:
                row = parse_row(page)
                if row["plan_id"]:
                    out[row["plan_id"]] = row
            cursor = res.get("next_cursor")
            if not res.get("has_more"):
                return out

    def get(self, page_id: str) -> dict | None:
        """One row, or None if it is in the trash. Used to tell a deletion from a glitch."""
        page = self.call("GET", f"/pages/{page_id}")
        return None if page.get("in_trash") or page.get("archived") else parse_row(page)

    # ---- writing
    def create(self, props: dict, parent_page: str | None = None) -> str:
        parent = ({"type": "page_id", "page_id": parent_page} if parent_page
                  else {"type": "data_source_id", "data_source_id": self.ds})
        return self.call("POST", "/pages", {"parent": parent, "properties": props})["id"]

    def update(self, page_id: str, props: dict) -> None:
        self.call("PATCH", f"/pages/{page_id}", {"properties": props})

    def trash(self, page_id: str) -> None:
        self.call("PATCH", f"/pages/{page_id}", {"in_trash": True})

    def set_page_text(self, page_id: str, lines: list[str]) -> None:
        """Replace a plain page's body with these paragraphs (the stats page)."""
        kids = self.call("GET", f"/blocks/{page_id}/children?page_size=100")["results"]
        for b in kids:
            self.call("DELETE", f"/blocks/{b['id']}")
        blocks = [{"object": "block", "type": "paragraph",
                   "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]}}
                  for line in lines]
        for i in range(0, len(blocks), 100):
            self.call("PATCH", f"/blocks/{page_id}/children", {"children": blocks[i:i + 100]})


# ---- rows -------------------------------------------------------------------
def parse_row(page: dict) -> dict:
    p = page["properties"]
    text = lambda k: "".join(t["plain_text"] for t in p.get(k, {}).get("rich_text", []))
    title = "".join(t["plain_text"] for t in p.get("Name", {}).get("title", []))
    select = lambda k: (p.get(k, {}).get("select") or {}).get("name")
    date = p.get("Due Date", {}).get("date") or {}
    return {
        "page_id": page["id"], "plan_id": text("Plan ID"), "name": title,
        "done": bool(p.get("Done", {}).get("checkbox")),
        "planned_min": p.get("Planned min", {}).get("number"),
        "actual_min": p.get("Actual min", {}).get("number"),
        "state": select("Plan state"),
        "start": date.get("start"), "end": date.get("end"),
        "edited": page["last_edited_time"],
        "edited_by": page.get("last_edited_by", {}).get("id"),
    }


# ---- property helpers -------------------------------------------------------
def rich(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def title(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": value}}]}


def date_range(start: dt.datetime, end: dt.datetime | None = None) -> dict:
    """A Notion date with an explicit Eastern offset, so the hour survives the UTC round
    trip on both sides of the Nov 1 DST change."""
    off = lambda t: t.replace(tzinfo=TZ).isoformat(timespec="seconds")
    return {"date": {"start": off(start), "end": off(end) if end else None}}


def edited_at(row: dict) -> dt.datetime:
    """Notion's last edit time as naive Eastern; Notion rounds it down to the minute."""
    stamp = dt.datetime.fromisoformat(row["edited"].replace("Z", "+00:00"))
    return stamp.astimezone(TZ).replace(tzinfo=None)
