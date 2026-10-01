#!/usr/bin/env python3
"""Standalone review of ONE take/ad mp4 against references/qa-rubric.md. Output: one JSON object (stdout and, with --out, a file).
Checks: frame (media.py cropcheck, local), words (Gemini audio transcript + diff vs the lines the prompt quotes), visual candidates (Gemini on the video),
hair (Gemini on head-only 4x4 grids at 4 fps, per-frame level 0/1/2, level-2 runs are blocking). Gemini is a candidate finder, not proof.
Model default gemini-3.8-flash (Nico 2026-10-01; env GEMINI_MODEL / --model). Route: OpenRouter (google/<model>) when OPENROUTER_API_KEY is set,
else the Gemini API with GEMINI_API_KEY / GOOGLE_API_KEY; --via openrouter|google forces one. Keys are never printed. Runs outside any Comfy graph.

  review.py <clip.mp4> (--spec S.json --take N | --prompt-file P.txt | --expected L.txt) [--out review.json] [--model M] [--via openrouter|google] [--no-visual] [--no-hair] [--cutaways t0-t1,..]
    --expected: text file, one scripted line per line. Without any of these the word check is skipped (reported as skipped).
    --cutaways: an assembled ad's planned b-roll/packshot ranges, so those cuts are not reported.
  review.py <still.png> --still --spec S.json --take N [--same-as BASE.png] [--no-realism]
    first-frame gate: props visible and reachable, hands, camera, a real-phone-photo look, and with --same-as the same person as the base still.
Score (rubric): 0 when clean, else (real issues) + 0.5 per second affected. Lower is better. Real = audio blocking, visual blocking with conf >= 0.85,
hair level-2 run, frame shrink. Fix hint: `fix.ranges` lists removable spoken defects (cut candidates)."""
import argparse, base64, json, os, pathlib, re, subprocess, sys, tempfile, time, urllib.request, urllib.error
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media

PRICE_PER_M = {"in": float(os.environ.get("GEMINI_PRICE_IN", "0.75")), "out": float(os.environ.get("GEMINI_PRICE_OUT", "3.75"))}  # gemini-3.8-flash list price (OpenRouter, 2026-10-01), USD / 1M tokens, override via env; only used when the provider reports no cost

AUDIO_SYS = (
    "Check a spoken ad take against its script. Inputs: EXPECTED (the lines the speaker must say, one per line, in order) and the take's AUDIO. "
    "First transcribe the audio with word-level start/end seconds, then diff it with EXPECTED. "
    "Ignore case, punctuation, speech-recognition homophones and small spelling differences of names. "
    "Report EXTRA (unscripted words, filler, babble, or a VOCAL sound longer than 0.3 s such as laughter, mumbling, humming, singing, a sigh or a whisper), REPEAT (a scripted line or phrase said twice), "
    "WRONG (a scripted word replaced by a different word), MISSING (a scripted word not said) and ORDER. "
    "Non-vocal sounds (clattering, rustling, a thud, a click, a drawer, footsteps, room tone) are expected: ignore them. "
    "t0 = start of the first word involved, t1 = end of the last word involved; cut_in and cut_out = the middle of the silence just before and just after that span. "
    "sev is blocking for any WRONG content word, 2 or more EXTRA/MISSING words, any REPEAT, or a vocal sound longer than 0.5 s; otherwise minor. "
    "removable is true only if deleting the audio between cut_in and cut_out leaves EXPECTED exactly. "
    'Answer with JSON only: {"transcript":"full text","issues":[{"type":"EXTRA|REPEAT|WRONG|MISSING|ORDER","sev":"blocking|minor","t0":0.0,"t1":0.0,"cut_in":0.0,"cut_out":0.0,"removable":false,"evidence":"said vs expected"}]}. '
    'If the take matches EXPECTED, issues is an empty list.')

VISUAL_SYS = (
    "You are the QA reviewer of a vertical UGC ad take made by an AI video model: one continuous handheld selfie shot, no cuts. "
    "The video is shown at reduced resolution; judge motion as if it played at normal speed. "
    "Report ONLY these defects: CUT (a hard cut, or a sudden change of camera angle or framing), POPIN (hair, a hand, a prop or the product appears, vanishes, jumps or changes shape for 0.25 s or longer), "
    "MORPH (face or identity changes, extra or fused fingers), LABEL (the product label changes or becomes unreadable), ARTIFACT (a freeze, or a smear or warp over the face for 0.3 s or longer). "
    "An INTENDED TAKE may be given: anything it describes (holding up or putting down an object, phone tilts, gestures) is a staged action, never a defect. "
    "Ignore mild motion blur, blinks, hair swaying, compression noise and style. If unsure use sev minor. sev is blocking only if a viewer would obviously notice it at normal speed. conf is 0 to 1. "
    'Answer with JSON only: {"issues":[{"type":"CUT|POPIN|MORPH|LABEL|ARTIFACT","sev":"blocking|minor","conf":0.0,"t0":0.0,"t1":0.0,"what":"one sentence"}]}. If nothing, {"issues":[]}.')

HAIR_SYS = (
    "You check ONE thing in frames of a selfie video: the shape of the hair on top of the head. Each cell shows only the upper part of the frame (head and hair). "
    "You receive up to 4 images in order. Each image is a 4x4 grid of frames, read left to right, top to bottom, sampled every 0.25 s; the first cell of image 1 is t=0; "
    "the cell in image I (1-based), row R, column C is at t = ((I-1)*16 + (R-1)*4 + (C-1)) * 0.25 seconds. Blank cells after the end of the video are padding: skip them. "
    "For every cell give a level: 0 = hair short or swept, flat or with only a small tuft; 1 = a medium bump or bun shape; 2 = a tall ponytail or bun clearly standing up above the head, much taller than the usual hair. Use x when the head is not visible. "
    "Then list the runs of consecutive cells with level 2 (each run: t0 = time of its first cell, t1 = time of its last cell + 0.25). "
    'Answer with JSON only: {"levels":["16 characters for image 1","..."],"tall_runs":[{"t0":0.0,"t1":0.0}]}. If no cell is level 2, tall_runs is an empty list.')

class Usage:
    tin = tout = calls = 0; cost = None   # cost: USD the provider reported (OpenRouter), else estimated from tokens
    via = None

PROPS_SYS = (
    "You check ONE thing in a selfie video made by an AI video model: how the listed OBJECTS behave. At every moment each object must either rest on a surface "
    "or be gripped by a hand, with fingers visibly wrapped around it. Report FLOAT (the object is in the air or moves while no hand grips it, or a hand only touches its side), "
    "APPEAR (it appears from nowhere), VANISH (it disappears), DUPLICATE (a second copy appears), SHAPE (its shape, size or contents jump or change), "
    "THROUGH (it passes through a hand, the face or another object). Look only at the listed objects. sev is blocking if a viewer would notice it at normal speed. conf is 0 to 1. "
    'Answer with JSON only: {"issues":[{"type":"FLOAT|APPEAR|VANISH|DUPLICATE|SHAPE|THROUGH","object":"name","sev":"blocking|minor","conf":0.0,"t0":0.0,"t1":0.0,"what":"one sentence"}]}. If nothing, {"issues":[]}.')

STILL_SYS = (
    "You check the FIRST FRAME (a still image) a video will be generated from. Inputs: the OBJECTS the person will handle with where each one should be, the expected HANDS, "
    "the room DETAILS that belong in the picture, and the CAMERA. "
    "For each object answer visible (fully inside the frame and clearly recognizable), reachable (within easy reach of a free hand; in a selfie, the hand that is not holding the phone) "
    "and where (a short phrase). Then hands_ok: the hands match the expected HANDS. Then extra: every supplement, vitamin, pill, capsule or medicine product or container "
    "(a bottle, jar, tub or box that could be read as the advertised product or a competitor) within the person's reach (on the surface in front of them or in a hand) "
    "that is in none of OBJECTS, HANDS and DETAILS; everyday room items (food, drinks, cups, notebooks, cables, appliances) are never extra; ignore the background of the room. "
    "Then camera_ok: the picture matches the CAMERA (selfie = shot from the person's own outstretched arm, the phone itself out of view; propped = a steady phone standing in front of them, "
    "both hands free; first-person = only hands and forearms seen from the person's own eyes, no face). "
    "Then realistic: the image passes as a real, unedited iPhone photo a person took. List realism_issues, each a short phrase, for anything that gives it away as AI or an ad: "
    "plastic, waxy or airbrushed skin, studio or glamour lighting, a stock-photo or catalogue look, an overly tidy or empty room, CGI-looking objects, malformed hands, warped text. "
    "realistic is false if any issue is obvious at phone size. If a SECOND image is given it is the REFERENCE photo: same_person is true when the first image shows the same person "
    "(face, hair, skin tone, age, build) as the reference; else same_person is null. "
    'Answer with JSON only: {"objects":[{"name":"...","visible":true,"reachable":true,"where":"..."}],"hands_ok":true,"extra":["..."],"camera_ok":true,"realistic":true,'
    '"realism_issues":["..."],"same_person":null,"note":"one sentence"}. extra and realism_issues are empty lists if there is none.')

def gemini(model, system, parts, retries=4):
    via = Usage.via or os.environ.get("GEMINI_VIA") or ("openrouter" if os.environ.get("OPENROUTER_API_KEY") else "google")
    if via == "openrouter": return _openrouter(model, system, parts, retries)
    try: return _gemini(model, system, parts, retries)
    except _Unavailable:
        print(f"{model} unavailable, falling back to gemini-flash-latest", file=sys.stderr); return _gemini("gemini-flash-latest", system, parts, retries)

class _Unavailable(Exception): pass

def _gemini(model, system, parts, retries):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key: raise SystemExit("GEMINI_API_KEY / GOOGLE_API_KEY not set (run preflight.py)")
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 16384}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "x-goog-api-key": key})
    for k in range(retries):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=300)); break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and k < retries - 1: time.sleep(5 * (k + 1)); continue
            if e.code in (429, 500, 503) and model != "gemini-flash-latest": raise _Unavailable()
            raise SystemExit(f"Gemini HTTP {e.code}: {e.read().decode()[:300]}")
    u = d.get("usageMetadata", {}); Usage.tin += u.get("promptTokenCount", 0); Usage.tout += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0); Usage.calls += 1
    text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
    m = re.search(r"\{[\s\S]*\}", text)
    try: return json.loads(m.group(0))
    except Exception: return {"_unparsable": text[:300]}

def _openrouter(model, system, parts, retries):
    """The same call through OpenRouter's chat completions (google/<model>): Gemini parts become text, image_url, input_audio and video_url (data URLs)."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key: raise SystemExit("OPENROUTER_API_KEY not set (run preflight.py)")
    content = []
    for p in parts:
        if "text" in p: content.append({"type": "text", "text": p["text"]}); continue
        mime, data = p["inlineData"]["mimeType"], p["inlineData"]["data"]
        if mime.startswith("audio/"): content.append({"type": "input_audio", "input_audio": {"data": data, "format": mime.split("/")[1]}})
        elif mime.startswith("video/"): content.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64,{data}"}})
        else: content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    body = {"model": model if "/" in model else f"google/{model}", "temperature": 0, "max_tokens": 16384, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    for k in range(retries):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=300)); break
        except urllib.error.HTTPError as e:
            if e.code in (408, 429, 500, 502, 503) and k < retries - 1: time.sleep(5 * (k + 1)); continue
            raise SystemExit(f"OpenRouter HTTP {e.code}: {e.read().decode()[:300]}")
    if "choices" not in d: raise SystemExit(f"OpenRouter error: {json.dumps(d.get('error', d))[:300]}")
    u = d.get("usage") or {}; Usage.tin += u.get("prompt_tokens", 0); Usage.tout += u.get("completion_tokens", 0); Usage.calls += 1
    if u.get("cost") is not None: Usage.cost = (Usage.cost or 0) + u["cost"]
    text = d["choices"][0]["message"].get("content") or ""
    m = re.search(r"\{[\s\S]*\}", text)
    try: return json.loads(m.group(0))
    except Exception: return {"_unparsable": text[:300]}

def inline(path, mime): return {"inlineData": {"mimeType": mime, "data": base64.b64encode(pathlib.Path(path).read_bytes()).decode()}}

def expected_lines(args):
    """(spoken lines or None, timeline text, kind, scripted non-verbal sounds, props). From the spec when given (both prompt styles)."""
    if args.expected: return [l.strip() for l in pathlib.Path(args.expected).read_text().splitlines() if l.strip()], None, "speech", [], []
    if args.spec:
        import h3prompt
        spec = json.loads(pathlib.Path(args.spec).read_text()); take = spec["takes"][args.take - 1]
        lines, tl, kind, sounds = h3prompt.expected(spec, take); props, _ = h3prompt._layout(spec, take)
        listed = {p["name"].lower() for p in props} & {a.lower() for a in spec.get("product_aliases", ["bottle"])}
        return lines, tl, kind, sounds, [p.get("desc") or p["name"] for p in props] + (["the product bottle"] if take.get("shows_product") and not listed else [])
    if args.prompt_file:
        prompt = pathlib.Path(args.prompt_file).read_text()   # H3 (says "..", <d>, Timeline) or HeyGen (says <delivery>: "..", timed "0.2-5s:" lines)
        lines = re.findall(r'\bsays\b[^"\n]*?"([^"]+)"', prompt) + re.findall(r"<d>\[[^\]]*\]\s*(.*?)</d>", prompt, re.S)
        tl = re.search(r"Timeline, second by second: (.*?) Throughout", prompt, re.S)
        tl = tl.group(1) if tl else "\n".join(re.findall(r"^\s*\d+(?:\.\d+)?-\d+(?:\.\d+)?s: .+$", prompt, re.M)) or None
        sounds = sorted({m.lower() for m in re.findall(r"\b(yawn|sigh|swallow|gulp|exhale)", tl or "", re.I)})   # scripted body sounds, never EXTRA
        return lines, tl, ("speech" if lines else "action"), sounds, []
    return None, None, "speech", [], []

CAMERA_TEXT = {"selfie": "selfie (front camera at arm's length)", "propped": "propped (a phone standing in front of them)", "pov": "first-person (hands and forearms only)"}

def still_check(png, props, hands, model, camera="selfie", same_as=None, realism=True, details=()):
    """Free-ish pre-render gate: every declared prop visible and within reach in the first-frame still, hands as scripted, no undeclared
    product within reach (e.g. the product left in the still of a pain take), the camera as scripted, a real-phone-photo look (realism=False for
    an x-ray end still, which is a graphic on purpose) and, with same_as (the base still), the same person. details: the still's room details."""
    parts = [{"text": "OBJECTS:\n" + ("\n".join(props) or "none") + f"\nHANDS: {hands or 'not specified'}\nDETAILS: {'; '.join(details) or 'none'}"
                      f"\nCAMERA: {CAMERA_TEXT.get(camera, camera)}"}, inline(png, "image/png")]
    if same_as: parts.append(inline(same_as, "image/png"))
    r = gemini(model, STILL_SYS, parts)
    objs = r.get("objects", [])
    ok = (all(o.get("visible") and o.get("reachable") for o in objs) and (bool(objs) or not props) and r.get("hands_ok", True) is not False and not r.get("extra")
          and r.get("camera_ok", True) is not False and (not realism or r.get("realistic", True) is not False) and (not same_as or r.get("same_person") is True))
    return {"still": str(png), "ok": ok, **r, "model": model, "gemini_calls": Usage.calls}

def head_grids(clip, tmp, dur):
    """Head-only (top 45%) frames at 4 fps, 4x4 grids of 320x260 cells; up to 4 grids (16 s)."""
    w, h = [int(x) for x in media.probe(clip, "stream=width,height", "v:0").split(",")]; out = []
    n = min(4, max(1, int(-(-dur * 4 // 16))))
    for k in range(n):
        p = pathlib.Path(tmp) / f"hair{k}.png"
        media.ff("-ss", str(k * 4), "-t", "4", "-i", str(clip), "-vf", f"fps=4,crop={w}:{int(h*0.45)}:0:0,scale=320:260,tile=4x4:padding=4", "-frames:v", "1", str(p))
        if p.exists(): out.append(p)
    return out

def review(clip, expected, timeline, model, do_visual=True, do_hair=True, kind="speech", sounds=(), props=()):
    clip = pathlib.Path(clip); dur = media.duration(clip); F = []; aff = []; notes = []; fixr = []
    w, h, bad = media.cropcheck(clip)
    if bad: F.append({"source": "frame", "type": "SHRINK", "sev": "blocking", "t0": 0.0, "t1": round(dur, 2), "what": f"frame {w}x{h} shrinks into crops {bad[:3]}: re-render with a new seed"}); aff.append(dur)
    with tempfile.TemporaryDirectory() as tmp:
        if expected or (expected is not None and kind == "action"):   # an action take expects silence from the speaker: any word is EXTRA
            a = pathlib.Path(tmp) / "a.mp3"; media.ff("-i", str(clip), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(a))
            exp = "EXPECTED:\n" + ("\n".join(expected) if expected else "NONE. This is a silent action clip: every spoken, mumbled or whispered word is EXTRA, blocking.")
            if sounds: exp += "\nSCRIPTED NON-VERBAL SOUNDS (part of the script, never defects): " + "; ".join(sounds)
            import h3prompt
            if expected and any(re.search(h3prompt.FILLER, l, re.I) for l in expected):   # scripted fillers are texture: said, skipped or swapped is never a defect
                exp += "\nFILLERS (scripted): um, uh, like, you know, I mean and trailing pauses in EXPECTED are optional texture; a filler said, skipped, repeated or swapped is never an issue."
            r = gemini(model, AUDIO_SYS, [{"text": exp}, inline(a, "audio/mp3")])
            transcript = r.get("transcript", "")
            for i in r.get("issues", []):
                F.append({"source": "audio", "type": i.get("type"), "sev": i.get("sev"), "t0": i.get("t0"), "t1": i.get("t1"), "what": i.get("evidence"), "removable": bool(i.get("removable")), "cut_in": i.get("cut_in"), "cut_out": i.get("cut_out")})
                if i.get("sev") == "blocking":
                    aff.append(max(0.0, (i.get("t1") or 0) - (i.get("t0") or 0)))
                    if i.get("removable") and i.get("cut_in") is not None and i.get("cut_out") is not None: fixr.append([i["cut_in"], i["cut_out"]])
            if "_unparsable" in r: notes.append("audio review unparsable: " + r["_unparsable"])
        else: transcript = None; notes.append("word check skipped: no expected lines (pass --spec/--take, --prompt-file or --expected)")
        if do_visual:
            v = pathlib.Path(tmp) / "v.mp4"; media.ff("-i", str(clip), "-an", "-vf", "scale=360:-2", "-r", "12", "-c:v", "libx264", "-crf", "30", str(v))
            r = gemini(model, VISUAL_SYS, [{"text": ("INTENDED TAKE, by time:\n" + timeline + "\n\n" if timeline else "") + "Review this video for the listed defects."}, inline(v, "video/mp4")])
            for i in r.get("issues", []):
                real = i.get("sev") == "blocking" and (i.get("conf") or 0) >= 0.85
                F.append({"source": "visual", "type": i.get("type"), "sev": i.get("sev"), "conf": i.get("conf"), "t0": i.get("t0"), "t1": i.get("t1"), "what": i.get("what"), "real": real})
                if real: aff.append(max(0.0, (i.get("t1") or 0) - (i.get("t0") or 0)))
            if "_unparsable" in r: notes.append("visual review unparsable")
        if do_visual and props:   # dedicated object check (one question, like the hair check): grip, float, pop-in, duplicates
            v = pathlib.Path(tmp) / "p.mp4"; media.ff("-i", str(clip), "-an", "-vf", "scale=480:-2", "-r", "12", "-c:v", "libx264", "-crf", "28", str(v))
            r = gemini(model, PROPS_SYS, [{"text": "OBJECTS:\n" + "\n".join(props) + ("\n\nINTENDED TAKE, by time:\n" + timeline if timeline else "")}, inline(v, "video/mp4")])
            for i in r.get("issues", []):
                real = i.get("sev") == "blocking" and (i.get("conf") or 0) >= 0.85
                F.append({"source": "props", "type": i.get("type"), "object": i.get("object"), "sev": i.get("sev"), "conf": i.get("conf"), "t0": i.get("t0"), "t1": i.get("t1"), "what": i.get("what"), "real": real})
                if real: aff.append(max(0.0, (i.get("t1") or 0) - (i.get("t0") or 0)))
            if "_unparsable" in r: notes.append("props check unparsable")
        if do_hair:
            gs = head_grids(clip, tmp, dur)
            r = gemini(model, HAIR_SYS, [{"text": "Classify the hair in every cell."}] + [inline(g, "image/png") for g in gs])
            for run in r.get("tall_runs", []):
                F.append({"source": "hair", "type": "HAIR", "sev": "blocking", "t0": run.get("t0"), "t1": run.get("t1"), "what": "tall ponytail/bun (level 2) run"}); aff.append(max(0.25, (run.get("t1") or 0) - (run.get("t0") or 0)))
            if "_unparsable" in r: notes.append("hair check unparsable")
    real_n = len(aff); score = 0.0 if not real_n else round(real_n + 0.5 * sum(aff), 2)
    non_removable = [f for f in F if f["sev"] == "blocking" and not (f["source"] == "audio" and f.get("removable"))]
    action = "none" if not real_n else ("cut" if (fixr and not non_removable) else "rerender")
    return {"clip": str(clip), "duration": round(dur, 2), "score": score, "clean": real_n == 0, "findings": F, "transcript": transcript,
            "fix": {"action": action, "ranges": fixr if action == "cut" else []}, "notes": notes,
            "model": model, "gemini_calls": Usage.calls, "tokens_in": Usage.tin, "tokens_out": Usage.tout,
            "cost_usd_est": round(Usage.cost if Usage.cost is not None else (Usage.tin * PRICE_PER_M["in"] + Usage.tout * PRICE_PER_M["out"]) / 1e6, 4),
            "disclaimer": "Gemini is a candidate finder (hallucinates, misses short glitches). clean = no defects found, not proof. A human editor does the final look."}

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", help="the take mp4, or with --still the first-frame png"); ap.add_argument("--spec"); ap.add_argument("--take", type=int, default=1); ap.add_argument("--prompt-file"); ap.add_argument("--expected")
    ap.add_argument("--out"); ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")); ap.add_argument("--via", choices=["openrouter", "google"]); ap.add_argument("--no-visual", action="store_true"); ap.add_argument("--no-hair", action="store_true")
    ap.add_argument("--still", action="store_true", help="check a first-frame still against the props, hands and camera of --take (a take that starts from it) before rendering (exit 1 if not ok)")
    ap.add_argument("--same-as", metavar="BASE.png", help="with --still: the base still; the gate also requires the same person (a hook in a new place)")
    ap.add_argument("--no-realism", action="store_true", help="with --still: skip the real-photo check (an x-ray end still is a graphic on purpose)")
    ap.add_argument("--cutaways", default="", help="assembled ad: comma list of t0-t1 ranges where the picture cuts to b-roll or the packshot on purpose (never a CUT defect)")
    a = ap.parse_args(); Usage.via = a.via
    exp, tl, kind, sounds, props = expected_lines(a)
    import h3prompt
    spec = json.loads(pathlib.Path(a.spec).read_text()) if a.spec else {}
    take = spec["takes"][a.take - 1] if spec.get("takes") else {}
    cam = h3prompt._scene(spec, take)[1]
    if a.still:
        sp, hands = h3prompt._layout(spec, take)
        res = still_check(a.clip, [f"{p['name']}: {p.get('desc', '')} {p.get('where', '')}".strip() for p in sp], hands, a.model, cam, a.same_as, not a.no_realism,
                          take.get("details", spec.get("details", [])))
        s = json.dumps(res, indent=1)
        if a.out: pathlib.Path(a.out).write_text(s)
        print(s); sys.exit(0 if res["ok"] else 1)
    if a.cutaways:
        tl = ((tl + "\n") if tl else "") + "INTENDED CUTAWAYS: at " + ", ".join(f"{r}s" for r in a.cutaways.split(",")) + \
             " the picture cuts away to b-roll or the product packshot and then back to the creator; these cuts are staged, never CUT defects."
    res = review(a.clip, exp, tl, a.model, not a.no_visual, not a.no_hair and cam != "pov", kind, sounds, props)   # a first-person b-roll has no head to check
    s = json.dumps(res, indent=1)
    if a.out: pathlib.Path(a.out).write_text(s)
    print(s)

if __name__ == "__main__": main()
