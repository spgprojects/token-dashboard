#!/usr/bin/env python3
"""
Token Dashboard — PUBLIC projection.

Writes public.json: the numbers the website shows. A projection of the private store, never a
second build (spec §v3). Rules it enforces:
  - no project, session, purpose, outcome, title, path or machine names — aggregates only
  - token sums restricted to unit='tokens' rows; activity-only rows (claude.ai export) count as
    presence, never magnitude
  - every headline carries the new-work share and the attribution coverage next to it
  - media are not token-denominated and are excluded entirely

  python export_public.py                 -> public.json beside this file
  python export_public.py --days 120      -> longer daily series
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "events.db")
OUT = os.path.join(HERE, "public.json")

TOTAL = "COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+COALESCE(cache_creation_tokens,0)+COALESCE(cache_read_tokens,0)"
NEW = "COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+COALESCE(cache_creation_tokens,0)"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    c = sqlite3.connect(a.db)
    tok = "unit='tokens' AND COALESCE(is_sidechain,0)=0"

    total, new, cache_read, out_tok = c.execute(
        f"SELECT SUM({TOTAL}), SUM({NEW}), SUM(COALESCE(cache_read_tokens,0)), SUM(COALESCE(output_tokens,0)) FROM events WHERE {tok}").fetchone()
    attributed = c.execute(
        f"SELECT SUM({TOTAL}) FROM events WHERE {tok} AND confidence='high'").fetchone()[0] or 0
    first_tok, last_tok = c.execute(f"SELECT MIN(ts), MAX(ts) FROM events WHERE {tok}").fetchone()
    first_any = c.execute("SELECT MIN(ts) FROM events").fetchone()[0]
    sessions = c.execute(f"SELECT COUNT(DISTINCT session_id) FROM events WHERE {tok}").fetchone()[0]
    machines = c.execute(f"SELECT COUNT(DISTINCT machine) FROM events WHERE {tok}").fetchone()[0]
    surfaces = {s: int(v or 0) for s, v in c.execute(
        f"SELECT surface, SUM({TOTAL}) FROM events WHERE {tok} GROUP BY surface")}

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).strftime("%Y-%m-%dT00:00:00")
    got = {d: (int(t or 0), int(n or 0)) for d, t, n in c.execute(
        f"SELECT substr(ts,1,10) d, SUM({TOTAL}), SUM({NEW}) FROM events WHERE {tok} AND ts >= ? GROUP BY d ORDER BY d",
        (since,))}
    # new-work per surface per day, so the site can stack the bars by where the work ran.
    # Same unit (tokens) on both parts of the stack, so this is a legitimate split.
    by_surf: dict[str, dict[str, int]] = {}
    for d, s, n in c.execute(
            f"SELECT substr(ts,1,10) d, surface, SUM({NEW}) FROM events WHERE {tok} AND ts >= ? GROUP BY d, surface",
            (since,)):
        by_surf.setdefault(d, {})[s] = int(n or 0)
    # zero-fill every calendar day in the window so the x-axis is real time, not "days with events"
    start = dt.date.fromisoformat(since[:10]); end = dt.date.fromisoformat((last_tok or since)[:10])
    daily = []
    day = start
    while day <= end:
        k = day.isoformat(); t, n = got.get(k, (0, 0))
        daily.append({"d": k, "total": t, "new": n, "new_by_surface": by_surf.get(k, {})})
        day += dt.timedelta(days=1)
    # activity-only days (no token fields) in the same window, for the amber 'presence' marks
    activity = [d for (d,) in c.execute(
        "SELECT DISTINCT substr(ts,1,10) d FROM events WHERE unit='messages' AND ts >= ? ORDER BY d", (since,))]

    pub = {
        "_note": "Public projection of the SPG token dashboard. Aggregates only; no project or session names. "
                 "Token sums are unit='tokens' rows only; media and unmetered chat are never counted as tokens.",
        "available": True,
        "as_of": (last_tok or "")[:10],
        "since": (first_any or "")[:10],
        "metered_since": (first_tok or "")[:10],
        "total_tokens": int(total or 0),
        "new_work_tokens": int(new or 0),
        "output_tokens": int(out_tok or 0),
        "new_work_share": round((new or 0) / total, 4) if total else None,
        "cache_read_share": round((cache_read or 0) / total, 4) if total else None,
        "attribution_coverage": round(attributed / total, 4) if total else None,
        "sessions": int(sessions or 0),
        "machines": int(machines or 0),
        "surfaces": surfaces,
        "window_days": a.days,
        "daily": daily,
        "activity_only_days": activity,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(pub, f, indent=1)
    print(f"wrote {os.path.normpath(a.out)} · {pub['total_tokens']:,} tokens · new-work {pub['new_work_share']:.1%} · "
          f"coverage {pub['attribution_coverage']:.1%} · {len(daily)} days in window")

if __name__ == "__main__":
    main()
