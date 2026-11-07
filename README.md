# UTSC Study Room Demand Tracker

An automated pipeline that records the booking status of every bookable study room at the
University of Toronto Scarborough Library every 30 minutes. The goal is to measure real student demand for study
space: when rooms fill up, how far ahead they're booked, and how demand changes during exam season.

> Status: data collection running. Analysis and dashboard coming next.

## Why
UTSC publishes how many seats each space has, but not how heavily those spaces are used. The library's public
booking calendar only shows the current state, with no history. This project builds that history.

## Data source
The public [UTSC Library LibCal booking page](https://libcal.library.utoronto.ca/reserve/spaces/utsclibrary)
loads its calendar from a JSON endpoint. That endpoint gives booked/available status for each room and
30-minute slot. **No personal information is collected**: the endpoint doesn't say who booked a room.

The scraper makes **one request per run** (plus a few one-time requests to look up room names), identifies itself with a
descriptive User-Agent, and runs at most every 30 minutes.

Rooms tracked: 16 (8 small rooms for 3 people, 6 medium rooms for 4, plus the Podcast Room and One Button Studio).

## How it works
```
GitHub Actions (cron, every 30 min)
  └─ scraper.py
       ├─ fetches all slots for the next ~8 days
       ├─ compares each slot with data/state.json
       └─ appends only slots whose status changed → data/changes.csv
  └─ commits data/ back to the repo
```
Storing only **changes** (a slot becoming booked, or being cancelled) keeps the dataset to a few MB over a
whole term, while still letting you rebuild the full calendar as it looked at any snapshot time.

### Data files
| File | One row per | Columns |
|---|---|---|
| `data/changes.csv` | status change of a slot | `observed_at_utc, slot_start, slot_end, room_id, status, raw_class` |
| `data/runs.csv` | scraper run | `observed_at_utc, days_fetched, n_slots, n_booked, n_changes, ok, error` |
| `data/rooms.csv` | room | `room_id, name, category, capacity, first_seen_utc` |
| `data/state.json` | (internal) latest known status per slot | |

Notes for analysis:
- `observed_at_utc` is UTC. `slot_start`/`slot_end` are Toronto local time, as LibCal gives them.
- The first row for each slot is the first time the slot was seen, not necessarily when it was booked.
  Lead-time analysis should use slots that were first seen as `available` and later changed to `booked`.
- Ignore changes observed **after** `slot_start`, because a slot's status may change once its time has passed.
- `raw_class` keeps LibCal's original label (`s-lc-eq-checkout` = booked) in case other labels show up later,
  such as for closures or padding time between bookings.
- Gaps in `runs.csv` mean GitHub didn't run the job at that time. Rows with `ok = 0` mean the request failed; the error is logged.

## Setup
1. Create a new **public** GitHub repository (Actions minutes are free and unlimited for public repos).
2. Push this folder:
   ```bash
   git init
   git add .
   git commit -m "Initial scraper"
   git branch -M main
   git remote add origin https://github.com/<you>/utsc-room-tracker.git
   git push -u origin main
   ```
3. On GitHub: **Settings → Actions → General → Workflow permissions → Read and write**.
4. **Actions** tab → *Scrape room availability* → **Run workflow** to test. After that, it runs every 30 minutes.

GitHub disables scheduled workflows on repositories with no activity for 60 days. Because this workflow commits data, the repository stays active.

Run locally: `python scraper.py` (Python 3.9+, no dependencies).

## Roadmap
- [x] Automated collection
- [ ] Clean & model: rebuild per-snapshot occupancy, lead-time to sell-out
- [ ] Analysis: demand by hour/weekday/room type; normal weeks vs. exam period (Dec 2026)
- [ ] Prediction: probability a room is still free *N* days ahead
- [ ] Streamlit dashboard + write-up with recommendations for the library
