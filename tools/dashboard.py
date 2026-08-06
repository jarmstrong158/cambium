#!/usr/bin/env python3
"""One operator dashboard for the whole memory mesh: every project's decision
log, and cambium's knowledge layer sitting on top of them.

    python tools/dashboard.py            # -> dashboard.html, then open it
    python tools/dashboard.py --open     # build and open in the browser
    python tools/dashboard.py --root D:/code --out /tmp/mem.html

WHY THIS EXISTS

Counting entries was already possible; understanding them was not. Two things
were true across 12 populated stores and nothing surfaced either:

  * 214 of 229 distilled items had never been recalled -- and 111 of the 117
    total recalls came from a single project. A knowledge layer that is 93%
    write-only is either mis-scoped or capturing the wrong things, and you
    cannot tell which from a count.
  * 69 of 229 items carried cp1252 mojibake, distilled out of stores before
    context-keeper fixed the transport. cambium has no repair path, so the
    corruption simply sits there being recalled.

Both are aggregate properties. Neither is visible in any single tool's output.

LOCAL BY DESIGN

Output is gitignored and stays on your machine. The mesh spans private repos
and ones with no remote at all, so a published version could honestly show
aggregates and nothing else (con-015-12da in context-keeper). This one shows
everything, because it is yours.

Stdlib only, like the rest of the suite. Reads .context/ and .cambium/ as plain
JSON -- it imports neither server, so it works whatever state those are in.
"""

import argparse
import datetime as _dt
import json
import os
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_ROOT = os.path.dirname(REPO)

CK_FILES = (("decisions", "decisions.json"),
            ("pipelines", "pipelines.json"),
            ("constraints", "constraints.json"))

# Mirrors context-keeper's mojibake.py::_MOJIBAKE_MARKERS. Inlined rather than
# imported so this tool has no cross-repo dependency -- it must run even when
# the sibling checkout is absent or mid-refactor, which is exactly when you
# want to look at the stores.
#
# Duplicating a pattern list is normally how two copies come to disagree, and
# they did: this list caught a multiplication-sign mis-decode that
# context-keeper's did not, which is what exposed con-016-16be. The duplication
# is tolerable HERE only because nothing is gated on it -- this tool reports and
# never repairs, so drift makes it under-report rather than silently skip a
# repair. Keep it in step with the source list when that one changes.
_MOJI = (
    "\u00e2\u20ac", "\u00c3\u00a9", "\u00c3\u00a8", "\u00c3\u00bc",
    "\u00c3\u00b1", "\u00c3\u00a0", "\u00c3\u00b4", "\u00c3\u00b6",
    "\u00c3\u00a4", "\u00c3\u2014", "\u00c3\u00b7", "\u00e2\u201e",
    "\u00e2\u02c6", "\u00c2\u00a0", "\u00c2\u00b7", "\u00c2\u00ab",
    "\u00c2\u00bb", "\u00c2\u00b0", "\u00c2\u00b1",
)

STALE_DAYS = 30
THIN_CHARS = 80


def looks_garbled(text):
    return isinstance(text, str) and any(m in text for m in _MOJI)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _days_since(iso):
    if not iso:
        return None
    try:
        d = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return (_dt.datetime.now(_dt.timezone.utc) - d).days
    except Exception:
        return None


def _label(e):
    return e.get("summary") or e.get("rule") or e.get("name") or "(untitled)"


def _reason_len(kind, e):
    if kind == "decisions":
        return len(((e.get("why_chosen") or "") + " " + (e.get("rationale") or "")).strip())
    if kind == "constraints":
        return len((e.get("reason") or "").strip())
    return len((e.get("purpose") or "").strip())


def collect_project(name, project_dir):
    """Everything one project's two stores can tell us. None if it has neither."""
    ctx = os.path.join(project_dir, ".context")
    cam = os.path.join(project_dir, ".cambium", "knowledge.json")
    if not os.path.isdir(ctx) and not os.path.exists(cam):
        return None

    entries, counts = [], {}
    superseded_by = {}
    for kind, fname in CK_FILES:
        raw = _read(os.path.join(ctx, fname))
        counts[kind] = len(raw)
        for e in raw:
            eid = e.get("id")
            if not eid:
                continue
            status = e.get("status", "active")
            sup = e.get("superseded_by")
            if sup:
                superseded_by[eid] = sup
            rlen = _reason_len(kind, e)
            garbled = any(looks_garbled(e.get(f, "")) for f in
                          ("summary", "rule", "name", "problem", "why_chosen",
                           "reason", "purpose", "tradeoffs", "what_we_tried",
                           "triggering_incident"))
            verified = e.get("verified_at") or e.get("updated_at") or e.get("created_at")
            entries.append({
                "id": eid, "kind": kind[:-1], "t": _label(e)[:240],
                "tags": [str(t) for t in (e.get("tags") or [])][:6],
                "scope": e.get("scope") or ("global" if kind == "constraints" else ""),
                "hardness": e.get("hardness", ""),
                "status": status,
                "sup": sup or "",
                "created": (e.get("created_at") or "")[:10],
                "age": _days_since(verified),
                "thin": rlen < THIN_CHARS,
                "rlen": rlen,
                "notags": not e.get("tags"),
                "moji": garbled,
                "links": len(e.get("related_to") or []),
                "origin": e.get("origin", "agent"),
            })

    items = []
    cam_raw = []
    if os.path.exists(cam):
        try:
            cam_raw = (json.load(open(cam, encoding="utf-8")) or {}).get("items", [])
        except Exception:
            cam_raw = []
    for i in cam_raw:
        trust = i.get("trust") or {}
        items.append({
            "id": i.get("id", ""), "kind": i.get("kind", "?"),
            "scope": i.get("scope", "local"),
            "status": i.get("status", "active"),
            "t": (i.get("content") or "")[:240],
            "recalls": int(trust.get("recalls") or 0),
            "endorsements": len(trust.get("endorsements") or []),
            "ref": ((i.get("source") or {}).get("ref") or ""),
            "moji": looks_garbled(i.get("content", "")) or looks_garbled(i.get("why", "")),
            "created": (i.get("created_at") or "")[:10],
        })

    active = [e for e in entries if e["status"] == "active"]
    return {
        "name": name,
        "counts": counts,
        "entries": entries,
        "items": items,
        "stats": {
            "entries": len(entries),
            "active": len(active),
            "superseded": sum(1 for e in entries if e["status"] == "superseded"),
            "deprecated": sum(1 for e in entries if e["status"] == "deprecated"),
            "links": len(superseded_by),
            "thin": sum(1 for e in active if e["thin"]),
            "notags": sum(1 for e in active if e["notags"]),
            "stale": sum(1 for e in active if (e["age"] or 0) > STALE_DAYS),
            "moji_ck": sum(1 for e in entries if e["moji"]),
            "scoped": sum(1 for e in active
                          if e["kind"] == "constraint" and e["scope"] not in ("", "global")),
            "constraints": sum(1 for e in active if e["kind"] == "constraint"),
            "distilled": len(items),
            "recalled": sum(1 for i in items if i["recalls"]),
            "recalls": sum(i["recalls"] for i in items),
            "promoted": sum(1 for i in items if i["scope"] != "local"),
            "moji_cam": sum(1 for i in items if i["moji"]),
            "cam_dep": sum(1 for i in items if i["status"] != "active"),
        },
    }


def discover(roots):
    found = {}
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isdir(path) or name.startswith("."):
                continue
            p = collect_project(name, path)
            if p and (p["stats"]["entries"] or p["stats"]["distilled"]):
                found[name] = p
    return [found[k] for k in sorted(found, key=lambda k: -found[k]["stats"]["entries"])]


def build(roots, out):
    projects = discover(roots)
    tot = {}
    for k in ("entries", "active", "superseded", "deprecated", "links", "thin",
              "notags", "stale", "moji_ck", "scoped", "constraints", "distilled",
              "recalled", "recalls", "promoted", "moji_cam", "cam_dep"):
        tot[k] = sum(p["stats"][k] for p in projects)
    payload = {
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "roots": [os.path.abspath(r) for r in roots],
        "stale_days": STALE_DAYS,
        "thin_chars": THIN_CHARS,
        "totals": tot,
        "projects": projects,
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    doc = TEMPLATE.replace("/*DATA*/", blob.replace("</", "<\\/"))
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    return payload, out


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>memory mesh</title>
<style>
:root{--bg:#0b0e13;--pan:#12161d;--pan2:#171c25;--line:#232a35;--ink:#e7edf5;
 --dim:#8b97a8;--dim2:#5f6a7a;--ac:#2dd4bf;--ac2:#7c9cf5;--warn:#f0b429;--bad:#f9748f;--ok:#4ade80}
@media(prefers-color-scheme:light){:root{--bg:#f7f9fb;--pan:#fff;--pan2:#f2f5f9;
 --line:#e2e8f0;--ink:#131820;--dim:#5b6673;--dim2:#8b97a8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.top{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.brand{font-weight:650;letter-spacing:-.01em;font-size:16px}
.brand small{color:var(--dim2);font-weight:400;margin-left:8px;font-size:12px}
.tabs{display:flex;gap:4px;margin-left:auto}
.tab{padding:6px 13px;border-radius:7px;border:1px solid transparent;color:var(--dim);cursor:pointer;font-size:13px;background:none}
.tab:hover{color:var(--ink)}
.tab.on{background:var(--pan);border-color:var(--line);color:var(--ink)}
.wrap{max-width:1240px;margin:0 auto;padding:22px 20px 70px}
h2{font-size:15px;margin:0 0 3px;letter-spacing:.02em}
.sub{color:var(--dim);font-size:12.5px;margin:0 0 14px;max-width:70ch}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:11px;margin-bottom:22px}
.card{background:var(--pan);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.card .n{font-size:25px;font-weight:640;letter-spacing:-.02em;line-height:1.15}
.card .nt{font-size:14.5px;font-weight:600;line-height:1.35;margin:3px 0 1px}
.card .l{font-size:11.5px;color:var(--dim);margin-top:2px}
.card .f{font-size:11px;color:var(--dim2);margin-top:5px}
.card.warn .n{color:var(--warn)} .card.bad .n{color:var(--bad)} .card.ok .n{color:var(--ok)}
.pan{background:var(--pan);border:1px solid var(--line);border-radius:11px;padding:16px 18px;margin-bottom:18px}
.funnel{display:grid;gap:9px;margin-top:6px}
.fr{display:grid;grid-template-columns:184px 1fr 118px;gap:12px;align-items:center}
.fr .fl{font-size:12.5px;color:var(--dim)}
.trk{height:16px;background:var(--pan2);border-radius:5px;overflow:hidden;position:relative}
.fill{height:100%;border-radius:5px;transition:width .5s cubic-bezier(.2,.7,.3,1)}
.fr .fv{font-size:12.5px;text-align:right;font-variant-numeric:tabular-nums;color:var(--dim)}
.fr .fv b{color:var(--ink);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;
 font-weight:600;padding:7px 9px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--ink)}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
td{padding:7px 9px;border-bottom:1px solid var(--line)}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--pan2)}
.pill{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10.5px;border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.pill.d{border-color:rgba(45,212,191,.4);color:var(--ac)}
.pill.c{border-color:rgba(124,156,245,.45);color:var(--ac2)}
.pill.p{border-color:rgba(240,180,41,.45);color:var(--warn)}
.pill.sup{border-color:rgba(240,180,41,.5);color:var(--warn)}
.pill.dep{border-color:var(--line);color:var(--dim2);text-decoration:line-through}
.mini{height:7px;background:var(--pan2);border-radius:4px;overflow:hidden;min-width:60px}
.mini i{display:block;height:100%}
.bar-lex{background:var(--ac2)} .bar-ac{background:var(--ac)} .bar-warn{background:var(--warn)}
.bar-bad{background:var(--bad)} .bar-dim{background:var(--dim2)}
.controls{display:flex;gap:9px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
input[type=search],select{background:var(--pan);border:1px solid var(--line);color:var(--ink);
 border-radius:7px;padding:6px 10px;font:inherit;font-size:13px;outline:none}
input[type=search]{min-width:230px}
input[type=search]:focus,select:focus{border-color:var(--ac)}
.chip{padding:4px 10px;border-radius:20px;border:1px solid var(--line);color:var(--dim);
 cursor:pointer;font-size:12px;background:var(--pan);white-space:nowrap}
.chip.on{border-color:var(--ac);color:var(--ac)}
.count{color:var(--dim2);font-size:12px;margin-left:auto}
.drawer{position:fixed;inset:0 0 0 auto;width:min(760px,94vw);background:var(--pan);
 border-left:1px solid var(--line);transform:translateX(100%);transition:transform .22s ease;
 z-index:40;display:flex;flex-direction:column;box-shadow:-20px 0 50px rgba(0,0,0,.35)}
.drawer.open{transform:none}
.dh{padding:15px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
.dh h3{margin:0;font-size:17px;letter-spacing:-.01em}
.db{overflow:auto;padding:16px 20px 40px;flex:1}
.x{margin-left:auto;background:none;border:1px solid var(--line);color:var(--dim);
 border-radius:7px;padding:4px 10px;cursor:pointer;font:inherit;font-size:12px}
.x:hover{color:var(--ink)}
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;
 transition:opacity .22s;z-index:30}
.scrim.on{opacity:1;pointer-events:auto}
.eg{display:grid;gap:7px}
.e{border:1px solid var(--line);border-radius:9px;padding:10px 12px;background:var(--pan2)}
.e .eh{display:flex;gap:8px;align-items:center;margin-bottom:3px;flex-wrap:wrap}
.e code{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--dim)}
.e .et{font-size:12.8px;line-height:1.5}
.flags{display:flex;gap:5px;margin-top:6px;flex-wrap:wrap}
.flag{font-size:10px;padding:1px 6px;border-radius:4px;border:1px solid}
.flag.thin{border-color:rgba(240,180,41,.45);color:var(--warn)}
.flag.stale{border-color:rgba(240,180,41,.45);color:var(--warn)}
.flag.moji{border-color:rgba(249,116,143,.5);color:var(--bad)}
.flag.notags{border-color:var(--line);color:var(--dim2)}
.flag.ok{border-color:rgba(74,222,128,.4);color:var(--ok)}
.chain{border-left:2px solid var(--warn);padding-left:11px;margin:5px 0}
.chain .old{color:var(--dim);font-size:12px}
.spark{display:flex;gap:2px;align-items:flex-end;height:34px;margin-top:8px}
.spark i{flex:1;background:var(--ac);border-radius:2px 2px 0 0;min-height:2px;opacity:.75}
.spark i:hover{opacity:1}
.legend{display:flex;gap:14px;font-size:11.5px;color:var(--dim);margin:10px 0 2px;flex-wrap:wrap}
.sw{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:5px;vertical-align:0}
.empty{color:var(--dim2);font-size:13px;padding:26px 0;text-align:center}
.note{font-size:11.5px;color:var(--dim2);margin-top:9px;line-height:1.5}
kbd{font:11px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-bottom-width:2px;
 border-radius:4px;padding:0 4px;color:var(--dim)}
@media(max-width:760px){.fr{grid-template-columns:1fr}.fr .fv{text-align:left}}
</style></head><body>

<div class="top">
  <div class="brand">memory mesh <small id="gen"></small></div>
  <div class="tabs">
    <button class="tab on" data-v="over">Overview</button>
    <button class="tab" data-v="proj">Projects</button>
    <button class="tab" data-v="know">Knowledge</button>
    <button class="tab" data-v="health">Health</button>
  </div>
</div>
<div class="wrap">
  <div id="over"></div>
  <div id="proj" hidden></div>
  <div id="know" hidden></div>
  <div id="health" hidden></div>
</div>
<div class="scrim" id="scrim"></div>
<div class="drawer" id="drawer">
  <div class="dh"><h3 id="dt"></h3><button class="x" id="dx">close <kbd>esc</kbd></button></div>
  <div class="db" id="dbody"></div>
</div>

<script>
const D = /*DATA*/;
const $ = s => document.querySelector(s);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = (a,b) => b ? Math.round(a/b*100) : 0;
const T = D.totals, P = D.projects;
$("#gen").textContent = D.generated + " - " + P.length + " projects";

/* ---------- overview ---------- */
function card(n,l,f,cls){return `<div class="card ${cls||''}"><div class="n">${n}</div>
  <div class="l">${l}</div>${f?`<div class="f">${f}</div>`:""}</div>`}
function cardT(n,l,f){return `<div class="card"><div class="nt">${n}</div>
  <div class="l">${l}</div>${f?`<div class="f">${f}</div>`:""}</div>`}
function fr(label,val,total,cls,note){
  return `<div class="fr"><div class="fl">${label}</div>
    <div class="trk"><div class="fill ${cls}" style="width:${total?val/total*100:0}%"></div></div>
    <div class="fv"><b>${val.toLocaleString()}</b> ${note||""}</div></div>`}

function renderOver(){
  const neverRecalled = T.distilled - T.recalled;
  const topRecall = [...P].sort((a,b)=>b.stats.recalls-a.stats.recalls)[0];
  $("#over").innerHTML = `
  <div class="cards">
    ${card(P.length,"projects with memory")}
    ${card(T.entries.toLocaleString(),"recorded entries",T.active+" active")}
    ${card(T.distilled,"distilled to cambium",pct(T.distilled,T.entries)+"% of entries")}
    ${card(T.recalled,"ever recalled",pct(T.recalled,T.distilled)+"% of distilled",T.recalled/Math.max(T.distilled,1)<0.25?"bad":"")}
    ${card(T.promoted,"promoted past local",T.promoted?"":"nothing has left local","warn")}
    ${card(T.links,"supersession links",T.links?"":"no history recorded",T.links?"":"warn")}
  </div>

  <div class="pan">
    <h2>Distillation funnel</h2>
    <p class="sub">Where recorded knowledge actually goes. Each stage is a subset of the one above it.</p>
    <div class="funnel">
      ${fr("recorded in context-keeper",T.entries,T.entries,"bar-lex")}
      ${fr("still active",T.active,T.entries,"bar-lex",`${pct(T.active,T.entries)}%`)}
      ${fr("distilled into cambium",T.distilled,T.entries,"bar-ac",`${pct(T.distilled,T.entries)}%`)}
      ${fr("recalled at least once",T.recalled,T.entries,"bar-ac",`${pct(T.recalled,T.distilled)}% of distilled`)}
      ${fr("promoted to team/org",T.promoted,T.entries,"bar-warn",`${pct(T.promoted,T.distilled)}% of distilled`)}
    </div>
    <p class="note"><b>${neverRecalled} of ${T.distilled} distilled items have never been recalled
    (${pct(neverRecalled,T.distilled)}%).</b> ${topRecall && topRecall.stats.recalls ?
    `${esc(topRecall.name)} alone accounts for ${topRecall.stats.recalls} of ${T.recalls} total recalls.` : ""}
    Distillation without recall is a write-only log: the cost is paid and the benefit never collected.</p>
  </div>

  <div class="pan">
    <h2>Recall concentration</h2>
    <p class="sub">Recalls per project. A healthy mesh spreads; a spike means one project is
    carrying the whole benefit.</p>
    ${P.filter(p=>p.stats.distilled).sort((a,b)=>b.stats.recalls-a.stats.recalls).map(p=>{
      const mx = Math.max(...P.map(x=>x.stats.recalls),1);
      return `<div class="fr" style="grid-template-columns:184px 1fr 118px">
        <div class="fl">${esc(p.name)}</div>
        <div class="trk"><div class="fill ${p.stats.recalls?'bar-ac':'bar-dim'}"
          style="width:${Math.max(p.stats.recalls/mx*100,p.stats.distilled?1.5:0)}%"></div></div>
        <div class="fv"><b>${p.stats.recalls}</b> <span style="color:var(--dim2)">/ ${p.stats.distilled} items</span></div>
      </div>`}).join("")}
  </div>`;
}

/* ---------- projects table ---------- */
let sortKey="entries", sortDir=-1, q="";
const COLS=[["name","project",0],["entries","entries",1],["active","active",1],
  ["constraints","constraints",1],["scoped","scoped",1],["links","supersedes",1],
  ["distilled","distilled",1],["recalls","recalls",1],["stale","stale",1],["moji_ck","mojibake",1]];

function renderProj(){
  const rows = P.filter(p=>!q||p.name.toLowerCase().includes(q))
    .sort((a,b)=>{const k=sortKey;
      const va=k==="name"?a.name.toLowerCase():a.stats[k], vb=k==="name"?b.name.toLowerCase():b.stats[k];
      return va<vb?-sortDir:va>vb?sortDir:0});
  const mx = Math.max(...P.map(p=>p.stats.entries),1);
  $("#proj").innerHTML = `
  <h2>Every project's decision log</h2>
  <p class="sub">Click a row to open its full log, supersession chains, constraint scopes and health flags.</p>
  <div class="controls">
    <input type="search" id="pq" placeholder="filter projects…  /" value="${esc(q)}">
    <span class="count">${rows.length} of ${P.length}</span>
  </div>
  <div class="pan" style="padding:4px 8px">
  <table><thead><tr>${COLS.map(c=>
    `<th data-k="${c[0]}" class="${c[2]?'num':''}">${c[1]}${sortKey===c[0]?(sortDir<0?" ▾":" ▴"):""}</th>`).join("")}
    <th style="width:90px"></th></tr></thead><tbody>
    ${rows.map(p=>{const s=p.stats;return `<tr data-p="${esc(p.name)}">
      <td><b>${esc(p.name)}</b></td>
      <td class="num">${s.entries}</td>
      <td class="num">${s.active}</td>
      <td class="num">${s.constraints}</td>
      <td class="num" title="constraints with a real path scope">${s.constraints?s.scoped:"-"}</td>
      <td class="num" style="${s.entries>8&&!s.links?'color:var(--warn)':''}">${s.links}</td>
      <td class="num">${s.distilled||"-"}</td>
      <td class="num" style="${s.distilled&&!s.recalls?'color:var(--dim2)':''}">${s.recalls||"-"}</td>
      <td class="num" style="${s.stale?'color:var(--warn)':''}">${s.stale||"-"}</td>
      <td class="num" style="${s.moji_ck?'color:var(--bad)':''}">${s.moji_ck||"-"}</td>
      <td><div class="mini"><i class="bar-ac" style="width:${s.entries/mx*100}%"></i></div></td>
    </tr>`}).join("")}
  </tbody></table></div>
  <p class="note"><b>supersedes</b> counts recorded history links. A project with many entries and
  zero links has been revising decisions in place -- the store can say what is true, not what changed.
  <b>scoped</b> counts constraints with a real path; a globally scoped rule never fires at edit time.</p>`;
  $("#pq").oninput = e => { q=e.target.value.toLowerCase(); renderProj(); $("#pq").focus(); };
  $("#proj").querySelectorAll("th[data-k]").forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=k==="name"?1:-1;}
    renderProj();});
  $("#proj").querySelectorAll("tr[data-p]").forEach(tr=>tr.onclick=()=>openProject(tr.dataset.p));
}

/* ---------- knowledge ---------- */
function renderKnow(){
  const all=[]; P.forEach(p=>p.items.forEach(i=>all.push({...i,p:p.name})));
  const byKind={}; all.forEach(i=>byKind[i.kind]=(byKind[i.kind]||0)+1);
  const top=[...all].sort((a,b)=>b.recalls-a.recalls).filter(i=>i.recalls).slice(0,12);
  const dead=all.filter(i=>!i.recalls).length;
  $("#know").innerHTML=`
  <h2>Cambium knowledge layer</h2>
  <p class="sub">What distillation produced, and whether anything reads it. Every item here was
  derived from a context-keeper entry.</p>
  <div class="cards">
    ${card(all.length,"knowledge items")}
    ${cardT(Object.entries(byKind).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${v} ${k}${v>1?"s":""}`).join("<br>")||"-","by kind")}
    ${card(T.recalls,"total recalls",T.recalls?`across ${T.recalled} items`:"")}
    ${card(dead,"never recalled",pct(dead,all.length)+"% of the layer",dead/Math.max(all.length,1)>.5?"bad":"warn")}
    ${card(T.promoted,"beyond local scope",T.promoted?"":"local only","warn")}
    ${card(T.moji_cam,"carry mojibake",T.moji_cam?"distilled from corrupted text":"clean",T.moji_cam?"bad":"ok")}
  </div>
  <div class="pan">
    <h2>Most-recalled knowledge</h2>
    <p class="sub">The items actually earning their place.</p>
    ${top.length?`<div class="eg">${top.map(i=>`<div class="e">
      <div class="eh"><span class="pill ${i.kind[0]==='d'?'d':'c'}">${esc(i.kind)}</span>
        <span class="pill">${esc(i.p)}</span>
        <span class="pill p">${i.recalls} recall${i.recalls>1?"s":""}</span>
        ${i.ref?`<code>${esc(i.ref)}</code>`:""}</div>
      <div class="et">${esc(i.t)}</div></div>`).join("")}</div>`
      :`<div class="empty">Nothing has been recalled yet.</div>`}
  </div>
  <div class="pan">
    <h2>Promotion ladder</h2>
    <p class="sub">local → team → org. Promotion is what makes knowledge reusable outside the
    project that learned it.</p>
    <div class="funnel">
      ${["local","team","org"].map(s=>{const n=all.filter(i=>i.scope===s).length;
        return fr(s,n,all.length||1,s==="local"?"bar-dim":"bar-ac",pct(n,all.length)+"%")}).join("")}
    </div>
    ${!T.promoted?`<p class="note">Nothing has been promoted. Team and org scopes live on shared git
    branches, so items promoted from another machine will not appear in this local view -- but nothing
    local has crossed either gate.</p>`:""}
  </div>`;
}

/* ---------- health ---------- */
function renderHealth(){
  const rows=[
    ["mojibake in context-keeper",T.moji_ck,T.entries,"bad",
     "cp1252-misdecoded text written before the transport was forced to UTF-8. Repairable with context-keeper's repair_mojibake."],
    ["mojibake in cambium",T.moji_cam,T.distilled,"bad",
     "distilled out of corrupted entries. cambium has no repair path, so these are recalled as-is."],
    ["thin rationale",T.thin,T.active,"warn",
     `active entries whose reasoning is under ${D.thin_chars} characters -- the why did not survive.`],
    ["untagged",T.notags,T.active,"warn","tags are the primary retrieval signal; untagged entries surface only by luck."],
    ["stale",T.stale,T.active,"warn",`not verified in over ${D.stale_days} days.`],
    ["unscoped constraints",T.constraints-T.scoped,T.constraints,"warn",
     "constraints scoped 'global' never fire at edit time and have no path to check drift against."],
    ["distilled but never recalled",T.distilled-T.recalled,T.distilled,"bad",
     "the cost of capture was paid and the benefit never collected."]];
  $("#health").innerHTML=`
  <h2>Health across the mesh</h2>
  <p class="sub">Every number is a share of the population it is drawn from, so a big store does not
  look unhealthy just for being big.</p>
  <div class="pan"><div class="funnel">
  ${rows.map(([l,n,d,cls,note])=>`
    <div style="margin-bottom:11px">
      <div class="fr"><div class="fl">${l}</div>
        <div class="trk"><div class="fill bar-${cls==='bad'?'bad':'warn'}"
          style="width:${d?n/d*100:0}%"></div></div>
        <div class="fv"><b>${n}</b> <span style="color:var(--dim2)">/ ${d} (${pct(n,d)}%)</span></div></div>
      <div class="note" style="margin-left:196px">${note}</div>
    </div>`).join("")}
  </div></div>
  <div class="pan">
    <h2>Worst offenders</h2>
    <p class="sub">Projects ranked by total flagged entries.</p>
    <table><thead><tr><th>project</th><th class="num">mojibake</th><th class="num">thin</th>
      <th class="num">untagged</th><th class="num">stale</th><th class="num">total</th></tr></thead><tbody>
    ${P.map(p=>({p,n:p.stats.moji_ck+p.stats.thin+p.stats.notags+p.stats.stale}))
       .filter(x=>x.n).sort((a,b)=>b.n-a.n).map(({p,n})=>`<tr data-p="${esc(p.name)}">
      <td><b>${esc(p.name)}</b></td>
      <td class="num" style="${p.stats.moji_ck?'color:var(--bad)':''}">${p.stats.moji_ck||"-"}</td>
      <td class="num">${p.stats.thin||"-"}</td><td class="num">${p.stats.notags||"-"}</td>
      <td class="num">${p.stats.stale||"-"}</td><td class="num"><b>${n}</b></td></tr>`).join("")}
    </tbody></table>
  </div>`;
  $("#health").querySelectorAll("tr[data-p]").forEach(tr=>tr.onclick=()=>openProject(tr.dataset.p));
}

/* ---------- project drawer ---------- */
let eq="", ekind="all";
function openProject(name){
  const p=P.find(x=>x.name===name); if(!p) return;
  eq=""; ekind="all";
  $("#dt").textContent=name;
  drawProject(p);
  $("#drawer").classList.add("open"); $("#scrim").classList.add("on");
}
function drawProject(p){
  const s=p.stats;
  const supMap={}; p.entries.forEach(e=>{if(e.sup)supMap[e.sup]=e;});
  const chains=p.entries.filter(e=>supMap[e.id]).map(e=>({newer:e,older:supMap[e.id]}));
  const byMonth={}; p.entries.forEach(e=>{const m=(e.created||"").slice(0,7); if(m)byMonth[m]=(byMonth[m]||0)+1;});
  const months=Object.keys(byMonth).sort(); const mmax=Math.max(...Object.values(byMonth),1);
  const scopes={}; p.entries.filter(e=>e.kind==="constraint"&&e.status==="active")
    .forEach(e=>{const k=e.scope||"global"; scopes[k]=(scopes[k]||0)+1;});
  let list=p.entries.filter(e=>(ekind==="all"||e.kind===ekind)&&
    (!eq||(e.t+" "+e.id+" "+e.tags.join(" ")+" "+e.scope).toLowerCase().includes(eq)));

  $("#dbody").innerHTML=`
  <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(104px,1fr))">
    ${card(s.entries,"entries")}${card(s.active,"active")}
    ${card(s.links,"supersedes",s.links?"":"none recorded",s.entries>8&&!s.links?"warn":"")}
    ${card(s.distilled||"-","distilled")}${card(s.recalls||"-","recalls",s.distilled&&!s.recalls?"never read":"")}
  </div>
  ${months.length>1?`<div class="pan" style="padding:13px 15px">
    <h2>Recording activity</h2><p class="sub">entries created per month</p>
    <div class="spark">${months.map(m=>`<i style="height:${byMonth[m]/mmax*100}%" title="${m}: ${byMonth[m]}"></i>`).join("")}</div>
    <div style="display:flex;justify-content:space-between;font-size:10.5px;color:var(--dim2);margin-top:4px">
      <span>${months[0]}</span><span>${months[months.length-1]}</span></div></div>`:""}

  ${chains.length?`<div class="pan" style="padding:13px 15px"><h2>What changed</h2>
    <p class="sub">recorded supersessions, newest first</p>
    ${chains.map(c=>`<div class="chain">
      <div><code>${esc(c.newer.id)}</code> ${esc(c.newer.t.slice(0,140))}</div>
      <div class="old">replaced <code>${esc(c.older.id)}</code> ${esc(c.older.t.slice(0,110))}</div>
    </div>`).join("")}</div>`
   :(s.entries>8?`<div class="pan" style="padding:13px 15px"><h2>What changed</h2>
    <p class="sub" style="margin:0">No supersession links across ${s.entries} entries. Revisions were
    made in place, so this store can say what is true now but not what changed or why.</p></div>`:"")}

  ${Object.keys(scopes).length?`<div class="pan" style="padding:13px 15px"><h2>Constraint scopes</h2>
    <p class="sub">what each active rule guards</p>
    ${Object.entries(scopes).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<div class="fr"
      style="grid-template-columns:200px 1fr 54px"><div class="fl">
      ${k==="global"?'<span style="color:var(--warn)">global</span>':esc(k)}</div>
      <div class="trk"><div class="fill ${k==="global"?"bar-warn":"bar-ac"}"
        style="width:${v/Math.max(...Object.values(scopes))*100}%"></div></div>
      <div class="fv"><b>${v}</b></div></div>`).join("")}</div>`:""}

  <div class="pan" style="padding:13px 15px">
    <h2>Entries</h2>
    <div class="controls" style="margin:9px 0 11px">
      <input type="search" id="eqi" placeholder="search this log…" value="${esc(eq)}">
      ${["all","decision","constraint","pipeline"].map(k=>
        `<span class="chip ${ekind===k?"on":""}" data-k="${k}">${k}</span>`).join("")}
      <span class="count">${list.length}</span>
    </div>
    ${list.length?`<div class="eg">${list.map(e=>`<div class="e">
      <div class="eh">
        <span class="pill ${e.kind[0]==='d'?'d':e.kind[0]==='c'?'c':'p'}">${esc(e.kind)}</span>
        <code>${esc(e.id)}</code>
        ${e.status!=="active"?`<span class="pill ${e.status==="superseded"?"sup":"dep"}">${esc(e.status)}</span>`:""}
        ${e.scope&&e.scope!=="global"?`<span class="pill">${esc(e.scope)}</span>`:""}
        ${e.scope==="global"?`<span class="pill p">global</span>`:""}
      </div>
      <div class="et">${esc(e.t)}</div>
      <div class="flags">
        ${e.tags.map(t=>`<span class="flag notags">${esc(t)}</span>`).join("")}
        ${e.moji?'<span class="flag moji">mojibake</span>':""}
        ${e.thin?`<span class="flag thin">thin (${e.rlen}c)</span>`:""}
        ${e.notags?'<span class="flag notags">no tags</span>':""}
        ${e.age>D.stale_days?`<span class="flag stale">${e.age}d</span>`:""}
        ${e.sup?`<span class="flag stale">→ ${esc(e.sup)}</span>`:""}
      </div></div>`).join("")}</div>`:`<div class="empty">no matches</div>`}
  </div>`;
  const inp=$("#eqi");
  if(inp){inp.oninput=ev=>{eq=ev.target.value.toLowerCase();drawProject(p);
    const n=$("#eqi");n.focus();n.setSelectionRange(n.value.length,n.value.length);};}
  $("#dbody").querySelectorAll(".chip[data-k]").forEach(c=>c.onclick=()=>{ekind=c.dataset.k;drawProject(p);});
}
function closeDrawer(){$("#drawer").classList.remove("open");$("#scrim").classList.remove("on");}
$("#dx").onclick=closeDrawer; $("#scrim").onclick=closeDrawer;

/* ---------- nav ---------- */
const VIEWS={over:renderOver,proj:renderProj,know:renderKnow,health:renderHealth};
function show(v){
  Object.keys(VIEWS).forEach(k=>{$("#"+k).hidden=k!==v;});
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.v===v));
  VIEWS[v](); location.hash=v;
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>show(t.dataset.v));
document.addEventListener("keydown",e=>{
  if(e.key==="Escape")closeDrawer();
  if(e.key==="/"&&!/input|textarea/i.test(document.activeElement.tagName)){
    e.preventDefault(); if($("#proj").hidden)show("proj"); setTimeout(()=>$("#pq")&&$("#pq").focus(),0);}
});
show(["over","proj","know","health"].includes(location.hash.slice(1))?location.hash.slice(1):"over");
</script></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", action="append", default=None,
                    help="Directory whose children are projects. Repeatable. "
                         "Default: the directory holding this repo.")
    ap.add_argument("--out", default=os.path.join(REPO, "dashboard.html"))
    ap.add_argument("--open", action="store_true", dest="open_",
                    help="Open the result in a browser.")
    args = ap.parse_args(argv)
    roots = args.root or [DEFAULT_ROOT]

    payload, out = build(roots, args.out)
    t = payload["totals"]
    print("projects: %d   entries: %d   distilled: %d   recalled: %d (%d%%)"
          % (len(payload["projects"]), t["entries"], t["distilled"], t["recalled"],
             (t["recalled"] * 100 // t["distilled"]) if t["distilled"] else 0))
    print("flags: %d mojibake (ck) / %d mojibake (cambium) / %d thin / %d stale"
          % (t["moji_ck"], t["moji_cam"], t["thin"], t["stale"]))
    print("wrote %s" % out)
    if args.open_:
        webbrowser.open("file:///" + os.path.abspath(out).replace("\\", "/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
