#!/usr/bin/env python3
"""One HeyGen Video 1 take on Comfy Cloud (partner node HeyGenImageToVideoNode), the default model (references/heygen.md).
  heygen_take.py --spec S.json --take N --still FIRST.png --work W [--seed N] [--try a] [--print-prompt | --check] --user-ok --cap USD
  heygen_take.py ... --prompt-file P.txt [--seconds N]   a hand-written prompt instead of the compiled one
The prompt is compiled from the spec (h3prompt.heygen_prompt) and the length from the take (5-15 s). --print-prompt and --check are free.
Before any spend: the spec lint (h3prompt.lint) and the prompt check (no impact sound in the audio paragraph, no small objects falling, no
show-the-result gesture). Then the spend guard, submit, the job in <work>/jobs.jsonl, and the take at <work>/takes/<name>.mp4.
The seed defaults to the spec seed for every take of a creator: the image-to-video node has no voice reference (the reference-to-video node takes @Audio1; not wired in yet), and one seed keeps the voices closer."""
import argparse, json, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import h3prompt as H

USD15 = 0.32   # 768p, 15 s: Comfy billing 2026-10-01 (67.89 credits)

def check(prompt):
    """Errors as 'RULE: sentence'. The prompt's last paragraph is its audio (references/heygen.md)."""
    E, paras = [], [p for p in prompt.split("\n\n") if p.strip()]
    for s in re.split(r"(?<=[.!?])\s+|\n", prompt):
        if H._hits("small", s) and H._hits("fall", s): E.append(f"SMALL_FALL: {s.strip()[:90]!r}: keep them in a closed fist, the drop happens out of frame")
        if H._hits("show", s): E.append(f"SHOW_RESULT: {s.strip()[:90]!r}: after an action the hand goes back to rest")
    if paras and H._hits("sfx", paras[-1]): E.append(f"SOUND_EFFECT: the audio paragraph asks for {H._hits('sfx', paras[-1])}: room tone and ambience only")
    return E

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True); ap.add_argument("--take", type=int, required=True); ap.add_argument("--still")
    ap.add_argument("--work", default="."); ap.add_argument("--seed", type=int); ap.add_argument("--try", dest="tryn", default="a")
    ap.add_argument("--prompt-file"); ap.add_argument("--seconds", type=int); ap.add_argument("--allow", default="")
    ap.add_argument("--print-prompt", action="store_true"); ap.add_argument("--check", action="store_true")
    ap.add_argument("--user-ok", action="store_true"); ap.add_argument("--cap", type=float)
    a = ap.parse_args(); work = pathlib.Path(a.work)
    spec = json.loads(pathlib.Path(a.spec).read_text()); n = a.take
    if not 1 <= n <= len(spec["takes"]): raise SystemExit(f"--take must be 1..{len(spec['takes'])}")
    take = spec["takes"][n - 1]
    prompt = pathlib.Path(a.prompt_file).read_text().strip() if a.prompt_file else H.heygen_prompt(spec, take)
    secs = a.seconds or H.heygen_seconds(spec, take); seed = a.seed if a.seed is not None else int(spec.get("seed", 1000))
    if a.print_prompt: print(prompt); return
    E = [] if a.prompt_file else H.lint(spec, take, True, 30, allow={r.strip() for r in a.allow.split(",") if r.strip()})[0]
    E += check(prompt)
    if not 5 <= secs <= 15: E.append(f"HEYGEN_DURATION: {secs} s is outside HeyGen's 5-15 s; split the take")
    for e in E: print(e)
    if E: raise SystemExit("check failed, nothing submitted")
    if a.check: print(f"check OK: {secs} s, seed {seed}"); return
    if not a.still: raise SystemExit("--still FIRST.png required to render")
    if not a.user_ok: raise SystemExit("refusing to spend: pass --user-ok only after the user said yes to the quote")
    if a.cap is None: raise SystemExit("--cap USD required")
    import take_graph as T, ledger
    name = f"{spec['name']}-T{n}-heygen-768p-{a.tryn}"
    est = round(USD15 * secs / 15, 2); spent = ledger.build(work)["total_usd"]
    print(f"spend guard: spent ${spent:.2f} + est ${est:.2f} vs cap ${a.cap:.2f}")
    if spent + est > a.cap: raise SystemExit("BUDGET STOP: over the cap, no job submitted")
    g = {"1": {"class_type": "LoadImage", "inputs": {"image": T.upload(work, a.still)}},
         "2": {"class_type": "HeyGenImageToVideoNode", "inputs": {"model": "heygen-video-1", "model.image": ["1", 0], "model.prompt": prompt,
                                                                 "model.duration": secs, "model.resolution": "768p", "model.seed": seed}},
         "3": {"class_type": "SaveVideo", "inputs": {"video": ["2", 0], "filename_prefix": f"video/{name}", "format": "mp4", "format.codec": "h264"}}}
    (work / "graphs").mkdir(parents=True, exist_ok=True); gp = work / "graphs" / f"{name}.api.json"; gp.write_text(json.dumps(g, indent=1))
    r = T.comfy("run", "--workflow", str(gp), "--where", "cloud", "--allow-spend", "--no-watch")
    if not r.get("ok"): raise SystemExit("SUBMIT FAIL: " + json.dumps(r.get("error"))[:600])
    pid = r["data"]["prompt_id"]; ledger.record(work, name, pid, "heygen"); print("submitted", name, pid, flush=True)
    (work / "takes").mkdir(exist_ok=True); sys.exit(0 if T.collect(pid, work / "takes" / f"{name}.mp4") else 1)

if __name__ == "__main__":
    main()
