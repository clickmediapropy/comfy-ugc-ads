#!/usr/bin/env python3
"""ffmpeg helpers for the ad skill (local, free). Ported/parameterized from h3_chain.py, ad_graph edit ops and the H3 notes.
  media.py lastframe <in.mp4> <out.png>                 last frame = frame-0 guide of the next chained take
  media.py cropcheck <in.mp4>                           exit 1 + report if the frame shrinks into a pillarbox (re-render with a new seed)
  media.py strip <in.mp4> <out.png>                     2 fps contact sheet to eyeball actions vs the timeline
  media.py cut <in.mp4> <out.mp4> <t0-t1> [<t0-t1>..]   remove flagged ranges (seconds), audio and video in sync
  media.py concat <out.mp4> <in1.mp4> <in2.mp4> ...     join takes, loudnorm -14 LUFS
  media.py export <in.mp4> <out_dir> <basename>         9:16 1080x1920 and 4:5 1080x1350 (scale to cover + center crop, never a stretch: HeyGen is
                                                        768x1344 and H3 768x1376, neither exactly 9:16; --y=0..1 vertical bias of the 4:5 crop, default 0.35)
  media.py overlay <in.mp4> <out.mp4> <packshot.png> [<t0-t1>..] [--card=SECONDS] [--card-text=overlay.png] [--pad=white]
                                                        packshot cutaway over each range (fit in frame, bars in --pad color, default black: use the
                                                        packshot's own background, e.g. white; the take's voice keeps playing),
                                                        then an optional end card: packshot for SECONDS with silent audio and an optional transparent
                                                        PNG (disclosure/disclaimer text) laid over it. This ffmpeg build has no drawtext: supply text as a PNG.
  media.py insert <in.mp4> <out.mp4> <clip.mp4>@<t0>[+<dur>] ..   b-roll over the picture from t0 (fills the frame); the ad's audio stays untouched.
                                                        Order in assembly: concat -> insert -> export -> overlay (once per format: the packshot is fitted, never cropped)
  media.py fitframe <in.png> <out.png> [--w=576 --h=1024 --x=0.5]   crop a still to 9:16 (center, --x=0..1 horizontal bias) and scale: the first/last-frame guide
  media.py roomtone <in.mp4> <out.mp4> <t0>[-<t1>] [--from=a-b]   replace the audio from t0 (to t1 or the end) with the take's own room tone,
                                                        looped from its longest quiet stretch (or --from): kills a drop/thud the model added in a silent window
  media.py duration <in.mp4>"""
import re, subprocess, sys

def run(*a, capture=False):
    r = subprocess.run(list(a), capture_output=capture, text=True)
    if r.returncode and not capture: raise SystemExit(f"{a[0]} failed ({r.returncode})")
    return r

def ff(*a): return run("ffmpeg", "-loglevel", "error", "-y", *a)

def probe(path, entries, stream=None):
    args = ["ffprobe", "-v", "error"] + (["-select_streams", stream] if stream else []) + ["-show_entries", entries, "-of", "csv=p=0", str(path)]
    return run(*args, capture=True).stdout.strip()

def duration(p): return float(probe(p, "format=duration"))

def cropcheck(p):
    r = run("ffmpeg", "-i", str(p), "-vf", "fps=4,cropdetect=limit=24:round=2:reset=1", "-f", "null", "-", capture=True)
    w, h = [int(x) for x in probe(p, "stream=width,height", "v:0").split(",")]
    bad = sorted({m for m in re.findall(r"crop=(\d+:\d+:\d+:\d+)", r.stderr) if not m.startswith(f"{w}:{h}:0:0")})
    return w, h, bad

def cut(src, dst, ranges):
    """Keep everything outside the ranges. Re-encodes at CRF 14 so cuts land on the requested time, not the nearest keyframe."""
    d = duration(src); rs = sorted(tuple(map(float, r.split("-"))) for r in ranges); keep, t = [], 0.0
    for a, b in rs:
        if a > t: keep.append((t, a))
        t = max(t, b)
    if t < d: keep.append((t, d))
    if not keep: raise SystemExit("ranges cover the whole clip")
    fc = "".join(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];" for i, (a, b) in enumerate(keep))
    fc += "".join(f"[v{i}][a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=1[v][a]"
    ff("-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(dst))

def concat(dst, parts):
    n = len(parts)
    ff(*sum([["-i", p] for p in parts], []), "-filter_complex", "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a];[a]loudnorm=I=-14:TP=-1.5:LRA=11[an]",
       "-map", "[v]", "-map", "[an]", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", dst)

def overlay(src, dst, pack, ranges, card=0.0, card_text=None, pad="black"):
    """Cutaway(s) of the real packshot over `ranges`, then an end card of `card` seconds. Output keeps the source size, 24 fps, AAC audio."""
    w, h = [int(x) for x in probe(src, "stream=width,height", "v:0").split(",")]
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:{pad},setsar=1,format=yuv420p"
    rs = sorted(tuple(map(float, x.split("-"))) for x in ranges)
    fc = ([f"[1:v]{fit}[pk]"] if rs else []) + ["[0:v]setsar=1[b0]"]; last = "b0"   # an end card alone has no cutaway: an unused [pk] fails the graph
    for i, r in enumerate(rs):
        fc.append(f"[{last}][pk]overlay=enable='between(t,{r[0]},{r[1]})'[b{i+1}]"); last = f"b{i+1}"
    ins, enc = ["-i", str(src), "-loop", "1", "-t", str(duration(src)), "-i", str(pack)], ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-r", "24"]
    if card <= 0:
        ff(*ins, "-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "0:a", "-shortest", *enc, str(dst)); return
    ins += ["-loop", "1", "-t", str(card), "-i", str(pack)] + (["-loop", "1", "-t", str(card), "-i", str(card_text)] if card_text else [])
    fc.append(f"[2:v]{fit.replace('format=yuv420p', 'format=rgba')}[c0]")
    fc.append(f"[c0][3:v]overlay=0:0,format=yuv420p,fps=24[cv]" if card_text else "[c0]format=yuv420p,fps=24[cv]")
    fc.append(f"[{last}]fps=24[mv]")
    fc.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{card}[ca]")
    fc.append("[0:a]aresample=48000,aformat=channel_layouts=stereo[ma]")
    fc.append("[mv][ma][cv][ca]concat=n=2:v=1:a=1[v][a]")
    ff(*ins, "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", *enc, str(dst))

def insert(src, dst, items):
    """B-roll laid over the picture: items "clip.mp4@t0[+dur]" (dur default = the clip's length). Each clip fills the frame (scale up + center crop)
    from t0 for dur seconds; the ad's own audio is copied untouched underneath (references/broll.md). Overlapping ranges are refused."""
    w, h = [int(x) for x in probe(src, "stream=width,height", "v:0").split(",")]; T = duration(src); its = []
    for x in items:
        clip, at = x.rsplit("@", 1); t0, _, d = at.partition("+"); t0 = float(t0); d = float(d) if d else duration(clip)
        if d > duration(clip) + 0.05: raise SystemExit(f"{clip} is {duration(clip):.2f}s, shorter than the {d}s asked")
        its.append((t0, round(t0 + d, 3), clip))
    its.sort()
    for (a0, a1, c), (b0, _, d) in zip(its, its[1:]):
        if b0 < a1: raise SystemExit(f"b-roll ranges overlap: {c} ends at {a1}s, {d} starts at {b0}s")
    if its and its[-1][1] > T + 0.05: raise SystemExit(f"{its[-1][2]} ends at {its[-1][1]}s, after the ad ({T:.2f}s)")
    fc, last = ["[0:v]setsar=1,fps=24[b0]"], "b0"
    for i, (t0, t1, _) in enumerate(its, 1):
        fc.append(f"[{i}:v]trim=0:{t1 - t0},setpts=PTS-STARTPTS+{t0}/TB,scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=24[c{i}]")
        fc.append(f"[{last}][c{i}]overlay=enable='between(t,{t0},{t1})':eof_action=pass[b{i}]"); last = f"b{i}"
    ff("-i", str(src), *sum([["-i", str(c)] for _, _, c in its], []), "-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "0:a",
       "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "copy", str(dst))

def roomtone(src, dst, t0, t1=None, frm=None):
    """Replace the audio from t0 to t1 (default: the end) with the take's own room tone, looped from a quiet stretch (frm (a, b); default: the
    longest silence before t0). Removes a sound the model added in a silent window (a drop, a thud); the picture is copied untouched."""
    d = duration(src); t1 = d if t1 is None else min(t1, d)
    if frm is None:
        r = run("ffmpeg", "-t", f"{t0}", "-i", str(src), "-af", "silencedetect=noise=-42dB:d=0.25", "-f", "null", "-", capture=True)
        ss = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]; se = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
        spans = [(a + 0.05, b - 0.05) for a, b in zip(ss, se + [t0]) if b - a >= 0.35]   # trim the edges off speech
        if not spans: raise SystemExit("no quiet stretch of 0.35 s+ before t0; pass --from=a-b")
        frm = max(spans, key=lambda s: s[1] - s[0])
    a, b = frm; sr = int(probe(src, "stream=sample_rate", "a:0")); L = t1 - t0; f = 0.04
    tone = (f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS,aloop=loop=-1:size={round((b - a) * sr)},atrim=0:{L},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d={f},afade=t=out:st={L - f}:d={f}[tone]")
    pre = f"[0:a]atrim=0:{t0},asetpts=PTS-STARTPTS,afade=t=out:st={t0 - f}:d={f}[pre]"
    post, n = (f";[0:a]atrim={t1},asetpts=PTS-STARTPTS,afade=t=in:d={f}[post]", 3) if t1 < d - 0.01 else ("", 2)
    fc = f"{pre};{tone}{post};[pre][tone]{'[post]' if n == 3 else ''}concat=n={n}:v=0:a=1[a]"
    ff("-i", str(src), "-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(dst))
    print(f"room tone {a:.2f}-{b:.2f}s looped over {t0:.2f}-{t1:.2f}s -> {dst}")

def export(src, out_dir, base, y=0.35):
    import pathlib; o = pathlib.Path(out_dir); o.mkdir(parents=True, exist_ok=True)
    enc = ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "copy"]
    cover = "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920"   # a 4:7 take is cropped a few pixels, never stretched
    ff("-i", str(src), "-vf", f"{cover},setsar=1", *enc, str(o / f"{base}_9x16.mp4"))
    # 4:5 of a 9:16 frame keeps the full width and 70% of the height; bias toward the top so the face stays in frame
    ff("-i", str(src), "-vf", f"{cover},crop=1080:1350:0:(ih-1350)*{y},setsar=1", *enc, str(o / f"{base}_4x5.mp4"))
    print(o / f"{base}_9x16.mp4"); print(o / f"{base}_4x5.mp4")

def fitframe(src, dst, w=576, h=1024, x=0.5):
    """Crop a still to the w:h aspect (full height if it is wider than w:h, else full width; --x = horizontal bias) and scale to w x h."""
    iw, ih = (int(v) for v in probe(src, "stream=width,height").replace(",", " ").split()[:2])
    cw, ch = (round(ih * w / h), ih) if iw / ih > w / h else (iw, round(iw * h / w))
    ff("-i", str(src), "-vf", f"crop={cw}:{ch}:{round((iw - cw) * x)}:{round((ih - ch) / 2)},scale={w}:{h}:flags=lanczos", "-frames:v", "1", "-update", "1", str(dst))

if __name__ == "__main__":
    a = sys.argv[1:]; c = a[0] if a else ""
    if c == "lastframe" and len(a) == 3: ff("-sseof", "-0.1", "-i", a[1], "-frames:v", "1", "-update", "1", a[2])
    elif c == "cropcheck" and len(a) == 2:
        w, h, bad = cropcheck(a[1]); print(f"frame {w}x{h}; " + ("OK full frame" if not bad else f"SHRINK detected: {bad[:5]}")); sys.exit(1 if bad else 0)
    elif c == "strip" and len(a) == 3: ff("-i", a[1], "-vf", "fps=2,scale=135:-1,tile=13x2", "-frames:v", "1", a[2])
    elif c == "cut" and len(a) >= 4: cut(a[1], a[2], a[3:])
    elif c == "concat" and len(a) >= 3: concat(a[1], a[2:])
    elif c == "export" and len(a) >= 4: export(a[1], a[2], a[3], float(next((x[4:] for x in a if x.startswith("--y=")), 0.35)))
    elif c == "insert" and len(a) >= 4: insert(a[1], a[2], a[3:])
    elif c == "overlay" and len(a) >= 4:
        opt = lambda k: next((x.split("=", 1)[1] for x in a if x.startswith(k + "=")), None)
        overlay(a[1], a[2], a[3], [x for x in a[4:] if not x.startswith("--")], float(opt("--card") or 0), opt("--card-text"), opt("--pad") or "black")
    elif c == "fitframe" and len(a) >= 3:
        opt = lambda k, d: next((x.split("=", 1)[1] for x in a if x.startswith(k + "=")), d)
        fitframe(a[1], a[2], int(opt("--w", 576)), int(opt("--h", 1024)), float(opt("--x", 0.5)))
    elif c == "roomtone" and len(a) >= 4:
        t = [float(x) for x in a[3].split("-")]; frm = next((tuple(map(float, x[7:].split("-"))) for x in a if x.startswith("--from=")), None)
        roomtone(a[1], a[2], t[0], t[1] if len(t) > 1 else None, frm)
    elif c == "duration" and len(a) == 2: print(duration(a[1]))
    else: sys.exit(__doc__)
