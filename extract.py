#!/usr/bin/env python3
"""
Token Dashboard — extractor.

Reads Claude Code transcripts (~/.claude/projects/**/*.jsonl) into a SQLite event
store, one row per assistant message, per BUILD_SPEC_v1_25Aug26.md section 4.

Stdlib only, on purpose: this has to run on the big box without a setup step.

Usage
  python extract.py                          ingest this machine into events.db
  python extract.py --analyze-paths          show path prefixes, to build projects.csv
  python extract.py --stats                  summarise the store
  python extract.py --export out.csv.gz      portable extract for the other machine
  python extract.py --import in.csv.gz       merge another machine's extract
  python extract.py --rebuild                ignore mtime cache, re-read everything
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import gzip
import io
import json
import os
import re
import socket
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "events.db")
DEFAULT_PROJECTS = os.path.join(HERE, "projects.csv")
DEFAULT_ROOT = os.path.expanduser(os.path.join("~", ".claude", "projects"))
# Cowork runs its own nested Claude Code instances; their transcripts live here and
# NOT under ~/.claude/projects. Deduped on event_id, they proved disjoint from the
# Claude Code store - genuinely additional burn, not a re-count.
DEFAULT_COWORK = os.path.expanduser(os.path.join(
    "~", "AppData", "Roaming", "Claude", "local-agent-mode-sessions"))

# Tool inputs that carry a clean, unambiguous filesystem path.
# Bash/PowerShell 'command' is deliberately excluded here - paths are embedded in
# shell text and only usable as a low-confidence regex fallback (spec 5.1).
CLEAN_PATH_TOOLS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "path",
    "Glob": "path",
}
SHELL_TOOLS = {"Bash": "command", "PowerShell": "command"}

# Outcome vocabulary - formalises the title-prefix habit already in use (spec 5.2).
OUTCOME_PREFIXES = {
    "DONE": "shipped",
    "DEAD": "abandoned",
    "LESSON": "salvaged",
}

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    path        TEXT PRIMARY KEY,
    mtime       REAL,
    size        INTEGER,
    machine     TEXT,
    surface     TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    machine             TEXT,
    ai_title            TEXT,
    custom_title        TEXT,
    first_ts            TEXT,
    last_ts             TEXT,
    clean_paths         TEXT,   -- json list
    shell_paths         TEXT,   -- json list
    project             TEXT,
    purpose             TEXT,
    outcome             TEXT,
    attribution_method  TEXT,
    confidence          TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    session_id          TEXT,
    ts                  TEXT,
    machine             TEXT,
    surface             TEXT,
    source_file         TEXT,

    provider            TEXT,
    model               TEXT,
    modality            TEXT,
    unit                TEXT,
    cost_basis          TEXT,

    input_tokens            INTEGER,
    output_tokens           INTEGER,
    cache_creation_tokens   INTEGER,
    cache_read_tokens       INTEGER,
    cache_creation_1h       INTEGER,
    cache_creation_5m       INTEGER,
    qty                     REAL,
    service_tier            TEXT,
    web_search_requests     INTEGER,
    web_fetch_requests      INTEGER,

    is_sidechain        INTEGER,
    project             TEXT,
    purpose             TEXT,
    outcome             TEXT,
    attribution_method  TEXT,
    confidence          TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_source  ON events(source_file);
"""

EXPORT_COLUMNS = [
    "event_id", "session_id", "ts", "machine", "surface", "source_file",
    "provider", "model", "modality", "unit", "cost_basis",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_creation_1h", "cache_creation_5m", "qty", "service_tier",
    "web_search_requests", "web_fetch_requests",
    "is_sidechain", "project", "purpose", "outcome",
    "attribution_method", "confidence",
]


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'|;,()<>]{3,}")


def norm_path(p: str) -> str | None:
    """Normalise to 'C:\\a\\b' form. Returns None if it is not a usable abs path."""
    if not isinstance(p, str) or len(p) < 4:
        return None
    p = p.strip().strip("\"'")
    p = p.replace("/", os.sep if os.sep == "\\" else "/")
    p = p.replace("/", "\\")
    if not re.match(r"^[A-Za-z]:\\", p):
        return None
    return p[0].upper() + p[1:]


def paths_from_shell(cmd: str) -> list[str]:
    out = []
    for m in _WIN_PATH_RE.finditer(cmd or ""):
        n = norm_path(m.group(0))
        if n:
            out.append(n)
    return out


# --------------------------------------------------------------------------- #
# projects lookup
# --------------------------------------------------------------------------- #

def load_projects(path: str) -> list[tuple[str, str, str, int]]:
    """Return [(prefix_lower, project, purpose, tier)] sorted longest-prefix-first.

    tier 1 = a real project signal.
    tier 2 = infrastructure (memory, config, sandbox, scratch). These are touched by
    almost every session regardless of subject, so they must never outvote a tier-1
    path - they only win when a session touched nothing else. This is the same
    failure as attributing by `cwd`, one level down.
    """
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            pre = (r.get("path_prefix") or "").strip()
            if not pre or pre.startswith("#"):
                continue
            try:
                tier = int((r.get("tier") or "1").strip() or 1)
            except ValueError:
                tier = 1
            rows.append((pre.replace("/", "\\").lower().rstrip("\\"),
                         (r.get("project") or "").strip(),
                         (r.get("purpose") or "").strip(),
                         tier))
    rows.sort(key=lambda t: len(t[0]), reverse=True)
    return rows


def match_project(p: str, table, tier: int) -> tuple[str, str] | None:
    lp = p.lower()
    for pre, proj, purpose, t in table:
        if lp.startswith(pre):
            return (proj, purpose) if t == tier else None
    return None


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def longpath(p: str) -> str:
    """Windows MAX_PATH escape. Cowork nests transcripts deep enough to exceed 260
    chars, and plain open() raises FileNotFoundError on them - a silent data loss
    if you only glob."""
    p = os.path.abspath(p)
    return "\\\\?\\" + p if len(p) > 250 and not p.startswith("\\\\?\\") else p


def find_transcripts(root: str) -> list[str]:
    """os.walk, NOT glob: glob's wildcards skip dot-directories, so a `**` pattern
    silently misses every transcript nested under a `.claude` folder. That is how
    8.9B tokens of Cowork burn stayed invisible."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def parse_file(path: str):
    """Yield ('event', dict) and ('meta', dict) items for one transcript file."""
    with open(longpath(path), encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue

            t = d.get("type")
            # Field-name drift between surfaces: Claude Code writes `sessionId` and
            # `timestamp`; Cowork's audit.jsonl writes `session_id` and
            # `_audit_timestamp`. Reading only the camelCase pair silently drops the
            # date AND the attribution key on every Cowork record.
            sid = d.get("sessionId") or d.get("session_id")
            rec_ts = d.get("timestamp") or d.get("_audit_timestamp")

            if t == "ai-title" and sid:
                yield "meta", {"session_id": sid, "ai_title": d.get("aiTitle")}
                continue
            if t == "custom-title" and sid:
                yield "meta", {"session_id": sid, "custom_title": d.get("customTitle")}
                continue

            msg = d.get("message") or {}
            content = msg.get("content")

            # tool-use paths (any record type that carries them)
            if isinstance(content, list) and sid:
                clean, shell = [], []
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name = b.get("name")
                    inp = b.get("input") or {}
                    key = CLEAN_PATH_TOOLS.get(name)
                    if key:
                        n = norm_path(inp.get(key))
                        if n:
                            clean.append(n)
                    skey = SHELL_TOOLS.get(name)
                    if skey:
                        shell.extend(paths_from_shell(inp.get(skey)))
                if clean or shell:
                    yield "meta", {"session_id": sid,
                                   "clean_paths": clean, "shell_paths": shell}

            if t != "assistant":
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            model = msg.get("model")
            if model == "<synthetic>":          # spec 8 - never counted
                continue

            cc = usage.get("cache_creation") or {}
            stu = usage.get("server_tool_use") or {}
            ts = rec_ts

            yield "event", {
                "event_id": d.get("uuid") or f"{sid}:{ts}:{d.get('requestId')}",
                "session_id": sid,
                "ts": ts,
                "source_file": path,
                "provider": "anthropic",
                "model": model,
                "modality": "text",
                "unit": "tokens",
                "cost_basis": "subscription",
                # NB: usage['iterations'] repeats these; reading top level only (spec 8)
                "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "cache_creation_tokens": usage.get("cache_creation_input_tokens") or 0,
                "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
                "cache_creation_1h": cc.get("ephemeral_1h_input_tokens") or 0,
                "cache_creation_5m": cc.get("ephemeral_5m_input_tokens") or 0,
                "qty": None,
                "service_tier": usage.get("service_tier"),
                "web_search_requests": stu.get("web_search_requests") or 0,
                "web_fetch_requests": stu.get("web_fetch_requests") or 0,
                "is_sidechain": 1 if d.get("isSidechain") else 0,
            }
            if ts:
                yield "meta", {"session_id": sid, "ts": ts}


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #

def derive_outcome(custom_title: str | None) -> tuple[str | None, str | None]:
    if not custom_title:
        return None, None
    first = custom_title.strip().split()
    if not first:
        return None, None
    w = first[0].upper().strip(":-")
    if w in OUTCOME_PREFIXES:
        return OUTCOME_PREFIXES[w], "title-prefix"
    return None, None


def derive_project(clean, shell, table):
    """Modal project across the paths a session touched (spec 5.1).

    Preference order, first non-empty wins:
      tier-1 clean paths -> tier-1 shell paths -> tier-2 clean -> tier-2 shell.
    Infrastructure (tier 2) is a last resort, never a vote against real work.
    """
    if not table:
        return None, None, "none", "none"

    def vote(paths, tier):
        v = collections.Counter()
        for p in paths:
            m = match_project(p, table, tier)
            if m:
                v[m] += 1
        return v

    for paths, tier, method, conf in (
            (clean, 1, "file-paths", "high"),
            (shell, 1, "bash-regex", "low"),
            (clean, 2, "file-paths-infra", "low"),
            (shell, 2, "bash-regex-infra", "low"),
    ):
        v = vote(paths, tier)
        if v:
            (proj, purpose), _ = v.most_common(1)[0]
            return proj, purpose, method, conf
    return None, None, "none", "none"


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #

def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    return con


def ingest(con, roots: list[tuple[str, str]], machine: str, projects_path: str,
           rebuild=False):
    """roots: [(path, surface_label), ...]"""
    files: list[tuple[str, str]] = []
    for root, surface in roots:
        if os.path.isdir(root):
            files += [(f, surface) for f in find_transcripts(root)]
    known = {r[0]: (r[1], r[2]) for r in
             con.execute("SELECT path, mtime, size FROM sources")}

    meta: dict[str, dict] = {}
    changed = 0
    for path, surface in files:
        st = os.stat(longpath(path))
        if not rebuild and known.get(path) == (st.st_mtime, st.st_size):
            continue
        changed += 1
        con.execute("DELETE FROM events WHERE source_file=?", (path,))
        rows = []
        for kind, item in parse_file(path):
            if kind == "event":
                item["machine"] = machine
                item["surface"] = surface
                rows.append(item)
            else:
                sid = item.get("session_id")
                if not sid:
                    continue
                m = meta.setdefault(sid, {"clean": set(), "shell": set(),
                                          "ts": [], "ai": None, "custom": None})
                if item.get("ai_title"):
                    m["ai"] = item["ai_title"]
                if item.get("custom_title"):
                    m["custom"] = item["custom_title"]
                m["clean"].update(item.get("clean_paths") or [])
                m["shell"].update(item.get("shell_paths") or [])
                if item.get("ts"):
                    m["ts"].append(item["ts"])
        if rows:
            cols = ["event_id", "session_id", "ts", "machine", "surface", "source_file",
                    "provider", "model", "modality", "unit", "cost_basis",
                    "input_tokens", "output_tokens", "cache_creation_tokens",
                    "cache_read_tokens", "cache_creation_1h", "cache_creation_5m",
                    "qty", "service_tier", "web_search_requests",
                    "web_fetch_requests", "is_sidechain"]
            con.executemany(
                f"INSERT OR REPLACE INTO events ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [[r.get(c) for c in cols] for r in rows])
        con.execute(
            "INSERT OR REPLACE INTO sources (path, mtime, size, machine, surface) "
            "VALUES (?,?,?,?,?)", (path, st.st_mtime, st.st_size, machine, surface))

    # merge session metadata
    for sid, m in meta.items():
        row = con.execute(
            "SELECT ai_title, custom_title, clean_paths, shell_paths, first_ts, last_ts "
            "FROM sessions WHERE session_id=?", (sid,)).fetchone()
        clean, shell = set(m["clean"]), set(m["shell"])
        ai, custom = m["ai"], m["custom"]
        ts = sorted(m["ts"])
        first = ts[0] if ts else None
        last = ts[-1] if ts else None
        if row:
            ai = ai or row[0]
            custom = custom or row[1]
            clean |= set(json.loads(row[2] or "[]"))
            shell |= set(json.loads(row[3] or "[]"))
            first = min([x for x in (first, row[4]) if x], default=None)
            last = max([x for x in (last, row[5]) if x], default=None)
        con.execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_id, machine, ai_title, custom_title, first_ts, last_ts, "
            " clean_paths, shell_paths) VALUES (?,?,?,?,?,?,?,?)",
            (sid, machine, ai, custom, first, last,
             json.dumps(sorted(clean)), json.dumps(sorted(shell))))

    con.commit()
    reattribute(con, projects_path)
    return changed, len(files)


def reattribute(con, projects_path: str):
    """Re-derive project/purpose/outcome for every session, then push onto events."""
    table = load_projects(projects_path)
    for sid, clean_j, shell_j, custom in con.execute(
            "SELECT session_id, clean_paths, shell_paths, custom_title FROM sessions"):
        clean = json.loads(clean_j or "[]")
        shell = json.loads(shell_j or "[]")
        proj, purpose, method, conf = derive_project(clean, shell, table)
        outcome, o_method = derive_outcome(custom)
        if outcome and method == "none":
            method = o_method
        con.execute(
            "UPDATE sessions SET project=?, purpose=?, outcome=?, "
            "attribution_method=?, confidence=? WHERE session_id=?",
            (proj, purpose, outcome, method, conf, sid))
    con.execute("""
        UPDATE events SET
            project            = (SELECT project            FROM sessions s WHERE s.session_id=events.session_id),
            purpose            = (SELECT purpose            FROM sessions s WHERE s.session_id=events.session_id),
            outcome            = (SELECT outcome            FROM sessions s WHERE s.session_id=events.session_id),
            attribution_method = (SELECT attribution_method FROM sessions s WHERE s.session_id=events.session_id),
            confidence         = (SELECT confidence         FROM sessions s WHERE s.session_id=events.session_id)
        WHERE EXISTS (SELECT 1 FROM sessions s WHERE s.session_id=events.session_id)
    """)
    # NB: the WHERE EXISTS guard matters. Events imported from the other machine
    # arrive already attributed but bring no `sessions` rows, so an unguarded
    # UPDATE would null out exactly the data the merge exists to carry.
    con.commit()


# --------------------------------------------------------------------------- #
# reporting / portability
# --------------------------------------------------------------------------- #

def total_expr(alias="") -> str:
    a = f"{alias}." if alias else ""
    return " + ".join(f"COALESCE({a}{f},0)" for f in TOKEN_FIELDS)


def analyze_paths(con, depth_max=7, floor=8):
    seg = collections.Counter()
    for (cj,) in con.execute("SELECT clean_paths FROM sessions"):
        for p in json.loads(cj or "[]"):
            parts = [x for x in p.split("\\") if x]
            for n in range(2, depth_max + 1):
                if len(parts) >= n:
                    seg["\\".join(parts[:n])] += 1
    print("# path prefixes seen in clean tool paths (count >= %d)" % floor)
    print("# paste the useful ones into projects.csv")
    for k, v in seg.most_common():
        if v >= floor:
            print(f"{v:6}  {k}")


def stats(con):
    t = total_expr()
    g = con.execute(f"SELECT COUNT(*), SUM({t}) FROM events").fetchone()
    print(f"events {g[0]:,}   total tokens {(g[1] or 0):,}")
    for label, col in (("machine", "machine"), ("surface", "surface"), ("model", "model"),
                       ("purpose", "purpose"), ("outcome", "outcome"),
                       ("attribution", "attribution_method")):
        print(f"\nby {label}:")
        for row in con.execute(
                f"SELECT COALESCE({col},'(null)'), SUM({t}) v FROM events "
                f"GROUP BY 1 ORDER BY v DESC"):
            print(f"  {row[0]:<28} {row[1]:>15,}")
    print("\nby component:")
    for f in TOKEN_FIELDS:
        v = con.execute(f"SELECT SUM({f}) FROM events").fetchone()[0] or 0
        print(f"  {f:<28} {v:>15,}  {100*v/(g[1] or 1):5.1f}%")


def export(con, out_path: str):
    rows = con.execute(f"SELECT {','.join(EXPORT_COLUMNS)} FROM events").fetchall()
    with gzip.open(out_path, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        w.writerows(rows)
    print(f"exported {len(rows):,} events -> {out_path}")


def import_(con, in_path: str):
    with gzip.open(in_path, "rt", newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        rows = [[(row.get(c) or None) for c in EXPORT_COLUMNS] for row in r]
    con.executemany(
        f"INSERT OR REPLACE INTO events ({','.join(EXPORT_COLUMNS)}) "
        f"VALUES ({','.join('?' * len(EXPORT_COLUMNS))})", rows)
    con.commit()
    machines = con.execute("SELECT machine, COUNT(*) FROM events GROUP BY 1").fetchall()
    print(f"imported {len(rows):,} events from {in_path}")
    print("machines now in store:", dict(machines))


# --------------------------------------------------------------------------- #

def roots(a) -> list[tuple[str, str]]:
    r = [(a.root, "claude-code")]
    if not a.no_cowork:
        r.append((a.cowork_root, "cowork"))
    return r


def main():
    ap = argparse.ArgumentParser(description="Token dashboard extractor")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="Claude Code transcripts")
    ap.add_argument("--cowork-root", default=DEFAULT_COWORK,
                    help="Cowork / local-agent-mode session transcripts")
    ap.add_argument("--no-cowork", action="store_true")
    ap.add_argument("--projects", default=DEFAULT_PROJECTS)
    ap.add_argument("--machine", default=socket.gethostname())
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--analyze-paths", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--reattribute", action="store_true",
                    help="re-derive attribution only (after editing projects.csv)")
    ap.add_argument("--export")
    ap.add_argument("--import", dest="import_path")
    a = ap.parse_args()

    con = connect(a.db)

    if a.import_path:
        import_(con, a.import_path)
        return
    if a.analyze_paths:
        if not con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
            ingest(con, roots(a), a.machine, a.projects)
        analyze_paths(con)
        return
    if a.reattribute:
        reattribute(con, a.projects)
        print("re-attributed.")
        stats(con)
        return
    if a.stats:
        stats(con)
        return

    changed, total = ingest(con, roots(a), a.machine, a.projects, rebuild=a.rebuild)
    print(f"machine={a.machine}  files={total}  reparsed={changed}")
    if a.export:
        export(con, a.export)
    stats(con)


if __name__ == "__main__":
    main()
