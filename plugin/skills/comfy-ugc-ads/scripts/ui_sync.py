#!/usr/bin/env python3
"""Read back the workflows edited in the Comfy Cloud web UI (a folder such as "Comfy UGC", --folder) and show what he changed versus what the skill uses.
READ-ONLY: never publishes, never submits a job, never edits the skill's configs (a human or the agent applies the diff by hand).

  ui_sync.py list                                   list the workflows in the Cloud folder
  ui_sync.py pull "<name>" [--baseline B] [--out DIR] [--json]
        download the workflow, convert it to API format (`comfy run --print-prompt`, no submit), print a human-readable diff.
        B = path to a UI .json or an API .json (default: the local copy in workflows_ui/ = what ui_build.py last published)
  ui_sync.py diff <new.json> <baseline.json>        same diff between two local files (UI or API format), no network for API files

Credentials: uses the comfy CLI's own login (comfy_cli.target.resolve_target); nothing is printed or stored. Needs the `comfy` CLI installed and logged in for Cloud."""
import argparse, collections, difflib, json, os, pathlib, shutil, subprocess, sys, tempfile, urllib.error, urllib.parse

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]   # the skill folder (holds workflows_ui/)
FOLDER = "Comfy UGC"   # ui_build.py --folder sets it; a client batch uses its own folder

def _reexec_in_comfy_venv():
    try:
        import comfy_cli  # noqa
        return
    except ImportError:
        pass
    cands = [os.environ.get("COMFY_PY", ""), str(pathlib.Path.home() / ".local/share/uv/tools/comfy-cli/bin/python")]
    w = shutil.which("comfy")
    if w:
        try:
            first = open(w, "rb").readline().decode().strip()
            if first.startswith("#!"): cands.append(first[2:].split()[0])
        except OSError: pass
    for c in cands:
        if c and os.path.exists(c) and os.path.realpath(c) != os.path.realpath(sys.executable):
            os.execv(c, [c, *sys.argv])
    sys.exit("comfy_cli not importable: install the comfy CLI (uv tool install comfy-cli) or set COMFY_PY to its python")

# ---------------------------------------------------------------- Cloud access
def _target():
    from comfy_cli.target import resolve_target
    return resolve_target(where="cloud")

def cloud_list():
    from comfy_cli.http import request_json
    t = _target()
    try: st, body = request_json(t.base_url + t.path_prefix + "/userdata?dir=workflows&recurse=true&split=false", t, max_bytes=20_000_000)
    except urllib.error.HTTPError as e:
        if e.code == 404: return []   # no workflows folder at all (everything deleted)
        raise
    return [e for e in (body or []) if e["path"].startswith(FOLDER + "/")]

def cloud_get(name):
    from comfy_cli.http import authed_urlopen, read_capped
    t = _target()
    fn = name if name.endswith(".json") else name + ".json"
    url = t.base_url + t.path_prefix + "/userdata/" + urllib.parse.quote(f"workflows/{FOLDER}/{fn}", safe="")
    r = authed_urlopen(url, t)   # GET
    return json.loads(read_capped(r, url))

# ---------------------------------------------------------------- UI -> API
def is_api(d): return isinstance(d, dict) and "nodes" not in d and all(isinstance(v, dict) and "class_type" in v for v in d.values())

def to_api(ui_path):
    """comfy run --print-prompt converts without submitting (envelope status 'preview')."""
    p = subprocess.run(["comfy", "--json", "run", "--workflow", str(ui_path), "--where", "cloud", "--print-prompt"], capture_output=True, text=True, timeout=180)
    try: r = json.loads(p.stdout)
    except ValueError: raise SystemExit(f"comfy did not return JSON (exit {p.returncode}): {p.stderr[:300]}")
    if not r.get("ok") or r["data"].get("status") != "preview": raise SystemExit("conversion failed or would have submitted; aborting: " + json.dumps(r.get("error") or r["data"].get("status"))[:400])
    return r["data"]["prompt"]

def load_api(path):
    d = json.load(open(path))
    return d if is_api(d) else to_api(path)

# ---------------------------------------------------------------- settings extraction (id independent)
def is_link(v): return isinstance(v, list) and len(v) == 2 and isinstance(v[1], int) and isinstance(v[0], (str, int))

def resolve(P, v, depth=0):
    """Literal value(s) behind an input. Primitives are followed; If/Else switches keep their branch (true:/false:); other nodes show as <Class>."""
    if not is_link(v): return [v]
    n = P.get(str(v[0]))
    if not n or depth > 8: return ["<?>"]
    c, i = n["class_type"], n["inputs"]
    if c.startswith("Primitive"): return resolve(P, i.get("value"), depth + 1)
    if c == "ComfySwitchNode":
        return [f"{tag}:{x}" for tag in ("true", "false") for x in resolve(P, i.get("on_" + tag), depth + 1)]
    if c == "ComfyMathExpression": return [f"<expr {i.get('expression')}>"]
    return [f"<{c}>"]

def S(vals): return sorted({str(x) for x in vals})

# label -> (class_type, input key)
SIMPLE = [("diffusion model", "UNETLoader", "unet_name"), ("text encoder", "CLIPLoader", "clip_name"), ("VAE", "VAELoader", "vae_name"),
          ("attention backend", "ModelAttentionBackend", "attention"),
          ("sparse attention: selection", "BlockSparseAttention", "selection"), ("sparse attention: tau", "BlockSparseAttention", "selection.tau"),
          ("sparse attention: start_percent", "BlockSparseAttention", "start_percent"), ("sparse attention: end_percent", "BlockSparseAttention", "end_percent"),
          ("sparse attention: dense_blocks", "BlockSparseAttention", "dense_blocks"), ("sparse attention: min_tokens", "BlockSparseAttention", "min_tokens"),
          ("sampler", "KSamplerSelect", "sampler_name"), ("scheduler", "BasicScheduler", "scheduler"), ("steps", "BasicScheduler", "steps"), ("denoise", "BasicScheduler", "denoise"),
          ("width", "MiniMaxH3ReferenceToVideo", "width"), ("height", "MiniMaxH3ReferenceToVideo", "height"), ("length (frames)", "MiniMaxH3ReferenceToVideo", "length"),
          ("ref image size mode", "MiniMaxH3ReferenceToVideo", "ref_image_size"), ("seed", "RandomNoise", "noise_seed"),
          ("ref image (LoadImage)", "LoadImage", "image"), ("ref audio (LoadAudio)", "LoadAudio", "audio"),
          ("loudness LUFS", "NormalizeAudioLoudness", "lufs"), ("output fps", "CreateVideo", "fps"),
          ("reviewer model", "GeminiNodeV3", "model"), ("reviewer thinking", "GeminiNodeV3", "model.thinking_level"), ("reviewer temperature", "GeminiNodeV3", "model.temperature"),
          ("speech-to-text node", "ElevenLabsSpeechToText", "model")]
HEADLINE_CLASSES = {c for _, c, _ in SIMPLE}
TEXT_CLASSES = {"PrimitiveStringMultiline", "GeminiNodeV3"}

def settings(P):
    out = {}
    for label, cls, key in SIMPLE:
        vals = []
        for n in P.values():
            if n["class_type"] == cls and key in n["inputs"]: vals += resolve(P, n["inputs"][key])
        if vals: out[label] = S(vals)
    # named primitives a human is expected to edit (title set by ad_publish.TITLES), e.g. SEED base, HQ flag, TAKE
    for n in P.values():
        if n["class_type"].startswith("Primitive") and not isinstance(n["inputs"].get("value"), str) or n["class_type"] == "PrimitiveString":
            t = (n.get("_meta") or {}).get("title", "")
            if t and t != n["class_type"] and not t.startswith("Primitive") and len(t) < 140:
                out[f"input: {t[:70]}"] = S(resolve(P, n["inputs"].get("value")))
    exprs = S(f"{n['inputs'].get('expression')}" for n in P.values() if n["class_type"] == "ComfyMathExpression" and "%" in str(n["inputs"].get("expression")))
    if exprs: out["seed formula(s)"] = exprs
    return out

def texts(P):
    """Long free-text values: prompt templates and reviewer/judge system prompts."""
    t = []
    for n in P.values():
        if n["class_type"] == "PrimitiveStringMultiline" and isinstance(n["inputs"].get("value"), str):
            t.append(("prompt/text input", (n.get("_meta") or {}).get("title", ""), n["inputs"]["value"]))
        if n["class_type"] == "GeminiNodeV3":
            for k in ("model.system_prompt", "model.prompt"):
                if isinstance(n["inputs"].get(k), str): t.append((f"{n['inputs'].get('model')} {k}", "", n["inputs"][k]))
    return t

def scalars(n):
    return {k: v for k, v in n["inputs"].items() if not is_link(v) and isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= 120 and not is_link(v))}

# ---------------------------------------------------------------- diff
def diff(new, base):
    lines = []; s_new, s_base = settings(new), settings(base)
    lines.append("== Settings ==")
    ch = 0
    for k in list(dict.fromkeys([*s_base, *s_new])):
        a, b = s_base.get(k), s_new.get(k)
        if a == b or (k.startswith("input: ") and (a is None or b is None)): continue   # input titles exist only in UI exports, not in the source graph
        ch += 1
        if a is None: lines.append(f"  + {k}: {b}   (new in your workflow)")
        elif b is None: lines.append(f"  - {k}: {a}   (gone from your workflow)")
        else: lines.append(f"  ~ {k}: skill {a}  ->  yours {b}")
    if not ch: lines.append("  (no change in the tracked settings)")
    # text
    lines.append("== Prompts / review text ==")
    tb, tn = texts(base), texts(new); used = set(); any_t = False
    for kind, title, txt in tn:
        best, r = None, 0.0
        for j, (k2, _t2, x2) in enumerate(tb):
            if j in used or k2 != kind: continue
            q = difflib.SequenceMatcher(None, x2, txt).ratio()
            if q > r: best, r = j, q
        if best is None or r < 0.35:
            any_t = True; lines.append(f"  + {kind} [{title[:50]}]: no close match in the skill ({len(txt)} chars): {txt[:140]!r}..."); continue
        used.add(best)
        if tb[best][2] == txt: continue
        any_t = True; lines.append(f"  ~ {kind} [{title[:50]}]: changed (similarity {r:.2f})")
        sm = difflib.SequenceMatcher(None, tb[best][2], txt, autojunk=False)   # character level: prompts are often one long line
        for op, i1, i2, j1, j2 in [o for o in sm.get_opcodes() if o[0] != "equal"][:12]:
            ctx = tb[best][2][max(i1 - 25, 0):i1]
            lines.append(f"      ...{ctx}[{op}] {tb[best][2][i1:i2][:160]!r} -> {txt[j1:j2][:160]!r}")
    for j, (kind, title, txt) in enumerate(tb):
        if j not in used and not any(kind == k and tb[j][2] == x for k, _t, x in tn): any_t = True; lines.append(f"  - {kind} [{title[:50]}]: missing from your workflow ({len(txt)} chars)")
    if not any_t: lines.append("  (no change)")
    # structure: other nodes
    lines.append("== Other node changes (nodes not covered above) ==")
    cn, cb = collections.Counter(n["class_type"] for n in new.values()), collections.Counter(n["class_type"] for n in base.values())
    cc = 0
    for c in sorted(set(cn) | set(cb)):
        if cn[c] != cb[c]: cc += 1; lines.append(f"  {'+' if cn[c] > cb[c] else '-'} node count {c}: skill {cb[c]} -> yours {cn[c]}")
    for c in sorted(set(cn) & set(cb)):
        if c in HEADLINE_CLASSES or c in TEXT_CLASSES: continue
        A = [scalars(n) for n in base.values() if n["class_type"] == c]; B = [scalars(n) for n in new.values() if n["class_type"] == c]
        A2 = [x for x in A if x not in B]; B2 = [x for x in B if x not in A]
        for b in B2:
            if not A2: break
            a = max(A2, key=lambda x: sum(1 for k in b if x.get(k) == b[k])); A2.remove(a)
            # only keys present on both sides: the UI converter adds defaults the source graph leaves out, which is not a human edit
            delta = {k: (a[k], b[k]) for k in set(a) & set(b) if a[k] != b[k]}
            if delta: cc += 1; lines.append(f"  ~ {c}: " + "; ".join(f"{k}: {str(x)[:50]} -> {str(y)[:50]}" for k, (x, y) in list(delta.items())[:6]))
    if not cc: lines.append("  (none)")
    return "\n".join(lines)

def baseline(arg, name):
    if arg: return load_api(arg)
    fn = name if name.endswith(".json") else name + ".json"
    # what ui_build.py last published: the skill's own workflows, or a client batch's in the repo (fixtures/<brand>/workflows_ui/)
    loc = next((p for p in [SKILL_DIR / "workflows_ui" / fn, *SKILL_DIR.parents[2].glob(f"fixtures/*/workflows_ui/{fn}")] if p.exists()), None)
    if not loc: raise SystemExit(f"no local baseline for {fn} in workflows_ui; pass --baseline <file>")
    return load_api(loc)

def main():
    _reexec_in_comfy_venv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sub = ap.add_subparsers(dest="cmd", required=True)
    ap.add_argument("--folder", default=FOLDER, help='Comfy Cloud workflows folder (default "Comfy UGC")')
    sub.add_parser("list")
    p = sub.add_parser("pull"); p.add_argument("name"); p.add_argument("--baseline"); p.add_argument("--out"); p.add_argument("--json", action="store_true")
    d = sub.add_parser("diff"); d.add_argument("new"); d.add_argument("base")
    a = ap.parse_args(); globals()["FOLDER"] = a.folder
    if a.cmd == "list":
        for e in sorted(cloud_list(), key=lambda e: e["path"]):
            import datetime; print(datetime.datetime.fromtimestamp(e["modified"] / 1000).strftime("%Y-%m-%d %H:%M"), f"{e['size']:>8}", e["path"].split("/", 1)[1])
        return
    if a.cmd == "diff": print(diff(load_api(a.new), load_api(a.base))); return
    ui = cloud_get(a.name)
    out = pathlib.Path(a.out or pathlib.Path(tempfile.gettempdir()) / "ui_sync"); out.mkdir(parents=True, exist_ok=True)
    fn = out / (a.name if a.name.endswith(".json") else a.name + ".json"); json.dump(ui, open(fn, "w"), indent=1)
    print(f"pulled {a.name!r} ({len(ui.get('nodes', []))} nodes) -> {fn}")
    new = to_api(fn); base = baseline(a.baseline, a.name)
    if a.json: print(json.dumps({"settings_yours": settings(new), "settings_skill": settings(base)}, indent=1))
    print(diff(new, base))
    print("\nNothing was applied. Update the skill's configs by hand from the lines above.")

if __name__ == "__main__": main()
