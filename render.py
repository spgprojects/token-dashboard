#!/usr/bin/env python3
"""
Token Dashboard — private view renderer.

Reads events.db and writes a self-contained dashboard.html.
Stdlib only. No network, no CDN.

  python render.py                 -> dashboard.html
  python render.py --out other.html
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import math
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "events.db")
DEFAULT_OUT = os.path.join(HERE, "dashboard.html")

TOTAL = ("COALESCE(input_tokens,0)+COALESCE(output_tokens,0)"
         "+COALESCE(cache_creation_tokens,0)+COALESCE(cache_read_tokens,0)")
NEWWORK = ("COALESCE(input_tokens,0)+COALESCE(output_tokens,0)"
           "+COALESCE(cache_creation_tokens,0)")

PURPOSE_COLORS = {
    "client":      "#3f7fb0",
    "product":     "#4c9a7a",
    "content":     "#b08a3f",
    "internal-rd": "#8a6fb0",
    "ops":         "#7a7a7a",
    "personal":    "#b06f6f",
    "(unattributed)": "#4a4a4a",
}
SURFACE_COLORS = {"cowork": "#8a6fb0", "claude-code": "#3f7fb0", "claude-ai-chat": "#b08a3f"}
ACTIVITY_FILL = "rgba(176,138,63,0.55)"   # amber: a day with activity but no token data

OUTCOME_LABEL = {
    "shipped":   ("shipped",   "#4c9a7a"),
    "salvaged":  ("salvaged",  "#b08a3f"),
    "abandoned": ("abandoned", "#b05f5f"),
    None:        ("untagged",  "#4a4a4a"),
}


def fmt(n) -> str:
    n = int(n or 0)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(n) >= div:
            return f"{n/div:,.2f}{suf}".replace(".00", "")
    return f"{n:,}"


def pct(a, b) -> str:
    return f"{100*a/b:.1f}%" if b else "—"


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #

# Two kinds of row live in `events`. METERED rows carry a real usage object (unit='tokens').
# ACTIVITY rows come from the claude.ai data export (unit='messages', tokens NULL) — the
# export carries no token fields, so those rows may count as activity and never as burn.
# Every token sum below is restricted to METERED; nothing here ever sums the two.
METERED = "(unit IS NULL OR unit='tokens')"
ACTIVITY = "unit='messages'"


def gather(con):
    d = {}
    row = con.execute(
        f"SELECT COUNT(*), SUM({TOTAL}), SUM({NEWWORK}), MIN(date(ts)), MAX(date(ts)),"
        f" COUNT(DISTINCT session_id) FROM events WHERE ts IS NOT NULL AND {METERED}").fetchone()
    d["events"], d["total"], d["newwork"] = row[0], row[1] or 0, row[2] or 0
    d["first"], d["last"], d["sessions"] = row[3], row[4], row[5]

    # activity-only tier (claude.ai chat export): messages per day, no tokens
    d["activity"] = {r[0]: r[1] for r in con.execute(
        f"SELECT date(ts), COUNT(*) FROM events WHERE ts IS NOT NULL AND {ACTIVITY} GROUP BY 1")}
    d["activity_span"] = con.execute(
        f"SELECT MIN(date(ts)), MAX(date(ts)), COUNT(*), COUNT(DISTINCT session_id),"
        f" COUNT(DISTINCT date(ts)) FROM events WHERE ts IS NOT NULL AND {ACTIVITY}").fetchone()
    d["first_any"] = min(x for x in (d["first"], d["activity_span"][0]) if x) if \
        (d["first"] or d["activity_span"][0]) else d["first"]
    d["last_any"] = max(x for x in (d["last"], d["activity_span"][1]) if x) if \
        (d["last"] or d["activity_span"][1]) else d["last"]

    d["components"] = [(c, con.execute(f"SELECT SUM({c}) FROM events WHERE {METERED}").fetchone()[0] or 0)
                       for c in ("cache_read_tokens", "cache_creation_tokens",
                                 "output_tokens", "input_tokens")]

    d["daily"] = {r[0]: r[1] for r in con.execute(
        f"SELECT date(ts), SUM({TOTAL}) FROM events WHERE ts IS NOT NULL AND {METERED} GROUP BY 1")}

    d["by_purpose"] = con.execute(
        f"SELECT COALESCE(purpose,'(unattributed)'), SUM({TOTAL}) v FROM events "
        f"WHERE {METERED} GROUP BY 1 ORDER BY v DESC").fetchall()

    d["by_model"] = con.execute(
        f"SELECT model, SUM({TOTAL}) v FROM events WHERE {METERED} GROUP BY 1 ORDER BY v DESC").fetchall()

    # (surface, tokens, rows) — activity surfaces show 0 tokens and a message count
    d["by_surface"] = con.execute(
        f"SELECT COALESCE(surface,'?'), SUM(CASE WHEN {METERED} THEN {TOTAL} ELSE 0 END) v,"
        f" COUNT(*) FROM events GROUP BY 1 ORDER BY v DESC").fetchall()

    d["surface_span"] = con.execute(
        "SELECT COALESCE(surface,'?'), MIN(date(ts)), MAX(date(ts)), "
        "COUNT(DISTINCT date(ts)) FROM events WHERE ts IS NOT NULL "
        "GROUP BY 1 ORDER BY 2").fetchall()

    d["by_machine"] = con.execute(
        f"SELECT COALESCE(machine,'?'), SUM({TOTAL}) v FROM events "
        f"WHERE {METERED} GROUP BY 1 ORDER BY v DESC").fetchall()

    d["by_outcome"] = con.execute(
        f"SELECT outcome, SUM({TOTAL}) v FROM events WHERE {METERED} GROUP BY 1 ORDER BY v DESC").fetchall()

    d["by_project"] = con.execute(
        f"SELECT COALESCE(project,'(unattributed)'), COALESCE(purpose,'(unattributed)'),"
        f" SUM({TOTAL}) v, COUNT(DISTINCT session_id),"
        f" SUM(CASE WHEN outcome='shipped'   THEN {TOTAL} ELSE 0 END),"
        f" SUM(CASE WHEN outcome='salvaged'  THEN {TOTAL} ELSE 0 END),"
        f" SUM(CASE WHEN outcome='abandoned' THEN {TOTAL} ELSE 0 END)"
        f" FROM events WHERE {METERED} GROUP BY 1,2 ORDER BY v DESC").fetchall()

    d["sidechain"] = con.execute(
        f"SELECT is_sidechain, SUM({TOTAL}) FROM events WHERE {METERED} GROUP BY 1").fetchall()

    d["attribution"] = con.execute(
        f"SELECT COALESCE(attribution_method,'none'), SUM({TOTAL}) v FROM events "
        f"WHERE {METERED} GROUP BY 1 ORDER BY v DESC").fetchall()

    # top days, with the activity in them
    top = con.execute(
        f"SELECT date(ts) d, SUM({TOTAL}) v FROM events WHERE ts IS NOT NULL AND {METERED} "
        f"GROUP BY 1 ORDER BY v DESC LIMIT 10").fetchall()
    d["top_days"] = []
    for day, v in top:
        acts = con.execute(
            f"SELECT COALESCE(e.project,'(unattributed)') p, SUM({TOTAL}) sv,"
            f" GROUP_CONCAT(DISTINCT COALESCE(s.custom_title, s.ai_title))"
            f" FROM events e LEFT JOIN sessions s ON s.session_id=e.session_id"
            f" WHERE date(e.ts)=? AND {METERED} GROUP BY 1 ORDER BY sv DESC LIMIT 4", (day,)).fetchall()
        d["top_days"].append((day, v, acts))

    # sessions table (largest first)
    d["sessions_top"] = con.execute(
        f"SELECT COALESCE(s.custom_title, s.ai_title, '(untitled)'),"
        f" COALESCE(e.project,'(unattributed)'), e.outcome, SUM({TOTAL}) v,"
        f" MIN(date(e.ts)), e.confidence"
        f" FROM events e LEFT JOIN sessions s ON s.session_id=e.session_id"
        f" WHERE {METERED} GROUP BY e.session_id ORDER BY v DESC LIMIT 25").fetchall()

    # the pre-metering era, as conversations — what the export can tell us
    d["activity_convs"] = con.execute(
        f"SELECT COALESCE(s.custom_title,'(untitled)'), COUNT(*), MIN(date(e.ts)), s.outcome"
        f" FROM events e JOIN sessions s ON s.session_id=e.session_id"
        f" WHERE {ACTIVITY} GROUP BY e.session_id ORDER BY 3 LIMIT 12").fetchall()
    return d


# --------------------------------------------------------------------------- #
# contribution grid
# --------------------------------------------------------------------------- #

def grid_svg(daily: dict[str, int], activity: dict[str, int] | None = None) -> str:
    """GitHub-style grid, log10 colour scale (spec 6 - linear breaks at this range).

    `activity` = days with claude.ai-chat messages but no token data. They render amber
    at fixed intensity — presence, not magnitude — and never enter the blue scale.
    """
    activity = activity or {}
    if not daily and not activity:
        return "<p>no data</p>"
    days = sorted(set(daily) | set(activity))
    d0 = dt.date.fromisoformat(days[0])
    d1 = dt.date.fromisoformat(days[-1])
    start = d0 - dt.timedelta(days=(d0.weekday() + 1) % 7)   # back to Sunday
    vals = [v for v in daily.values() if v > 0] or [1]
    lo, hi = math.log10(min(vals)), math.log10(max(vals))
    span = (hi - lo) or 1.0

    CELL, GAP = 13, 3
    weeks = ((d1 - start).days // 7) + 1
    W = weeks * (CELL + GAP) + 40
    H = 7 * (CELL + GAP) + 34

    cells, months, seen = [], [], set()
    for w in range(weeks):
        for wd in range(7):
            day = start + dt.timedelta(days=w * 7 + wd)
            if day > d1:
                continue
            key = day.isoformat()
            v = daily.get(key, 0)
            x, y = 34 + w * (CELL + GAP), 22 + wd * (CELL + GAP)
            a = activity.get(key, 0)
            if v > 0:
                t = (math.log10(v) - lo) / span
                fill = f"rgba(88,166,255,{0.16 + 0.84*t:.3f})"
                tip = f"{key}  {fmt(v)}" + (f"  + {a} chat msgs (unmetered)" if a else "")
            elif a > 0:
                fill = ACTIVITY_FILL
                tip = f"{key}  {a} claude.ai chat messages — no token data"
            else:
                fill = "var(--cell-empty)"
                tip = f"{key}  0"
            cells.append(
                f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="2.5" '
                f'fill="{fill}"><title>{tip}</title></rect>')
            if day.day <= 7 and (day.year, day.month) not in seen:
                seen.add((day.year, day.month))
                months.append(f'<text class="ax" x="{x}" y="14">'
                              f'{day.strftime("%b")}</text>')
    for i, lbl in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        months.append(f'<text class="ax" x="0" y="{22+i*(CELL+GAP)+10}">{lbl}</text>')

    # legend
    leg = []
    for i in range(5):
        t = i / 4
        leg.append(f'<rect x="{i*17}" y="0" width="13" height="13" rx="2.5" '
                   f'fill="rgba(88,166,255,{0.16+0.84*t:.3f})"/>')
    legend = (f'<svg width="150" height="15" class="legend">{"".join(leg)}</svg>')

    return (f'<div class="gridwrap"><svg viewBox="0 0 {W} {H}" width="{W}" '
            f'height="{H}">{"".join(months)}{"".join(cells)}</svg></div>'
            f'<div class="legendrow"><span class="dim">less</span>{legend}'
            f'<span class="dim">more &nbsp;·&nbsp; log&#8321;&#8320; scale, '
            f'{fmt(min(vals))} – {fmt(max(vals))} per day</span>'
            + (f'&nbsp;&nbsp;<svg width="13" height="13" class="legend"><rect width="13" height="13" '
               f'rx="2.5" fill="{ACTIVITY_FILL}"/></svg><span class="dim">claude.ai chat — '
               f'activity only, no token data ({len(activity)} days)</span>' if activity else "")
            + '</div>')


# --------------------------------------------------------------------------- #
# html
# --------------------------------------------------------------------------- #

CSS = """
:root{
  --bg:#0f1216; --panel:#161b22; --line:#242c37; --fg:#d7dde5; --dim:#8b96a5;
  --cell-empty:#1b2129; --accent:#58a6ff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  padding:32px clamp(16px,4vw,56px);max-width:1180px;margin:0 auto}
h1{font-size:20px;font-weight:600;margin:0 0 2px;letter-spacing:-.01em}
h2{font-size:13px;font-weight:600;margin:34px 0 12px;text-transform:uppercase;
  letter-spacing:.09em;color:var(--dim)}
.sub{color:var(--dim);font-size:13px;margin-bottom:26px}
.hero{display:flex;flex-wrap:wrap;gap:34px;align-items:flex-end;
  padding:22px 24px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.big{font-size:40px;font-weight:650;letter-spacing:-.02em;line-height:1}
.big small{font-size:14px;font-weight:400;color:var(--dim);letter-spacing:0}
.kv{font-size:12px;color:var(--dim)}
.kv b{display:block;font-size:19px;color:var(--fg);font-weight:600}
.note{border-left:2px solid var(--accent);padding:9px 14px;margin:14px 0;
  background:#131a24;color:#b9c4d2;font-size:13px;border-radius:0 6px 6px 0}
.warn{border-left-color:#b08a3f;background:#1d1a13}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:600;color:var(--dim);font-size:11px;
  text-transform:uppercase;letter-spacing:.07em;padding:7px 10px;
  border-bottom:1px solid var(--line)}
td{padding:7px 10px;border-bottom:1px solid #1b212a;vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:hover td{background:#141a22}
.bar{height:9px;border-radius:2px;display:block;min-width:2px}
.stack{display:flex;height:26px;border-radius:5px;overflow:hidden;margin:4px 0 8px}
.stack span{display:block}
.chip{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
  border:1px solid var(--line);color:var(--dim)}
.dim{color:var(--dim)}
.small{font-size:12px}
.gridwrap{overflow-x:auto;padding:6px 0}
.ax{fill:var(--dim);font-size:9px}
.legendrow{display:flex;align-items:center;gap:10px;margin-top:6px;font-size:11px}
.legend{vertical-align:middle}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:26px}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--dim);font-size:12px}
"""


def bar_table(rows, total, color_map=None, label_hdr="", extra_hdr=None):
    out = [f'<table><tr><th>{label_hdr}</th><th class="n">tokens</th>'
           f'<th class="n">share</th><th style="width:34%"></th></tr>']
    for r in rows:
        name, v = r[0], r[1]
        c = (color_map or {}).get(name, "#3f7fb0")
        w = 100 * v / total if total else 0
        out.append(
            f'<tr><td>{html.escape(str(name))}</td><td class="n">{fmt(v)}</td>'
            f'<td class="n dim">{pct(v,total)}</td>'
            f'<td><span class="bar" style="width:{max(w,0.6):.2f}%;'
            f'background:{c}"></span></td></tr>')
    out.append("</table>")
    return "".join(out)


def render(d: dict) -> str:
    total = d["total"] or 1
    attributed = sum(v for k, v in d["by_purpose"] if k != "(unattributed)")
    tagged = sum(v for k, v in d["by_outcome"] if k)

    comp_stack = "".join(
        f'<span style="width:{100*v/total:.3f}%;background:{c}" title="{n} {fmt(v)}"></span>'
        for (n, v), c in zip(d["components"],
                             ("#2d4a63", "#3f7fb0", "#58a6ff", "#8fc7ff")))

    # top days
    td = ['<table><tr><th>day</th><th class="n">tokens</th><th>what was in it</th></tr>']
    for day, v, acts in d["top_days"]:
        bits = []
        for p, sv, titles in acts:
            t = (titles or "").split(",")
            t = [x for x in t if x][:2]
            lbl = html.escape(p)
            if t:
                lbl += ' <span class="dim">— ' + html.escape(" · ".join(t)) + "</span>"
            bits.append(f"{lbl} <span class='dim'>({fmt(sv)})</span>")
        td.append(f'<tr><td>{day}</td><td class="n">{fmt(v)}</td>'
                  f'<td class="small">{"<br>".join(bits)}</td></tr>')
    td.append("</table>")

    # projects
    pt = ['<table><tr><th>project</th><th>purpose</th><th class="n">tokens</th>'
          '<th class="n">share</th><th class="n">sessions</th><th>outcome mix</th></tr>']
    for name, purpose, v, ns, sh, sa, ab in d["by_project"]:
        un = max(v - sh - sa - ab, 0)
        mix = "".join(
            f'<span style="width:{100*x/v if v else 0:.2f}%;background:{c}" title="{lbl} {fmt(x)}"></span>'
            for x, (lbl, c) in ((sh, OUTCOME_LABEL["shipped"]),
                                (sa, OUTCOME_LABEL["salvaged"]),
                                (ab, OUTCOME_LABEL["abandoned"]),
                                (un, OUTCOME_LABEL[None])))
        pt.append(
            f'<tr><td>{html.escape(name)}</td>'
            f'<td><span class="chip" style="border-color:{PURPOSE_COLORS.get(purpose,"#444")}">'
            f'{html.escape(purpose)}</span></td>'
            f'<td class="n">{fmt(v)}</td><td class="n dim">{pct(v,total)}</td>'
            f'<td class="n dim">{ns}</td>'
            f'<td><div class="stack" style="height:11px;margin:3px 0">{mix}</div></td></tr>')
    pt.append("</table>")

    # sessions
    st = ['<table><tr><th>session</th><th>project</th><th>outcome</th>'
          '<th class="n">tokens</th><th class="n">day</th></tr>']
    for title, proj, outcome, v, day, conf in d["sessions_top"]:
        lbl, c = OUTCOME_LABEL.get(outcome, OUTCOME_LABEL[None])
        st.append(
            f'<tr><td>{html.escape(title or "")}</td><td class="dim">{html.escape(proj)}</td>'
            f'<td><span class="chip" style="border-color:{c};color:{c}">{lbl}</span></td>'
            f'<td class="n">{fmt(v)}</td><td class="n dim">{day or ""}</td></tr>')
    st.append("</table>")

    side = {bool(k): v for k, v in d["sidechain"]}
    machines = " · ".join(f"{m} {fmt(v)}" for m, v in d["by_machine"])
    def _surface_line(s_, v, n_rows):
        span = next((f" · {a} → {b}, {n} days" for s2, a, b, n in d["surface_span"] if s2 == s_), "")
        if s_ == "claude-ai-chat":
            return (f"&nbsp;&nbsp;<code>{s_}</code> <b>activity only</b> — {n_rows:,} assistant "
                    f"messages, no token data{span}")
        return f"&nbsp;&nbsp;<code>{s_}</code> {fmt(v)} ({pct(v,total)}){span}"
    surface_lines = "<br>".join(_surface_line(*r) for r in d["by_surface"])
    cc_start = next((a for s2, a, b, n in d["surface_span"] if s2 == "claude-code"), "?")
    only_one = len(d["by_machine"]) == 1
    act_first, act_last, act_msgs, act_convs, act_days = d["activity_span"]
    active_days = len(set(d["daily"]) | set(d["activity"]))
    elapsed = (dt.date.fromisoformat(d["last_any"]) - dt.date.fromisoformat(d["first_any"])).days + 1
    # the pre-metering era, listed as conversations
    pre = ['<table><tr><th>conversation (claude.ai)</th><th class="n">assistant msgs</th>'
           '<th class="n">first day</th></tr>']
    for title, n_msgs, day, _o in d["activity_convs"]:
        pre.append(f'<tr><td>{html.escape(title)}</td><td class="n">{n_msgs}</td>'
                   f'<td class="n dim">{day}</td></tr>')
    pre.append("</table>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token Dashboard — private</title><style>{CSS}</style></head><body>

<h1>Token Dashboard <span class="dim" style="font-weight:400">· private instrument</span></h1>
<div class="sub">{d['first_any']} → {d['last_any']} · metered from {d['first']}: {d['sessions']:,} sessions,
{d['events']:,} metered messages · {machines}
{f" · before that, activity only: {act_convs} claude.ai conversations from {act_first}" if act_msgs else ""}</div>

<div class="hero">
  <div><div class="big">{fmt(d['total'])}<small> tokens</small></div>
       <div class="kv" style="margin-top:6px">grand total, counted not inferred</div>
       <div class="kv" style="margin-top:4px;color:#e0a040"><b>local sessions only — a floor, not a total.</b>
       Excludes claude.ai chat, server-side Cowork (<code>cse_</code> sessions) and the Cowork VM.</div></div>
  <div class="kv">new work<b>{fmt(d['newwork'])}</b>{pct(d['newwork'], total)} of total</div>
  <div class="kv">re-sent context<b>{fmt(total - d['newwork'])}</b>{pct(total-d['newwork'], total)}</div>
  <div class="kv">subagents<b>{fmt(side.get(True,0))}</b>{pct(side.get(True,0), total)}</div>
  <div class="kv">active days<b>{active_days}</b>of {elapsed} elapsed since {d['first_any']}</div>
</div>

<div class="stack" style="margin-top:14px">{comp_stack}</div>
<div class="small dim">
  cache_read {fmt(d['components'][0][1])} ({pct(d['components'][0][1],total)}) ·
  cache_creation {fmt(d['components'][1][1])} ·
  output {fmt(d['components'][2][1])} ·
  input {fmt(d['components'][3][1])}
</div>

<div class="note">
  <b>Read the denominator before the headline.</b>
  {pct(total-d['newwork'], total)} of the grand total is <code>cache_read</code> — context re-sent
  on every turn, not new work. Both numbers are real; only one is effort.
</div>

<h2>Daily burn</h2>
{grid_svg(d['daily'], d['activity'])}

<h2>Coverage — what this chart does and does not know</h2>
<div class="note warn">
  <b>Attributed to a project:</b> {pct(attributed, total)} of burn
  ({fmt(total-attributed)} unattributed).<br>
  <b>Tagged with an outcome:</b> {pct(tagged, total)} of burn
  ({fmt(total-tagged)} untagged) — outcome comes from a <code>DONE</code> /
  <code>DEAD</code> / <code>LESSON</code> prefix on the session title.<br>
  <b>Surfaces counted:</b> {surface_lines}<br>
  <b>Activity tier (amber on the grid):</b> the claude.ai data export covers {act_first} → {act_last}
  — {act_convs} conversations, {act_msgs:,} assistant messages over {act_days} days — but carries
  <i>no token fields at any nesting level</i>. Those rows are stored with <code>unit=messages</code>
  and NULL tokens; they appear as presence on the grid and nowhere in any token sum. A fresh export
  dropped into <code>SPG Ops\\05_AI_Library\\Claude\\</code> extends this tier automatically.<br>
  <b>Not counted — and not countable from local disk (established 02 Sep 26, both machines):</b>
  claude.ai web/mobile chat; <b>server-side Cowork</b> sessions (<code>claude.ai/cowork/cse_…</code>,
  50 known on the laptop 16 Jul → 19 Aug, none with a transcript); <b>Cowork running in its VM</b>
  (adopted on both machines by 02 Sep — the VM disks hold no transcripts, so Cowork burn after
  that is invisible here); Codex; and all non-token work. Proof the gap is real: 22–23 Aug peaked at
  42% of the 5-hour plan window with <i>zero</i> local tokens on either machine.
  Claude Code transcripts only start {cc_start}; anything earlier exists in the Claude data
  export as conversations but carries <i>no token fields at all</i>, so it is unrecoverable as
  burn and is deliberately not estimated.
  {"<br><b>One machine only</b> — the second machine has not been merged in yet, so this is a floor, not a total." if only_one else ""}
</div>

<div class="cols">
  <div><h2>By purpose</h2>{bar_table(d['by_purpose'], total, PURPOSE_COLORS, 'purpose')}</div>
  <div><h2>By surface</h2>{bar_table(d['by_surface'], total, SURFACE_COLORS, 'surface')}</div>
  <div><h2>By model</h2>{bar_table(d['by_model'], total, None, 'model')}</div>
</div>

<h2>Top 10 days, and what was in them</h2>
{''.join(td)}

<h2>By project</h2>
{''.join(pt)}
<div class="small dim" style="margin-top:6px">
  outcome mix:
  <span style="color:{OUTCOME_LABEL['shipped'][1]}">■ shipped</span>
  <span style="color:{OUTCOME_LABEL['salvaged'][1]}">■ salvaged</span>
  <span style="color:{OUTCOME_LABEL['abandoned'][1]}">■ abandoned</span>
  <span style="color:{OUTCOME_LABEL[None][1]}">■ untagged</span>
</div>

<h2>Largest sessions</h2>
{''.join(st)}

{"<h2>Before metering — the first conversations (claude.ai export, no token data)</h2>" + ''.join(pre) if act_msgs else ""}

<footer>
  Generated from <code>events.db</code> by <code>render.py</code>.
  Attribution: project from file paths touched (never from <code>cwd</code> — 94% of records
  report the same sandbox folder); outcome from the session-title prefix.
  Private artifact — project and client names are present and must be scrubbed before any
  public version.
</footer>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    d = gather(con)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(render(d))
    print(f"wrote {a.out}  ({os.path.getsize(a.out):,} bytes)")
    print(f"  {fmt(d['total'])} tokens | {d['first']} -> {d['last']} | "
          f"{d['sessions']:,} sessions")


if __name__ == "__main__":
    main()
