#!/usr/bin/env python3
r"""
Claude data-export adapter: claude.ai conversations -> events.db as ACTIVITY, never as burn.

The export (claude.ai -> Settings -> Privacy -> Export data) carries every message with a
timestamp and its text but NO token fields at any nesting level. So this adapter writes one
row per assistant message with

    surface   = 'claude-ai-chat'     machine = 'claude.ai'
    unit      = 'messages'           qty     = 1
    *_tokens  = NULL                 model   = NULL

The token columns are left NULL on purpose (spec §5.4: no adapter may write a token count for
work that was not metered). The dashboard renders these days as activity and the headline
token total does not move.

It scans a folder for every export it can find and ingests all of them, deduped on message
uuid, so dropping a fresh export into the folder is the whole update procedure:

    <folder>\conversations.json
    <folder>\export_*\conversations.json
    <folder>\*.zip  (containing conversations.json)

Usage:
    python claude_export.py                       # default folder, incremental
    python claude_export.py --rebuild             # re-read every export
    python claude_export.py --source <file|dir>   # a specific export or folder

Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract  # noqa: E402  (shares schema, connect(), reattribute())

DEFAULT_DIR = os.path.expanduser(os.environ.get("CLAUDE_EXPORT_DIR", os.path.join("~", "claude-exports")))
MACHINE = "claude.ai"
SURFACE = "claude-ai-chat"

EVENT_COLS = ["event_id", "session_id", "ts", "machine", "surface", "source_file",
              "provider", "model", "modality", "unit", "cost_basis",
              "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
              "cache_creation_1h", "cache_creation_5m", "qty", "service_tier",
              "web_search_requests", "web_fetch_requests", "is_sidechain"]


def find_sources(path: str) -> list[tuple[str, str | None]]:
    """-> [(source_key, zip_member_or_None)]. source_key is the on-disk file path."""
    if os.path.isfile(path):
        if path.lower().endswith(".zip"):
            return [(path, "conversations.json")]
        return [(path, None)]
    out: list[tuple[str, str | None]] = []
    for p in [os.path.join(path, "conversations.json")] + \
             sorted(glob.glob(os.path.join(path, "export_*", "conversations.json"))):
        if os.path.isfile(p):
            out.append((p, None))
    for z in sorted(glob.glob(os.path.join(path, "*.zip"))):
        try:
            with zipfile.ZipFile(z) as zf:
                names = [n for n in zf.namelist() if n.endswith("conversations.json")]
        except zipfile.BadZipFile:
            continue
        if names:
            out.append((z, names[0]))
    return out


def load(path: str, member: str | None) -> list[dict]:
    if member:
        with zipfile.ZipFile(path) as zf, zf.open(member) as fh:
            return json.load(io.TextIOWrapper(fh, encoding="utf-8"))
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def rows_for(convs: list[dict], source_key: str):
    events, sessions = [], []
    for c in convs:
        cid = c.get("uuid")
        if not cid:
            continue
        msgs = c.get("chat_messages") or []
        ts = sorted(m.get("created_at") for m in msgs if m.get("created_at"))
        sessions.append((cid, MACHINE, None, (c.get("name") or "").strip() or None,
                         ts[0] if ts else c.get("created_at"),
                         ts[-1] if ts else c.get("updated_at"), "[]", "[]"))
        for m in msgs:
            if m.get("sender") != "assistant" or not m.get("uuid"):
                continue
            events.append({
                "event_id": m["uuid"], "session_id": cid, "ts": m.get("created_at"),
                "machine": MACHINE, "surface": SURFACE, "source_file": source_key,
                "provider": "anthropic", "model": None, "modality": "text",
                "unit": "messages", "cost_basis": "subscription",
                "qty": 1, "is_sidechain": 0,
            })
    return events, sessions


def ingest(con, sources, rebuild=False):
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, mtime, size FROM sources")}
    done = []
    for path, member in sources:
        st = os.stat(path)
        if not rebuild and known.get(path) == (st.st_mtime, st.st_size):
            continue
        convs = load(path, member)
        events, sessions = rows_for(convs, path)
        con.execute("DELETE FROM events WHERE source_file=?", (path,))
        con.executemany(
            f"INSERT OR REPLACE INTO events ({','.join(EVENT_COLS)}) "
            f"VALUES ({','.join('?' * len(EVENT_COLS))})",
            [[e.get(k) for k in EVENT_COLS] for e in events])
        # a conversation may appear in several exports; keep the widest span / any title
        for sid, mach, ai, custom, first, last, cp, sp in sessions:
            row = con.execute("SELECT custom_title, first_ts, last_ts FROM sessions "
                              "WHERE session_id=?", (sid,)).fetchone()
            if row:
                custom = custom or row[0]
                first = min(x for x in (first, row[1]) if x) if (first or row[1]) else None
                last = max(x for x in (last, row[2]) if x) if (last or row[2]) else None
            con.execute(
                "INSERT OR REPLACE INTO sessions (session_id, machine, ai_title, custom_title,"
                " first_ts, last_ts, clean_paths, shell_paths) VALUES (?,?,?,?,?,?,?,?)",
                (sid, mach, ai, custom, first, last, cp, sp))
        con.execute("INSERT OR REPLACE INTO sources (path, mtime, size, machine, surface) "
                    "VALUES (?,?,?,?,?)", (path, st.st_mtime, st.st_size, MACHINE, SURFACE))
        done.append((path, len(convs), len(events)))
    con.commit()
    return done


def main():
    ap = argparse.ArgumentParser(description="Claude data-export -> events.db (activity only)")
    ap.add_argument("--db", default=extract.DEFAULT_DB)
    ap.add_argument("--projects", default=extract.DEFAULT_PROJECTS)
    ap.add_argument("--source", default=DEFAULT_DIR, help="export file, zip, or folder")
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()

    sources = find_sources(a.source)
    if not sources:
        print(f"no export found under {a.source}")
        return 0
    con = extract.connect(a.db)
    done = ingest(con, sources, a.rebuild)
    for path, nc, ne in done:
        print(f"ingested {os.path.basename(path)}: {nc} conversations, {ne} assistant messages")
    if done:
        extract.reattribute(con, a.projects)   # picks up DONE/DEAD/LESSON title prefixes
    n, first, last, nconv = con.execute(
        "SELECT COUNT(*), MIN(date(ts)), MAX(date(ts)), COUNT(DISTINCT session_id) "
        "FROM events WHERE surface=?", (SURFACE,)).fetchone()
    print(f"claude-ai-chat in store: {nconv} conversations, {n:,} assistant messages, "
          f"{first} -> {last}  (activity only - no token data exists for this surface)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
