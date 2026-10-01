#!/usr/bin/env python3
"""Build, validate, submit and collect ONE MiniMax H3 take on Comfy Cloud. Parameterized port of h3_long.py / h3_chain.py (originals untouched).
No brand, creator or setting is hardcoded: all of it comes from the spec JSON. Secrets are never read or printed (the comfy CLI holds the login).

Spec (JSON): fields and a full example in references/take-spec.example.json; field meanings in h3prompt.py (prompt compiler + lint).
  Spec level: name, product_name, packshot, seed, pronoun, creator, voice, setting, camera, camera_where, details, light, props [{name, desc, where}], hands, start, pause.
  Per take: kind ("speech" | "action"), seconds ("auto" default), shows_product, tone ("negative" = tired/serious face), soundscape, end_state,
            part ("hook1".."hook3", "body", "close1".."close3"), category (b-roll: references/broll.md),
            setting/camera/camera_where/props/hands (the take's own first frame, when it starts from another still, e.g. the bottle already in hand),
            beats: {"say": line, "action": gesture} or {"silent": s, "steps": [physical steps], "sound": "the non-verbal sound"};
            "prompt" overrides the generated prompt. "prompt_style": "legacy" (spec level) restores the pre-2026-09-30 template for comparison.

Usage:
  take_graph.py --spec S.json --take N --guide FIRST.png [--end-guide LAST.png] [--quality hq|draft] [--work W] [--voice-take V.mp4] [--seed N]
                [--turbo|--no-turbo] [--steps N] [--path fl|ref] [--attention sparse|dense] [--width W --height H] [--try a] [--allow RULE,..]
      (no action flag)  write the graph + prompt to W/graphs and print a summary; free
      --lint            pre-render lint only (references/failure-classes.md); free; also runs before every --submit and blocks on errors
      --print-prompt    print the H3 prompt only; free
      --print-still [--still-base M] [--still-end]   print the keyframe still prompt (gpt-image; references/stills.md); free
      --validate        check every node class/required input/choice against Comfy Cloud object info (`comfy nodes show`); free
      --submit --user-ok --cap USD [--est USD]   spend guard (ledger.py check) -> upload refs -> comfy run -> ledger record -> wait -> download
      --collect PROMPT_ID                        wait for and download an already-submitted job (resume; never resubmits)
Every take gets its keyframe stills (--guide, --end-guide; SKILL.md step 4). Speech takes after a speaker's first take use that take's audio as the
voice reference (--voice-take; default take 1 of this spec); action takes never take a voice. Path "fl" (fl2va + MiniMaxH3ImageToVideo) is used when
a take has no packshot and no voice reference, else "ref" (ref2va + R2V + AddGuide). Default setting (Nico, 2026-10-01): --quality hq (768x1376, dense attention, int8 text encoder) and, for
speech takes, the 8-step turbo LoRA of that path (--no-turbo for base steps); action takes run base steps (25+)."""
import argparse, hashlib, json, pathlib, subprocess, sys, time
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media, h3prompt

QUALITY = {  # resolution, base steps, attention, text encoder, price per 15 s at usd_steps (scales with steps; references/h3-comfy-cloud.md)
    "draft": dict(w=576, h=1024, steps=20, attention="sparse", clip="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", usd15=0.30, usd_steps=20),
    # the default: Nico approved this setting on the Cowork take af42f2a7 (2026-10-01): 768x1376, dense, int8 encoder, turbo 8 steps = 342 GPU-s, $0.44 per 15 s
    "hq": dict(w=768, h=1376, steps=30, attention="dense", clip="qwen3vl_32b_minimax_h3_int8_convrot.safetensors", usd15=0.44, usd_steps=8)}
UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"      # path "ref": packshot <Picture 1> and/or voice <Audio 1> references
UNET_FL = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"    # path "fl": first/last keyframes, which the text encoder also sees
TURBO = {"fl": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",   # Comfy Cloud catalog
         "ref": "lightx2v__Minimax-h3-Turbo__minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"}   # imported from HF lightx2v/Minimax-h3-Turbo (not in the catalog)
ACTION_STEPS = 25   # Comfy's H3 guide: ~25 steps buys better motion; hand-object takes never run turbo (h3prompt lint FAST_ACTION)

def route(take, guide, voice):
    """'fl' when the take has a keyframe and no packshot/voice reference (official FL2VA path), else 'ref'."""
    return "fl" if guide and not voice and not take.get("shows_product") else "ref"

def legacy(spec): return spec.get("prompt_style") == "legacy"
def take_frames(spec, take): return frames(take["seconds"]) if legacy(spec) else h3prompt.length(spec, take)[0]

def frames(sec):
    """Frame count on H3's 17k+5 grid at 24 fps (15 s = 362)."""
    n = max(5, round(sec * 24)); return n + (5 - n % 17) % 17

def timeline(spec, take):
    """[(t0, t1, line|None, action)]; asserts the take fits its duration."""
    t0, pause, out = float(spec.get("start", 0.2)), float(spec.get("pause", 0.3)), []
    for b in take["beats"]:
        if "silent" in b: t1 = t0 + float(b["silent"]); out.append((round(t0, 1), round(t1, 1), None, b["action"]))
        else:
            t1 = t0 + float(b.get("dur") or max(1.0, len(b["say"].split()) / 2.6)); out.append((round(t0, 1), round(t1, 1), b["say"], b["action"]))
        t0 = t1 + pause
    T = frames(take["seconds"]) / 24
    if out[-1][1] > T - 0.2: raise SystemExit(f"timeline ends at {out[-1][1]}s but the take is {T:.1f}s: shorten lines or raise seconds")
    return out

def prompt(spec, take, index, guide=None, voice=None, end=False, path=None):
    """H3 prompt for one take: h3prompt.compile (official MiniMax format, default) or the pre-2026-09-30 template (spec "prompt_style": "legacy")."""
    guide = index > 1 if guide is None else guide; voice = index > 1 if voice is None else voice
    if take.get("prompt"): return take["prompt"]
    if legacy(spec): return prompt_legacy(spec, take, index, guide, voice, end)
    return h3prompt.compile(spec, take, path or route(take, guide, voice), guide, voice, end)

def prompt_legacy(spec, take, index, guide=None, voice=None, end=False):
    """Pre-2026-09-30 template: kept for comparison only. Its speech wording in silent beats and its negations caused babble and floating props
    (references/failure-classes.md). Take 1: no start image/voice. Takes 2+: start-image and voice-reference sentences."""
    guide = index > 1 if guide is None else guide; voice = index > 1 if voice is None else voice
    T, tl, he = frames(take["seconds"]) / 24, timeline(spec, take), spec.get("pronoun", "she")
    Pn = he.capitalize()
    head = ""
    if take.get("shows_product"): head += f"<Picture 1> is the {spec['product_name']}: keep its label exactly. "
    if voice:
        head += f"<Audio 1> is her voice: {he} speaks with exactly this voice, same timbre, accent, pitch and pace. " if he == "she" else f"<Audio 1> is the speaker's voice: same timbre, accent, pitch and pace. "
    if guide: head += "The first frame is exactly the given start image; continue from it. "
    if end: head += "The last frame is exactly the given end image: the take finishes in that exact pose and framing. "
    beats, prev = [], 0.0
    for t0, t1, line, act in tl:
        if t0 - prev > 0.25: beats.append(f"[{prev:.1f}s-{t0:.1f}s] silent beat, mouth closed, {he} holds eye contact.")
        beats.append(f"[{t0:.1f}s-{t1:.1f}s] no dialogue: {act}." if line is None else f"[{t0:.1f}s-{t1:.1f}s] {he} says \"{line}\" while {he} {act}.")
        prev = t1
    neg = take.get("tone") == "negative"   # negative scene (insomnia, pain...): the face stays tired/serious the whole take, no smile; smile only on positive takes
    end_face = take.get("end_expression") or ("a tired, serious face" if neg else "a small natural smile")
    beats.append(f"[{prev:.1f}s-{T:.1f}s] {he} stops talking, {end_face}, holds eye contact; no more words.")
    return (f"{head}Live-action, one continuous handheld front-camera selfie take with no cuts, vertical 9:16, realistic UGC phone video, {spec.get('setting', 'a real home')}. "
            f"{spec.get('creator', 'A relatable creator')} talking straight into the phone like a casual UGC creator: natural, conversational, honest, not an announcer. "
            f"Hand gestures never show numbers or count on fingers. {Pn} speaks {spec.get('language', 'English')} only, only the exact lines below, at the times given; "
            f"lips move in exact sync with the words and {he} is silent outside them. Each line is said exactly once and in order: never early, never repeated, never extra words; "
            f"if a line finishes before its time is up {he} pauses with the mouth closed. Timeline, second by second: " + " ".join(beats) +
            " Throughout: the same person the whole take, no morphing, natural hands with five fingers, subtle natural hand-held sway, "
            + ("the product label stays sharp and readable, only one branded product. " if take.get("shows_product") else "no branded products in frame. ")
            + (f"His expression stays tired, worn and serious throughout, with no smile at any point. " if neg and he == "he" else f"Her expression stays tired, worn and serious throughout, with no smile at any point. " if neg else "")
            + "Everything important stays in the center of the frame. overall_soundscape: " + take.get("soundscape", "quiet room tone and the voice only") + ". non_diegetic_music: N/A")

def graph(spec, index, q, files, seed, steps, attention, W, H, guide=None, voice=None, end=False, lora=None, lora_strength=1.0, vae=None, path=None):
    """API-format graph. files: {'packshot': cloud name|None, 'guide': cloud name|None, 'voice': cloud name|None}. Verified against h3-comfy-cloud.md 'Graph'.
    path 'fl': fl2va + MiniMaxH3ImageToVideo(first_frame, last_frame), res_multistep/simple (official template); 'ref': ref2va + R2V + AddGuide, euler/beta."""
    take = spec["takes"][index - 1]; guide = index > 1 if guide is None else guide; voice = index > 1 if voice is None else voice
    path = path or ("ref" if legacy(spec) else route(take, guide, voice))
    wf = {
        "1": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": prompt(spec, take, index, guide, voice, end, path)}},
        "119": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "120": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "127": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_FL if path == "fl" else UNET, "weight_dtype": "default"}},
        "128": {"class_type": "CLIPLoader", "inputs": {"clip_name": q["clip"], "type": "minimax", "device": "default"}},
        "129": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "123": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "600": {"class_type": "ModelAttentionBackend", "inputs": {"model": ["127", 0], "attention": "comfy kitchen attention"}},
    }
    if lora:   # distillation/style LoRA on the UNET (before the attention patch), e.g. LightX2V turbo 8-step: pass --lora <name> --steps 8
        wf["610"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["127", 0], "lora_name": lora, "strength_model": float(lora_strength)}}
        wf["600"]["inputs"]["model"] = ["610", 0]
    if vae: wf["119"]["inputs"]["vae_name"] = vae
    model = ["600", 0]
    if attention == "sparse":
        wf["601"] = {"class_type": "BlockSparseAttention", "inputs": {"model": ["600", 0], "selection": "sol-attn", "selection.tau": 1.15, "start_percent": 0.2, "end_percent": 0.9,
                     "dense_blocks": "", "min_tokens": 12288, "extra_tokens": 256, "sink_conditioning": "exact_kv_and_rows", "verbose": False}}
        model = ["601", 0]
    if path == "fl":   # the text encoder sees the keyframes here (AddGuide only pins them): the official first/last-frame path
        i2v = {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["128", 0], "vae": ["119", 0], "prompt": ["1", 0], "width": W, "height": H, "length": take_frames(spec, take)}}
        if guide: wf["40"] = {"class_type": "LoadImage", "inputs": {"image": files["guide"]}}; i2v["inputs"]["first_frame"] = ["40", 0]
        if end: wf["41"] = {"class_type": "LoadImage", "inputs": {"image": files["end"]}}; i2v["inputs"]["last_frame"] = ["41", 0]
        wf["136"] = i2v; wf["123"]["inputs"]["sampler_name"] = "res_multistep"
        return tail(wf, model, ["136", 0], steps, "simple")
    r2v = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0], "prompt": ["1", 0],
           "width": W, "height": H, "length": take_frames(spec, take), "ref_image_size": "match"}}
    if take.get("shows_product"):
        wf["400"] = {"class_type": "LoadImage", "inputs": {"image": files["packshot"]}}; r2v["inputs"]["ref_images.ref_image_0"] = ["400", 0]
    if voice:
        wf["510"] = {"class_type": "LoadAudio", "inputs": {"audio": files["voice"]}}; r2v["inputs"]["ref_audios.ref_audio_0"] = ["510", 0]
    wf["136"] = r2v; cond = ["136", 0]
    if guide:
        wf["40"] = {"class_type": "LoadImage", "inputs": {"image": files["guide"]}}
        wf["500"] = {"class_type": "MiniMaxH3AddGuide", "inputs": {"positive": ["136", 0], "latent": ["136", 1], "vae": ["119", 0], "image": ["40", 0], "frame_idx": 0}}; cond = ["500", 0]
        if end:   # join pattern (join_test.py): same image also anchors the LAST frame, so a hook can hand over to the body without a jump
            wf["41"] = {"class_type": "LoadImage", "inputs": {"image": files["end"]}}
            wf["501"] = {"class_type": "MiniMaxH3AddGuide", "inputs": {"positive": ["500", 0], "latent": ["136", 1], "vae": ["119", 0], "image": ["41", 0], "frame_idx": -1}}; cond = ["501", 0]
    return tail(wf, model, cond, steps, "beta")

def tail(wf, model, cond, steps, scheduler):
    """Sampler, decode and save nodes shared by both paths."""
    wf["124"] = {"class_type": "BasicScheduler", "inputs": {"model": model, "scheduler": scheduler, "steps": steps, "denoise": 1.0}}
    wf["126"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": cond}}
    wf["125"] = {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": ["124", 0], "latent_image": ["136", 1]}}
    wf["121"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["125", 0], "vae": ["120", 0]}}
    wf["122"] = {"class_type": "VAEDecode", "inputs": {"samples": ["125", 0], "vae": ["119", 0]}}
    wf["130"] = {"class_type": "CreateVideo", "inputs": {"images": ["122", 0], "fps": 24.0, "audio": ["121", 0]}}
    wf["92"] = {"class_type": "SaveVideo", "inputs": {"video": ["130", 0], "filename_prefix": "", "format": "mp4", "format.codec": "h264"}}
    return wf

def comfy(*a):
    r = subprocess.run(["comfy", "--json", *a], capture_output=True, text=True)
    try: return json.loads(r.stdout)
    except Exception: return {"ok": False, "error": (r.stderr or r.stdout)[-300:]}

def validate(wf, custom=()):
    """Free: every class_type exists on Comfy Cloud, required non-link inputs are present, combo values are valid choices. Returns a list of problems."""
    problems, schemas = [], {}
    for nid, n in wf.items():
        c = n["class_type"]
        if c not in schemas:
            d = comfy("nodes", "show", c, "--where", "cloud"); schemas[c] = d["data"] if d.get("ok") else None
        s = schemas[c]
        if not s: problems.append(f"{nid} {c}: node class not found on Comfy Cloud"); continue
        known = {i["name"]: i for i in s["inputs"]}
        for name, i in known.items():
            if i.get("required") and name not in n["inputs"] and i.get("section") == "required": problems.append(f"{nid} {c}: missing required input {name}")
        for name, v in n["inputs"].items():
            i = known.get(name)
            if i and i.get("choices") and not isinstance(v, list) and v not in i["choices"] and v not in custom and not str(v).endswith((".png", ".jpg", ".wav", ".mp3", ".mp4")):
                problems.append(f"{nid} {c}.{name}={v!r} not in choices")
    return problems

def upload(work, path):
    """Upload once (cached by content hash in W/uploads.json); returns the Cloud input name."""
    path = pathlib.Path(path); h = hashlib.sha256(path.read_bytes()).hexdigest()[:16]; cp = work / "uploads.json"
    cache = json.loads(cp.read_text()) if cp.exists() else {}
    if h in cache: return cache[h]
    d = comfy("upload", str(path), "--where", "cloud")
    if not d.get("ok"): raise SystemExit(f"upload failed for {path.name}: {str(d.get('error'))[:200]}")
    cache[h] = d["data"]["uploads"][0]["cloud_name"]; cp.write_text(json.dumps(cache, indent=1)); return cache[h]

def wait(pid, max_wait=3300):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        d = (comfy("jobs", "status", pid, "--where", "cloud").get("data") or {})
        if d.get("status") in ("completed", "success"): return True, None
        if d.get("status") in ("error", "failed", "cancelled"): return False, d.get("error_message") or d.get("status")
        time.sleep(15)
    return False, "timeout (job may still be running: re-run with --collect <id>, never resubmit)"

def collect(pid, dest):
    ok, err = wait(pid)
    if not ok: print(f"JOB FAIL {pid}: {err}", file=sys.stderr); return False
    tmp = pathlib.Path(dest).parent / ".dl" / pid[:8]
    d = comfy("download", pid, "--where", "cloud", "--out-dir", str(tmp))
    if not d.get("ok") or not d["data"].get("files"): print("download failed", str(d.get("error"))[:200], file=sys.stderr); return False
    pathlib.Path(d["data"]["files"][0]["path"]).replace(dest); print("got", dest); return True

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True); ap.add_argument("--take", type=int, required=True); ap.add_argument("--quality", choices=QUALITY, default="hq")
    ap.add_argument("--work", default="."); ap.add_argument("--prev-take"); ap.add_argument("--voice-take"); ap.add_argument("--seed", type=int)
    ap.add_argument("--steps", type=int); ap.add_argument("--attention", choices=["sparse", "dense"]); ap.add_argument("--width", type=int); ap.add_argument("--height", type=int)
    ap.add_argument("--guide", help="frame-0 guide image (default for take>1: last frame of the previous take). Give it to take 1 of a hook: the body's first frame")
    ap.add_argument("--end-guide", help="also anchor the LAST frame to this image (hook ends on the body's first frame); requires a frame-0 guide")
    ap.add_argument("--try", dest="tryn", default="a", help="suffix of the output name; never reuse one")
    ap.add_argument("--print-prompt", action="store_true"); ap.add_argument("--validate", action="store_true")
    ap.add_argument("--print-still", action="store_true", help="print the gpt-image prompt of this take's first-frame still (references/stills.md); free")
    ap.add_argument("--still-base", type=int, metavar="M", help="with --print-still: the still is made from take M's still (an edit, or the same person in a new place)")
    ap.add_argument("--still-end", action="store_true", help="with --print-still: the take's last frame (its end_state) instead of its first")
    ap.add_argument("--submit", action="store_true"); ap.add_argument("--user-ok", action="store_true", help="assert the user gave an explicit OK to the quote")
    ap.add_argument("--cap", type=float); ap.add_argument("--est", type=float, help="estimated USD of this job (default: from quality and seconds)")
    ap.add_argument("--collect", metavar="PROMPT_ID")
    ap.add_argument("--lora", help="LoRA file name as it appears in Comfy Cloud (catalog or your imported assets); applied with LoraLoaderModelOnly")
    ap.add_argument("--lora-strength", type=float, default=1.0); ap.add_argument("--vae", help="video VAE file name (default minimax_h3_video_vae_fp16)")
    ap.add_argument("--turbo", action="store_true", help="8-step distillation LoRA matching the path; the default for speech takes (lint refuses it on hand-object action takes)")
    ap.add_argument("--no-turbo", action="store_true", help="speech take at the base steps instead of the default 8-step turbo")
    ap.add_argument("--path", choices=["fl", "ref"], help="override the model path (default: fl when the take has a keyframe and no packshot/voice reference)")
    ap.add_argument("--lint", action="store_true", help="run the pre-render lint only (free); exit 1 on errors")
    ap.add_argument("--allow", default="", help="comma list of lint rules to waive for this take, e.g. NO_KEYFRAME (say why in the run notes)")
    a = ap.parse_args()
    spec = json.loads(pathlib.Path(a.spec).read_text()); work = pathlib.Path(a.work); n = a.take
    if not 1 <= n <= len(spec["takes"]): raise SystemExit(f"--take must be 1..{len(spec['takes'])}")
    q = QUALITY[a.quality]; W, H = a.width or q["w"], a.height or q["h"]; steps = a.steps or q["steps"]; att = a.attention or q["attention"]
    seed = a.seed if a.seed is not None else int(spec.get("seed", 1000)) + n - 1
    label = f"{spec['name']}-T{n}-{a.quality}-{a.tryn}"; takes = work / "takes"; dest = takes / f"{label}.mp4"
    if a.collect:
        takes.mkdir(parents=True, exist_ok=True); sys.exit(0 if collect(a.collect, dest) else 1)
    take = spec["takes"][n - 1]; old = legacy(spec); k = "speech" if old else h3prompt.kind(take)
    if a.print_still: print(h3prompt.still_prompt(spec, take, spec["takes"][a.still_base - 1] if a.still_base else None, a.still_end)); return
    use_guide = n > 1 or bool(a.guide); use_voice = (n > 1 or bool(a.voice_take)) and k == "speech"; end = bool(a.end_guide)   # action takes have no speaker, so no voice reference
    if end and not use_guide: raise SystemExit("--end-guide needs a frame-0 guide (take>1 or --guide)")
    path = "ref" if old else (a.path or route(take, use_guide, use_voice))
    turbo = a.turbo or (k == "speech" and not a.no_turbo and not a.lora)   # default: speech takes turbo, action takes base 25+ steps
    if turbo: steps = a.steps or 8
    elif k == "action" and not a.steps: steps = max(steps, ACTION_STEPS)
    lora = a.lora or (TURBO[path] if turbo else None)
    if not old:
        errs, warns = h3prompt.lint(spec, take, use_guide, steps, fast=bool(lora and "turbo" in lora.lower()), allow={r.strip() for r in a.allow.split(",") if r.strip()})
        for w in warns: print("lint warning:", w, file=sys.stderr)
        for e in errs: print("LINT ERROR:", e, file=sys.stderr)
        if a.lint: print(json.dumps({"take": n, "kind": k, "path": path, "seconds": round(take_frames(spec, take) / 24, 2), "steps": steps, "errors": errs, "warnings": warns}, indent=1)); sys.exit(1 if errs else 0)
        if errs and a.submit: raise SystemExit("LINT: fix the spec (references/failure-classes.md) or waive a rule with --allow; nothing submitted")
    if a.print_prompt: print(prompt(spec, take, n, use_guide, use_voice, end, path)); return
    files = {"packshot": "PACKSHOT_PLACEHOLDER.png", "guide": "GUIDE_PLACEHOLDER.png", "voice": "VOICE_PLACEHOLDER.wav", "end": "END_PLACEHOLDER.png"}
    prev = pathlib.Path(a.prev_take) if a.prev_take else takes / f"{spec['name']}-T{n-1}-{a.quality}-{a.tryn}.mp4"
    voice = pathlib.Path(a.voice_take) if a.voice_take else takes / f"{spec['name']}-T1-{a.quality}-{a.tryn}.mp4"
    if a.submit:
        if not a.user_ok: raise SystemExit("refusing to spend: pass --user-ok only after the user said yes to the quote")
        if a.cap is None: raise SystemExit("--cap USD required")
        est = a.est if a.est is not None else round(q["usd15"] * take_frames(spec, take) / 24 / 15 * steps / q["usd_steps"], 2)
        import ledger
        spent = ledger.build(work)["total_usd"]; print(f"spend guard: spent ${spent:.2f} + est ${est:.2f} vs cap ${a.cap:.2f}")
        if spent + est > a.cap: raise SystemExit("BUDGET STOP: over the cap, no job submitted")
        takes.mkdir(parents=True, exist_ok=True); (work / "graphs").mkdir(parents=True, exist_ok=True)
        if take.get("shows_product"): files["packshot"] = upload(work, spec["packshot"])
        if use_guide:
            if a.guide: files["guide"] = upload(work, a.guide)
            else:
                if not prev.exists(): raise SystemExit(f"missing {prev}: render and keep the previous take first (or pass --prev-take/--guide)")
                g = work / "takes" / f"{spec['name']}-T{n-1}-last.png"; media.ff("-sseof", "-0.1", "-i", str(prev), "-frames:v", "1", "-update", "1", str(g)); files["guide"] = upload(work, g)
            if end: files["end"] = upload(work, a.end_guide)
        if use_voice:
            if not voice.exists(): raise SystemExit(f"missing {voice}: render take 1 first (or pass --voice-take)")
            wav = work / "takes" / f"{spec['name']}-voice.wav"; media.ff("-i", str(voice), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav)); files["voice"] = upload(work, wav)
    wf = graph(spec, n, q, files, seed, steps, att, W, H, use_guide, use_voice, end, lora, a.lora_strength, a.vae, path); wf["92"]["inputs"]["filename_prefix"] = f"video/{label}"
    (work / "graphs").mkdir(parents=True, exist_ok=True); gp = work / "graphs" / f"{label}.api.json"; gp.write_text(json.dumps(wf, indent=1))
    meta = {"take": n, "kind": k, "path": path, "quality": a.quality, "size": f"{W}x{H}", "steps": steps, "attention": att, "seed": seed, "frames": take_frames(spec, take), "lora": lora, "vae": a.vae, "prompt_chars": len(wf["1"]["inputs"]["value"])}
    if a.validate:
        probs = validate(wf, [x for x in (lora, a.vae) if x]); print(json.dumps({**meta, "graph": str(gp), "nodes": len(wf), "validate": "OK" if not probs else probs}, indent=1)); sys.exit(1 if probs else 0)
    if not a.submit: print(json.dumps({**meta, "graph": str(gp), "nodes": len(wf), "note": "dry run, nothing submitted"}, indent=1)); return
    if lora or a.vae:   # imported models are not in the catalog the CLI validates against: POST /prompt with the CLI's own login (never prints it)
        pid = post_prompt(gp)
    else:
        dry = comfy_run_print(gp)
        if not dry: raise SystemExit("comfy --print-prompt rejected the graph; nothing submitted")
        r = comfy("run", "--workflow", str(gp), "--where", "cloud", "--allow-spend", "--no-watch")
        if not r.get("ok"): raise SystemExit("SUBMIT FAIL (nothing recorded, no job id): " + json.dumps(r.get("error"))[:600])
        pid = r["data"]["prompt_id"]
    ledger.record(work, label, pid, "h3-take"); print("submitted", label, pid)
    sys.exit(0 if collect(pid, dest) else 1)

def post_prompt(gp):
    """POST the API graph to Cloud /prompt via comfy-cli's authed client (run under comfy-cli's python). Returns the prompt_id or exits with the server's validation error."""
    py = pathlib.Path.home() / ".local/share/uv/tools/comfy-cli/bin/python"
    code = ("import json,sys\nfrom comfy_cli.target import resolve_target\nfrom comfy_cli.http import request_json\n"
            "t=resolve_target(where='cloud'); g=json.load(open(sys.argv[1]))\n"
            "try:\n    st,b=request_json(t.base_url+t.path_prefix+'/prompt',t,method='POST',body={'prompt':g},max_bytes=5_000_000)\n    print(json.dumps(b))\n"
            "except Exception as e:\n    print(json.dumps({'error':str(e)[:400]}))\n")
    r = subprocess.run([str(py), "-c", code, str(gp)], capture_output=True, text=True)
    try: d = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception: raise SystemExit("SUBMIT FAIL (no job id): " + (r.stderr or r.stdout)[-400:])
    if not d.get("prompt_id"): raise SystemExit("SUBMIT FAIL (nothing recorded): " + json.dumps(d)[:600])
    return d["prompt_id"]

def comfy_run_print(gp):
    r = subprocess.run(["comfy", "--json", "run", "--workflow", str(gp), "--where", "cloud", "--print-prompt"], capture_output=True, text=True)
    return r.returncode == 0

if __name__ == "__main__": main()
