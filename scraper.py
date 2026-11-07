"""Snapshot UTSC Library study-room availability from LibCal.

Each run fetches the booking grid for the next several days and compares it
with the last known state. Only *changes* are appended to data/changes.csv
(a slot becoming booked or freed up), so the dataset stays small even when
the scraper runs every 30 minutes for months.

Files written (all plain CSV, safe to commit to git):
  data/changes.csv  observed_at_utc, slot_start, slot_end, room_id, status, raw_class
  data/runs.csv     observed_at_utc, days_fetched, n_slots, n_booked, n_changes, ok, error
  data/rooms.csv    room_id, name, category, capacity, first_seen_utc
  data/state.json   latest status per (room_id, slot_start) - used to detect changes

Standard library only, so it runs anywhere with Python 3.9+.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = "https://libcal.library.utoronto.ca"
PAGE_URL = f"{BASE}/reserve/spaces/utsclibrary"
GRID_URL = f"{BASE}/spaces/availability/grid"
LOCATION_ID = 3530  # UTSC Library
DAYS_AHEAD = 8  # how far ahead to look (the booking window is ~1 week)
USER_AGENT = "utsc-room-tracker/1.0 (university student data project)"
TIMEOUT = 30

DATA = Path(__file__).resolve().parent / "data"
CHANGES = DATA / "changes.csv"
RUNS = DATA / "runs.csv"
ROOMS = DATA / "rooms.csv"
STATE = DATA / "state.json"

CHANGE_FIELDS = ["observed_at_utc", "slot_start", "slot_end", "room_id", "status", "raw_class"]
RUN_FIELDS = ["observed_at_utc", "days_fetched", "n_slots", "n_booked", "n_changes", "ok", "error"]
ROOM_FIELDS = ["room_id", "name", "category", "capacity", "first_seen_utc"]


# --------------------------------------------------------------------------- fetching

def _request(url: str, data: dict | None = None) -> bytes:
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": USER_AGENT,
        "Referer": PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
    })
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except Exception as e:  # network hiccup: back off and retry
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"request failed after 3 attempts: {url}: {last_err}")


def fetch_grid(start: date, end: date) -> list[dict]:
    """Return all slots between start (inclusive) and end (exclusive)."""
    slots: list[dict] = []
    page = 0
    while True:
        raw = _request(GRID_URL, {
            "lid": LOCATION_ID, "gid": 0, "eid": -1, "seat": 0, "seatId": 0, "zone": 0,
            "start": start.isoformat(), "end": end.isoformat(),
            "pageIndex": page, "pageSize": 50,
        })
        payload = json.loads(raw)
        if "slots" not in payload:
            raise RuntimeError(f"unexpected response shape: keys={list(payload)[:10]}")
        batch = payload["slots"]
        slots.extend(batch)
        # LibCal pages by room; a short page means we have everything.
        rooms_in_batch = {s["itemId"] for s in batch}
        if len(rooms_in_batch) < 50 or page > 20:
            return slots
        page += 1


def _parse_room_blocks(html: str) -> dict[int, dict]:
    """Extract {eid: ..., title: ..., grouping: ..., capacity: ...} JS objects from a page."""
    rooms: dict[int, dict] = {}
    for block in re.findall(r"\{[^{}]*?\beid:\s*\d+[^{}]*\}", html, flags=re.S):
        eid = re.search(r"\beid:\s*(\d+)", block)

        def field(name: str) -> str:
            m = re.search(rf'\b{name}:\s*"((?:[^"\\]|\\.)*)"', block)
            return json.loads(f'"{m.group(1)}"') if m else ""

        title = field("title")
        if not title:
            continue
        cap = re.search(r"\bcapacity:\s*(\d+)", block)
        rooms[int(eid.group(1))] = {
            "name": re.sub(r"\s*\(Capacity \d+\)", "", title),
            "category": field("grouping"),
            "capacity": cap.group(1) if cap else "",
        }
    return rooms


def fetch_room_metadata(room_ids: set[int]) -> dict[int, dict]:
    """Room names/capacities from the public booking page, falling back to each room's own page."""
    rooms = _parse_room_blocks(_request(PAGE_URL).decode("utf-8", "replace"))
    for rid in sorted(room_ids - rooms.keys()):
        try:
            page = _parse_room_blocks(_request(f"{BASE}/space/{rid}").decode("utf-8", "replace"))
            if rid in page:
                rooms[rid] = page[rid]
        except Exception as e:
            print(f"warning: no metadata for room {rid}: {e}", file=sys.stderr)
        time.sleep(1)  # be polite
    return rooms


# --------------------------------------------------------------------------- storage

def _append(path: Path, fields: list[str], rows: list[dict]) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def _load_state() -> dict[str, str]:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _known_room_ids() -> set[int]:
    if not ROOMS.exists():
        return set()
    with ROOMS.open(encoding="utf-8") as f:
        return {int(r["room_id"]) for r in csv.DictReader(f)}


def status_of(slot: dict) -> str:
    # LibCal omits className for open slots; any class means it can't be booked
    # (s-lc-eq-checkout = already booked, others = padding / closed).
    return "booked" if slot.get("className") else "available"


# --------------------------------------------------------------------------- main

def run() -> int:
    DATA.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_s = now.isoformat()
    # Start a day early so Toronto's "today" is always covered regardless of UTC offset.
    start = now.date() - timedelta(days=1)
    end = now.date() + timedelta(days=DAYS_AHEAD)

    try:
        slots = fetch_grid(start, end)
        if not slots:
            raise RuntimeError("grid returned zero slots - endpoint may have changed")
    except Exception as e:
        _append(RUNS, RUN_FIELDS, [{"observed_at_utc": now_s, "days_fetched": 0, "n_slots": 0,
                                    "n_booked": 0, "n_changes": 0, "ok": 0, "error": str(e)[:300]}])
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    state = _load_state()
    changes = []
    for s in slots:
        key = f'{s["itemId"]}|{s["start"]}'
        st = status_of(s)
        if state.get(key) != st:
            changes.append({
                "observed_at_utc": now_s, "slot_start": s["start"], "slot_end": s["end"],
                "room_id": s["itemId"], "status": st, "raw_class": s.get("className", ""),
            })
            state[key] = st

    # Drop state for slots more than 2 days in the past to keep the file small.
    cutoff = (now.date() - timedelta(days=2)).isoformat()
    state = {k: v for k, v in state.items() if k.split("|", 1)[1][:10] >= cutoff}

    # Record any rooms we haven't seen before (metadata failure is non-fatal).
    seen = {s["itemId"] for s in slots}
    new_ids = seen - _known_room_ids()
    if new_ids:
        try:
            meta = fetch_room_metadata(new_ids)
        except Exception as e:
            print(f"warning: room metadata unavailable: {e}", file=sys.stderr)
            meta = {}
        _append(ROOMS, ROOM_FIELDS, [
            {"room_id": rid, **meta.get(rid, {"name": "", "category": "", "capacity": ""}),
             "first_seen_utc": now_s}
            for rid in sorted(new_ids)
        ])

    _append(CHANGES, CHANGE_FIELDS, changes)
    STATE.write_text(json.dumps(state, sort_keys=True, indent=0))
    n_booked = sum(status_of(s) == "booked" for s in slots)
    _append(RUNS, RUN_FIELDS, [{"observed_at_utc": now_s, "days_fetched": (end - start).days,
                                "n_slots": len(slots), "n_booked": n_booked,
                                "n_changes": len(changes), "ok": 1, "error": ""}])
    print(f"{now_s}: {len(slots)} slots across {len(seen)} rooms, "
          f"{n_booked} booked, {len(changes)} changes recorded")
    return 0


if __name__ == "__main__":
    sys.exit(run())
