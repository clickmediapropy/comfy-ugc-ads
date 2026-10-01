#!/usr/bin/env python3
"""Bounded fix loop for ONE take/ad (references/qa-rubric.md "Fix policy"). Max 2 attempts. A fix replaces the original only if its re-review
score is STRICTLY lower; otherwise the original ships and the defect is marked NEEDS EDITOR with timestamps. Cuts are free (ffmpeg via media.py);
a re-render is a paid job and is never started here: when the policy says re-render, this script stops and reports `rerender` so the agent can
guard/record/submit it with take_graph.py (new --seed, new --try suffix, positive one-line feedback in the prompt), then run `fix.py --review-only` on the new clip.

  fix.py <clip.mp4> --out fixed.mp4 [--spec S.json --take N | --prompt-file P | --expected L.txt] [--review review.json] [--model M] [--report fix.json]
      --review: reuse an existing review.json of the original (skips one Gemini call)
      --rerender-candidate NEW.mp4: you re-rendered the take; compare its review to the original's and keep it only if strictly lower
Exit 0 = `kept` or `clean`, 2 = NEEDS EDITOR (original left untouched at --out as a copy), 3 = rerender needed (see report).
Report JSON: {status: clean|kept|needs_editor|rerender, original_score, final_score, attempts:[...], final_clip, needs_editor:[{t0,t1,what}]}"""
import argparse, json, pathlib, shutil, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media, review as R

MAX_ATTEMPTS = 2

def do_review(clip, ctx, model):
    exp, tl, kind, sounds, props = ctx   # R.expected_lines(): lines, timeline, take kind, scripted sounds, props
    return R.review(clip, exp, tl, model, kind=kind, sounds=sounds, props=props)

def unresolved(rv):
    return [{"t0": f.get("t0"), "t1": f.get("t1"), "what": f"{f['source']} {f['type']}: {f.get('what')}"} for f in rv["findings"]
            if f.get("sev") == "blocking" and (f["source"] not in ("visual", "props") or f.get("real"))]

def run(clip, out, ctx, model, first=None, candidate=None):
    clip, out = pathlib.Path(clip), pathlib.Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    orig = first or do_review(clip, ctx, model); rep = {"original": str(clip), "original_score": orig["score"], "attempts": [], "final_clip": str(clip)}
    if orig["clean"]:
        shutil.copyfile(clip, out); rep.update(status="clean", final_score=orig["score"], final_clip=str(out), needs_editor=[]); return rep
    best, best_rv = clip, orig
    if candidate:   # a re-render the caller made (paid, outside this script): same strict-improvement rule
        rv = do_review(candidate, ctx, model)
        rep["attempts"].append({"kind": "rerender", "clip": str(candidate), "score": rv["score"], "kept": rv["score"] < orig["score"]})
        if rv["score"] < orig["score"]: best, best_rv = pathlib.Path(candidate), rv
    else:
        cur_rv = orig
        for n in range(1, MAX_ATTEMPTS + 1):
            fx = cur_rv["fix"]
            if fx["action"] != "cut":
                rep["attempts"].append({"kind": "stop", "reason": "policy says re-render (non-removable, overlaps speech, frame shrink, hair or 3+ issues)"}); break
            tmp = out.with_name(f"{out.stem}.try{n}.mp4"); media.cut(str(clip), str(tmp), [f"{a}-{b}" for a, b in fx["ranges"]])
            rv = do_review(tmp, ctx, model); kept = rv["score"] < best_rv["score"]
            rep["attempts"].append({"kind": "cut", "ranges": fx["ranges"], "clip": str(tmp), "score": rv["score"], "kept": kept})
            if kept: best, best_rv = tmp, rv
            if rv["clean"] or not kept: break
            cur_rv = rv; clip = tmp   # cut again on the improved clip
    if best != pathlib.Path(rep["original"]) : shutil.copyfile(best, out)
    else: shutil.copyfile(rep["original"], out)
    rep["final_score"] = best_rv["score"]; rep["final_clip"] = str(out)
    rest = unresolved(best_rv); rep["needs_editor"] = rest
    if best_rv["clean"]: rep["status"] = "kept" if best != pathlib.Path(rep["original"]) else "clean"
    elif not candidate and any(a["kind"] == "stop" for a in rep["attempts"]) and len([a for a in rep["attempts"] if a["kind"] == "cut"]) < MAX_ATTEMPTS: rep["status"] = "rerender"
    else: rep["status"] = "needs_editor"
    return rep

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip"); ap.add_argument("--out", required=True); ap.add_argument("--spec"); ap.add_argument("--take", type=int, default=1)
    ap.add_argument("--prompt-file"); ap.add_argument("--expected"); ap.add_argument("--review"); ap.add_argument("--rerender-candidate")
    ap.add_argument("--model", default="gemini-3.8-flash"); ap.add_argument("--report")
    a = ap.parse_args()
    ctx = R.expected_lines(a)
    first = json.loads(pathlib.Path(a.review).read_text()) if a.review else None
    rep = run(a.clip, a.out, ctx, a.model, first, a.rerender_candidate)
    s = json.dumps(rep, indent=1); print(s)
    if a.report: pathlib.Path(a.report).write_text(s)
    sys.exit({"clean": 0, "kept": 0, "needs_editor": 2, "rerender": 3}[rep["status"]])

if __name__ == "__main__": main()
