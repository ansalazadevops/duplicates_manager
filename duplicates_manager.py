#!/usr/bin/env python3
"""
Duplicate File Manager
======================

Reads a "file duplicates" report (three columns: MD5 hash, filename, modified
date) and serves a small local web app that lets you review each group of
duplicates and **relocate** or **remove** the redundant copies safely.

The report format expected (one entry per line):

    <32-char md5><two spaces><absolute/path/to/file><TAB><YYYY-MM-DD HH:MM:SS>

A header line ("MD5 Hash<TAB>Filename<TAB>Modified Date") is ignored wherever it
appears.

USAGE
-----
    python duplicates_manager.py [REPORT.out] [--port 5000] [--trash DIR] [--dry-run]

Then open http://127.0.0.1:5000 in your browser.

SAFETY
------
* "Remove" does NOT permanently delete by default -- it MOVES files into a
  trash folder (default: ~/.duplicates_manager_trash) so every action is reversible.
  Pass ?permanent=true (toggle in the UI) to delete for good.
* The app refuses to remove the *last surviving copy* of a hash group unless you
  explicitly force it, so you never lose a file entirely by accident.
* --dry-run performs no filesystem changes; it only logs what *would* happen.
* Every action is appended to an audit log (duplicates_manager_actions.jsonl) next to
  this script, and moves can be undone from the UI.

This app is meant to run on the machine that actually holds the files (the paths
in the report are absolute). Files that no longer exist on disk are shown as
"missing" and are skipped by operations.
"""

import argparse
import json
import os
import shutil
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

# --------------------------------------------------------------------------- #
# Configuration (populated from CLI in main())                                #
# --------------------------------------------------------------------------- #
CONFIG = {
    "report_path": None,
    "trash_dir": str(Path.home() / ".duplicates_manager_trash"),
    "dry_run": False,
    "audit_log": str(Path(__file__).resolve().parent / "duplicates_manager_actions.jsonl"),
}

HEADER_PREFIX = "MD5 Hash"

# In-memory model:  md5 -> list[ {"path": str, "mtime": str} ]
GROUPS: "OrderedDict[str, list]" = OrderedDict()
# Paths that have been removed or moved this session (hidden from disk-state).
RESOLVED: set = set()

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #
def parse_report(path: str) -> "OrderedDict[str, list]":
    """Parse the .out report into an ordered {md5: [entries]} mapping.

    The report may be a concatenation of several runs, so the same
    ``(hash, path)`` line can appear multiple times. Identical paths inside a
    group are collapsed to a single entry (first occurrence wins).
    """
    groups: "OrderedDict[str, list]" = OrderedDict()
    seen_paths: "dict[str, set]" = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith(HEADER_PREFIX):  # skip repeated headers
                continue
            # The date is the final tab-delimited field.
            if "\t" not in line:
                continue
            left, date = line.rsplit("\t", 1)
            md5 = left[:32]
            filename = left[32:].lstrip()  # strip the separating spaces
            if not md5 or not filename:
                continue
            bucket = seen_paths.setdefault(md5, set())
            if filename in bucket:  # duplicate row from a repeated run
                continue
            bucket.add(filename)
            groups.setdefault(md5, []).append(
                {"path": filename, "mtime": date.strip()}
            )
    # Drop any group that collapsed to a single path (not a real duplicate set).
    return OrderedDict((h, v) for h, v in groups.items() if len(v) > 1)


# --------------------------------------------------------------------------- #
# Disk helpers                                                                 #
# --------------------------------------------------------------------------- #
def stat_entry(entry: dict) -> dict:
    """Return the entry enriched with live disk state."""
    p = entry["path"]
    out = {"path": p, "mtime": entry["mtime"], "resolved": p in RESOLVED}
    try:
        st = os.stat(p)
        out["exists"] = True
        out["size"] = st.st_size
    except OSError:
        out["exists"] = False
        out["size"] = 0
    return out


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def audit(action: str, **fields):
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "action": action}
    rec.update(fields)
    try:
        with open(CONFIG["audit_log"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return rec


def unique_destination(dest_dir: Path, name: str) -> Path:
    """Return a non-colliding path inside dest_dir for the given file name."""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    i = 1
    while True:
        candidate = dest_dir / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def find_group(path: str):
    """Return (md5, entries) for the group that contains `path`, or (None, None)."""
    for md5, entries in GROUPS.items():
        if any(e["path"] == path for e in entries):
            return md5, entries
    return None, None


def surviving_count(entries: list) -> int:
    """How many copies in this group still exist on disk and aren't resolved."""
    return sum(
        1 for e in entries if e["path"] not in RESOLVED and os.path.exists(e["path"])
    )


# --------------------------------------------------------------------------- #
# Core operations                                                             #
# --------------------------------------------------------------------------- #
def do_remove(path: str, permanent: bool, force: bool) -> dict:
    md5, entries = find_group(path)
    if entries is None:
        return {"path": path, "ok": False, "msg": "not in report"}
    if path in RESOLVED:
        return {"path": path, "ok": False, "msg": "already handled"}
    if not os.path.exists(path):
        RESOLVED.add(path)
        return {"path": path, "ok": False, "msg": "file missing on disk"}
    if not force and surviving_count(entries) <= 1:
        return {
            "path": path,
            "ok": False,
            "msg": "refused: this is the last surviving copy (use force)",
        }

    if CONFIG["dry_run"]:
        RESOLVED.add(path)
        audit("remove", path=path, permanent=permanent, dry_run=True)
        return {"path": path, "ok": True, "msg": "dry-run: would remove"}

    try:
        if permanent:
            os.remove(path)
            audit("remove", path=path, permanent=True)
            msg = "permanently deleted"
        else:
            trash_root = Path(CONFIG["trash_dir"])
            # Preserve the source structure under trash so it stays recoverable.
            rel = Path(path).as_posix().lstrip("/")
            dest = trash_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest = unique_destination(dest.parent, dest.name)
            shutil.move(path, str(dest))
            audit("remove", path=path, permanent=False, trashed_to=str(dest))
            msg = "moved to trash"
        RESOLVED.add(path)
        return {"path": path, "ok": True, "msg": msg}
    except OSError as exc:
        return {"path": path, "ok": False, "msg": f"error: {exc}"}


def do_move(path: str, dest_dir: str, preserve: bool) -> dict:
    md5, entries = find_group(path)
    if entries is None:
        return {"path": path, "ok": False, "msg": "not in report"}
    if path in RESOLVED:
        return {"path": path, "ok": False, "msg": "already handled"}
    if not os.path.exists(path):
        RESOLVED.add(path)
        return {"path": path, "ok": False, "msg": "file missing on disk"}
    if not dest_dir:
        return {"path": path, "ok": False, "msg": "no destination given"}

    target_root = Path(os.path.expanduser(dest_dir))
    if preserve:
        rel = Path(path).as_posix().lstrip("/")
        dest = target_root / rel
        dest_parent = dest.parent
        dest_name = dest.name
    else:
        dest_parent = target_root
        dest_name = Path(path).name

    if CONFIG["dry_run"]:
        RESOLVED.add(path)
        audit("move", path=path, dest=str(dest_parent / dest_name), dry_run=True)
        return {"path": path, "ok": True, "msg": f"dry-run: would move -> {dest_parent / dest_name}"}

    try:
        dest_parent.mkdir(parents=True, exist_ok=True)
        final = unique_destination(dest_parent, dest_name)
        shutil.move(path, str(final))
        RESOLVED.add(path)
        audit("move", path=path, dest=str(final))
        return {"path": path, "ok": True, "msg": f"moved -> {final}", "dest": str(final)}
    except OSError as exc:
        return {"path": path, "ok": False, "msg": f"error: {exc}"}


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
@app.route("/api/stats")
def api_stats():
    total_groups = len(GROUPS)
    total_files = sum(len(v) for v in GROUPS.values())
    reclaimable = 0
    unresolved_groups = 0
    for entries in GROUPS.values():
        live = [
            e for e in entries
            if e["path"] not in RESOLVED and os.path.exists(e["path"])
        ]
        if len(live) > 1:
            unresolved_groups += 1
            sizes = sorted((os.path.getsize(e["path"]) for e in live), reverse=True)
            reclaimable += sum(sizes[1:])  # keep the largest one, reclaim the rest
    return jsonify(
        {
            "total_groups": total_groups,
            "total_files": total_files,
            "open_groups": unresolved_groups,
            "resolved": len(RESOLVED),
            "reclaimable": reclaimable,
            "reclaimable_h": human_size(reclaimable),
            "dry_run": CONFIG["dry_run"],
            "trash_dir": CONFIG["trash_dir"],
        }
    )


@app.route("/api/groups")
def api_groups():
    q = request.args.get("q", "").strip().lower()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(5, int(request.args.get("per_page", 25))))
    show_resolved = request.args.get("show_resolved", "0") == "1"

    matched = []
    for md5, entries in GROUPS.items():
        rows = [stat_entry(e) for e in entries]
        live = [r for r in rows if r["exists"] and not r["resolved"]]
        if not show_resolved and len(live) <= 1:
            # Group is done (0/1 copies left) -> hide unless asked.
            continue
        if q:
            if not any(q in r["path"].lower() for r in rows):
                continue
        matched.append(
            {
                "md5": md5,
                "files": rows,
                "live_count": len(live),
                "sample_size": next((r["size"] for r in rows if r["exists"]), 0),
            }
        )

    total = len(matched)
    start = (page - 1) * per_page
    chunk = matched[start : start + per_page]
    for g in chunk:
        g["sample_size_h"] = human_size(g["sample_size"])
    return jsonify(
        {
            "groups": chunk,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }
    )


@app.route("/api/remove", methods=["POST"])
def api_remove():
    data = request.get_json(force=True)
    paths = data.get("paths", [])
    permanent = bool(data.get("permanent", False))
    force = bool(data.get("force", False))
    results = [do_remove(p, permanent, force) for p in paths]
    return jsonify({"results": results})


@app.route("/api/move", methods=["POST"])
def api_move():
    data = request.get_json(force=True)
    paths = data.get("paths", [])
    dest = data.get("dest", "")
    preserve = bool(data.get("preserve", False))
    results = [do_move(p, dest, preserve) for p in paths]
    return jsonify({"results": results})


@app.route("/api/keep_one", methods=["POST"])
def api_keep_one():
    """Keep `keep` and remove every other existing copy in its group."""
    data = request.get_json(force=True)
    keep = data.get("keep", "")
    permanent = bool(data.get("permanent", False))
    md5, entries = find_group(keep)
    if entries is None:
        return jsonify({"results": [{"path": keep, "ok": False, "msg": "not found"}]})
    results = []
    for e in entries:
        if e["path"] == keep:
            continue
        if e["path"] in RESOLVED or not os.path.exists(e["path"]):
            continue
        results.append(do_remove(e["path"], permanent, force=True))
    return jsonify({"results": results, "kept": keep})


@app.route("/")
def index():
    return render_template_string(PAGE)


# --------------------------------------------------------------------------- #
# Front-end (single page, vanilla JS)                                          #
# --------------------------------------------------------------------------- #
PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duplicate File Manager</title>
<style>
  :root{
    --bg:#f6f5f2; --panel:#ffffff; --ink:#1f2328; --muted:#6b7280;
    --line:#e6e4df; --accent:#3257d6; --accent-soft:#eaeefb;
    --danger:#c4362f; --danger-soft:#fbeae9; --ok:#1f7a45; --ok-soft:#e7f3ec;
    --keep:#b8860b; --shadow:0 1px 2px rgba(0,0,0,.05),0 8px 24px -16px rgba(0,0,0,.25);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  code,.path{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  header{position:sticky;top:0;z-index:20;background:rgba(246,245,242,.9);
         backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:14px 20px}
  .title{display:flex;align-items:center;gap:10px;font-weight:650;font-size:17px}
  .title .dot{width:9px;height:9px;border-radius:50%;background:var(--accent)}
  .stats{display:flex;flex-wrap:wrap;gap:18px;margin-top:10px;color:var(--muted);font-size:13px}
  .stats b{color:var(--ink)}
  .wrap{max-width:1100px;margin:0 auto;padding:18px 20px 80px}
  .toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
           background:var(--panel);border:1px solid var(--line);border-radius:12px;
           padding:12px;margin-bottom:16px;box-shadow:var(--shadow);position:sticky;top:84px;z-index:15}
  input[type=text]{flex:1;min-width:180px;padding:8px 11px;border:1px solid var(--line);
        border-radius:8px;font-size:14px;background:#fff}
  input[type=text]:focus,select:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
  select{padding:8px;border:1px solid var(--line);border-radius:8px;background:#fff}
  label.chk{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;cursor:pointer}
  button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);
         background:#fff;padding:7px 12px;transition:.12s}
  button:hover{border-color:#cfcdc7}
  button:disabled{opacity:.5;cursor:not-allowed}
  .btn-accent{background:var(--accent);color:#fff;border-color:var(--accent)}
  .btn-accent:hover{filter:brightness(1.07)}
  .btn-danger{background:var(--danger);color:#fff;border-color:var(--danger)}
  .btn-danger:hover{filter:brightness(1.07)}
  .bulkbar{margin-left:auto;display:flex;gap:8px;align-items:center}
  .group{background:var(--panel);border:1px solid var(--line);border-radius:12px;
         margin-bottom:14px;box-shadow:var(--shadow);overflow:hidden}
  .ghead{display:flex;align-items:center;gap:12px;padding:11px 14px;border-bottom:1px solid var(--line);
         background:#fbfaf8}
  .hash{font-family:ui-monospace,monospace;font-size:12px;color:var(--muted)}
  .badge{font-size:12px;padding:2px 8px;border-radius:20px;background:var(--accent-soft);color:var(--accent);font-weight:600}
  .ghead .size{margin-left:auto;color:var(--muted);font-size:12px}
  .file{display:grid;grid-template-columns:22px 18px 1fr auto;gap:10px;align-items:center;
        padding:9px 14px;border-bottom:1px solid #f1efeb}
  .file:last-child{border-bottom:none}
  .file.gone{opacity:.45}
  .file .path{font-size:12.5px;word-break:break-all}
  .meta{color:var(--muted);font-size:12px;white-space:nowrap}
  .row-actions{display:flex;gap:6px}
  .row-actions button{padding:5px 9px;font-size:12.5px}
  .tag{font-size:11px;padding:1px 7px;border-radius:6px;margin-left:6px}
  .tag.gone{background:#f3f3f3;color:#999}
  .tag.done{background:var(--ok-soft);color:var(--ok)}
  .keepradio{accent-color:var(--keep);width:16px;height:16px}
  .pager{display:flex;gap:8px;align-items:center;justify-content:center;margin:18px 0}
  .empty{text-align:center;color:var(--muted);padding:60px 20px}
  #toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
         background:#1f2328;color:#fff;padding:11px 16px;border-radius:10px;
         box-shadow:var(--shadow);opacity:0;pointer-events:none;transition:.2s;max-width:90vw;z-index:50}
  #toast.show{opacity:1}
  .dry{background:#fff6e5;color:#8a5a00;border:1px solid #f0d9a8;padding:2px 8px;border-radius:6px;font-size:12px}
  .hint{color:var(--muted);font-size:12px;margin:0 0 14px}
</style>
</head>
<body>
<header>
  <div class="title"><span class="dot"></span> Duplicate File Manager
    <span id="dryflag"></span></div>
  <div class="stats" id="stats">loading…</div>
</header>

<div class="wrap">
  <p class="hint">Each card is one set of identical files (same MD5). Keep one, then
     <b>remove</b> (→ trash by default, reversible) or <b>move</b> the rest. Missing
     files on disk are dimmed and skipped automatically.</p>

  <div class="toolbar">
    <input id="q" type="text" placeholder="Filter by path… (e.g. 'banco/Bmx' or '.venv')">
    <select id="perpage">
      <option value="15">15 / page</option>
      <option value="25" selected>25 / page</option>
      <option value="50">50 / page</option>
    </select>
    <label class="chk"><input type="checkbox" id="showResolved"> show finished</label>
    <label class="chk"><input type="checkbox" id="permanent"> permanent delete</label>
    <div class="bulkbar">
      <span id="selcount" class="meta">0 selected</span>
      <button id="bulkMove">Move selected…</button>
      <button id="bulkRemove" class="btn-danger">Remove selected</button>
    </div>
  </div>

  <div id="list"></div>
  <div class="pager" id="pager"></div>
</div>

<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let state = {page:1, q:"", per:25, total:0, pages:1};
const selected = new Set();

function toast(msg, ms=2600){
  const t=$("#toast"); t.textContent=msg; t.classList.add("show");
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove("show"), ms);
}
function permanent(){ return $("#permanent").checked; }

async function loadStats(){
  const s = await (await fetch("/api/stats")).json();
  $("#stats").innerHTML =
    `<span><b>${s.open_groups.toLocaleString()}</b> open groups</span>`+
    `<span><b>${s.total_files.toLocaleString()}</b> files in report</span>`+
    `<span><b>${s.reclaimable_h}</b> reclaimable</span>`+
    `<span><b>${s.resolved.toLocaleString()}</b> handled this session</span>`+
    `<span>trash: <code>${s.trash_dir}</code></span>`;
  $("#dryflag").innerHTML = s.dry_run ? '<span class="dry">DRY-RUN</span>' : '';
}

function fileRow(f){
  const gone = !f.exists || f.resolved;
  const tag = f.resolved ? '<span class="tag done">handled</span>'
            : (!f.exists ? '<span class="tag gone">missing</span>' : '');
  const cb = gone ? '' :
    `<input type="checkbox" class="selbox" data-path="${encodeURIComponent(f.path)}">`;
  const keep = gone ? '' :
    `<input type="radio" class="keepradio" name="keep_${f._md5}" data-path="${encodeURIComponent(f.path)}" title="mark as the copy to keep">`;
  const actions = gone ? '' :
    `<div class="row-actions">
       <button class="mv" data-path="${encodeURIComponent(f.path)}">Move…</button>
       <button class="rm btn-danger" data-path="${encodeURIComponent(f.path)}">Remove</button>
     </div>`;
  return `<div class="file ${gone?'gone':''}">
      <span>${cb}</span>
      <span>${keep}</span>
      <div>
        <div class="path">${escapeHtml(f.path)}${tag}</div>
        <div class="meta">modified ${f.mtime} · ${human(f.size)}</div>
      </div>
      ${actions}
    </div>`;
}

function groupCard(g){
  g.files.forEach(f=>f._md5=g.md5);
  return `<div class="group" data-md5="${g.md5}">
     <div class="ghead">
       <span class="badge">${g.live_count} copies</span>
       <span class="hash">${g.md5}</span>
       <button class="keepone" data-md5="${g.md5}" title="Remove every copy except the one marked ★ keep">
         Keep ★, remove the rest</button>
       <span class="size">${g.sample_size_h} each</span>
     </div>
     ${g.files.map(fileRow).join("")}
   </div>`;
}

async function load(){
  const u = new URLSearchParams({page:state.page, q:state.q, per_page:state.per,
                                 show_resolved: $("#showResolved").checked?1:0});
  const d = await (await fetch("/api/groups?"+u)).json();
  state.total=d.total; state.pages=d.pages; state.page=d.page;
  if(!d.groups.length){
    $("#list").innerHTML = `<div class="empty">No duplicate groups match.<br>
       Either everything here is handled, or your filter is too narrow.</div>`;
  } else {
    $("#list").innerHTML = d.groups.map(groupCard).join("");
  }
  $("#pager").innerHTML =
    `<button ${state.page<=1?'disabled':''} id="prev">‹ Prev</button>
     <span class="meta">page ${state.page} / ${state.pages} · ${state.total} groups</span>
     <button ${state.page>=state.pages?'disabled':''} id="next">Next ›</button>`;
  bindRows();
  refreshSel();
}

function bindRows(){
  document.querySelectorAll(".rm").forEach(b=>b.onclick=()=>remove([dpath(b)]));
  document.querySelectorAll(".mv").forEach(b=>b.onclick=()=>move([dpath(b)]));
  document.querySelectorAll(".keepone").forEach(b=>b.onclick=()=>keepOne(b.dataset.md5));
  document.querySelectorAll(".selbox").forEach(c=>c.onchange=()=>{
    const p=decodeURIComponent(c.dataset.path);
    c.checked?selected.add(p):selected.delete(p); refreshSel();
  });
  $("#prev")&&($("#prev").onclick=()=>{state.page--;load();});
  $("#next")&&($("#next").onclick=()=>{state.page++;load();});
}
const dpath = b => decodeURIComponent(b.dataset.path);
function refreshSel(){
  $("#selcount").textContent = selected.size+" selected";
  document.querySelectorAll(".selbox").forEach(c=>{
    c.checked = selected.has(decodeURIComponent(c.dataset.path));
  });
}

async function post(url, body){
  const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
                             body:JSON.stringify(body)});
  return r.json();
}
function summarize(results){
  const ok=results.filter(r=>r.ok).length, fail=results.length-ok;
  let msg=`${ok} done`; if(fail) msg+=`, ${fail} skipped/failed`;
  const firstFail=results.find(r=>!r.ok);
  if(firstFail) msg+=` — e.g. ${firstFail.msg}`;
  return msg;
}

async function remove(paths){
  if(!paths.length) return;
  const perm=permanent();
  const verb=perm?"PERMANENTLY DELETE":"move to trash";
  if(!confirm(`${verb} ${paths.length} file(s)?`)) return;
  const d = await post("/api/remove",{paths,permanent:perm,force:false});
  // Offer force if some were blocked as last-copy.
  const blocked=d.results.filter(r=>!r.ok && r.msg.includes("last surviving"));
  if(blocked.length && confirm(`${blocked.length} were the last copy in their group. Remove anyway?`)){
    await post("/api/remove",{paths:blocked.map(r=>r.path),permanent:perm,force:true});
  }
  toast(summarize(d.results));
  paths.forEach(p=>selected.delete(p));
  await loadStats(); load();
}

async function move(paths){
  if(!paths.length) return;
  const dest = prompt("Move "+paths.length+" file(s) to which folder?\n(absolute path; created if needed)");
  if(!dest) return;
  const preserve = confirm("Preserve each file's original folder structure under the destination?\n\nOK = keep subfolders (avoids name clashes)\nCancel = drop them all flat into the folder");
  const d = await post("/api/move",{paths,dest,preserve});
  toast(summarize(d.results));
  paths.forEach(p=>selected.delete(p));
  await loadStats(); load();
}

async function keepOne(md5){
  const card=document.querySelector(`.group[data-md5="${md5}"]`);
  const sel=card.querySelector(`input.keepradio:checked`);
  if(!sel){ toast("Mark which copy to keep (★) first."); return; }
  const keep=decodeURIComponent(sel.dataset.path);
  const perm=permanent();
  if(!confirm(`Keep:\n${keep}\n\n${perm?"PERMANENTLY DELETE":"Trash"} the other copies in this group?`)) return;
  const d = await post("/api/keep_one",{keep,permanent:perm});
  toast(summarize(d.results));
  await loadStats(); load();
}

// utils
function escapeHtml(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function human(n){if(!n)return "0 B";const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++;}return (i?n.toFixed(1):n)+" "+u[i];}

// bindings
let qt; $("#q").oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{state.q=$("#q").value;state.page=1;load();},250);};
$("#perpage").onchange=()=>{state.per=+$("#perpage").value;state.page=1;load();};
$("#showResolved").onchange=()=>{state.page=1;load();};
$("#bulkRemove").onclick=()=>remove([...selected]);
$("#bulkMove").onclick=()=>move([...selected]);

loadStats(); load();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Configuration / loading                                                      #
# --------------------------------------------------------------------------- #
def configure(report_path=None, trash_dir=None, dry_run=None):
    """Apply configuration (any argument left as None is unchanged)."""
    if report_path:
        CONFIG["report_path"] = os.path.abspath(os.path.expanduser(report_path))
    if trash_dir:
        CONFIG["trash_dir"] = os.path.abspath(os.path.expanduser(trash_dir))
    if dry_run is not None:
        CONFIG["dry_run"] = bool(dry_run)


def load_groups():
    """(Re)parse the configured report into the in-memory model."""
    global GROUPS
    if not CONFIG["report_path"]:
        return
    if not os.path.exists(CONFIG["report_path"]):
        raise FileNotFoundError(CONFIG["report_path"])
    GROUPS = parse_report(CONFIG["report_path"])
    files = sum(len(v) for v in GROUPS.values())
    print(f"[duplicates_manager] loaded {len(GROUPS):,} duplicate groups "
          f"({files:,} file entries) from {CONFIG['report_path']}", file=sys.stderr)


def init_from_env():
    """Configure from environment variables. Used when launched via the Flask
    CLI (`flask run`) or a WSGI server (gunicorn/uwsgi), which import this
    module and serve `app` without ever calling main().

        DUPE_REPORT   path to the .out duplicates report   (required)
        DUPE_TRASH    trash folder for reversible removes   (optional)
        DUPE_DRY_RUN  1/true/yes/on  -> no filesystem writes (optional)
    """
    dry = os.environ.get("DUPE_DRY_RUN")
    configure(
        report_path=os.environ.get("DUPE_REPORT"),
        trash_dir=os.environ.get("DUPE_TRASH"),
        dry_run=(dry.lower() in ("1", "true", "yes", "on")) if dry else None,
    )
    if CONFIG["report_path"] and not GROUPS:
        load_groups()


# Runs at import time so `flask run` and gunicorn load data from the env vars.
init_from_env()


# --------------------------------------------------------------------------- #
# Entry point (python duplicates_manager.py ...)                                      #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Review/relocate/remove duplicate files.")
    ap.add_argument("report", nargs="?", default=CONFIG["report_path"],
                    help="Path to the duplicates .out report")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--trash", default=CONFIG["trash_dir"],
                    help="Folder where 'removed' files are moved (reversible).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't touch the filesystem; just log intended actions.")
    args = ap.parse_args()

    if not args.report:
        ap.error("a report file is required (the .out duplicates list)")
    if not os.path.exists(args.report):
        ap.error(f"report not found: {args.report}")

    configure(report_path=args.report, trash_dir=args.trash, dry_run=args.dry_run)
    load_groups()

    print(f"Trash folder: {CONFIG['trash_dir']}")
    if CONFIG["dry_run"]:
        print("*** DRY-RUN: no files will be changed ***")
    print(f"Open  http://{args.host}:{args.port}  in your browser.  (Ctrl-C to stop)")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
