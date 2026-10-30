"""Readable projections of an instrument: semantic hierarchy, overview, and offline explorer.

The source document stays intact. Collection wrappers are transparent in the semantic view;
metadata and connection records belong in the inspector rather than in a wall of boxes.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter, deque
from pathlib import Path

COLLECTIONS = {
    "layers", "keygroups", "oscillators", "inserts", "auxs", "eventprocessors", "children",
    "modulations", "mods", "controlsignalsources", "connections", "busrouters", "sends",
}
DATA_BRANCHES = {"properties", "script", "scriptdata", "state", "signalconnection"}
ROLES = {"oscillators": "Sources", "inserts": "Effects", "eventprocessors": "Events",
         "modulations": "Modulation", "mods": "Modulation", "controlsignalsources": "Modulation",
         "busrouters": "Routing", "sends": "Routing", "auxs": "Aux returns"}


def node_id(path: str) -> str:
    return "n" + hashlib.sha256(path.encode()).hexdigest()[:16]


def semantic_view(document: dict) -> dict:
    """Collapse collection-only levels; retain stable source order, paths, and all named modules."""
    source = document["nodes"]
    by_path = {n["path"]: n for n in source}
    parents = {n["path"]: n.get("parent") for n in source}
    visible, hidden_branch, roles = {}, set(), {}
    for node in source:  # the export is already depth-first preorder
        path, kind = node["path"], node["type"].lower()
        parent = parents[path]
        if parent in hidden_branch or kind in DATA_BRANCHES:
            hidden_branch.add(path)
            continue
        if kind in COLLECTIONS:
            continue
        role = ROLES.get(str(node.get("relation", "")).lower(), "Modules")
        while parent and parent not in visible:
            wrapper = by_path.get(parent, {})
            role = ROLES.get(str(wrapper.get("type", "")).lower(), role)
            parent = parents.get(parent)
        if kind == "busrouter":
            role = "Routing"
        visible[path] = {**node, "parent": parent, "depth": visible[parent]["depth"] + 1 if parent else 0,
                         "children": [], "role": role}
        roles[path] = role
        if parent:
            visible[parent]["children"].append(path)
    nodes = list(visible.values())
    return {"nodes": nodes, "hidden_nodes": len(source) - len(nodes),
            "edges": [{"kind": "contains", "source": n["parent"], "target": n["path"],
                       "relation": roles[n["path"]]} for n in nodes if n["parent"]]}


def diagram_view(view: dict, limit: int) -> tuple[list, list]:
    """Breadth-first budget: include sibling Layers before filling the first Layer's internals."""
    by_path = {n["path"]: n for n in view["nodes"]}
    queue = deque(n["path"] for n in view["nodes"] if n["parent"] is None)
    shown = []
    while queue and len(shown) < limit:
        node = by_path[queue.popleft()]
        shown.append(node)
        queue.extend(node["children"])
    paths = {n["path"] for n in shown}
    return shown, [e for e in view["edges"] if e["source"] in paths and e["target"] in paths]


def _name(node):
    return str(node.get("displayName") or node["name"])


def _clip(value, length=65):
    # No silent truncation of identity: every SVG card also has a full accessible title/path.
    text = " ".join(str(value).split())
    return text if len(text) <= length else text[:length - 1] + "…"


def overview_svg(document: dict, view: dict, limit: int = 150) -> str:
    """A bounded-width, grouped overview; associations are inspected locally in the HTML explorer."""
    nodes = view["nodes"]
    if not nodes:
        raise ValueError("Instrument view has no visible root")
    by_path = {n["path"]: n for n in nodes}
    root = nodes[0]
    anchors = [n for n in nodes if n["type"] in {"Layer", "AuxEffect"}]
    if not anchors:
        anchors = [n for n in nodes if n["type"] == "Keygroup"]
    anchors = anchors[:max(0, limit - 1)]
    shared = [by_path[p] for p in root["children"] if by_path[p]["type"] not in {"Layer", "AuxEffect", "Keygroup"}]
    shared = shared[:max(0, limit - 1 - len(anchors))]
    # Root children have collection wrappers removed, so master effects/events remain in source order.
    parts = []

    def text(x, y, value, size=14, fill="#263449", weight=400):
        parts.append(f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-weight="{weight}">'
                     f'{html.escape(str(value))}</text>')

    def card(node, x, y, width, height, accent="#087f78"):
        parts.extend([(f'<a href="#{node_id(node["path"])}" data-node-path="{html.escape(node["path"], quote=True)}" '
                      f'aria-label="{html.escape(_name(node), quote=True)}">'),
                      f'<title>{html.escape(_name(node) + " — " + node["path"])}</title>',
                      (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="12" '
                      'fill="#ffffff" stroke="#d8e1e9"/>'),
                      f'<rect x="{x}" y="{y + 16}" width="4" height="30" rx="2" fill="{accent}"/>'])

    text(36, 36, "INSTRUMENT / STRUCTURE OVERVIEW", 14, "#687a8c", 600)
    card(root, 36, 56, 1128, 94, "#4059c8")
    text(58, 88, _clip(_name(root), 74), 25, "#172841", 650)
    counts = Counter(n["type"] for n in nodes)
    text(58, 121, f'{counts["Layer"]} layers   ·   {counts["Keygroup"]} keygroups   ·   '
         f'{len(nodes)} modules   ·   {len(document["nodes"])} source records', 16, "#627389")
    parts.append('</a>')
    y = 184
    if shared:
        text(36, y, "PROGRAM-LEVEL MODULES · SOURCE ORDER", 14, "#687a8c", 600)
        y += 18
        for offset in range(0, min(len(shared), 12), 3):
            for col, node in enumerate(shared[offset:offset + 3]):
                x = 36 + col * 382
                card(node, x, y, 364, 66, "#be7927")
                text(x + 18, y + 26, _clip(_name(node), 32), 17, "#263449", 600)
                text(x + 18, y + 48, _clip(node["type"], 42), 14, "#687a8c")
                parts.append('</a>')
            y += 78
        if len(shared) > 12:
            text(36, y, f"+ {len(shared) - 12} more program modules in the explorer", 13, "#627389")
            y += 28
        y += 20
    groups = [("LAYERS", [n for n in anchors if n["type"] != "AuxEffect"]),
              ("AUX RETURNS", [n for n in anchors if n["type"] == "AuxEffect"])]
    for title, group in groups:
        if not group:
            continue
        text(36, y, f"{title} / {len(group):02d}", 14, "#687a8c", 600)
        y += 20
        for offset in range(0, len(group), 2):
            for col, node in enumerate(group[offset:offset + 2]):
                x = 36 + col * 576
                card(node, x, y, 552, 190)
                text(x + 20, y + 31, _clip(_name(node), 42), 21, "#172841", 650)
                descendants = []
                queue = deque(node["children"])
                while queue:
                    child = by_path[queue.popleft()]
                    descendants.append(child)
                    queue.extend(child["children"])
                roles = Counter(n["role"] for n in descendants)
                keygroups = sum(n["type"] == "Keygroup" for n in descendants)
                text(x + 20, y + 55, f'{node["type"]}  ·  {keygroups} keygroups  ·  {len(descendants)} modules',
                     14, "#687a8c")
                for row, role in enumerate(["Sources", "Effects", "Modulation", "Routing"]):
                    members = [_name(n) for n in descendants if n["role"] == role]
                    sample = ", ".join(members[:3]) if members else "—"
                    if len(members) > 3:
                        sample += f"  +{len(members) - 3}"
                    text(x + 20, y + 85 + row * 23, f'{role}  {roles[role]:02d}', 14, "#687a8c")
                    text(x + 137, y + 85 + row * 23, _clip(sample, 40), 15)
                parts.append('</a>')
            y += 206
        y += 22
    text(36, y + 6, "Composition overview · not DSP signal flow. Select a module in the HTML explorer to inspect its connections.",
         14, "#687a8c")
    height = y + 36
    return '\n'.join([
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}" '
         'role="group" aria-labelledby="overview-title overview-desc" font-family="Segoe UI,Arial,sans-serif">'),
        '<title id="overview-title">Instrument structure overview</title>',
        ('<desc id="overview-desc">Grouped layers, program modules and aux returns. '
        'Metadata containers are hidden; the HTML explorer retains complete records and associations.</desc>'),
        '<rect width="100%" height="100%" fill="#f3f6f9"/>', *parts, '</svg>'])


def explorer_html(document: dict, view: dict, svg: str, outputs: dict) -> str:
    """Offline-only page. All user values enter DOM through textContent, never executable markup."""
    data = json.dumps({"document": document, "view": view}, ensure_ascii=True).replace("<", "\\u003c")
    links = " ".join(f'<a href="{html.escape(Path(path).name, quote=True)}">{html.escape(kind.upper())}</a>'
                     for kind, path in outputs.items() if kind != "html")
    title = html.escape(_name(document["nodes"][0]))
    substitutions = {"__TITLE__": title, "__LINKS__": links, "__OVERVIEW__": svg, "__PAYLOAD__": data}
    return re.sub(r"__TITLE__|__LINKS__|__OVERVIEW__|__PAYLOAD__", lambda m: substitutions[m[0]], _PAGE)


_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>__TITLE__ · Instrument explorer</title>
<style>
:root{color-scheme:light;--ink:#182a40;--muted:#61748b;--line:#dce3eb;--accent:#4059c8;--paper:#fff}
*{box-sizing:border-box}body{margin:0;background:#f3f6f9;color:var(--ink);font:14px/1.55 'Segoe UI',Arial,sans-serif}
button,input,select{font:inherit}button,a,input,summary,select{touch-action:manipulation}button{cursor:pointer}
button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #6b88ff;outline-offset:3px}
header{background:#fff;border-bottom:1px solid var(--line);padding:25px 32px 22px}.eyebrow{font-size:11px;font-weight:700;letter-spacing:2px;color:var(--accent)}
h1{font-size:28px;line-height:1.25;margin:8px 0}h2{font-size:18px;margin:0}h3{font-size:14px;margin:24px 0 10px}
.headrow{display:flex;gap:20px;align-items:center;justify-content:space-between}.muted,small{color:var(--muted)}
.downloads{display:flex;flex-wrap:wrap;gap:7px}.downloads a,.pill{font-size:11px;font-weight:650;letter-spacing:.4px;border:1px solid var(--line);border-radius:6px;padding:5px 9px;text-decoration:none;color:#40536a;background:#fff}
#status{padding:5px 10px;border-radius:20px;background:#e4f4ed;color:#286149;font-size:12px;white-space:nowrap}.warn{background:#fff0d9!important;color:#895918!important}
.layout{display:grid;grid-template-columns:280px minmax(0,1fr);max-width:1900px;margin:auto;min-height:80vh}
nav{padding:24px 16px;background:#fff;border-right:1px solid var(--line)}.navheading{display:flex;justify-content:space-between;align-items:center;margin:0 8px 18px;font-weight:650}
.search{width:100%;border:1px solid var(--line);padding:9px 11px;border-radius:7px;background:#f7f9fc;color:var(--ink)}
.toggle{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted);margin:12px 0 20px}.tree{overflow:auto;max-height:calc(100vh - 310px);min-height:260px}
.tree details{margin-left:12px}.tree>details{margin-left:0}.tree summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:4px}.tree summary::before{content:'▸';color:#8996a8;width:12px;flex:none}.tree details[open]>summary::before{content:'▾'}
.tree button{border:0;background:none;text-align:left;color:var(--ink);padding:7px 6px;flex:1;min-width:0;border-radius:5px;overflow-wrap:anywhere}
.tree button:hover,.tree .active{background:#edf1ff;color:#354eb7}.leaf{display:flex;margin-left:24px}.tree small{font-size:10px;margin-left:5px}.more{font-size:12px;color:var(--accent)!important}
main{padding:26px;min-width:0}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:20px;align-items:start}
.panel{background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}.panelhead{padding:18px 20px;border-bottom:1px solid var(--line)}.panelhead p{font-size:12px;margin:6px 0 0;color:var(--muted)}
.map svg{display:block;width:100%;height:auto}.map svg a:hover rect[fill="#ffffff"]{fill:#edf5ff}.map svg a:focus rect{stroke:#4059c8;stroke-width:3}
.legend{display:flex;flex-wrap:wrap;gap:14px;padding:12px 20px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.legend b{font-weight:650;color:#344963}
.inspector{position:sticky;top:16px}.inspector .body{padding:18px 20px;max-height:calc(100vh - 125px);overflow:auto}#selected-name{font-size:20px;overflow-wrap:anywhere;margin:8px 0}
.path{font:11px/1.6 Consolas,monospace;color:var(--muted);overflow-wrap:anywhere;background:#f5f7fa;border-radius:5px;padding:8px;margin:12px 0}
.breadcrumb{font-size:11px;color:var(--muted);margin-bottom:8px}.tabbar{display:flex;flex-wrap:wrap;gap:4px;margin-top:18px}.tabbar button{border:1px solid var(--line);border-radius:5px;background:#fff;padding:6px 8px;font-size:12px}.tabbar button[aria-selected=true]{background:#4059c8;border-color:#4059c8;color:white}
.kv{border-collapse:collapse;width:100%;font-size:12px}.kv th,.kv td{text-align:left;vertical-align:top;border-bottom:1px solid #edf0f4;padding:8px 4px;overflow-wrap:anywhere;white-space:pre-wrap;max-width:160px}.kv th{color:var(--muted);font-weight:500}
.relation{border:1px solid var(--line);border-left:3px solid #439789;border-radius:6px;padding:10px;margin:10px 0;font-size:12px;overflow-wrap:anywhere}.relation.routing{border-left-color:#b77d29}.relation .tag{font-size:10px;letter-spacing:1px;color:var(--muted);text-transform:uppercase}.relation button{background:none;border:0;padding:0;color:#4059c8;text-align:left;text-decoration:underline}.relation pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:10px;color:var(--muted)}
.relationtools{display:flex;gap:8px;align-items:center;margin:12px 0}.relationtools select{padding:5px;max-width:100%;border:1px solid var(--line);border-radius:4px}.empty{font-size:13px;color:var(--muted);padding:10px 0}.note{font-size:12px;color:var(--muted);padding:16px 20px;background:#f9fafc;border-top:1px solid var(--line)}
.subnode{display:block;width:100%;background:#f8fafc;border:1px solid var(--line);border-radius:5px;text-align:left;padding:8px;margin:6px 0;color:#334b69}.subnode small{display:block;margin:4px 0 0;font-size:11px}.sr{position:absolute;left:-10000px}footer{padding:20px 2px;color:var(--muted);font-size:11px}#source-details{margin-top:20px}#source-details pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}
@media(min-width:1700px){.workspace{grid-template-columns:minmax(0,1fr) 390px}}
@media(max-width:1550px){.layout{grid-template-columns:240px minmax(0,1fr)}.workspace{grid-template-columns:minmax(0,1fr)}.inspector{position:static}.inspector .body{max-height:none}.tree{position:sticky;top:16px}}
@media(max-width:720px){header{padding:20px}.headrow{align-items:flex-start;flex-direction:column}.layout{display:block}nav{border-right:0;border-bottom:1px solid var(--line)}.tree{position:static;max-height:280px;min-height:0}main{padding:15px}.downloads{margin-top:10px}h1{font-size:24px}}
@media print{nav,.downloads,.tabbar{display:none}.layout,.workspace{display:block}header,main{padding:12px}.inspector{position:static}.inspector .body{max-height:none}.panel{break-inside:avoid}}
</style></head><body>
<header><div class="headrow"><div><div class="eyebrow">FALCON / INSTRUMENT ATLAS</div><h1>__TITLE__</h1><div class="muted" id="subtitle"></div></div><div><div id="status"></div><div class="downloads" style="margin-top:14px">__LINKS__</div></div></div></header>
<div class="layout"><nav aria-label="Instrument hierarchy"><div class="navheading">Hierarchy <span class="pill" id="module-count"></span></div>
<label class="sr" for="search">Find a module or parameter</label><input id="search" class="search" placeholder="Find a module or parameter…" type="search">
<label class="toggle"><input id="raw" type="checkbox"> Show XML / data containers</label><div class="tree" id="tree"></div></nav>
<main><div class="workspace"><section class="panel map" aria-label="Instrument overview"><div class="panelhead"><h2>Instrument overview</h2><p>Start with a layer. Select a card or tree item to inspect its modules and connections.</p></div>
__OVERVIEW__<div class="legend"><span><b>Blue</b> Program</span><span><b>Teal</b> Layers & aux returns</span><span><b>Amber</b> Program modules</span></div>
<div class="note" id="coverage"></div></section>
<section class="panel inspector" aria-label="Selected module"><div class="panelhead"><span class="eyebrow">MODULE INSPECTOR</span></div><div class="body"><div class="breadcrumb" id="breadcrumb"></div><span class="pill" id="selected-type"></span><h2 id="selected-name"></h2><div class="path" id="selected-path"></div>
<div class="tabbar" role="tablist" aria-label="Module details"><button role="tab" id="tab-children" data-tab="children" aria-controls="detail">Modules</button><button role="tab" id="tab-parameters" data-tab="parameters" aria-controls="detail">Parameters</button><button role="tab" id="tab-connections" data-tab="connections" aria-controls="detail">Connections</button></div>
<div id="detail" role="tabpanel" aria-live="polite"></div></div></section></div>
<details class="panel" id="source-details"><summary class="panelhead">Source, coverage & limitations</summary><div class="panelhead" id="source-content"></div></details>
<footer>Local, self-contained export. Hierarchy and module order describe composition, not verified DSP signal flow.</footer></main></div>
<script id="instrument-data" type="application/json">__PAYLOAD__</script>
<script>
'use strict';
const {document:doc,view}=JSON.parse(document.getElementById('instrument-data').textContent);
const $=id=>document.getElementById(id), all=new Map(doc.nodes.map(n=>[n.path,n])), semantic=new Map(view.nodes.map(n=>[n.path,n]));
const refs=doc.edges.filter(e=>!['contains','reference'].includes(e.kind)), byNode=new Map();
for(const e of refs)for(const p of new Set([e.source,e.target])){if(!byNode.has(p))byNode.set(p,[]);byNode.get(p).push(e);}
let selected=doc.root,activeTab='children',kindFilter='all',raw=false;
const name=n=>n.displayName||n.name, value=x=>typeof x==='string'?x:JSON.stringify(x,null,2);
function el(tag,text,cls){const x=document.createElement(tag);if(text!==undefined)x.textContent=text;if(cls)x.className=cls;return x;}
function button(text,fn,cls){const b=el('button',text,cls);b.type='button';b.onclick=fn;return b;}
const map=()=>raw?all:semantic;
function currentNode(p){return map().get(p)||all.get(p);}
function choose(p){if(!all.has(p))return;selected=p;renderInspector();document.querySelectorAll('.tree button[data-path]').forEach(b=>{b.classList.toggle('active',b.dataset.path===p);b.setAttribute('aria-current',b.dataset.path===p?'true':'false');});}
function selectAndShow(p){choose(p);if(matchMedia('(max-width:1550px)').matches)document.querySelector('.inspector').scrollIntoView({behavior:'smooth',block:'start'});}
function jump(p){return all.has(p)?button(name(all.get(p)),()=>selectAndShow(p)):el('span',p||'(unresolved)');}
function pageList(items,container,draw){let end=0;function more(){const upto=Math.min(end+80,items.length);for(;end<upto;end++)draw(items[end],container);if(end<items.length){const b=button('Show next '+Math.min(80,items.length-end)+' ('+(items.length-end)+' remaining)',()=>{b.remove();more();},'more');container.append(b);}}more();}
function treeNode(n,parent){const children=(n.children||[]).map(p=>map().get(p)).filter(Boolean);const b=button(name(n),()=>selectAndShow(n.path));b.dataset.path=n.path;b.classList.toggle('active',n.path===selected);b.setAttribute('aria-current',n.path===selected?'true':'false');b.title=n.type+' — '+n.path;
if(children.length){const d=el('details'),s=el('summary');s.append(b,el('small',children.length));d.append(s);let built=false;d.addEventListener('toggle',()=>{if(d.open&&!built){built=true;pageList(children,d,treeNode);}});parent.append(d);if(n.path===doc.root)d.open=true;}
else{const line=el('div',undefined,'leaf');line.append(b);parent.append(line);}}
function scopeLabel(n){const parts=[];let p=n.parent,guard=0;while(p&&guard++<128){const node=currentNode(p);if(!node)break;parts.unshift(name(node)+' ('+node.type+')');p=node.parent;}return parts.slice(-2).join(' / ');}
function renderTree(){const host=$('tree');host.replaceChildren();const q=$('search').value.trim().toLowerCase();if(q){const matches=[...map().values()].filter(n=>(name(n)+' '+n.type+' '+n.path+' '+(n.parameters||[]).map(p=>p.definition.name).join(' ')).toLowerCase().includes(q));host.append(el('p',matches.length+' matching modules','muted'));pageList(matches,host,(n,parent)=>{const b=button(name(n)+' · '+n.type,()=>selectAndShow(n.path),'subnode');b.dataset.path=n.path;b.append(el('small',scopeLabel(n)));parent.append(b);});if(!matches.length)host.append(el('p','No matching modules. Try another name or enable data containers.','empty'));}
else for(const n of map().values())if(!n.parent)treeNode(n,host);}
function addTable(target,entries){const table=el('table',undefined,'kv');for(const [k,v] of entries){const row=el('tr');row.append(el('th',k),el('td',value(v)));table.append(row);}target.append(table);}
function renderInspector(){const n=currentNode(selected);$('selected-name').textContent=name(n);$('selected-type').textContent=n.type;$('selected-path').textContent=n.selector||n.path;const chain=[];let p=n.parent;let guard=0;while(p&&guard++<128){const node=currentNode(p);if(!node)break;chain.unshift(name(node));p=node.parent;}$('breadcrumb').textContent=chain.join(' / ')||'Instrument root';
for(const b of document.querySelectorAll('[data-tab]')){b.setAttribute('aria-selected',b.dataset.tab===activeTab?'true':'false');b.tabIndex=b.dataset.tab===activeTab?0:-1;}$('detail').setAttribute('aria-labelledby','tab-'+activeTab);const target=$('detail');target.replaceChildren();
if(activeTab==='children'){const children=(n.children||[]).map(p=>currentNode(p)).filter(Boolean);target.append(el('h3',children.length+' direct modules'));pageList(children,target,(c,host)=>host.append(button(name(c)+' · '+c.type,()=>choose(c.path),'subnode')));if(!children.length)target.append(el('p','No child modules. Select Parameters or Connections for details.','empty'));}
if(activeTab==='parameters'){const params=all.get(selected).parameters||[];target.append(el('h3',params.length+' parameters'));if(!params.length)target.append(el('p',doc.parameters_included?'No parameters on this record.':'Parameter values were excluded from this export.','empty'));pageList(params,target,(param,host)=>addTable(host,[[param.definition.name,param.value??param.error??'Unavailable']]));if(all.get(selected).text){const d=el('details'),s=el('summary','Serialized text (never executed)'),pre=el('pre',all.get(selected).text);d.append(s,pre);pre.style.whiteSpace='pre-wrap';pre.style.overflowWrap='anywhere';target.append(d);}}
if(activeTab==='connections'){const edges=byNode.get(selected)||[];target.append(el('h3',edges.length+' direct connections'));const tools=el('div',undefined,'relationtools'),label=el('label','Show '),filter=el('select');filter.setAttribute('aria-label','Filter connection type');for(const v of ['all','modulation','routing']){const o=el('option',v);o.value=v;filter.append(o);}filter.value=kindFilter;filter.onchange=()=>{kindFilter=filter.value;renderInspector();};label.append(filter);tools.append(label);target.append(tools);
const shown=edges.filter(e=>kindFilter==='all'||e.kind===kindFilter);pageList(shown,target,(e,host)=>{const card=el('div',undefined,'relation '+e.kind);card.append(el('div',e.kind+' · '+(e.resolved===false?'symbolic / unresolved':e.external?'external reference':'resolved'),'tag'));const line=el('div');line.append(jump(e.source),el('span',' → '),jump(e.target));card.append(line);if(e.parameter)card.append(el('strong',e.parameter));if(e.settings)card.append(el('pre',value(e.settings)));host.append(card);});if(!shown.length)target.append(el('p','No direct connections for this selection / filter. Select a child module for its own connections.','empty'));}
}
for(const b of document.querySelectorAll('[data-tab]')){b.onclick=()=>{activeTab=b.dataset.tab;renderInspector();};b.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const tabs=[...document.querySelectorAll('[data-tab]')],i=tabs.indexOf(b);const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;activeTab=tabs[next].dataset.tab;renderInspector();tabs[next].focus();};}
$('search').oninput=renderTree;$('raw').onchange=()=>{raw=$('raw').checked;renderTree();renderInspector();};
const hashMap=new Map();
const cardLinks=[...document.querySelectorAll('.map svg a')];for(const a of cardLinks){hashMap.set(a.getAttribute('href').slice(1),a.dataset.nodePath);a.onclick=e=>{e.preventDefault();selectAndShow(a.dataset.nodePath);};}
function followHash(){const p=hashMap.get(location.hash.slice(1));if(p)selectAndShow(p);}
addEventListener('hashchange',followHash);
$('module-count').textContent=view.nodes.length+' modules';$('subtitle').textContent=doc.nodes.length+' records · '+view.nodes.length+' readable modules · '+refs.length+' associations';
$('status').textContent=doc.complete?'Source parsed / complete':'Source incomplete · inspect errors';if(!doc.complete)$('status').classList.add('warn');
$('coverage').textContent=view.hidden_nodes+' collection / metadata records hidden from the simplified view. All records and associations remain available in the tree, inspector and JSON. '+(doc.diagram.truncated?'The UML source is capped; see diagram coverage below.':'');
for(const [heading,body] of [['Source',doc.source],['Diagram coverage',doc.diagram],['Errors',doc.errors],['Structural changes',doc.structural_changes],['Limitations',doc.limitations]]){const block=el('div');block.append(el('h3',heading),el('pre',value(body)));$('source-content').append(block);}
renderTree();renderInspector();followHash();
</script></body></html>'''
