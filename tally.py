"""Hook entry point: keep events.db and dashboard.html current as Claude Code is used.

Called by a Stop hook in ~/.claude/settings.json (async, so it never blocks a turn).
Cheap by design:
  - debounced: skips if a run finished < DEBOUNCE_S ago
  - single-flight: skips if another run holds the lock
  - incremental: extract.py only re-reads transcripts whose mtime/size changed
Stdlib only. Never prints to stdout (hook output would land in the transcript).
"""
import io, os, subprocess, sys, time

HERE       = os.path.dirname(os.path.abspath(__file__))
PY         = sys.executable
STAMP      = os.path.join(HERE, ".tally.last")
LOCK       = os.path.join(HERE, ".tally.lock")
LOG        = os.path.join(HERE, "tally.log")
DEBOUNCE_S = 60
STALE_LOCK = 600   # seconds; a lock older than this is from a crashed run

# Second-machine handoff. Set TOKEN_DASHBOARD_IMPORT to the drop file another machine exports
# to (see README); when its mtime/size changes, import it before the local scan. import is
# INSERT OR REPLACE on event_id, so re-importing the same file is harmless.
BIGBOX_EXPORT = os.path.expanduser(os.environ.get("TOKEN_DASHBOARD_IMPORT", ""))
IMPORT_STAMP  = os.path.join(HERE, ".tally.import")

def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

def maybe_import_bigbox():
    if not os.path.exists(BIGBOX_EXPORT):
        return
    st = os.stat(BIGBOX_EXPORT)
    sig = f"{st.st_mtime_ns}:{st.st_size}"
    try:
        if io.open(IMPORT_STAMP, encoding="utf-8").read().strip() == sig:
            return
    except OSError:
        pass
    r = subprocess.run([PY, os.path.join(HERE, "extract.py"), "--import", BIGBOX_EXPORT],
                       cwd=HERE, capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        with io.open(IMPORT_STAMP, "w", encoding="utf-8") as f:
            f.write(sig)
        log("imported big-box export: " + (r.stdout.strip().splitlines() or [""])[-1])
    else:
        log("FAIL big-box import: " + r.stderr.strip()[-800:])


def main():
    force = "--force" in sys.argv
    now = time.time()
    if not force and os.path.exists(STAMP) and now - os.path.getmtime(STAMP) < DEBOUNCE_S:
        return 0
    if os.path.exists(LOCK):
        if now - os.path.getmtime(LOCK) < STALE_LOCK:
            return 0
        os.remove(LOCK)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return 0
    try:
        t0 = time.time()
        maybe_import_bigbox()
        # claude.ai data exports (activity only, no tokens) - incremental on file mtime/size,
        # so this is a handful of stat() calls unless a new export was dropped in the folder.
        r0 = subprocess.run([PY, os.path.join(HERE, "claude_export.py")], cwd=HERE,
                            capture_output=True, text=True, timeout=300)
        if r0.returncode != 0:
            log("FAIL claude_export: " + r0.stderr.strip()[-800:])
        elif "ingested" in r0.stdout:
            log("claude.ai export: " + " | ".join(l for l in r0.stdout.splitlines() if l.startswith("ingested")))
        r1 = subprocess.run([PY, os.path.join(HERE, "extract.py")], cwd=HERE,
                            capture_output=True, text=True, timeout=300)
        r2 = subprocess.run([PY, os.path.join(HERE, "render.py")], cwd=HERE,
                            capture_output=True, text=True, timeout=120)
        ok = r1.returncode == 0 and r2.returncode == 0
        tail = (r2.stdout.strip().splitlines() or [""])[-1]
        log(f"{'ok' if ok else 'FAIL'} {time.time()-t0:.1f}s  {tail}")
        if not ok:
            log("  extract stderr: " + r1.stderr.strip()[-800:])
            log("  render stderr: " + r2.stderr.strip()[-800:])
        with open(STAMP, "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
        return 0 if ok else 1
    except Exception as e:
        log(f"FAIL exception {e!r}")
        return 1
    finally:
        try: os.remove(LOCK)
        except OSError: pass

if __name__ == "__main__":
    sys.exit(main())
