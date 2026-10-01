#!/usr/bin/env python3
"""Replace a take's voice track with ElevenLabs v4, keeping the original timing (draft; needs ELEVENLABS_API_KEY in the env, never printed).
  voice_replace.py transcribe <video|audio> <out.json>                 Scribe speech-to-text, word timestamps + audio events
  voice_replace.py plan <scribe.json> <plan.json> [--gap=0.45]         group words into segments (speech / event) with their original windows; EDIT the texts
  voice_replace.py render <video> <plan.json> <out.mp4> --voice=<id>   one v4 TTS call per segment, fit into its window, mix, mux over the video
  voice_replace.py check <video> <plan.json>                           re-transcribe the result and print plan window vs actual word times
Plan JSON: {"model": "eleven_v4", "segments": [{"kind": "speech"|"event", "start": s, "end": s, "text": "...[tags] allowed"}]}
Sound events use bracketed audio tags in the text (e.g. "[takes a sip of water and swallows, audible gulp] [yawns]"); never onomatopoeia, never the SFX endpoint."""
import json, os, pathlib, re, subprocess, sys, tempfile, urllib.error, urllib.request

KEY_ENV = "ELEVENLABS_API_KEY"

def need_key():
    if not os.environ.get(KEY_ENV): sys.exit(f"{KEY_ENV} is not set (run under doppler or export it; it is never printed)")
    return os.environ[KEY_ENV]

def sh(*a): return subprocess.run(list(a), capture_output=True, text=True, check=True).stdout.strip()
def dur(p): return float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)))

def transcribe(src, out, model="scribe_v1"):
    need_key(); tmp = pathlib.Path(tempfile.mkdtemp()) / "a.wav"
    sh("ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "44100", str(tmp))
    r = subprocess.run(["curl", "-s", "-X", "POST", "https://api.elevenlabs.io/v1/speech-to-text", "-H", f"xi-api-key: {os.environ[KEY_ENV]}", "-F", f"model_id={model}",
                        "-F", f"file=@{tmp}", "-F", "timestamps_granularity=word", "-F", "tag_audio_events=true", "-F", "language_code=eng"], capture_output=True, text=True)
    d = json.loads(r.stdout)
    if "words" not in d: sys.exit("scribe error: " + r.stdout[:300])
    pathlib.Path(out).write_text(json.dumps(d, indent=1)); return d

def make_plan(scribe, out, gap=0.45):
    words = [w for w in json.load(open(scribe))["words"] if w["type"] != "spacing"]; segs, cur = [], None
    for w in words:
        if w["type"] == "audio_event":
            if cur: segs.append(cur); cur = None
            segs.append({"kind": "event", "start": round(w["start"], 2), "end": round(w["end"], 2), "text": w["text"]}); continue
        if cur and w["start"] - cur["end"] <= gap: cur["end"] = round(w["end"], 2); cur["text"] += " " + w["text"]
        else:
            if cur: segs.append(cur)
            cur = {"kind": "speech", "start": round(w["start"], 2), "end": round(w["end"], 2), "text": w["text"]}
    if cur: segs.append(cur)
    pathlib.Path(out).write_text(json.dumps({"model": "eleven_v4", "segments": segs}, indent=1)); return segs

def tts(text, voice, model, dst):
    if not re.sub(r"\[[^\]]*\]", "", text).strip(): text += " ..."   # v4 rejects tag-only text (input_text_empty); a trailing "..." is accepted
    body = json.dumps({"text": text, "model_id": model}).encode()
    req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128", data=body, headers={"xi-api-key": need_key(), "Content-Type": "application/json"})
    try: pathlib.Path(dst).write_bytes(urllib.request.urlopen(req, timeout=120).read())
    except urllib.error.HTTPError as e: sys.exit(f"TTS HTTP {e.code} for {text[:60]!r}: {e.read().decode()[:300]}")

def render(video, plan, out, voice, work=None):
    P = json.load(open(plan)); T = dur(video); work = pathlib.Path(work or pathlib.Path(out).with_suffix("")); work.mkdir(parents=True, exist_ok=True); parts = []
    for i, s in enumerate(P["segments"]):
        mp3, wav = work / f"seg{i:02d}.mp3", work / f"seg{i:02d}.wav"; tts(s["text"], voice, P.get("model", "eleven_v4"), mp3)
        sh("ffmpeg", "-loglevel", "error", "-y", "-i", str(mp3), "-af", "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05", "-ar", "44100", "-ac", "1", str(wav))
        d, win = dur(wav), s["end"] - s["start"]; tempo = 1.0
        fitted = work / f"seg{i:02d}_fit.wav"
        if s["kind"] == "event":                          # sound events: keep natural pace, cut the tail (the trailing "..." filler) at the window end with a short fade
            sh("ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-af", f"atrim=0:{win:.3f},afade=t=out:st={max(win - 0.15, 0):.3f}:d=0.15", str(fitted))
        else:
            if d > win: tempo = min(d / win, 1.25)        # speech: only speed up, never slow down, never beyond 1.25x; report what did not fit
            sh("ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-af", f"atempo={tempo:.4f}", str(fitted))
        nd = dur(fitted); print(f"seg{i} {s['kind']:6s} window {s['start']:.2f}-{s['end']:.2f} ({win:.2f}s)  tts {d:.2f}s  tempo {tempo:.2f}  final {nd:.2f}s" + ("  OVERRUNS window" if nd > win + 0.05 else ""))
        parts.append((fitted, s["start"]))
    ins, flt = [], []
    for k, (f, st) in enumerate(parts): ins += ["-i", str(f)]; flt.append(f"[{k}:a]adelay={int(st * 1000)}:all=1[a{k}]")
    mix = work / "mix.wav"; n = len(parts)
    sh("ffmpeg", "-loglevel", "error", "-y", *ins, "-filter_complex", ";".join(flt) + ";" + "".join(f"[a{k}]" for k in range(n)) + f"amix=inputs={n}:normalize=0:duration=longest,apad=whole_dur={T},atrim=0:{T}[m]", "-map", "[m]", "-ar", "44100", str(mix))
    sh("ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-i", str(mix), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)); print("wrote", out)

def check(video, plan):
    d = transcribe(video, pathlib.Path(tempfile.mkdtemp()) / "chk.json"); P = json.load(open(plan))
    print("plan windows:"); [print(f"  {s['kind']:6s} {s['start']:.2f}-{s['end']:.2f}  {s['text'][:70]}") for s in P["segments"]]
    print("actual words (start-end):")
    for w in d["words"]:
        if w["type"] != "spacing": print(f"  {w['start']:.2f}-{w['end']:.2f} {w['type']:11s} {w['text']}")

if __name__ == "__main__":
    a = sys.argv[1:]; c = a[0] if a else ""; opt = lambda k, d=None: next((x.split("=", 1)[1] for x in a if x.startswith(k + "=")), d); pos = [x for x in a[1:] if not x.startswith("--")]
    if c == "transcribe" and len(pos) == 2: transcribe(*pos)
    elif c == "plan" and len(pos) == 2: [print(s) for s in make_plan(pos[0], pos[1], float(opt("--gap", 0.45)))]
    elif c == "render" and len(pos) == 3 and opt("--voice"): render(*pos, opt("--voice"))
    elif c == "check" and len(pos) == 2: check(*pos)
    else: sys.exit(__doc__)
