#!/usr/bin/env python3
"""Web mode (claude.ai / Cowork, Comfy Cloud connector, no comfy CLI): the `run_saved_workflow` call for one take, from the spec. Free, stdlib only.
  web_overrides.py --spec S.json --take N --first FIRST.png [--model heygen|h3] [--seed N] [--allow RULE,..]
      heygen (the default, h3prompt.DEFAULT_MODEL): 8-heygen-take, the same prompt, length and seed as heygen_take.py
  web_overrides.py --spec S.json --take N --first F.png [--take M --first G.png ..] --write-workflow OUT.json
      HeyGen, because the connector can't submit it yet (references/web-mode.md): a ready save-format workflow with one
      still -> HeyGen -> save chain per take, values already in the widgets. save_workflow(name, workflow_json: OUT.json), then the user
      presses Run once in the Comfy Cloud web UI for the whole batch
      h3 (the voice-reference fallback): [--last LAST.png] [--packshot P.png] [--voice V.wav] [--draft], the same as take_graph.py
FIRST/LAST/P/V are Comfy input file names (from use_previous_output or upload_file). Prints JSON {workflow, path, seconds, frames, steps, errors,
input_overrides}: pass `workflow` as run_saved_workflow's filename and `input_overrides` as is. Exit 1 when the lint has errors (nothing to run).
It picks the same workflow and prompt as take_graph.py (references/web-mode.md): speech take, no voice/product -> 2-speech-take (fl path);
speech take with a voice reference or the product -> 3-speech-take-voice-reference (ref path); action take -> 4-action-take."""
import argparse, json, pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import h3prompt

WF = {"speech-first": "2-speech-take", "speech-voice": "3-speech-take-voice-reference", "action": "4-action-take"}   # workflows_ui/ file stems (ui_build.py --file-names slug)

def nodes_by_title(wf):
    """{title prefix: node} for the nodes the web mode sets (titles written by ui_build.py)."""
    out = {}
    for n in wf["nodes"]:
        t = (n.get("title") or "").upper()
        for key in ("PROMPT", "SEED", "FIRST FRAME", "LAST FRAME", "PICTURE 1", "AUDIO 1", "HQ?"):
            if t.startswith(key): out[key] = n
        if n["type"] in ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"): out["H3"] = n
    return out

def write_workflow(spec, pairs, a):
    """Save-format workflow with one LoadImage -> HeyGenImageToVideoNode -> SaveVideo chain per (take, first still), cloned from
    workflows_ui/8-heygen-take.json with the take's prompt, length and seed in the widgets. Prints a summary; returns the exit code."""
    import copy, heygen_take
    base = json.loads((pathlib.Path(a.workflows) / "8-heygen-take.json").read_text())
    tmpl = {x["type"]: x for x in base["nodes"]}
    seed = a.seed if a.seed is not None else int(spec.get("seed", 1000))   # one seed for every take of a creator (no voice reference)
    nodes, links, nid, lid, rows, bad = [], [], 1, 1, [], 0
    for col, (n, first) in enumerate(pairs):
        take = spec["takes"][n - 1]; prompt = h3prompt.heygen_prompt(spec, take); secs = h3prompt.heygen_seconds(spec, take)
        errs = h3prompt.lint(spec, take, True, 30, allow={r.strip() for r in a.allow.split(",") if r.strip()})[0] + heygen_take.check(prompt)
        bad += bool(errs); name = f"{spec['name']}-T{n}-heygen-768p-{a.tryn}"
        li, hg, sv = (copy.deepcopy(tmpl[k]) for k in ("LoadImage", "HeyGenImageToVideoNode", "SaveVideo"))
        li["id"], hg["id"], sv["id"] = nid, nid + 1, nid + 2; l1, l2 = lid, lid + 1
        x = 50 + col * 380
        li["pos"], hg["pos"], sv["pos"] = [x, 90], [x, 300], [x, 760]
        li["title"] = f"FIRST FRAME · take {n}"; hg["title"] = f"HEYGEN · take {n}, {secs} s, seed {seed}"; sv["title"] = f"SAVE · take {n}"
        li["widgets_values"][0] = first; hg["widgets_values"][1:5] = [prompt, secs, "768p", seed]; sv["widgets_values"][0] = f"video/{name}"
        li["outputs"][0]["links"] = [l1]; hg["inputs"][0]["link"] = l1; hg["outputs"][0]["links"] = [l2]; sv["inputs"][0]["link"] = l2
        links += [[l1, li["id"], 0, hg["id"], 0, "IMAGE"], [l2, hg["id"], 0, sv["id"], 0, "VIDEO"]]
        nodes += [li, hg, sv]; nid += 3; lid += 2
        rows.append({"take": n, "first": first, "seconds": secs, "seed": seed, "save_as": f"video/{name}", "errors": errs})
    note = copy.deepcopy(tmpl["MarkdownNote"]); note["id"] = nid; note["pos"] = [50, 1200]
    note["widgets_values"][0] = ("## HeyGen batch (run once)\n\nOne HeyGen Video 1 take per column, compiled from the spec by `web_overrides.py --write-workflow`. "
                                 f"Press **Run** once to render all {len(pairs)} (about ${0.32 * sum(r['seconds'] for r in rows) / 15:.2f}). Each take starts exactly from its still.")
    nodes.append(note)
    wf = dict(base, nodes=nodes, links=links, groups=[], last_node_id=nid, last_link_id=lid - 1)
    pathlib.Path(a.write_workflow).write_text(json.dumps(wf, indent=1, ensure_ascii=False))
    print(json.dumps({"workflow_json": a.write_workflow, "takes": rows, "est_usd": round(0.32 * sum(r["seconds"] for r in rows) / 15, 2)}, indent=1, ensure_ascii=False))
    return 1 if bad else 0

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True); ap.add_argument("--take", type=int, action="append", required=True)
    ap.add_argument("--first", action="append", required=True); ap.add_argument("--write-workflow"); ap.add_argument("--try", dest="tryn", default="a"); ap.add_argument("--last"); ap.add_argument("--packshot"); ap.add_argument("--voice")
    ap.add_argument("--seed", type=int); ap.add_argument("--draft", action="store_true"); ap.add_argument("--allow", default="")
    ap.add_argument("--model", choices=["heygen", "h3"], default=h3prompt.DEFAULT_MODEL)
    ap.add_argument("--workflows", default=str(HERE.parent / "workflows_ui"), help="folder with the UI workflow JSONs (default: this skill's)")
    a = ap.parse_args()
    spec = json.loads(pathlib.Path(a.spec).read_text())
    if len(a.take) != len(a.first): raise SystemExit("give one --first per --take")
    if a.write_workflow:
        if a.model != "heygen": raise SystemExit("--write-workflow is for HeyGen (the connector runs the H3 workflows itself)")
        sys.exit(write_workflow(spec, list(zip(a.take, a.first)), a))
    if len(a.take) > 1: raise SystemExit("several --take values only with --write-workflow")
    a.take, a.first = a.take[0], a.first[0]
    n = a.take; take = spec["takes"][n - 1]; k = h3prompt.kind(take)
    if a.model == "heygen":
        import heygen_take
        prompt = h3prompt.heygen_prompt(spec, take); secs = h3prompt.heygen_seconds(spec, take)
        errs = h3prompt.lint(spec, take, True, 30, allow={r.strip() for r in a.allow.split(",") if r.strip()})[0] + heygen_take.check(prompt)
        w = json.loads((pathlib.Path(a.workflows) / "8-heygen-take.json").read_text())
        hg = next(x for x in w["nodes"] if x["type"] == "HeyGenImageToVideoNode"); first = nodes_by_title(w)["FIRST FRAME"]
        o = {str(first["id"]): {"image": a.first}, str(hg["id"]): {"model.prompt": prompt, "model.duration": secs,
             "model.seed": a.seed if a.seed is not None else int(spec.get("seed", 1000))}}   # one seed for every take of a creator (no voice reference)
        print(json.dumps({"workflow": "8-heygen-take", "model": "heygen", "seconds": secs, "errors": errs, "input_overrides": o}, indent=1, ensure_ascii=False))
        sys.exit(1 if errs else 0)
    voice = bool(a.voice) and k == "speech"
    if take.get("shows_product") and not a.packshot: raise SystemExit("this take shows the product: pass --packshot <Comfy input name>")
    role = "action" if k == "action" else "speech-voice" if (voice or take.get("shows_product")) else "speech-first"
    path = "fl" if role != "speech-voice" else "ref"
    steps = 8 if k == "speech" else (25 if a.draft else 30)   # the workflows' own settings: speech turbo 8, action base 25 draft / 30 HQ
    end = True; frames = h3prompt.length(spec, take)[0]
    errs, warns = h3prompt.lint(spec, take, True, steps, fast=(k == "speech"), allow={r.strip() for r in a.allow.split(",") if r.strip()})
    prompt = take.get("prompt") or h3prompt.compile(spec, take, path, True, voice, end)
    stem = WF[role]
    N = nodes_by_title(json.loads((pathlib.Path(a.workflows) / f"{stem}.json").read_text()))
    o = {str(N["PROMPT"]["id"]): {"value": prompt},
         str(N["SEED"]["id"]): {"noise_seed": a.seed if a.seed is not None else int(spec.get("seed", 1000)) + n - 1},
         str(N["FIRST FRAME"]["id"]): {"image": a.first}, str(N["LAST FRAME"]["id"]): {"image": a.last or a.first},
         str(N["H3"]["id"]): {"length": frames}, str(N["HQ?"]["id"]): {"value": not a.draft}}
    if "PICTURE 1" in N and a.packshot: o[str(N["PICTURE 1"]["id"])] = {"image": a.packshot}
    if "AUDIO 1" in N:
        if not a.voice: raise SystemExit("the voice-reference workflow needs --voice <Comfy input name of take 1's audio>")
        o[str(N["AUDIO 1"]["id"])] = {"audio": a.voice}
    print(json.dumps({"workflow": stem, "model": "h3", "path": path, "seconds": round(frames / 24, 2), "frames": frames, "steps": steps,
                      "errors": errs, "warnings": warns, "input_overrides": o}, indent=1, ensure_ascii=False))
    sys.exit(1 if errs else 0)

if __name__ == "__main__":
    main()
