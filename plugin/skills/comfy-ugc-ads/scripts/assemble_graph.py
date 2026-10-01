#!/usr/bin/env python3
"""Assemble the ads on Comfy Cloud instead of local ffmpeg (step 9): one Cloud job per ad renders its 9:16 and 4:5 files.
  assemble_graph.py --plan P.json --work W [--ads 1,4] [--print-graph] [--validate] --submit --user-ok --cap USD
Every node is core Comfy (plus the VHS audio loader the skill already uses) and streams one frame at a time, so a 60 s ad never
decodes into memory (the GetVideoComponents worker kill, references/h3-comfy-cloud.md):
  picture: LoadVideo -> Video Slice per cut (lazy) -> VideoCrop for 4:5 (lazy) -> ConcatenateVideo -> SaveVideo h264
  packshot cutaways and the end card: a short silent packshot clip per format (fitted on the pad color, made once here with ffmpeg: a still,
         under a second; CreateVideo's clip is tagged sRGB and ConcatenateVideo refuses to mix it with the untagged takes) -> Video Slice
  sound: VHS_LoadVideoFFmpeg audio -> AudioAdjustVolume (each take's measured gain to `lufs`, true peak <= -1 dB) -> TrimAudioDuration per cut
         -> AudioConcat -> the ad's complete_audio (the voice keeps playing under a cutaway; the end card is silence)
Every cut is a whole frame (picture windows on half-frame marks, sound on the frame times) so picture and sound stay in sync over many splices. Output is the takes' native width (no
video-scale node on Cloud): 768x1344 and 768x960 for HeyGen takes.
Plan JSON (paths relative to the plan file):
  {"name": "acme", "fps": 24, "takes": {"1": "takes/T1.mp4", ..}, "gain_from": {"2": "1"},     action take -> the speech take whose gain it uses
   "parts": {"hook1": [[1, 0.05, 10.75], [2, 0.25, 4.1], [1, 11.35, 15.17]], .., "body": [..], "close1": [..]},   [take, t0, t1] cuts in order
   "ads": [["hook1", "body", "close1"], ..],        default: every hook x the body x every close (3 x 1 x 3 = 9)
   "packshot": "brief/packshot.png", "pad": "white", "cutaways": [[7, 3.1, 4.7]],   packshot over take 7 from 3.1 to 4.7 s
   "card": 2.0, "lufs": -14, "y45": 0.35, "crf": 18}
Cut points belong in measured silences (SKILL.md step 9). Prints the plan per ad; --print-graph and --validate are free."""
import argparse, json, math, pathlib, re, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media

USD_PER_AD = 0.06   # ASSUMPTION until measured: GPU-seconds of a ~65 s ad, both formats; ledger.py build has the billed number

def loudness(path):
    """(integrated LUFS, true peak dBTP) of a file's audio (ffmpeg ebur128, audio only, under a second)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True).stderr
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", r); p = re.findall(r"Peak:\s+(-?[\d.inf]+) dBFS", r)
    return float(i[-1]), float(p[-1]) if p and p[-1] != "-inf" else -99.0

def gains(plan, files):
    """Whole-dB gain per take: speech takes to plan lufs with the true peak at -1 dB or lower; an action take uses its speech take's gain."""
    g, target = {}, float(plan.get("lufs", -14))
    for n, f in files.items():
        if n in plan.get("gain_from", {}): continue
        lu, tp = loudness(f); g[n] = int(math.floor(min(target - lu, -1.0 - tp)))
    for n, src in plan.get("gain_from", {}).items(): g[n] = g[str(src)]
    return {**g, **{str(k): v for k, v in plan.get("gains", {}).items()}}

def ads(plan):
    if plan.get("ads"): return [list(a) for a in plan["ads"]]
    P = plan["parts"]; hooks = sorted(p for p in P if p.startswith("hook")); closes = sorted(p for p in P if p.startswith("close"))
    return [[h, "body", c] for h in hooks for c in closes]

def graph(plan, parts, names, gain, W, H, H45, rate, prefix):
    """API graph of one ad: 9:16 and 4:5 SaveVideo outputs named <prefix>_9x16 / <prefix>_4x5."""
    fps = plan.get("fps", 24); fr = lambda t: int(round(float(t) * fps))   # every cut is a whole frame
    win = lambda f0, n: {"start_time": round(max(0.0, (f0 - 0.5) / fps), 4), "duration": round((f0 + n - 0.5) / fps - max(0.0, (f0 - 0.5) / fps), 4)}
    # Video Slice keeps frames with start <= pts < end: windows on half-frame marks select exactly n frames (a cut on a frame's own
    # timestamp drops or adds one, and the picture drifts off the sound by a frame per splice)
    y = int(round((H - H45) * float(plan.get("y45", 0.35)))); y -= y % 2
    g, k = {}, [0]
    def add(ct, inp): k[0] += 1; g[str(k[0])] = {"class_type": ct, "inputs": inp}; return [str(k[0]), 0]
    vid, aud, pk = {}, {}, {}
    def take(n):
        if n not in vid:
            vid[n] = add("LoadVideo", {"file": names[n]})
            a = add("VHS_LoadVideoFFmpeg", {"video": names[n], "force_rate": 1, "custom_width": 64, "custom_height": 112, "frame_load_cap": 0, "start_time": 0, "format": "None"})
            aud[n] = add("AudioAdjustVolume", {"audio": [a[0], 2], "volume": int(gain[n])})
        return vid[n], aud[n]
    def packshot(sec, h):   # sec seconds of the packshot clip of height h (fitted, never cropped)
        if h not in pk: pk[h] = add("LoadVideo", {"file": names[f"packshot{h}"]})
        return add("Video Slice", {"video": pk[h], **win(0, fr(sec)), "strict_duration": False})
    cut = {str(c[0]): [] for c in plan.get("cutaways", [])}
    for c in plan.get("cutaways", []): cut[str(c[0])].append((fr(c[1]), fr(c[2])))
    pic916, pic45, sound, rows = [], [], None, []
    for part in parts:
        for n, t0, t1 in plan["parts"][part]:
            n = str(n); f0, f1 = fr(t0), fr(t1); v, a = take(n)
            s = add("TrimAudioDuration", {"audio": a, "start_index": f0 / fps, "duration": (f1 - f0) / fps})
            sound = s if sound is None else add("AudioConcat", {"audio1": sound, "audio2": s, "direction": "after"})
            edges = sorted(c for c in cut.get(n, []) if f0 <= c[0] < c[1] <= f1); a0 = f0
            for c0, c1 in edges + [(f1, None)]:   # picture: the take up to each cutaway, the packshot over it, then the take again
                if c0 > a0:
                    sl = add("Video Slice", {"video": v, **win(a0, c0 - a0), "strict_duration": False})
                    pic916.append(sl); pic45.append(add("VideoCrop", {"video": sl, "crop": {"crop": {"x": 0, "y": y, "width": W, "height": H45}}}))
                if c1 is not None: pic916.append(packshot((c1 - c0) / fps, H)); pic45.append(packshot((c1 - c0) / fps, H45)); a0 = c1
            rows.append(f"{part} T{n} {f0 / fps:.3f}-{f1 / fps:.3f}" + "".join(f" [packshot {c0 / fps:.3f}-{c1 / fps:.3f}]" for c0, c1 in edges))
    card = float(plan.get("card", 0))
    if card > 0:
        pic916.append(packshot(card, H)); pic45.append(packshot(card, H45))
        sound = add("AudioConcat", {"audio1": sound, "audio2": add("EmptyAudio", {"duration": card, "sample_rate": rate, "channels": 2}), "direction": "after"})
    enc = lambda p: {"filename_prefix": p, "format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": float(plan.get("crf", 18))}
    for fmt, pics in (("9x16", pic916), ("4x5", pic45)):
        cat = add("ConcatenateVideo", {**{f"videos.video{i}": p for i, p in enumerate(pics)}, "codec": "h264", "complete_audio": sound})
        add("SaveVideo", {"video": cat, **enc(f"video/{prefix}_{fmt}")})
    return g, rows

def packshot_clip(png, out, W, h, sec, fps, pad):
    """sec seconds of the packshot fitted in W x h on the pad color, h264, no audio, no color tags (like the takes)."""
    media.ff("-loop", "1", "-framerate", str(fps), "-i", str(png), "-t", f"{sec}", "-vf",
             f"scale={W}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{h}:(ow-iw)/2:(oh-ih)/2:{pad},setsar=1,format=yuv420p",
             "-c:v", "libx264", "-crf", "14", "-r", str(fps), "-an", str(out))
    return out

def collect(pid, work, prefix):
    """Wait for the job, then fetch only its two SaveVideo files (the job also lists every input and preview) to <work>/final/<prefix>_<fmt>.mp4."""
    import take_graph as T, ledger, urllib.error, urllib.parse, urllib.request
    ok, err = T.wait(pid)
    if not ok: print(f"JOB FAIL {prefix} {pid}: {err}", file=sys.stderr); return False
    code = ("import json, sys, urllib.error, urllib.parse, urllib.request\n"
            "from comfy_cli.target import resolve_target\nfrom comfy_cli.http import request_json, authed_urlopen\n"
            "t = resolve_target(where='cloud'); pid, out = sys.argv[1], sys.argv[2:]\n"
            "st, j = request_json(t.base_url + t.path_prefix + f'/jobs/{pid}', t, max_bytes=5_000_000)\n"
            "items = [i for o in j['outputs'].values() for i in o.get('images', []) + o.get('videos', []) if i.get('type') == 'output']\n"
            "for fmt, dest in zip(('9x16', '4x5'), out):\n"
            "    i = next(i for i in items if f'_{fmt}_' in (i.get('display_name') or ''))\n"
            "    q = urllib.parse.urlencode({'filename': i['filename'], 'subfolder': i.get('subfolder', ''), 'type': 'output'})\n"
            "    try: data = authed_urlopen(t.base_url + t.path_prefix + '/view?' + q, t, timeout=300).read()\n"
            "    except urllib.error.HTTPError as e:   # Cloud answers with a 302 to a signed storage URL\n"
            "        if e.code not in (301, 302, 307): raise\n"
            "        data = urllib.request.urlopen(e.headers['Location'], timeout=300).read()\n"
            "    open(dest, 'wb').write(data); print('got', dest)\n")
    dest = [str(work / "final" / f"{prefix}_{fmt}.mp4") for fmt in ("9x16", "4x5")]
    r = subprocess.run([ledger.comfy_python(), "-c", code, pid, *dest], capture_output=True, text=True)
    print(r.stdout.strip(), flush=True)
    if r.returncode: print(f"{prefix}: download failed: {r.stderr.strip()[-300:]}", file=sys.stderr)
    return r.returncode == 0

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True); ap.add_argument("--work", default="."); ap.add_argument("--ads", default="", help="comma list of ad numbers (default all)")
    ap.add_argument("--print-graph", action="store_true"); ap.add_argument("--validate", action="store_true", help="check each graph with comfy run --print-prompt (free)")
    ap.add_argument("--submit", action="store_true"); ap.add_argument("--user-ok", action="store_true"); ap.add_argument("--cap", type=float)
    a = ap.parse_args(); pp = pathlib.Path(a.plan).resolve(); plan = json.loads(pp.read_text()); work = pathlib.Path(a.work)
    rel = lambda p: (pp.parent / p).resolve()
    files = {str(n): rel(p) for n, p in plan["takes"].items()}
    W, H = (int(x) for x in media.probe(next(iter(files.values())), "stream=width,height", "v:0").split(","))
    H45 = int(round(W * 5 / 4)); H45 -= H45 % 2
    rate = int(media.probe(next(iter(files.values())), "stream=sample_rate", "a:0"))
    gain = gains(plan, files); print("gain dB per take:", gain)
    pick = {int(x) for x in a.ads.split(",") if x.strip()}
    jobs = [(i, A) for i, A in enumerate(ads(plan), 1) if not pick or i in pick]
    import take_graph as T, ledger
    longest = max([float(plan.get("card", 0))] + [float(c[2]) - float(c[1]) for c in plan.get("cutaways", [])]) + 0.5
    (work / "graphs").mkdir(parents=True, exist_ok=True)
    clips = {f"packshot{h}": packshot_clip(rel(plan["packshot"]), work / "graphs" / f"packshot-{W}x{h}.mp4", W, h, round(longest, 2), plan.get("fps", 24), plan.get("pad", "white"))
             for h in (H, H45)}
    if a.submit:
        if not a.user_ok: raise SystemExit("refusing to spend: pass --user-ok only after the user said yes to the quote")
        if a.cap is None: raise SystemExit("--cap USD required")
        est = round(USD_PER_AD * len(jobs), 2); spent = ledger.build(work)["total_usd"]
        print(f"spend guard: spent ${spent:.2f} + est ${est:.2f} vs cap ${a.cap:.2f}")
        if spent + est > a.cap: raise SystemExit("BUDGET STOP: over the cap, no job submitted")
        names = {n: T.upload(work, f) for n, f in {**files, **clips}.items()}
    else: names = {n: f.name for n, f in {**files, **clips}.items()}
    (work / "final").mkdir(exist_ok=True); sub = []
    for i, A in jobs:
        prefix = f"{plan['name']}-ad{i}-" + "-".join(re.sub(r"(hook|close)(\d+)", lambda m: m.group(1)[0] + m.group(2), p) for p in A if p != "body")
        g, rows = graph(plan, A, names, gain, W, H, H45, rate, prefix)
        gp = work / "graphs" / f"{prefix}-assemble.api.json"; gp.write_text(json.dumps(g, indent=1))
        print(f"ad{i} {prefix}: {len(g)} nodes; " + " | ".join(rows))
        if a.print_graph: print(json.dumps(g, indent=1))
        if a.validate and not T.comfy_run_print(gp): raise SystemExit(f"{gp.name}: comfy run --print-prompt rejected the graph")
        if a.submit:
            r = T.comfy("run", "--workflow", str(gp), "--where", "cloud", "--allow-spend", "--no-watch")
            if not r.get("ok"): raise SystemExit("SUBMIT FAIL: " + json.dumps(r.get("error"))[:600])
            pid = r["data"]["prompt_id"]; ledger.record(work, prefix, pid, "assemble"); sub.append((pid, prefix)); print("submitted", prefix, pid, flush=True)
    if sub:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(len(sub)) as ex: ok = list(ex.map(lambda s: collect(s[0], work, s[1]), sub))
        sys.exit(0 if all(ok) else 1)

if __name__ == "__main__":
    main()
