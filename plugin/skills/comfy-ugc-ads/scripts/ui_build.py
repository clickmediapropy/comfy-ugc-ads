#!/usr/bin/env python3
"""Build the editable Comfy UI mirrors of every step the skill runs, and publish them to a Comfy Cloud web-UI folder. Brand-neutral: everything comes
from the take spec / brief / sample files you pass; the only default is the folder name. FREE: it never submits a job (uploads of sample files are free).

Keyframe method (default spec), eight workflows (names are "<folder> - <n> <title>"), each with English labels, node groups step by step, a "Read me" note, refs/prompt/size/length/seed preloaded:
  1 Keyframe stills                   gpt-image-2.5-sunburst base still + edited still, cropped to 576x1024 (SKILL.md step 4)
  2 Speech take                       take_graph.graph(first speech take, stills, --turbo)          (fl2va path)
  3 Speech take with voice reference  take_graph.graph(a later speech take, stills, voice, --turbo)  (ref2va path; packshot when the take shows the product)
  4 Action take (insert)              take_graph.graph(first action take, stills, base 25 steps)     (fl2va path)
  5 Review (Gemini QA)                prompts = review.py AUDIO_SYS / VISUAL_SYS / PROPS_SYS / HAIR_SYS
  6 Fix - cut flagged ranges          Comfy equivalent of media.cut (Video Slice + Concatenate), no AI repair
  7 Assemble + export                 Comfy equivalent of media.concat (loudness) + export 9:16 / 4:5
  8 HeyGen take                       the default model: HeyGen Video 1 from the first still, prompt = h3prompt.heygen_prompt (= heygen_take.py)
A legacy spec ("prompt_style": "legacy") builds the old five: Take 1 - generate, Take 2-N - chained, Review, Fix, Assemble.
Take workflows are DERIVED from take_graph.graph() (draft and HQ graphs are both built there; every difference becomes a draft/HQ switch), so they cannot drift.

  ui_build.py --spec S.json [--folder "Comfy UGC"] [--brief B.md] [--out DIR] [--file-names title|slug] [--publish] [--verify]
  --file-names slug  plain file names (2-speech-take.json): required for the plugin's own workflows_ui/, since claude.ai refuses a zip
                     whose paths have spaces, parentheses or "+". Default "title" ("<folder> - 2 Speech take.json") for a client batch
              [--sample-guide last.png] [--sample-voice voice.wav] [--sample-clip take.mp4] [--sample-takes t1.mp4 t2.mp4 ..]
              [--review-prompt-file P.txt] [--cut-range T0-T1] [--work W] [--only 1,3]
  --publish  POST each JSON to the Cloud userdata store under workflows/<folder>/ (what the web UI sidebar reads; `comfy workflow save` does not show there)
  --verify   after building (and publishing): convert each JSON with `comfy run --print-prompt` (and, with --publish, the copy pulled back from Cloud)
             and compare it with the skill's graph. Exit 1 on any difference.
Run under the comfy-cli python (this script re-executes itself there). Credentials: the comfy CLI's own login; nothing is printed or stored."""
import argparse, collections, copy, json, pathlib, re, subprocess, sys, tempfile, urllib.parse
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ui_sync as S            # read-only Cloud access + UI->API conversion (its venv re-exec helper too)

EDIT = ["#223", "#335"]        # darker blue = nodes a human is expected to edit
PALETTE = ["#3f789e", "#8A8", "#A88", "#88A", "#A8A", "#8AA", "#AA8"]
PROPS = {"cnr_id": "comfy-core", "ver": "0.35.0"}
is_link = S.is_link

# ---------------------------------------------------------------- API graph -> UI workflow (emit engine)
def load_graph():
    from comfy_cli.cql.engine import Graph
    from comfy_cli.cql.loader import resilient_load_object_info
    g = Graph.from_object_info(resilient_load_object_info(mode="cloud", host=None, port=None, on_stale=None))
    g._try_default_annotations()
    return g

def _manual_connect(wf, src_id, src_slot, dst_id, dst_name, W):
    """JSON-level link for what the op engine refuses: a link into a dynamic-combo sub-widget (GeminiNodeV3 model.prompt) needs a socket entry on the destination."""
    nodes = {n["id"]: n for n in wf["nodes"]}; src, dst = nodes[src_id], nodes[dst_id]
    typ = src["outputs"][src_slot]["type"]; ins = dst.setdefault("inputs", [])
    idx = next((i for i, x in enumerate(ins) if x.get("name") == dst_name), None)
    if idx is None:
        ins.append({"name": dst_name, "type": "STRING" if typ in ("*", "COMFY_MATCHTYPE_V3") else typ, "widget": {"name": dst_name}, "link": None}); idx = len(ins) - 1
    lid = W.mint_id(); wf["links"].append([lid, src_id, src_slot, dst_id, idx, typ]); ins[idx]["link"] = lid
    src["outputs"][src_slot]["links"] = (src["outputs"][src_slot].get("links") or []) + [lid]

def _fix_dynamic_layout(wf, api, ids, graph):
    """Rebuild widgets_values from the schema for nodes with a dynamic combo (the op engine leaves the old shape when the selector changes)."""
    from comfy_cli.cql import engine as G
    by_id = {n["id"]: n for n in wf["nodes"]}
    for alias, node in api.items():
        n = by_id[ids[str(alias)]]; m = graph.node(n["type"])
        if m is None: continue
        wv = list(n.get("widgets_values") or []); entries = G._expand_widget_entries(m, wv)
        if not any(e.owner for e in entries): continue
        lits = {k: v for k, v in node["inputs"].items() if not is_link(v)}; new = []
        for i, e in enumerate(entries):
            if e.port is None and e.name == "control_after_generate": new.append("fixed")
            elif e.port is None: new.append(wv[i] if i < len(wv) else "")
            else:
                key = (e.name if e.name.startswith(f"{e.owner}.") else f"{e.owner}.{e.name}") if e.owner else e.name
                d = e.port.options.default
                new.append(lits[key] if key in lits else (d if d is not None else (wv[i] if i < len(wv) and wv[i] != "" else "")))
        n["widgets_values"] = new

def emit(api, graph=None):
    """(ui_workflow, alias->node id). Stages: nodes, widgets, links (engine first, JSON-level fallback)."""
    from comfy_cli import workflow_ops as W
    graph = graph or load_graph(); orig = W._types_compatible
    W._types_compatible = lambda l, d: True if "COMFY_MATCHTYPE" in str(d) or "COMFY_MATCHTYPE" in str(l) else orig(l, d)
    wf = {"id": "00000000-0000-0000-0000-000000000001", "revision": 0, "last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [], "groups": [], "config": {}, "extra": {}, "version": 0.4}
    wf, _, ids = W.apply_specs(wf, graph, [{"op": "add_node", "class_type": n["class_type"], "as": str(k), "allow_deprecated": True} for k, n in api.items()])
    ws = []
    for nid, n in api.items():
        lits = {k: v for k, v in n["inputs"].items() if not is_link(v)}
        for k in sorted(lits, key=lambda k: (k.count("."), k)): ws.append({"op": "set_widget", "node": ids[str(nid)], "widget": k, "value": lits[k]})
    imported = []   # a model the user imported (e.g. the LightX2V ref2v turbo LoRA) is not in the public catalog the engine validates against
    try: wf, _, _ = W.apply_specs(wf, graph, ws)
    except ValueError:
        for op in ws:
            try: wf, _, _ = W.apply_specs(wf, graph, [op])
            except ValueError as e:
                m = re.search(r"known options for \S+ — closest: ([^,]+?)(,|\.\s)", str(e))
                if not m: raise
                wf, _, _ = W.apply_specs(wf, graph, [{**op, "value": m.group(1)}]); imported.append([op["node"], op["widget"], op["value"], m.group(1)])
    manual = []
    for nid, n in api.items():
        for k, v in n["inputs"].items():
            if not is_link(v): continue
            try: wf, _, _ = W.apply_specs(wf, graph, [{"op": "connect", "from": f"{ids[str(v[0])]}.{v[1]}", "to": f"{ids[str(nid)]}.{k}"}])
            except ValueError: _manual_connect(wf, ids[str(v[0])], v[1], ids[str(nid)], k, W); manual.append(f"{nid}.{k}")
    _fix_dynamic_layout(wf, api, ids, graph)
    # a loader whose file name is driven by the draft/HQ switch shows the schema default in the UI (an uninstalled file = 'Missing Models'): show the draft value instead
    by_id = {n["id"]: n for n in wf["nodes"]}
    for nid, n in api.items():
        v = n["inputs"].get("clip_name")
        if n["class_type"] == "CLIPLoader" and is_link(v) and api[v[0]]["class_type"] == "ComfySwitchNode":
            src = api[api[v[0]]["inputs"]["on_false"][0]]["inputs"].get("value")
            if isinstance(src, str): by_id[ids[str(nid)]]["widgets_values"][0] = src
    for node_id, widget, real, placeholder in imported:   # swap the imported file name back in (set through a catalog placeholder above)
        wv = by_id[node_id]["widgets_values"]; wv[wv.index(placeholder)] = real
    if imported: wf.setdefault("extra", {})["skill_imported_models"] = imported
    W.strip_internal(wf); W._types_compatible = orig
    return wf, dict(ids)

def to_api(path):
    """S.to_api for a workflow that may name imported models: convert a copy with catalog placeholders, then put the imported names back by node id."""
    wf = json.load(open(path)); recs = (wf.get("extra") or {}).get("skill_imported_models") or []
    if not recs: return S.to_api(path)
    by_id = {n["id"]: n for n in wf["nodes"]}
    for node_id, widget, real, placeholder in recs:
        wv = by_id[node_id]["widgets_values"]; wv[wv.index(real)] = placeholder
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f: json.dump(wf, f)
    api = S.to_api(f.name)
    for node_id, widget, real, placeholder in recs: api[str(node_id)]["inputs"][widget] = real
    return api

def layout(ui, ids, api, group_of, order, titles, edit, sizes, note_text, name, desc):
    """Clustered layout: one box per group (longest-path depth = column), titles, blue 'edit me' nodes, a Read me note to the right."""
    inv = {v: k for k, v in ids.items()}; nodes = {n["id"]: n for n in ui["nodes"]}
    dep, preds = {}, {a: [] for a in ids}
    for l in ui["links"]: preds[inv[l[3]]].append(inv[l[1]])
    def depth(a, stack=()):
        if a in dep: return dep[a]
        ps = [p for p in preds[a] if group_of[p] == group_of[a] and p not in stack]
        dep[a] = 1 + max((depth(p, stack + (a,)) for p in ps), default=-1); return dep[a]
    for a in ids: depth(a)
    W, PAD, y, boxes = 340, 50, 0, {}
    for g in order:
        members = [a for a in ids if group_of[a] == g]
        if not members: continue
        cols = {}
        for a in members: cols.setdefault(dep[a], []).append(a)
        maxh = 0
        for c in sorted(cols):
            yy = y + PAD + 40
            for a in cols[c]:
                n = nodes[ids[a]]; w, h = n.get("size", [240, 110]); n["size"] = sizes.get(a, [max(w, 300), h])
                n["pos"] = [PAD + c * (W + 60), yy]; yy += n["size"][1] + 40
            maxh = max(maxh, yy - y)
        width = max(max(nodes[ids[a]]["pos"][0] + nodes[ids[a]]["size"][0] for a in members) - 0 + PAD, 500)
        boxes[g] = [0, y, width, maxh + PAD]; y += maxh + PAD + 100
    ui["groups"] = [{"title": g, "bounding": b, "color": PALETTE[i % len(PALETTE)], "font_size": 40, "flags": {}} for i, (g, b) in enumerate(boxes.items())]
    for a, t in titles.items():
        if a in ids: nodes[ids[a]]["title"] = t
    for a in edit:
        if a in ids: nodes[ids[a]]["color"], nodes[ids[a]]["bgcolor"] = EDIT
    nid = max(ids.values()) + 1; x = max(b[2] for b in boxes.values()) + 150
    ui["nodes"].append(markdown(nid, "Read me", note_text, [x, 0], [820, 900]))
    ui["last_node_id"] = max(ui.get("last_node_id", 0), nid)
    ui.setdefault("extra", {})["ds"] = {"scale": 0.25, "offset": [40, 40]}; ui["extra"]["info"] = {"name": name, "description": desc}
    return ui

def markdown(nid, title, text, pos, size):
    return {"id": nid, "type": "MarkdownNote", "pos": pos, "size": size, "flags": {}, "order": 0, "mode": 0, "inputs": [], "outputs": [],
            "properties": dict(PROPS, **{"Node name for S&R": "MarkdownNote"}), "widgets_values": [text], "title": title}

# ---------------------------------------------------------------- compare: a converted UI workflow vs the skill's graph (id independent)
def normalize(api, hq=False):
    """Resolve ComfySwitchNode branches (switch = hq), inline Primitive* values, keep only what an output node depends on."""
    P = copy.deepcopy(api)
    def val(v, depth=0):
        if not is_link(v): return v
        n = P.get(str(v[0]))
        if n and n["class_type"].startswith("Primitive") and depth < 8: return val(n["inputs"].get("value"), depth + 1)
        return v
    def follow(v, depth=0):                      # follow switches to the chosen source link
        while is_link(v) and depth < 20:
            n = P.get(str(v[0]))
            if not n or n["class_type"] != "ComfySwitchNode": break
            v = n["inputs"]["on_true" if bool(hq) else "on_false"]   # the one switch in these graphs is the draft/HQ flag; depth += 1
        return v
    for nid, n in P.items():
        if n["class_type"] == "ComfySwitchNode": continue
        for k, v in list(n["inputs"].items()):
            if is_link(v):
                f = follow(v); n["inputs"][k] = val(f) if not is_link(val(f)) else f
    # nodes that switches deactivate must never change the result: only output-reachable nodes count
    outs = [k for k, n in P.items() if n["class_type"] in ("SaveVideo", "SaveText")]; keep, stack = set(), list(outs)
    while stack:
        k = stack.pop()
        if k in keep: continue
        keep.add(k)
        for v in P[k]["inputs"].values():
            if is_link(v) and str(v[0]) in P: stack.append(str(v[0]))
    return {k: v for k, v in P.items() if k in keep and not v["class_type"].startswith("Primitive") and v["class_type"] != "ComfySwitchNode"}

IGNORE = {"filename_prefix"}
def sigs(P):
    memo = {}
    def lit(v):
        return repr(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else repr(v)
    def sig(k):
        if k in memo: return memo[k]
        n = P[k]; parts = []
        for name in sorted(n["inputs"]):
            if name in IGNORE: continue
            v = n["inputs"][name]
            parts.append((name, ("L", v[1], sig(str(v[0]))) if is_link(v) else ("V", lit(v))))
        memo[k] = (n["class_type"], tuple(parts)); return memo[k]
    return collections.Counter(sig(k) for k in P), {k: sig(k) for k in P}

def compare(expected, got, hq=False):
    """Problems (list of str) between the skill's graph and a converted workflow, after collapsing the draft/HQ switches for the chosen mode."""
    E, G = normalize(expected, hq), normalize(got, hq)
    known = collections.defaultdict(set)      # the UI converter adds widget defaults the source graph leaves out (e.g. SaveVideo codec ""): not a difference
    for n in E.values(): known[n["class_type"]] |= set(n["inputs"])
    for n in G.values(): n["inputs"] = {k: v for k, v in n["inputs"].items() if is_link(v) or k in known[n["class_type"]]}
    ce, _ = sigs(E); cg, _ = sigs(G); probs = []
    if ce == cg: return probs
    cls = lambda P: collections.Counter(n["class_type"] for n in P.values())
    for c in sorted(set(cls(E)) | set(cls(G))):
        if cls(E)[c] != cls(G)[c]: probs.append(f"node count {c}: skill {cls(E)[c]} vs workflow {cls(G)[c]}")
    lits = lambda P, c: [{k: v for k, v in n["inputs"].items() if not is_link(v) and k not in IGNORE} for n in P.values() if n["class_type"] == c]
    for c in sorted(set(cls(E)) & set(cls(G))):
        a, b = lits(E, c), lits(G, c)
        norm = lambda d: json.dumps({k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in d.items()}, sort_keys=True)
        a2, b2 = [x for x in map(norm, a) if x not in map(norm, b)], [x for x in map(norm, b) if x not in map(norm, a)]
        for x, y in zip(a2, b2): probs.append(f"{c}: skill {x[:200]} vs workflow {y[:200]}")
    if not probs: probs.append("same nodes and values but different wiring (links)")
    return probs

# ---------------------------------------------------------------- uploads (free) and sample files
def cloud_name(work, path):
    import take_graph as T
    return T.upload(pathlib.Path(work), path)

def make_sample(work, kind):
    """A tiny valid placeholder when no sample file is given (Cloud validates that input files exist)."""
    work = pathlib.Path(work); work.mkdir(parents=True, exist_ok=True)
    p = work / {"png": "placeholder.png", "wav": "placeholder.wav", "mp4": "placeholder.mp4"}[kind]
    if not p.exists():
        a = {"png": ["-f", "lavfi", "-i", "color=c=gray:s=576x1024", "-frames:v", "1"], "wav": ["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "3"],
             "mp4": ["-f", "lavfi", "-i", "color=c=black:s=576x1024:r=24:d=16", "-f", "lavfi", "-i", "anullsrc=r=32000:cl=stereo", "-t", "16", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]}[kind]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *a, str(p)], check=True)
    return str(p)

# ---------------------------------------------------------------- brief -> facts for the Read me
def brief_facts(path):
    if not path: return {}
    txt = pathlib.Path(path).read_text(); f = {"title": (re.search(r"^#\s+(.+)$", txt, re.M) or [None, pathlib.Path(path).stem])[1].strip(), "path": str(path)}
    sec = lambda h: (re.search(rf"^##\s+{h}[^\n]*\n(.*?)(?=^##\s|\Z)", txt, re.M | re.S | re.I) or [None, ""])[1].strip()
    f["must_not"] = sec("Must not"); f["channel"] = (re.search(r"^Channel:\s*(.+)$", txt, re.M) or [None, ""])[1].strip()
    return f

def read_me(title, what, steps, extra="", facts=None, not_mirrored=""):
    s = f"## {title}\n\n{what}\n\n**How to use**\n" + "\n".join(f"{i + 1}. {x}" for i, x in enumerate(steps))
    if extra: s += "\n\n" + extra
    if facts:
        s += f"\n\n**Brief** ({facts['path']}): {facts['title']}" + (f", {facts['channel']}" if facts.get("channel") else "")
        if facts.get("must_not"): s += "\n\nStay inside the brief's Must-not list:\n" + facts["must_not"]
    if not_mirrored: s += "\n\n**Not in this workflow (runs locally in the skill)**\n" + not_mirrored
    return s + ("\n\n**License**: the H3 community license excludes US/EU/UK/KR use, outputs included. Tests only until the client has a license from MiniMax." if "H3" in what else "") \
        + "\n\nSource: `scripts/ui_build.py` in the repo. Edit here in the UI, then run `scripts/ui_sync.py pull \"<name>\"` and the agent updates the skill to match."

# ---------------------------------------------------------------- 1 + 2: take workflows, derived from take_graph.graph()
def switchify(gd, gh, flag="in_hq"):
    """One graph with a draft/HQ switch. gd/gh = take_graph.graph() for draft/HQ. Every literal that differs becomes two Primitives + a ComfySwitchNode; every
    differing link (model with/without sparse attention) becomes a switch between the two sources. Nodes only in the draft graph stay (the switch routes around them)."""
    out, added = copy.deepcopy(gd), {"_": None}; grp = {}
    out[flag] = {"class_type": "PrimitiveBoolean", "inputs": {"value": True}}; grp[flag] = "1 · Draft / HQ switch"   # HQ is the default (Nico, 2026-10-01)
    prim = {bool: "PrimitiveBoolean", int: "PrimitiveInt", float: "PrimitiveFloat", str: "PrimitiveString"}
    for nid, n in gd.items():
        h = gh.get(nid)
        if not h: continue
        for k, v in n["inputs"].items():
            w = h["inputs"].get(k)
            if w == v: continue
            sw = f"sw_{nid}_{k.replace('.', '_')}"
            if is_link(v) and is_link(w): t, f = w, v
            elif not is_link(v) and not is_link(w):
                out[f"{sw}_hq"] = {"class_type": prim[type(w)], "inputs": {"value": w}}; out[f"{sw}_draft"] = {"class_type": prim[type(v)], "inputs": {"value": v}}
                grp[f"{sw}_hq"] = grp[f"{sw}_draft"] = "1 · Draft / HQ switch"; t, f = [f"{sw}_hq", 0], [f"{sw}_draft", 0]
            else: raise SystemExit(f"cannot switch {nid}.{k}: {v} vs {w}")
            out[sw] = {"class_type": "ComfySwitchNode", "inputs": {"switch": [flag, 0], "on_true": t, "on_false": f}}; grp[sw] = "1 · Draft / HQ switch"
            out[nid]["inputs"][k] = [sw, 0]
    return out, grp

def take_workflow(T, spec, n, files, label, facts, samples):
    q_d, q_h = T.QUALITY["draft"], T.QUALITY["hq"]; seed = int(spec.get("seed", 1000)) + n - 1; take = spec["takes"][n - 1]
    gd = T.graph(spec, n, q_d, files, seed, q_d["steps"], q_d["attention"], q_d["w"], q_d["h"])
    gh = T.graph(spec, n, q_h, files, seed, q_h["steps"], q_h["attention"], q_h["w"], q_h["h"])
    for g in (gd, gh): g["92"]["inputs"]["filename_prefix"] = f"video/{label}"
    api, grp = switchify(gd, gh)
    G = {"0 · Inputs you edit": ["1", "129", "400", "40", "510"], "2 · Models (don't touch)": ["119", "120", "127", "128", "600", "601"],
         "3 · References + H3 reference-to-video": ["136", "500"], "4 · Sampling": ["123", "124", "126", "125"], "5 · Decode + save (video with H3's own voice)": ["121", "122", "130", "92"]}
    for g, ks in G.items():
        for k in ks: grp[k] = g
    order = ["0 · Inputs you edit", "1 · Draft / HQ switch", "2 · Models (don't touch)", "3 · References + H3 reference-to-video", "4 · Sampling", "5 · Decode + save (video with H3's own voice)"]
    secs = T.frames(take["seconds"]) / 24; chained = n > 1
    titles = {"1": f"PROMPT · full script of take {n} (H3 speaks the quoted lines)", "129": "SEED · change it to re-roll this take",
              "400": "Picture 1 · product packshot (<Picture 1> in the prompt)", "40": "Start frame · last frame of the previous take (frame-0 guide)",
              "510": "Audio 1 · voice of take 1 (<Audio 1> in the prompt)", "in_hq": "HQ?  off = DRAFT 576x1024, 20 steps, sparse attention (about $0.30 per 15 s)  |  on = HQ 768x1376, 30 steps, dense attention (about $1.50)",
              "136": f"H3 reference-to-video · length {T.frames(take['seconds'])} frames = {secs:.2f} s at 24 fps", "500": "Start-frame guide (frame 0)",
              "601": "Speed: sparse attention (draft only)", "600": "Speed: Kitchen attention", "119": "Video VAE", "120": "Audio VAE", "92": "SAVE · take video (never reuse a prefix)"}
    friendly = {"128_clip_name": "text encoder file", "136_width": "width", "136_height": "height", "124_steps": "steps", "124_model": "model for the scheduler (HQ dense / draft sparse)", "126_model": "model for the guider (HQ dense / draft sparse)"}
    for k in api:
        if k.startswith("sw_"):
            base = k[3:].rsplit("_", 1)[0] if k.endswith(("_hq", "_draft")) else k[3:]; nm = friendly.get(base, base)
            titles[k] = f"{nm} · HQ value" if k.endswith("_hq") else f"{nm} · draft value" if k.endswith("_draft") else f"switch: {nm}"
    edit = ["1", "129", "400", "40", "510", "in_hq"]
    sizes = {"1": [700, 460]}
    later = "".join(f"\n\n**Take {k} values** (paste the prompt into PROMPT, set SEED and the loaders, length {T.frames(spec['takes'][k - 1]['seconds'])} frames = {T.frames(spec['takes'][k - 1]['seconds']) / 24:.2f} s):\n"
                    f"- SEED {int(spec.get('seed', 1000)) + k - 1}\n- Prompt:\n```\n{T.prompt(spec, spec['takes'][k - 1], k)}\n```" for k in range(3, len(spec["takes"]) + 1)) if chained else ""
    lines = "\n".join(f"{i + 1}. \"{b['say']}\"" for i, b in enumerate(b for b in take["beats"] if "say" in b))
    if not chained:
        what = (f"**Take 1 of the chain** ({secs:.1f} s): MiniMax H3 open weights on Comfy Cloud, text-to-video. H3 invents the creator and speaks the script itself (its own voice). "
                f"{'The only reference is the product packshot (<Picture 1>). ' if take.get('shows_product') else 'No reference image in this take (the bottle does not show). '}"
                f"\n\n**Lines in this take**\n{lines}")
        steps = ["Group 1: pick **HQ?** (off = draft for tests, on = finals). A different setting makes a different woman: decide before the first take and render the whole chain in the same setting.",
                 "Group 0: check the **PROMPT** (the skill builds it with `take_graph.py --print-prompt`), the **SEED** (change it to re-roll) and **Picture 1** (your packshot).",
                 "Run. The take is saved under `video/` with the name in the SAVE node. A draft takes 3-10 min, HQ about 20 min; Comfy Cloud stops jobs at 30-60 min.",
                 "Review the result with the workflow **Review (Gemini QA)**, then render take 2 with **Take 2-N - chained**."]
        extra = "**Preloaded**: prompt, seed, packshot, size (576x1024 draft / 768x1376 HQ), length " + f"{T.frames(take['seconds'])} frames (17k+5 grid at 24 fps; 15 s = 362)."
    else:
        what = (f"**Takes 2 to {len(spec['takes'])} of the chain** (built for take {n}, {secs:.1f} s): H3 continues from the LAST FRAME of the previous take (frame-0 guide) and speaks with the voice of TAKE 1 (<Audio 1>) so the woman and the voice stay the same. "
                f"{'The packshot is <Picture 1>. ' if take.get('shows_product') else ''}\n\n**Lines in take {n}**\n{lines}")
        steps = ["Make the two files from the takes you already rendered (local, free): `python3 scripts/media.py lastframe <previous take>.mp4 last.png` and `ffmpeg -i <take 1>.mp4 -vn -ac 1 -ar 24000 -c:a pcm_s16le voice.wav`.",
                 "Group 0: upload `last.png` on **Start frame** and `voice.wav` on **Audio 1** (the preloaded files are samples from a previous run).",
                 f"Check the **PROMPT** (take {n} preloaded; for another take use the values below), the **SEED** and **HQ?** (same setting as take 1).",
                 "Run, then review with **Review (Gemini QA)**."]
        extra = f"**Preloaded**: take {n} prompt, seed, packshot, sample start frame and voice, size, length {T.frames(take['seconds'])} frames." + later
    desc = ("Take 1: H3 text-to-video" if not chained else f"Take {n}: H3 chained continuation") + f" of the skill's graph (take_graph.py), draft/HQ switch"
    not_m = "Spend guard, ledger, upload of refs and downloading the file: `take_graph.py --submit` does them. In the UI you press Run yourself (it costs credits)."
    return api, grp, order, titles, edit, sizes, read_me(f"{label}", what, steps, extra, facts, not_m), desc

# ---------------------------------------------------------------- keyframe method (2026-09-30): stills + take workflows per kind, derived from take_graph.graph()
ROLES = {"speech-first": "Speech take (a speaker's first take)", "speech-voice": "Speech take with voice reference", "action": "Action take (insert, no speech)"}

def take_args(T, spec, n, role):
    """(voice, path, steps draft, steps HQ, lora) exactly as `take_graph.py` runs this kind of take: speech takes --turbo, action takes base 25 steps."""
    q_d, q_h = T.QUALITY["draft"], T.QUALITY["hq"]; voice = role == "speech-voice"; path = T.route(spec["takes"][n - 1], True, voice)
    if role == "action": return voice, path, T.ACTION_STEPS, max(T.ACTION_STEPS, q_h["steps"]), None
    return voice, path, 8, 8, T.TURBO[path]

def take_graphs(T, spec, n, files, role, label):
    """draft and HQ graphs of take n in this role (the verify step rebuilds the same pair)."""
    q_d, q_h = T.QUALITY["draft"], T.QUALITY["hq"]; seed = int(spec.get("seed", 1000)) + n - 1; voice, path, sd, sh, lora = take_args(T, spec, n, role)
    gd = T.graph(spec, n, q_d, files, seed, sd, q_d["attention"], q_d["w"], q_d["h"], True, voice, True, lora, 1.0, None, path)
    gh = T.graph(spec, n, q_h, files, seed, sh, q_h["attention"], q_h["w"], q_h["h"], True, voice, True, lora, 1.0, None, path)
    for g in (gd, gh): g["92"]["inputs"]["filename_prefix"] = f"video/{label}"
    return gd, gh

def take_workflow_v2(T, spec, n, files, label, facts, role):
    import h3prompt
    take = spec["takes"][n - 1]; gd, gh = take_graphs(T, spec, n, files, role, label); api, grp = switchify(gd, gh)
    voice, path, sd, sh, lora = take_args(T, spec, n, role); fr = T.take_frames(spec, take); secs = fr / 24
    I, M, K, S_, D = "0 · Inputs you edit", "2 · Models (don't touch)", "3 · Keyframes + H3 conditioning", "4 · Sampling", "5 · Decode + save (video with H3's own sound)"
    G = {I: ["1", "129", "400", "40", "41", "510"], M: ["119", "120", "127", "128", "600", "601", "610"], K: ["136", "500", "501"], S_: ["123", "124", "126", "125"], D: ["121", "122", "130", "92"]}
    for g, ks in G.items():
        for k in ks: grp[k] = g
    order = [I, "1 · Draft / HQ switch", M, K, S_, D]
    model = "fl2va (first/last-frame model: the text encoder also sees both stills)" if path == "fl" else "ref2va (reference model: packshot and/or voice references; stills pinned with Add Guide)"
    titles = {"1": f"PROMPT · take {n} in MiniMax's format (`take_graph.py --print-prompt`)", "129": "SEED · change it to re-roll this take",
              "40": "FIRST FRAME · keyframe still (576x1024)", "41": "LAST FRAME · keyframe still (often the same still)", "400": "Picture 1 · product packshot",
              "510": "Audio 1 · voice of this speaker's first take (24 kHz mono wav)", "136": f"H3 {'first/last-frame' if path == 'fl' else 'reference'}-to-video · {fr} frames = {secs:.2f} s",
              "500": "First-frame guide (frame 0)", "501": "Last-frame guide (frame -1)", "610": f"Turbo LoRA · {sd} steps (speech takes only)",
              "601": "Speed: sparse attention (draft only)", "600": "Speed: Kitchen attention", "119": "Video VAE", "120": "Audio VAE", "92": "SAVE · take video (never reuse a prefix)",
              "in_hq": f"HQ?  on (default) = HQ 768x1376, {sh} steps, dense attention  |  off = DRAFT 576x1024, {sd} steps, sparse attention"}
    friendly = {"128_clip_name": "text encoder file", "136_width": "width", "136_height": "height", "124_steps": "steps", "124_model": "model for the scheduler (HQ dense / draft sparse)", "126_model": "model for the guider (HQ dense / draft sparse)"}
    for k in api:
        if k.startswith("sw_"):
            base = k[3:].rsplit("_", 1)[0] if k.endswith(("_hq", "_draft")) else k[3:]; nm = friendly.get(base, base)
            titles[k] = f"{nm} · HQ value" if k.endswith("_hq") else f"{nm} · draft value" if k.endswith("_draft") else f"switch: {nm}"
    lines, tl, kind, sounds = h3prompt.expected(spec, take)
    body = ("\n".join(f"{i + 1}. \"{l}\"" for i, l in enumerate(lines)) if lines else "(no spoken line: an action take has no speaker)") + (f"\n\nScripted sound: {'; '.join(sounds)}" if sounds else "")
    what = {"speech-first": f"**{ROLES[role]}** ({secs:.1f} s): the creator speaks the take's lines with H3's own voice, starting and ending on the keyframe stills. Model: {model}. Speed: 8-step turbo LoRA.",
            "speech-voice": f"**{ROLES[role]}** ({secs:.1f} s): same as a speech take, plus the voice of this speaker's first take as <Audio 1> so the voice stays the same{' and the packshot as <Picture 1>' if take.get('shows_product') else ''}. Model: {model}. Speed: 8-step turbo LoRA.",
            "action": f"**{ROLES[role]}** ({secs:.1f} s): one physical action with no speech (drinking, a yawn, shaking a pill jar). Rendered at 4 s or more (the model's minimum), then cut down to the action in assembly. Model: {model}. Base model at {sd} steps: the turbo LoRA has weaker motion and floated a glass."}[role]
    what += f"\n\n**Take {n} of the spec**\n{body}"
    steps = ["Make the stills first (**1 Keyframe stills**) and set them on **FIRST FRAME** and **LAST FRAME** (the preloaded ones are samples). Every object the take moves must be visible and within reach in the first frame.",
             "Group 1: **HQ?** on (default) = HQ 768x1376, the approved look (a 15 s speech take is about $0.44); off = draft 576x1024 for quick tests.",
             "Group 0: the **PROMPT** comes from `take_graph.py --print-prompt` (MiniMax's own caption format). Edit it in the same style: describe only what is seen and heard, no negations ('nothing floats' makes things float), "
             "spoken words only inside `<d>[English] ...</d>` after `(S1)`. Change **SEED** to re-roll.",
             "Run, then check it with **5 Review (Gemini QA)** and by eye and ear."]
    if role == "speech-voice": steps.insert(1, "**Audio 1**: the voice of this speaker's first take: `ffmpeg -i <first take>.mp4 -vn -ac 1 -ar 24000 -c:a pcm_s16le voice.wav`.")
    extra = (f"**Preloaded**: take {n} prompt, seed, sample stills, size (576x1024 draft / 768x1376 HQ), length {fr} frames (17k+5 grid at 24 fps)."
             + (f"\n\n**Turbo LoRA**: `{lora}`" + (" (import it from huggingface.co/lightx2v/Minimax-h3-Turbo into your models first)" if path == "ref" else " (Comfy Cloud catalog)") if lora else ""))
    desc = f"{ROLES[role]}: H3 {path} path of the skill's graph (take_graph.py), draft/HQ switch"
    not_m = "Spend guard, the lint (`take_graph.py --lint`), upload of stills and downloading the file: `take_graph.py --submit` does them. In the UI you press Run yourself (it costs credits)."
    edit = ["1", "129", "400", "40", "41", "510", "in_hq"]
    return api, grp, order, titles, edit, {"1": [700, 460]}, read_me(label, what, steps, extra, facts, not_m), desc

def stills_workflow(spec, base_file, facts, pack_file=None):
    """Step 4 of SKILL.md with Comfy nodes: base still (gpt-image-2 text-to-image) and an edited still of the same person (gpt-image-2 edit; the packshot
    rides along as the second image when the edit puts the product in hand), both cropped to 576x1024 like media.py fitframe. Prompts: h3prompt.still_prompt."""
    g, grp = {}, {}
    def add(k, ct, inp, gr): g[k] = {"class_type": ct, "inputs": inp}; grp[k] = gr
    B, E = "1 · Base still (one per scene)", "2 · Edited still (same person and room, new pose or prop)"
    gpt = lambda prompt, **kw: {"prompt": prompt, "model": "gpt-image-2.5-sunburst", "model.size": "1024x1536", "model.quality": "high", "model.background": "auto", "n": 1, "seed": 0, **kw}
    crop = lambda src: {"image": [src, 0], "upscale_method": "lanczos", "width": 576, "height": 1024, "crop": "center"}
    import h3prompt
    takes = spec["takes"]; first = takes[0]; edited = next((t for t in takes if t.get("shows_product")), takes[-1])   # the bottle in hand: the most common edit
    add("base", "OpenAIGPTImageNodeV2", gpt(h3prompt.still_prompt(spec, first)), B); add("base_crop", "ImageScale", crop("base"), B)
    add("base_save", "SaveImage", {"images": ["base", 0], "filename_prefix": "stills/base_1024x1536"}, B); add("base_save_c", "SaveImage", {"images": ["base_crop", 0], "filename_prefix": "stills/base_576x1024"}, B)
    add("src", "LoadImage", {"image": base_file}, E)
    imgs = {"model.images.image_1": ["src", 0]}
    if edited.get("shows_product") and pack_file: add("pack", "LoadImage", {"image": pack_file}, E); imgs["model.images.image_2"] = ["pack", 0]
    add("edit", "OpenAIGPTImageNodeV2", gpt(h3prompt.still_prompt(spec, edited, base=first), **imgs), E)
    add("edit_crop", "ImageScale", crop("edit"), E); add("edit_save", "SaveImage", {"images": ["edit_crop", 0], "filename_prefix": "stills/edit_576x1024"}, E)
    titles = {"base": "BASE STILL · gpt-image-2.5-sunburst, 1024x1536, high (edit the prompt: creator, setting, every prop of the scene, hands)", "base_crop": "Crop to 9:16 576x1024 (= media.py fitframe)",
              "base_save": "SAVE · base still 1024x1536 (keep it: every edit starts from it)", "base_save_c": "SAVE · base still 576x1024 (the keyframe)",
              "src": "SET FILE: the base still 1024x1536", "pack": "SET FILE: the packshot (second image)", "edit": "EDITED STILL · say only what changes", "edit_crop": "Crop to 576x1024", "edit_save": "SAVE · edited still 576x1024"}
    what = ("Make the keyframe stills every take starts and ends on (SKILL.md step 4), with the same partner node the skill calls: **OpenAI GPT Image 2.5** set to `gpt-image-2.5-sunburst` (the top OpenAI image model for generation and editing, 2026-09-30), 1024x1536, quality high. "
            "One **base still** per scene; every other still of the same person is an **edit** of the base, so the face and the room stay the same. Both are cropped to 576x1024 (center), like `media.py fitframe`.")
    steps = ["Group 1: the base prompt comes from `take_graph.py --print-still` (references/stills.md: iPhone capture, the creator, **every object the script will move, visible and within easy reach of the free hand**, framing, room details, practical light, the anti-gloss finish). Run it once and keep the 1024x1536 file.",
             "Group 2: load that base still on **SET FILE** and write only what changes (a pose, the bottle in hand, the end pose of a take). Run once per still.",
             "Check each 576x1024 still before rendering a take: `python3 scripts/review.py <still.png> --still --spec <spec.json> --take N [--same-as <base still>]` (props visible and reachable, hands as scripted, looks like a real phone photo, same person).",
             "Set the stills on FIRST FRAME / LAST FRAME of the take workflows. Takes that meet at a seam share the same still."]
    extra = "**Cost**: one partner call per still (Comfy shows the price before running). Partner nodes cache identical calls: change the prompt or the seed to get a new image."
    not_m = "- The still check (`review.py --still`, Gemini) runs locally.\n- The skill calls the same node through `partner_generate` (edits pass the base still's job id instead of a file)."
    return g, grp, [B, E], titles, ["base", "src", "edit"] + (["pack"] if "pack" in g else []), {"base": [640, 420], "edit": [640, 420]}, read_me("Keyframe stills", what, steps, extra, facts, not_m), \
        "Keyframe stills: gpt-image-2.5-sunburst base still + edits, cropped to 576x1024"

# ---------------------------------------------------------------- 3: review (prompts come from review.py, so they cannot drift)
def gem(model, prompt, system, **kw):
    i = {"model": model, "model.prompt": prompt, "model.system_prompt": system, "model.thinking_level": "MEDIUM", "model.max_output_tokens": 16384, "model.seed": 1,
         "model.temperature": 0.0, "model.top_p": 0.95}
    return {"class_type": "GeminiNodeV3", "inputs": {**i, **kw}}

def review_workflow(R, clip, expected, timeline, facts, model="Gemini 3.8 Flash", props=None):
    g, grp = {}, {}
    def add(k, ct, inp, gr): g[k] = {"class_type": ct, "inputs": inp}; grp[k] = gr
    I, A, V, H, O = "0 · Inputs you edit", "1 · Transcript check (audio vs the script)", "2 · Visual check (video)", "3 · Hair check (head-only frame grids)", "4 · Findings report"
    P = "2b · Props check (each object rests on a surface or is gripped)"
    vhs = lambda **kw: {"video": clip, "force_rate": 0, "custom_width": 0, "custom_height": 0, "frame_load_cap": 0, "start_time": 0, **kw}
    add("in_expected", "PrimitiveStringMultiline", {"value": "\n".join(expected)}, I); add("in_timeline", "PrimitiveStringMultiline", {"value": timeline or ""}, I)
    add("fr4", "VHS_LoadVideoFFmpeg", vhs(force_rate=4, custom_width=320, custom_height=576), I)          # 4 fps frames (hair) + the take's whole audio
    add("fr12", "VHS_LoadVideoFFmpeg", vhs(force_rate=12, custom_width=360, custom_height=640), I)       # reduced video for the visual check (review.py: 360 wide, 12 fps)
    add("a_fmt", "StringFormat", {"values.a": ["in_expected", 0], "f_string": "EXPECTED:\n{a}"}, A)
    add("a_llm", "GeminiNodeV3", gem(model, ["a_fmt", 0], R.AUDIO_SYS, **{"model.audio.audio_1": ["fr4", 2]})["inputs"], A)
    add("v_cv", "CreateVideo", {"images": ["fr12", 0], "fps": 12.0}, V)
    add("v_fmt", "StringFormat", {"values.a": ["in_timeline", 0], "f_string": "INTENDED TAKE, by time:\n{a}\n\nReview this video for the listed defects."}, V)
    add("v_llm", "GeminiNodeV3", gem(model, ["v_fmt", 0], R.VISUAL_SYS, **{"model.video.video_1": ["v_cv", 0]})["inputs"], V)
    add("h_crop", "ImageCrop", {"image": ["fr4", 0], "width": 320, "height": 260, "x": 0, "y": 0}, H)        # top 45% of each frame = head and hair
    imgs = {}
    for k in range(4):
        add(f"h_ch{k}", "ImageFromBatch", {"image": ["h_crop", 0], "batch_index": 16 * k, "length": 16}, H)
        add(f"h_gr{k}", "ImageGrid", {"images": [f"h_ch{k}", 0], "columns": 4, "cell_width": 320, "cell_height": 260, "padding": 4}, H); imgs[f"model.images.image_{k + 1}"] = [f"h_gr{k}", 0]
    add("h_llm", "GeminiNodeV3", gem(model, "Classify the hair in every cell.", R.HAIR_SYS, **imgs)["inputs"], H)
    fmt = {"values.a": ["a_llm", 0], "values.b": ["v_llm", 0], "values.c": ["h_llm", 0],
           "f_string": "TRANSCRIPT CHECK (audio vs script; t0/t1 in seconds):\n{a}\n\nVISUAL CHECK (t0/t1 in seconds):\n{b}\n\nHAIR CHECK (tall_runs, seconds):\n{c}"}
    if props:   # review.py PROPS_SYS on the same reduced video (review.py uses 480 px; the 360 px loader is reused here)
        add("in_props", "PrimitiveStringMultiline", {"value": "\n".join(props)}, I)
        add("p_fmt", "StringFormat", {"values.a": ["in_props", 0], "values.b": ["in_timeline", 0], "f_string": "OBJECTS:\n{a}\n\nINTENDED TAKE, by time:\n{b}"}, P)
        add("p_llm", "GeminiNodeV3", gem(model, ["p_fmt", 0], R.PROPS_SYS, **{"model.video.video_1": ["v_cv", 0]})["inputs"], P)
        fmt["values.d"] = ["p_llm", 0]; fmt["f_string"] += "\n\nPROPS CHECK (t0/t1 in seconds):\n{d}"
    add("o_fmt", "StringFormat", fmt, O)
    add("o_save", "SaveText", {"text": ["o_fmt", 0], "filename_prefix": "text/review_findings", "format": "txt"}, O)
    order = [I, A, V] + ([P] if props else []) + [H, O]
    titles = {"in_expected": "EXPECTED · the lines the speaker must say, one per line (from the take's prompt)", "in_timeline": "INTENDED TAKE · what should happen by time (the 'Timeline, second by second' block of the prompt)",
              "fr4": "SET FILE: clip to review (4 fps frames + audio). Use the SAME clip on both loaders", "fr12": "SET FILE: same clip (12 fps, 360 px wide, for the visual check)",
              "a_llm": f"Transcript check · {model} (transcribes, then diffs against EXPECTED)", "v_llm": f"Visual check · {model}", "h_llm": f"Hair check · {model}",
              "h_crop": "Head crop: top 45% of each frame", "o_save": "SAVE · findings (timestamped JSON from the three checks)",
              "in_props": "OBJECTS · the props the take handles, one per line (spec.props)", "p_llm": f"Props check · {model} (float, pop-in, duplicate, passes through)"}
    if props: titles["o_save"] = "SAVE · findings (timestamped JSON from the four checks)"
    what = ("Review ONE take or ad the way `review.py` does, with Comfy nodes: **transcript check** (the audio is transcribed and diffed against the script), **visual check** (hard cuts, pop-ins, morphs, label changes) "
            f"and **hair check** (head-only 4x4 frame grids at 4 fps, level 0/1/2 per cell; a tall run of level 2 is the ponytail/bun pop-in). Model **{model}**, temperature 0. The prompts are the ones in `review.py`. Output: a findings text with timestamps in seconds.")
    steps = ["Group 0: set the clip on BOTH video loaders (Load Video nodes do not share a file), paste the take's quoted lines into **EXPECTED** (one per line) and its timeline into **INTENDED TAKE**. The preloaded ones are a sample take and its script. "
             "For an **action take** write `NONE. This is a silent action clip: every spoken, mumbled or whispered word is EXTRA, blocking.` and add a line `SCRIPTED NON-VERBAL SOUNDS (part of the script, never defects): <the yawn or gulp>`."
             + (" Put the take's props in **OBJECTS**." if props else ""),
             "Run. The three checks cost a few cents of Gemini (credits) per clip.",
             "Read the findings: every issue has t0/t1 in seconds, severity (blocking/minor) and, for audio, cut_in/cut_out and `removable`. Gemini is a candidate finder, not proof.",
             "If the only blocking issues are removable audio ones, cut them with **Fix - cut flagged ranges** (cut_in to cut_out); otherwise re-render the take with a new seed."]
    extra = ("**Score rule** (review.py): 0 when clean; else (real issues) + 0.5 per second affected. Real = audio blocking, visual blocking with conf >= 0.85, a hair level-2 run, or a frame shrink. "
             "Lower is better; a fix replaces the original only if its re-review score is strictly lower.")
    not_m = ("- The frame-shrink check (`media.py cropcheck`, ffmpeg cropdetect) and the score / `fix.ranges` arithmetic are computed by `review.py` locally.\n"
             "- 4 hair grids cover 16 s; `review.py` uses fewer grids for shorter clips (here the unused cells are blank padding).\n- Token and cost accounting: `review.py` prints them; in the UI the cost shows in the credits.")
    if props: what += " Plus the **props check** (review.py `PROPS_SYS`): each declared object must rest on a surface or be gripped; it flags floating, pop-ins, duplicates and objects passing through hands."
    return g, grp, order, titles, ["in_expected", "in_timeline", "fr4", "fr12"] + (["in_props"] if props else []), {"in_expected": [560, 260], "in_timeline": [560, 260]}, read_me("Review (Gemini QA)", what, steps, extra, facts, not_m), \
        "Gemini QA of one take: transcript diff, visual candidates" + (", props check" if props else "") + ", hair check, timestamped findings"

# ---------------------------------------------------------------- 4: fix (media.cut as Comfy nodes)
def fix_workflow(clip, t0, t1, facts):
    g, grp = {}, {}
    def add(k, ct, inp, gr): g[k] = {"class_type": ct, "inputs": inp}; grp[k] = gr
    I, C, O = "0 · Inputs you edit", "1 · Cut (keep everything outside the range)", "2 · Save"
    add("in_clip", "LoadVideo", {"file": clip}, I); add("in_t0", "PrimitiveFloat", {"value": t0}, I); add("in_t1", "PrimitiveFloat", {"value": t1}, I)
    add("head", "Video Slice", {"video": ["in_clip", 0], "start_time": 0.0, "duration": ["in_t0", 0], "strict_duration": False}, C)
    add("tail", "Video Slice", {"video": ["in_clip", 0], "start_time": ["in_t1", 0], "duration": 0.0, "strict_duration": False}, C)
    add("cat", "ConcatenateVideo", {"videos.video0": ["head", 0], "videos.video1": ["tail", 0], "codec": "h264"}, C)
    add("save", "SaveVideo", {"video": ["cat", 0], "filename_prefix": "video/fixed_cut", "format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0}, O)
    titles = {"in_clip": "SET FILE: clip to fix", "in_t0": "CUT FROM (seconds) = cut_in of the finding", "in_t1": "CUT TO (seconds) = cut_out of the finding",
              "head": "Keep: start -> CUT FROM", "tail": "Keep: CUT TO -> end (duration 0 = to the end)", "cat": "Join the two kept parts (audio and video stay in sync)", "save": "SAVE · fixed clip (never reuse a prefix)"}
    what = ("Remove ONE flagged time range from a clip with plain Comfy nodes, the same thing `media.py cut` does with ffmpeg: keep the part before the range and the part after it, join them, re-encode (CRF 14). "
            "**No AI repair**: nothing is regenerated, restyled or inpainted (decision `7cc533b`). It only fixes defects that are removable spoken words (the Review workflow marks them `removable` with `cut_in`/`cut_out`, taken from the silence around the defect).")
    steps = ["Set the clip on **SET FILE** and the range: **CUT FROM** / **CUT TO** = `cut_in` / `cut_out` of the finding in the review (the preloaded numbers are an example).",
             "Run. Check the joint by ear and by eye, then run **Review (Gemini QA)** on the result.",
             "Keep the fixed clip only if its re-review score is strictly lower than the original's (max 2 attempts, `fix.py` policy). Otherwise ship the original and mark the defect NEEDS EDITOR with its timestamps."]
    extra = "**More than one range**: run it again on the output (that is what `fix.py` does for its second attempt), shifting the second range by the seconds already removed."
    not_m = "- `fix.py` itself (attempt counter, score comparison, NEEDS EDITOR report, exit codes) and multi-range cuts in one pass run locally.\n- Re-render (paid) is never done here: use **Take N** with a new SEED."
    return g, grp, [I, C, O], titles, ["in_clip", "in_t0", "in_t1"], {}, read_me("Fix - cut flagged ranges", what, steps, extra, facts, not_m), "Cut a flagged time range out of a clip with Comfy nodes (no AI repair)"

# ---------------------------------------------------------------- 8: HeyGen Video 1 take (the default model), same graph as heygen_take.py
def heygen_workflow(spec, n, files, facts, label):
    import h3prompt
    take = spec["takes"][n - 1]; secs = h3prompt.heygen_seconds(spec, take); g, grp = {}, {}
    def add(k, ct, inp, gr): g[k] = {"class_type": ct, "inputs": inp}; grp[k] = gr
    I, M, O = "0 · Inputs you edit", "1 · HeyGen Video 1", "2 · Save"
    add("in_first", "LoadImage", {"image": files["guide"]}, I)
    add("heygen", "HeyGenImageToVideoNode", {"model": "heygen-video-1", "model.image": ["in_first", 0], "model.prompt": h3prompt.heygen_prompt(spec, take),
                                              "model.duration": secs, "model.resolution": "768p", "model.seed": int(spec.get("seed", 1000))}, M)
    add("save", "SaveVideo", {"video": ["heygen", 0], "filename_prefix": f"video/{label}", "format": "mp4", "format.codec": "h264"}, O)
    titles = {"in_first": "FIRST FRAME · keyframe still (the take starts exactly from it)", "heygen": f"HEYGEN · prompt, duration ({secs} s), seed",
              "save": "SAVE · take video (never reuse a prefix)"}
    what = (f"**HeyGen Video 1 take** ({secs} s, 768p, about $0.32 per 15 s): the default model. It starts from the keyframe still and speaks the take's lines "
            "with its own voice. It has no last frame, no voice reference and no product reference: the product is in the still, and the voice is held by the same "
            "voice text and seed for every take of a creator. If the voice drifts across clips, re-render the later speech takes with **3 Speech take with voice reference** (H3).")
    steps = ["Set the take's keyframe still on **FIRST FRAME** (from **1 Keyframe stills**).",
             "Check the prompt on the **HEYGEN** node: `heygen_take.py --spec <spec> --take N --print-prompt` writes it (natural language, timed beats, the audio in the last paragraph; no impact sounds).",
             "Run (paid). Review by eye and ear; then the next take."]
    not_m = "The spec lint, the prompt check, the spend guard and the download: `heygen_take.py --spec ... --user-ok --cap` does them. In the UI you press Run yourself (it costs credits)."
    return g, grp, [I, M, O], titles, ["in_first", "heygen"], {}, read_me(label, what, steps, "", facts, not_m), f"HeyGen Video 1 take {n} (the default model)"

# ---------------------------------------------------------------- 5: assemble (media.concat loudness + export as Comfy nodes)
def assemble_workflow(takes, w, h, facts, lufs=-14.0, y_bias=0.35):
    g, grp = {}, {}
    def add(k, ct, inp, gr, mode=None): g[k] = {"class_type": ct, "inputs": inp}; grp[k] = gr
    I, L, J, X = "0 · Takes you edit", "1 · Loudness (one track for the whole ad)", "2 · Join + export 9:16", "3 · Export 4:5 (crop)"
    n = len(takes); ch = int(round(w * 5 / 4)); ch -= ch % 2; y = int(round((h - ch) * y_bias)); y -= y % 2
    for i, t in enumerate(takes, 1):
        add(f"vid{i}", "LoadVideo", {"file": t}, I)
        add(f"aud{i}", "VHS_LoadVideoFFmpeg", {"video": t, "force_rate": 1, "custom_width": 64, "custom_height": 112, "frame_load_cap": 0, "start_time": 0}, I)   # audio only, tiny frames
    prev = ["aud1", 2]
    for i in range(2, n + 1):
        add(f"ac{i}", "AudioConcat", {"audio1": prev, "audio2": [f"aud{i}", 2], "direction": "after"}, L); prev = [f"ac{i}", 0]
    add("norm", "NormalizeAudioLoudness", {"audio": prev, "lufs": float(lufs)}, L)
    add("cat", "ConcatenateVideo", {**{f"videos.video{i - 1}": [f"vid{i}", 0] for i in range(1, n + 1)}, "codec": "h264", "complete_audio": ["norm", 0]}, J)
    enc = lambda p: {"filename_prefix": p, "format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 16.0}
    add("save_916", "SaveVideo", {"video": ["cat", 0], **enc("video/ad_9x16")}, J)
    order = [I, L, J, X]
    titles = {**{f"vid{i}": f"SET FILE: take {i} (video)" for i in range(1, n + 1)}, **{f"aud{i}": f"SET FILE: take {i} again (its audio)" for i in range(1, n + 1)},
              "norm": f"Loudness {lufs:g} LUFS (media.py concat uses ffmpeg loudnorm I={lufs:g})", "cat": "Join the takes in order, with the normalized audio", "save_916": f"SAVE · 9:16 ({w}x{h}, native size)"}
    what = (f"Join **{n} takes** in order into one ad, normalize the loudness of the whole ad, and export **9:16** and **4:5**, like `media.py concat` and `media.py export`. "
            f"Default: {n} takes x 15 s = {n * 15} s (take count is configurable: add a Load Video + audio loader pair per extra take, e.g. 4 takes = 60 s).")
    steps = [f"Group 0: set each take's file on BOTH loaders of that take (the video loader and its audio loader do not share a file). Order = take order (the preloaded files are a sample chain).",
             "Run. Output 1 is the ad at the takes' native size; output 2 is the 4:5 crop (group 3).",
             "4:5 is a crop of the 9:16 frame: full width, 70% of the height (768x960 from 768x1376; 576x720 from 576x1024). The **Crop Video** node is a frontend widget: un-mute it (Ctrl+M) and drag its box to the full width, "
             f"about {ch} px high, starting about {y} px from the top so the face stays in frame; its values cannot be preloaded by a script.",
             "Then do captions, the packshot cutaway(s) and the end card locally (see below), re-review the finished ad, and export again at 1080 wide."]
    extra = f"**Size**: sample takes are {w}x{h} (HQ). Comfy has no video scale node, so this workflow exports at the native size; the 1080x1920 / 1080x1350 versions come from `media.py export`."
    not_m = ("- **Captions, the packshot cutaway (muted real label) and the end card** (disclosure text as a PNG) run in local ffmpeg (`media.py overlay`); Comfy has no equivalent that works on the whole ad without decoding every frame.\n"
             f"- **Scale to 1080x1920 / 1080x1350** (`media.py export`, lanczos): no video-scale node on Cloud.\n- **Loudness**: ffmpeg `loudnorm` (I, TP -1.5, LRA 11) is replaced by Comfy's NormalizeAudioLoudness (integrated loudness only).\n"
             "- **4:5 crop values**: set by hand on the Crop Video node (see step 3).")
    # 4:5 branch: VideoCrop is a frontend crop widget; emitted muted (mode 2) so a run never depends on it
    add("crop_45", "VideoCrop", {"video": ["cat", 0]}, X); add("save_45", "SaveVideo", {"video": ["crop_45", 0], **enc("video/ad_4x5")}, X)
    titles.update({"crop_45": "Crop Video 4:5 · MUTED: un-mute (Ctrl+M) and drag the crop box", "save_45": "SAVE · 4:5"})
    return g, grp, order, titles, [f"vid{i}" for i in range(1, n + 1)] + [f"aud{i}" for i in range(1, n + 1)], {}, read_me("Assemble + export", what, steps, extra, facts, not_m), \
        f"Join {n} takes, normalize loudness, export 9:16 and 4:5", {"crop_45", "save_45"}

# ---------------------------------------------------------------- publish / pull
def publish(path, folder):
    from comfy_cli.target import resolve_target
    from comfy_cli.http import authed_urlopen, read_capped
    t = resolve_target(where="cloud")
    url = t.base_url + t.path_prefix + "/userdata/" + urllib.parse.quote(f"workflows/{folder}/{pathlib.Path(path).name}", safe="") + "?overwrite=true&full_info=true"
    r = authed_urlopen(url, t, method="POST", data=pathlib.Path(path).read_bytes(), content_type="application/json")
    return r.status, json.loads(read_capped(r, url)).get("path", "")

SLUG = {1: "1-keyframe-stills", 2: "2-speech-take", 3: "3-speech-take-voice-reference", 4: "4-action-take", 5: "5-review-gemini-qa",
        6: "6-fix-cut-ranges", 7: "7-assemble-export", 8: "8-heygen-take"}   # --file-names slug: only letters, digits and hyphens (claude.ai plugin zips)

def main():
    S._reexec_in_comfy_venv()
    import take_graph as T, review as R
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True); ap.add_argument("--folder", default="Comfy UGC"); ap.add_argument("--brief"); ap.add_argument("--out", default=str(HERE.parent / "workflows_ui"))
    ap.add_argument("--work", default=str(pathlib.Path(tempfile.gettempdir()) / "ui_build")); ap.add_argument("--publish", action="store_true"); ap.add_argument("--verify", action="store_true")
    ap.add_argument("--sample-guide"); ap.add_argument("--sample-voice"); ap.add_argument("--sample-clip"); ap.add_argument("--sample-takes", nargs="*"); ap.add_argument("--review-prompt-file")
    ap.add_argument("--cut-range", default="4.1-5.0"); ap.add_argument("--only", help="comma list of workflow numbers (1-5 legacy spec, 1-7 keyframe method)")
    ap.add_argument("--file-names", choices=["title", "slug"], default="title")
    ap.add_argument("--sample-still", help="keyframe method: a base still (1024x1536) preloaded on the edit branch of the stills workflow")
    a = ap.parse_args(); S.FOLDER = a.folder
    spec = json.loads(pathlib.Path(a.spec).read_text()); facts = brief_facts(a.brief); work = pathlib.Path(a.work); work.mkdir(parents=True, exist_ok=True)
    root = pathlib.Path(a.spec).resolve().parent; pk = pathlib.Path(spec["packshot"]); pk = pk if pk.is_absolute() else next((b / pk for b in (pathlib.Path.cwd(), *HERE.parents[2:4], HERE.parent, root) if (b / pk).exists()), pk)
    up = lambda p, kind: cloud_name(work, p or make_sample(work, kind))
    clip_f = up(a.sample_clip, "mp4"); files = {"packshot": cloud_name(work, pk), "guide": up(a.sample_guide, "png"), "voice": up(a.sample_voice, "wav")}
    takes_f = [cloud_name(work, p) for p in (a.sample_takes or [])] or [clip_f] * min(3, len(spec["takes"]))
    if a.review_prompt_file: prm = pathlib.Path(a.review_prompt_file).read_text()
    else: prm = T.prompt(spec, spec["takes"][min(1, len(spec["takes"]) - 1)], min(2, len(spec["takes"])))
    expected = re.findall(r'says "([^"]+)"', prm); tl = (re.search(r"Timeline, second by second: (.*?) Throughout", prm, re.S) or [None, ""])[1]
    c0, c1 = [float(x) for x in a.cut_range.split("-")]
    try: import media; sz = tuple(int(x) for x in media.probe(a.sample_takes[0], "stream=width,height", "v:0").split(","))
    except Exception: sz = (768, 1376)
    N = len(spec["takes"]); pre = a.folder + " - "; expect, review_num = None, 3
    if T.legacy(spec):
        jobs = [(1, pre + "Take 1 - generate", lambda: take_workflow(T, spec, 1, files, pre + "Take 1 - generate", facts, None)),
                (2, pre + (f"Take 2-{N} - chained" if N > 2 else "Take 2 - chained"), lambda: take_workflow(T, spec, 2, files, pre + (f"Take 2-{N} - chained" if N > 2 else "Take 2 - chained"), facts, None)),
                (3, pre + "Review (Gemini QA)", lambda: review_workflow(R, clip_f, expected, tl, facts)),
                (4, pre + "Fix - cut flagged ranges", lambda: fix_workflow(clip_f, c0, c1, facts)),
                (5, pre + "Assemble + export", lambda: assemble_workflow(takes_f, sz[0], sz[1], facts))]
    else:   # keyframe method (SKILL.md steps 4-9): stills, one workflow per kind of take, review with the props check
        import h3prompt
        kinds = [h3prompt.kind(t) for t in spec["takes"]]; speech = [i + 1 for i, k in enumerate(kinds) if k == "speech"]; action = [i + 1 for i, k in enumerate(kinds) if k == "action"]
        sp = speech[0] if speech else None; ac = action[0] if action else None
        sv = next((i for i in speech[1:] if spec["takes"][i - 1].get("shows_product")), speech[1] if len(speech) > 1 else None)
        files["end"] = files["guide"]; base_still = up(a.sample_still, "png")
        lines, tl2, _, _ = h3prompt.expected(spec, spec["takes"][(sp or 1) - 1])
        props = [f"{p['name']}: {p.get('desc', '')} {p.get('where', '')}".strip() for p in spec.get("props", [])]
        expect, review_num = {2: (sp, "speech-first"), 3: (sv, "speech-voice"), 4: (ac, "action")}, 5
        jobs = [(1, pre + "1 Keyframe stills", lambda: stills_workflow(spec, base_still, facts, files["packshot"]))]
        for num, n, role, title in ((2, sp, "speech-first", "2 Speech take"), (3, sv, "speech-voice", "3 Speech take with voice reference"), (4, ac, "action", "4 Action take (insert)")):
            if n: jobs.append((num, pre + title, (lambda n=n, role=role, title=title: take_workflow_v2(T, spec, n, files, pre + title, facts, role))))
        jobs += [(5, pre + "5 Review (Gemini QA)", lambda: review_workflow(R, clip_f, lines, tl2, facts, props=props)),
                 (6, pre + "6 Fix - cut flagged ranges", lambda: fix_workflow(clip_f, c0, c1, facts)),
                 (7, pre + "7 Assemble + export", lambda: assemble_workflow(takes_f, sz[0], sz[1], facts))]
        if sp: jobs.append((8, pre + "8 HeyGen take", lambda: heygen_workflow(spec, sp, files, facts, pre + "8 HeyGen take")))
    want = {int(x) for x in a.only.split(",")} if a.only else {j[0] for j in jobs}; out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True); graph = load_graph(); bad = 0
    for num, name, fn in jobs:
        if num not in want: continue
        r = fn(); api, grp, order, titles, edit, sizes, note, desc = r[:8]; muted = r[8] if len(r) > 8 else set()
        ui, ids = emit(api, graph); layout(ui, ids, api, grp, order, titles, edit, sizes, note, name, desc)
        for m in muted:
            nd = next(n for n in ui["nodes"] if n["id"] == ids[m]); nd["mode"] = 2
        fname = SLUG[num] if a.file_names == "slug" else name   # the file name is also the Cloud name on --publish
        p = out / f"{fname}.json"; json.dump(ui, open(p, "w"), indent=1)
        line = f"[{num}] {name}: {len(api)} nodes -> {p}"
        if a.verify:
            probs = []
            exp_api = {k: v for k, v in api.items() if k not in muted}
            got = to_api(p); probs += [f"local {x}" for x in verify_one(num, T, spec, files, exp_api, got, R, expected, tl, expect, review_num)]
            if a.publish:
                st, path = publish(p, a.folder); line += f" | published {st} {path}"
                pulled = work / f"pulled-{num}.json"; json.dump(S.cloud_get(fname), open(pulled, "w")); got2 = to_api(pulled)
                probs += [f"cloud {x}" for x in verify_one(num, T, spec, files, exp_api, got2, R, expected, tl, expect, review_num)]
            print(line + f" | verify: {'OK' if not probs else 'PROBLEMS'}"); [print("   -", x) for x in probs[:20]]; bad += bool(probs)
        else:
            if a.publish: st, path = publish(p, a.folder); line += f" | published {st} {path}"
            print(line)
    sys.exit(1 if bad else 0)

def verify_one(num, T, spec, files, api_src, got, R, expected, tl, expect=None, review_num=3):
    """Problems between the skill's graph for this step and a converted workflow. Take workflows: both draft and HQ collapses vs take_graph.graph()
    (legacy 1-2, keyframe method 2-4 via `expect`); others: the builder's own API graph; the review workflow also must carry review.py's prompts verbatim."""
    probs = []
    if expect is not None:
        if num in expect:
            n, role = expect[num]; gd, gh = take_graphs(T, spec, n, files, role, "x")
            for hq, sk in ((False, gd), (True, gh)): probs += [f"{'HQ' if hq else 'draft'}: {x}" for x in compare(sk, got, hq)]
        else: probs += compare(api_src, got)
        if num == review_num:
            texts = {t[2] for t in S.texts(got)}
            for nm, sysm in (("AUDIO_SYS", R.AUDIO_SYS), ("VISUAL_SYS", R.VISUAL_SYS), ("HAIR_SYS", R.HAIR_SYS)) + ((("PROPS_SYS", R.PROPS_SYS),) if "p_llm" in api_src else ()):
                if sysm not in texts: probs.append(f"{nm} from review.py not found verbatim in the workflow")
        return probs
    if num in (1, 2):
        n, seed = num, int(spec.get("seed", 1000)) + num - 1
        for hq in (False, True):
            q = T.QUALITY["hq" if hq else "draft"]; sk = T.graph(spec, n, q, files, seed, q["steps"], q["attention"], q["w"], q["h"]); sk["92"]["inputs"]["filename_prefix"] = "x"
            probs += [f"{'HQ' if hq else 'draft'}: {x}" for x in compare(sk, got, hq)]
    else:
        probs += compare(api_src, got)
    if num == 3:
        texts = {t[2] for t in S.texts(got)}
        for nm, sysm in (("AUDIO_SYS", R.AUDIO_SYS), ("VISUAL_SYS", R.VISUAL_SYS), ("HAIR_SYS", R.HAIR_SYS)):
            if sysm not in texts: probs.append(f"{nm} from review.py not found verbatim in the workflow")
    return probs

if __name__ == "__main__": main()
