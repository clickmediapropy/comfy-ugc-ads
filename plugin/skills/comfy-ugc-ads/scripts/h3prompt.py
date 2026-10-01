#!/usr/bin/env python3
"""H3 prompt compiler, clip timing and pre-render lint (prompt_style "h3", the default). Imported by take_graph.py and review.py.

Why this exists (references/failure-classes.md): MiniMax H3 is trained on video captions written in MiniMax's own rewrite format and is
sampled without a negative prompt (BasicGuider, cfg 1). It reads every word as something seen or heard in the clip: "he stops talking"
made a silent clip talk, "nothing floats" came back as a floating glass. So the compiler writes only what is seen and heard, in the
official format, and lint() refuses the spec patterns behind past defects before anything is paid for.
Sources: huggingface.co/MiniMaxAI/MiniMax-H3 docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md (FL2VA), docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md
(full reference), README (output 4-15 s); ComfyUI comfy_extras/nodes_minimax_h3.py (MiniMaxH3ImageToVideo shows the keyframes to the
text encoder; MiniMaxH3AddGuide only pins them in the latent).

Take kinds: "speech" (spoken lines, small gestures) and "action" (no spoken line; one physical action; non-verbal sounds such as a yawn
or a gulp only live here). Paths: "fl" = fl2va model + MiniMaxH3ImageToVideo, used when a take has a keyframe and no packshot or voice
reference; "ref" = ref2va model + MiniMaxH3ReferenceToVideo (+ AddGuide keyframes) for product and voice-referenced takes.

Spec fields read here (see references/take-spec.example.json): pronoun, creator, voice, setting, camera, camera_where, details, light,
props [{name, desc, where}], hands, language, music, start, pause, product_name, product_aliases; per take: kind, seconds ("auto"
default), tone, part, category (b-roll), beats, soundscape, face ("warm and wry": the face for the whole take; spec-level too), end_state, and its own setting/camera/camera_where/props/hands when it starts
from a different still than the spec's (the bottle already in hand, a hook in the car); per beat: say + action + delivery (speech; "in a low, confiding tone") or silent +
steps + sound (action; the creator's own non-verbal sound or a soft sound in place, never an impact). details and light only feed still_prompt() (keyframe stills), never the H3 prompt.

Camera ("camera", default "selfie"): "selfie" = handheld front-camera selfie, the phone hand is the camera; "propped" = the phone stands
on camera_where (dashboard, counter), a static frame, both hands free; "pov" = first-person view, only the hands and forearms in frame (b-roll).
Any other text is used as the shot description itself (e.g. "one continuous shot as the camera pushes in with small amplitude at slow speed toward his lower back" for an x-ray b-roll; MiniMax's camera terms: motion type + amplitude + speed).
"""
import math, re

DEFAULT_MODEL = "heygen"   # Nico 2026-10-01: HeyGen Video 1 is the default model until he says otherwise; H3 is the voice-reference fallback (references/heygen.md)

FPS, WPS, STEP_S = 24, 2.5, 0.7          # frame rate, speech words per second, minimum seconds per physical step
TAIL_PLAN, TAIL_MAX, ACTION_TAIL = 0.4, 1.25, 0.5
MIN_S, MAX_S = 4.0, 15.0                 # model card: output duration 4-15 s
SHOT = {"selfie": "one continuous handheld front-camera take with a subtle natural sway, small arm micro-jitters, autofocus gently re-focusing and natural exposure shifts",
        "propped": "one continuous static shot from a phone propped up on {where}, with a slight micro-wobble, autofocus gently re-focusing and natural exposure shifts",
        "pov": "one continuous handheld POV shot from {his} own eye line, with only {his} hands and forearms in view, small micro-jitters and autofocus gently re-focusing"}
REAL = {"person": "Real unretouched skin with visible pores, fine lines and natural shine, flyaway hairs and mild phone-camera noise.",   # the texture that reads as a phone,
        "pov": "Real unretouched skin on {his} hands and mild phone-camera noise."}                                                     # not a render (Cowork take, 2026-10-01)
MIC = "Close iPhone microphone audio"   # every soundscape starts here; speech takes add the phone's compressed, room-echoey voice
BROLL = ("pain", "failed", "discovery", "product_use", "lifestyle", "after", "xray")   # b-roll library categories (references/broll.md)
FILLER = r"\b(?:um+|uh+|you know|i mean)\b|\blike,|\.\.\.|…"   # natural fillers a speech line may script; review.py never counts them as defects

RX = dict(
    negation=r"\b(no|not|never|nothing|none|nobody|without|neither|nor|don't|doesn't|isn't|aren't|won't|can't|cannot)\b",
    speech=r"\b(talk\w*|speak\w*|says|said|saying|voice\w*|words?|dialogue|lip[- ]?sync\w*|chatt\w*|narrat\w*|mumbl\w*|whisper\w*)\b",
    nonverbal=r"\b(yawn\w*|gulp\w*|swallow\w*|sigh\w*|cough\w*|laugh\w*|giggl\w*|sneez\w*|burp\w*|gasp\w*|snor\w*|hiccup\w*|groan\w*)\b",
    handling=r"\b(pick\w*|grab\w*|lift\w*|hold\w*|pour\w*|open\w*|drink\w*|sip\w*|unscrew\w*|puts?|places?|sets?|lowers?|raises?|tilts?)\b",
    seethrough=r"\b(glass|clear|transparent|crystal|mirror|plastic bag)\b",
    number=r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand)\b",
    small=r"\b(pills?|capsules?|tablets?|gumm(?:y|ies)|powder|crumbs?|seeds?|coins?|beads?|grains?)\b",
    fall=r"\b(pour\w*|tips?|tipp\w*|slid\w*|fall\w*|drops?|dropp\w*|scatter\w*|spill\w*|toss\w*|throw\w*|threw|thrown|dump\w*|sprinkl\w*|tumbl\w*|shak\w* out)\b",
    show=r"\b(?:shows?|showing|holds?\s+up|flashes|presents?)\b[^.;]*\bempty\b|\bempty\s+(?:hand|palm)s?\b[^.;]*\b(?:to|toward|at)\s+the\s+(?:lens|camera)\b",
    sfx=r"\b(clatter\w*|clink\w*|thud\w*|taps?|tapping|drops?|dropping|lands?|landing|falls?|falling|hits?|bounc\w*|tumbl\w*|scatter\w*|spill\w*|patter\w*|clunk\w*|thump\w*|splash\w*|crunch\w*|crash\w*|smash\w*|slam\w*|knock\w*|bang\w*)\b"
        r"|\b(?:rattl|rustl|crinkl|click)\w*\b[^.,;]*\b(?:into|onto|against)\b")   # impact sounds; a soft sound in place (in her palm, of the cardigan) is fine
BODY = r"(?:hands?|head|eyebrows?|brows?|eyes|eyelids|chin|shoulders?|arms?|fingers?|thumb|jaw|gaze|face|lips|mouth|palm|wrist|elbow)"   # "raises one eyebrow" is a gesture, not handling

def frames(sec):
    """Frame count on H3's 17k+5 grid at 24 fps, snapping up (15 s = 362)."""
    n = max(5, round(sec * FPS)); return n + (5 - n % 17) % 17

def kind(take):
    return take.get("kind") or ("speech" if any("say" in b for b in take["beats"]) else "action")

def _words(say):
    """Spoken length in words; a scripted pause ("...", "…") counts as one more word of time."""
    return len(say.split()) + len(re.findall(r"\.\.\.|…", say))

def _durs(take):
    return [float(b["silent"]) if "silent" in b else float(b.get("dur") or max(1.0, _words(b["say"]) / WPS)) for b in take["beats"]]

def length(spec, take):
    """(frames, seconds). take["seconds"] is a number or "auto" (default): the shortest supported grid length that fits the beats."""
    start, pause, d = float(spec.get("start", 0.2)), float(spec.get("pause", 0.3)), _durs(take)
    need = start + sum(d) + pause * (len(d) - 1) + TAIL_PLAN
    s = take.get("seconds", "auto"); s = max(MIN_S, need) if s == "auto" else float(s)
    n = frames(s); return n, n / FPS

def timeline(spec, take):
    """[(t0, t1, beat)]. In an auto-length action take the grid slack slows the action down instead of leaving idle time."""
    start, pause, d = float(spec.get("start", 0.2)), float(spec.get("pause", 0.3)), _durs(take)
    _, T = length(spec, take)
    if kind(take) == "action" and take.get("seconds", "auto") == "auto":
        d = [x * max(1.0, (T - ACTION_TAIL - start - pause * (len(d) - 1)) / sum(d)) for x in d]
    out, t = [], start
    for b, x in zip(take["beats"], d): out.append((round(t, 2), round(t + x, 2), b)); t += x + pause
    return out

def _hits(rx, s): return sorted({m.group(0).lower() for m in re.finditer(RX[rx], s or "", re.I)})

def _layout(spec, take):
    """(props, hands) of the take's first frame: the take's own when it starts from a different still (the bottle already in hand), else the spec's."""
    return take.get("props", spec.get("props", [])), take.get("hands", spec.get("hands", ""))

def _scene(spec, take):
    """(setting, camera, camera_where) of the take: the take's own (a hook in another place) or the spec's."""
    g = lambda k, d: take.get(k, spec.get(k, d))
    return g("setting", "a real home"), g("camera", "selfie"), g("camera_where", "a shelf in front of them")

def _shot(spec, take):
    """The shot description: SHOT[camera] filled in, or the take's own camera text (an x-ray push-in)."""
    _, cam, where = _scene(spec, take); his = {"he": "his", "she": "her"}.get(spec.get("pronoun", "she"), "their")
    return SHOT[cam].format(where=where.strip().rstrip("."), his=his) if cam in SHOT else cam.strip().rstrip(".")

def _texts(spec, take):
    """(label, text) written by the script author. Spoken lines are excluded (client copy); the voice only for speech takes."""
    props, hands = _layout(spec, take); setting, cam, where = _scene(spec, take)
    out = [("creator", spec.get("creator")), ("setting", setting)] + ([("voice", spec.get("voice"))] if kind(take) == "speech" else []) + [("hands", hands)]
    out += [("camera_where", where)] if cam == "propped" else [("camera", cam)] if cam not in SHOT else []
    out += [(f"props[{i}]", f"{p.get('desc', '')} {p.get('where', '')}") for i, p in enumerate(props)]
    out += [(k, take.get(k)) for k in ("soundscape", "end_state", "end_expression")] + [("face", take.get("face") or spec.get("face"))]
    for i, b in enumerate(take["beats"], 1):
        out.append((f"beat{i}.action", b.get("action"))); out.append((f"beat{i}.delivery", b.get("delivery")))
        out += [(f"beat{i}.steps[{j}]", s) for j, s in enumerate(b.get("steps", []))]
        out.append((f"beat{i}.sound", b.get("sound")))
    return [(k, v) for k, v in out if v]

def _handles(b, grip=False):
    """True when the beat moves an object. A handling verb next to a body part ("lowers the hand", "his jaw opens") is a gesture.
    grip=True leaves out "hold" and lifting/lowering what is already held: only grip changes (pick up, put down, pour, drink, open), the motion turbo renders worst."""
    txt = " ".join([b.get("action", "")] + b.get("steps", []))
    for m in re.finditer(RX["handling"], txt, re.I):
        if grip and m.group(0).lower().startswith(("hold", "lift", "raise", "lower", "tilt")): continue   # moving what is already in the hand is fine at turbo (Cowork take af42f2a7 lifted the pills at 8 steps)
        if re.match(rf"\s+(?:\w+\s+){{0,2}}?{BODY}\b", txt[m.end():], re.I) or re.search(rf"\b{BODY}\s+$", txt[:m.start()], re.I): continue
        return True
    return False

def lint(spec, take, guide=True, steps=20, fast=False, allow=()):
    """(errors, warnings) as 'RULE: message'. Errors block a paid submit. Every rule is a past defect class: references/failure-classes.md."""
    E, W, k = [], [], kind(take)
    texts, tl = _texts(spec, take), timeline(spec, take)
    _, T = length(spec, take)
    _, cam, _ = _scene(spec, take)
    for lab, s in texts:
        if _hits("negation", s): E.append(f"NEGATION: {lab} uses {_hits('negation', s)}; H3 reads every word as content, so describe what is seen and heard instead")
        if k == "action" and _hits("speech", s): E.append(f"SPEECH_IN_ACTION: {lab} mentions {_hits('speech', s)}; an action take has no speaker, describe the face and lips physically")
        if k == "speech" and _hits("nonverbal", s): E.append(f"NONVERBAL_IN_SPEECH: {lab} has {_hits('nonverbal', s)}; give that sound its own action take")
        if take.get("tone") == "negative" and re.search(r"smil", s, re.I): E.append(f"SMILE_IN_NEGATIVE: {lab} smiles in a negative-tone take")
        if cam == "pov" and lab not in ("creator", "voice") and re.search(r"\b(face(?!\s+(?:up|down))|eyes|lips|mouth|smil\w*|lens|gaze|eye contact|stares?)\b", s, re.I):
            E.append(f"POV_FACE: {lab} describes a face or the lens in a first-person take; only the hands and forearms are in view")
    if k == "action" and any("say" in b for b in take["beats"]): E.append("KIND: an action take has a spoken line")
    for i, b in enumerate(take["beats"], 1):
        if "say" in b and re.search(r"[()\[\]*]", b["say"]): E.append(f"SAY_DIRECTION: beat{i} line has brackets or asterisks; H3 speaks everything inside the line, so a direction like (laughs) gets said or acted")
    if cam == "pov" and any("say" in b for b in take["beats"]): E.append("POV_SPEECH: a first-person b-roll take has a spoken line; b-roll is silent picture laid over the creator's voice")
    if cam != "selfie" and re.search(r"\bphone hand\b|\bis the camera\b", _layout(spec, take)[1] or "", re.I):
        E.append(f"CAMERA_MISMATCH: camera is {cam!r} but hands describes a selfie phone hand; describe where both hands are")
    cat = take.get("category")
    if cat is not None and (cat not in BROLL or (cat in ("pain", "failed") and take.get("tone") != "negative")):
        E.append(f"BROLL_CATEGORY: category {cat!r} must be one of {', '.join(BROLL)}, and pain/failed b-roll needs \"tone\": \"negative\" (the product rule then applies)")
    part = take.get("part")
    if part:   # seam rule: one place per hook; the body and the closes share one place (the closes start on the body's last still)
        grp = lambda p: p if p.startswith("hook") else "main"
        other = {_scene(spec, t) for t in spec.get("takes", []) if t.get("part") and grp(t["part"]) == grp(part)}
        if len(other) > 1: E.append(f"SCENE_SPLIT: takes of {grp(part)!r} use {len(other)} different setting/camera combinations; a place changes only at the hook-to-body cut")
    props, _ = _layout(spec, take)
    aliases = [a.lower() for a in spec.get("product_aliases", ["bottle", "<picture 1>"])]
    if take.get("tone") == "negative":
        seen = (["shows_product"] if take.get("shows_product") else []) + [f"props: {p['name']}" for p in props if p["name"].lower() in aliases] \
               + [lab for lab, s in texts if any(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", s.lower()) for a in aliases)]   # whole words: "stubble" holds "tub"
        if seen: E.append(f"PRODUCT_IN_PAIN: the product is in a negative-tone take ({', '.join(seen[:3])}); on screen during the pain it reads as the product that failed. "
                          "Keep it out of this take and its stills, and reveal it after the pain, on a cut")
    names = [p["name"].lower() for p in props + spec.get("props", [])] + aliases
    for i, (t0, t1, b) in enumerate(tl, 1):
        txt = " ".join([b.get("action", "")] + b.get("steps", [])).lower()
        if _handles(b) and not any(n in txt for n in names):
            E.append(f"UNDECLARED_PROP: beat{i} handles an object missing from spec.props; declare it and show it within reach in the first frame")
        if cam == "selfie" and re.search(r"\bphone\b(?!\s+hand)", txt): E.append(f"CAMERA_HAND: beat{i} mentions the phone, which is the camera of a selfie (described once in hands); beats move the free hand only")
        if b.get("steps") and t1 - t0 < STEP_S * len(b["steps"]) - 1e-6:
            E.append(f"ACTION_TIME: beat{i} gives {t1 - t0:.1f}s to {len(b['steps'])} steps; needs at least {STEP_S * len(b['steps']):.1f}s")
        if "say" in b and '"' in (b.get("action") or ""): E.append(f"QUOTE_IN_ACTION: beat{i} action has quoted words, and H3 speaks quoted words")
        if "say" in b and re.search(r"head\s+(?:rests?|tilts?|falls?|drops?|leans?)\s+back|eyes\s+closed|closes\s+(?:his|her|their)\s+eyes|covers?\s+(?:his|her|their)\s+(?:mouth|face)|turns?\s+away|looks?\s+away", b.get("action", ""), re.I):
            E.append(f"MOUTH_HIDDEN: beat{i} hides the mouth or face while the line is said (head back, eyes closed, hand over the face, turned away); H3 then plays the line as a voice-over with closed lips. Keep the face to the lens during a line")
        for s in [b.get("action", "")] + b.get("steps", []):
            if _hits("small", s) and _hits("fall", s):
                E.append(f"SMALL_FALL: beat{i} has {_hits('small', s)} falling on screen ({s[:60]!r}); tiny falling objects break up mid-air and the model adds a drop sound. "
                         "Keep them in a closed fist or a container and let the drop happen out of frame (\"lowers her closed fist past the bottom edge of the frame\")")
            if _hits("show", s):
                E.append(f"SHOW_RESULT: beat{i} shows the result to the lens ({s[:60]!r}); real people don't prove an action (an empty palm after tossing pills). "
                         "After an action the hand goes back to rest (flat on the counter, on the wheel)")
        if b.get("sound") and _hits("sfx", b["sound"]):
            E.append(f"SOUND_EFFECT: beat{i} sound asks for an impact ({b['sound'][:50]!r}); H3 and HeyGen render impacts badly (pills into a trash bag came out as metal), "
                     "and no sound beats a bad one. Keep a soft sound in place (a faint rattle of the pills in her palm, a fabric rustle) or the creator's own yawn or sigh")
        if "say" in b and _hits("number", b["say"]) and not re.search(r"\bhand", b.get("action", ""), re.I):
            W.append(f"NUMBER_GESTURE: beat{i} says a number; give the free hand a concrete action so it stays off finger-counting")
    if _hits("sfx", take.get("soundscape")): E.append(f"SOUND_EFFECT: soundscape asks for an impact ({_hits('sfx', take['soundscape'])}); keep room tone, ambience and soft sounds in place (a fridge hum, a fabric rustle)")
    if T < MIN_S - 1e-6 or T > MAX_S + 0.2: E.append(f"DURATION: {T:.2f}s is outside the model's 4-15 s; render a short insert at 4 s or more and extract the action in assembly")
    if tl[-1][1] > T - 0.2: E.append(f"OVERFLOW: the beats end at {tl[-1][1]:.1f}s but the take is {T:.1f}s")
    elif T - tl[-1][1] > TAIL_MAX: E.append(f"IDLE_TAIL: {T - tl[-1][1]:.1f}s after the last beat, which H3 fills with speech; use \"seconds\": \"auto\" or add a beat")
    if not guide: E.append("NO_KEYFRAME: every take needs a first-frame still (--guide); without one H3 drifts off-prompt (other people, food)")
    if any(_handles(b, grip=True) for _, _, b in tl) and (fast or steps < 20):
        E.append("FAST_ACTION: an object moves in the hand (picked up, put down, poured, drunk from) at turbo/low steps, where motion is weakest; "
                 "render it at base 20-25 steps, or give that motion its own action take and start the speech take from the still where it is done")
    held = " ".join(" ".join([b.get("action", "")] + b.get("steps", [])) for _, _, b in tl if _handles(b)).lower()
    for p in props:
        if p["name"].lower() in held and _hits("seethrough", f"{p['name']} {p.get('desc', '')}"): W.append(f"SEE_THROUGH_PROP: {p['name']} is transparent or reflective; an opaque mug or bottle is easier for H3 to hold")
    return [e for e in E if e.split(":")[0] not in allow], W

def _cap(s): s = (s or "").strip(); return s[:1].upper() + s[1:]
def _person(spec): return re.sub(r"^(he|she|they) (is|are) ", "", (spec.get("creator") or "a relatable creator").strip().rstrip("."), flags=re.I)

def compile(spec, take, path="fl", guide=True, voice=False, end=False):
    """The H3 prompt for one take in MiniMax's format: FL2VA for path "fl", full-reference for path "ref"."""
    he = spec.get("pronoun", "she"); his = {"he": "his", "she": "her"}.get(he, "their"); He, His = he.capitalize(), his.capitalize()
    short = spec.get("person_short") or {"he": "the man", "she": "the woman"}.get(he, "the person")
    k, tl, (_, T) = kind(take), timeline(spec, take), length(spec, take)
    (setting, cam, _), shot = _scene(spec, take), _shot(spec, take); pov = cam == "pov"
    lang, person = spec.get("language", "English"), _person(spec)
    seen = f"a first-person view of {his} own hands" if pov else person   # who the opening frame shows
    # the packshot is <Picture 1> in the spec; on the ref path it becomes <Subject 1>, on the fl path <Picture 1> is the first keyframe, so the tag goes
    sub = (lambda s: s.replace("<Picture 1>", "<Subject 1>")) if path == "ref" else (lambda s: re.sub(r"\s*\(<Picture 1>\)", "", s).replace("<Picture 1>", "the product"))
    lp, hands = _layout(spec, take)
    props = " ".join(f"{_cap(sub(p.get('desc') or p['name']))} {sub(p['where']).strip().rstrip('.')}." for p in lp)
    hands = sub(hands or "")
    parts, spoke, prev = [], False, None
    for t0, t1, b in tl:
        if prev is not None and t0 - prev > 0.6: parts.append(f"[{prev:.1f}s-{t0:.1f}s] " + (f"{His} hands rest still." if pov else f"{He} holds eye contact with {his} lips together."))
        if "say" in b:
            vc = spec.get("voice", "a natural, conversational voice").strip().rstrip(".")
            vc += "" if re.search(r"breath", vc, re.I) else ", with tiny breaths between phrases"
            first = f"{_cap(short)} on screen" if not pov else He   # official speaker identity: on-screen or not (MiniMax h3-prompt-writing 4.4)
            who = f"{He} (S1)" if spoke else (f"{first} (S1), using the voice timbre referenced from <Audio 1>," if voice else f"{first}, with {vc} (S1),")
            act = sub(b.get("action", "")).strip().rstrip("."); says = "says" + (f" {b['delivery'].strip().rstrip('.:')}" if b.get("delivery") else "")
            if re.match(r"(?:his|her|their|both)\b", act, re.I):   # "her free hand lifts..." can't follow "She (S1)": "She (S1), as her free hand lifts ..., says"
                line = f"{who.rstrip(',')}, as {act}, {says}:"
            else: line = f"{who} {act + ', and ' if ',' in act else act + ' and ' if act else ''}{says}:"
            parts.append(f"[{t0:.1f}s-{t1:.1f}s] {line} <d>[{lang}] {b['say'].strip()}</d> {He} closes {his} lips.")
            spoke = True
        else:   # one timed line per physical step: the timeline paces the motion (and the sound) instead of leaving H3 to cram it
            steps = [sub(x).strip().rstrip(".") for x in (b.get("steps") or [b.get("action", "")])]; dt = (t1 - t0) / len(steps)
            parts.append(" ".join(f"[{t0 + i * dt:.1f}s-{t0 + (i + 1) * dt:.1f}s] {_cap(s)}." for i, s in enumerate(steps))
                         + (f" {_cap(b['sound'].strip().rstrip('.'))}." if b.get("sound") else ""))
        prev = t1
    neg = take.get("tone") == "negative"
    end_state = take.get("end_state") or (f"{his} hands come to rest in the center of the frame" if pov else
                                          f"{he} holds eye contact with {take['end_expression']}" if take.get("end_expression") else
                                          f"{he} holds a tired, serious gaze into the lens with {his} lips together" if neg else f"{he} holds eye contact with a small natural smile")
    landing = ", settling into the pose, spacing and framing established by Picture 2" if (end and path == "fl") else ""
    parts.append(f"[{prev:.1f}s-{T:.1f}s] {_cap(end_state.strip().rstrip('.'))}{landing}.")
    face = (take.get("face") or spec.get("face") or ("tired and serious" if neg else "")).strip().rstrip(".")
    tail = [f"{His} face stays {face} throughout." if face and not pov else "",
            f"Outside the moments described above, {his} lips stay gently closed and relaxed." if k == "action" and not pov else "",
            f"{His} hands stay natural for the whole shot, and everything important stays in the center of the frame." if pov else
            f"{_cap(short)} stays the same person with natural hands for the whole shot, and everything important stays in the center of the frame."]
    sounds = [b["sound"].strip().rstrip(".") + "." for _, _, b in tl if b.get("sound")]
    room = (take.get("soundscape") or "the room's own quiet natural tone throughout").strip().rstrip(".")
    mic = MIC + (" with a slightly compressed, room-echoey voice" if k == "speech" else "")
    room = room if re.search(r"\bmic", room, re.I) else f"{mic}, {room[:1].lower() + room[1:]}"
    soundscape = " ".join([room + "."] + [_cap(s) for s in sounds])
    music = spec.get("music", "N/A")
    real = (REAL["pov"].format(his=his) if pov else REAL["person"]) if cam in SHOT else ""   # custom camera text (an x-ray push-in) gets no skin line
    style = f"Live-action, realistic UGC iPhone {'selfie ' if cam == 'selfie' else ''}video, vertical 9:16, {shot}. {real}".strip()
    if path == "fl":
        if guide and end: align = ("How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
                                   f"Picture 2 (from Shot 1) aligns with the {T:.2f}-second mark of the target video.\n\n")
        elif guide: align = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        else: align = ""
        opening = (f"The shot begins in the position and framing established by Picture 1: {seen}, in {setting}." if end else
                   f"The shot begins from <Picture 1>: {seen}, in {setting}.") if guide else f"{_cap(seen)}, in {setting}."
        body = " ".join(x for x in [style, opening, props, hands, *parts, *tail] if x)
        return f"{align}integrated_multimodal_description: [Shot 1] {body}\n\noverall_soundscape: {soundscape}\n\nnon_diegetic_music: {music}"
    defs, task, ret = [], (["keyframe completion"] if guide else []), []
    if take.get("shows_product"):
        defs.append(f"<Subject 1> is the {spec['product_name']} in <Picture 1>, with its exact label text, shape and colors.")
        task.append("reference generation"); ret.append("<Subject 1> (appears in [Shot 1]): fully_preserved - the label text, shape and colors of the product are retained.")
    if voice:
        defs.append(f"<Audio 1> is the voice-timbre reference for {short} (S1)."); task.append("audio reference")
        ret.append("<Audio 1>: reference - the speaker follows the voice timbre, accent, pitch and pace of <Audio 1> without copying the original signal.")
    summary = (f"[{' + '.join(task) or 'reference generation'}] A realistic UGC {'selfie' if cam == 'selfie' else 'phone'} video of {seen} in {setting}"
               + (", who shows <Subject 1> to the camera" if take.get("shows_product") and not pov else ", showing <Subject 1>" if take.get("shows_product") else "") + ".")
    detailed = " ".join(x for x in [f"The target video is a realistic UGC iPhone {'selfie ' if cam == 'selfie' else ''}video, vertical 9:16, {shot}.", real,
                                     f"[Shot 1] The shot opens on {seen}, in {setting}.", props, hands, *parts, *tail] if x)
    return (f"subject_definitions:\n{chr(10).join(defs)}\n\nsummary:\n{summary}\n\nretention_analysis:\n{chr(10).join(ret)}\n\n"
            f"detailed_description:\n{detailed}\n\noverall_soundscape:\n{soundscape}\n\nnon_diegetic_music:\n{music}")

def heygen_seconds(spec, take):
    """HeyGen Video 1 duration for the take: whole seconds, 5-15 (the node's range)."""
    return min(15, max(5, math.ceil(length(spec, take)[1] - 1e-6)))

def heygen_prompt(spec, take):
    """The HeyGen Video 1 prompt for one take: natural language with timed beats, the audio in the last paragraph (references/heygen.md).
    HeyGen sees only the first frame (no last frame, no voice or product reference on the image-to-video node), so the take starts exactly from its still."""
    he = spec.get("pronoun", "she"); his = {"he": "his", "she": "her"}.get(he, "their"); He, His = he.capitalize(), his.capitalize()
    short = spec.get("person_short") or {"he": "the man", "she": "the woman"}.get(he, "the person")
    k, tl, T = kind(take), timeline(spec, take), heygen_seconds(spec, take)
    (setting, cam, _), shot = _scene(spec, take), _shot(spec, take); pov = cam == "pov"
    sub = lambda s: re.sub(r"\s*\(<Picture 1>\)", "", s or "").replace("<Picture 1>", "the product")
    t = lambda x: f"{x:.1f}".rstrip("0").rstrip(".")
    lp, hands = _layout(spec, take)
    props = " ".join(f"{_cap(sub(p.get('desc') or p['name']))} {sub(p['where']).strip().rstrip('.')}." for p in lp)
    real = (REAL["pov"].format(his=his) if pov else REAL["person"]) if cam in SHOT else ""
    seen = f"a first-person view of {his} own hands" if pov else _person(spec)
    head = [f"Live-action, realistic UGC iPhone {'selfie ' if cam == 'selfie' else ''}video, vertical 9:16, {shot}.", real,
            f"It starts exactly from the reference image: {seen}, in {setting}.", props, sub(hands)]
    if k == "speech":
        vc = spec.get("voice", "a natural, conversational voice").strip().rstrip(".")
        vc += "" if re.search(r"breath", vc, re.I) else ", with tiny breaths between phrases"
        head.append(f"{He} is on screen and talks straight into the lens; {his} lips move in sync with every word, in {vc}.")
    elif not pov: head.append(f"{His} lips stay gently closed and relaxed for the whole clip.")
    beats, prev = [], None
    for t0, t1, b in tl:
        if prev is not None and t0 - prev > 0.6: beats.append(f"{t(prev)}-{t(t0)}s: " + (f"{His} hands rest still." if pov else f"{He} holds eye contact with {his} lips together."))
        if "say" in b:
            act = sub(b.get("action", "")).strip().rstrip("."); says = "says" + (f" {b['delivery'].strip().rstrip('.:')}" if b.get("delivery") else "")
            subj = _cap(act) if re.match(r"(?:his|her|their|both)\b", act, re.I) else f"{He} {act}" if act else ""
            who = f", and {he} {says}" if re.match(r"(?:his|her|their|both)\b", act, re.I) else (f"{',' if ',' in act else ''} and {says}" if act else f"{He} {says}")
            beats.append(f"{t(t0)}-{t(t1)}s: {subj}{who}: \"{b['say'].strip()}\"")
        else:
            steps = [sub(x).strip().rstrip(".") for x in (b.get("steps") or [b.get("action", "")])]; dt = (t1 - t0) / len(steps)
            for i, s in enumerate(steps):
                s = f"Without a word, {s[:1].lower() + s[1:]}" if (k == "speech" and i == 0) else _cap(s)
                beats.append(f"{t(t0 + i * dt)}-{t(t0 + (i + 1) * dt)}s: {s}.")
        prev = t1
    neg = take.get("tone") == "negative"
    end_state = take.get("end_state") or (f"{his} hands come to rest in the center of the frame" if pov else
                                          f"{he} holds eye contact with {take['end_expression']}" if take.get("end_expression") else
                                          f"{he} holds a tired, serious gaze into the lens with {his} lips together" if neg else f"{he} holds eye contact with a small natural smile")
    face = (take.get("face") or spec.get("face") or ("tired and serious" if neg else "")).strip().rstrip(".")
    beats.append(f"{t(prev)}-{T}s: {_cap(end_state.strip().rstrip('.'))}." + (f" {His} face stays {face} throughout." if face and not pov else "")
                 + (f" {His} hands stay natural for the whole clip." if pov else f" {_cap(short)} stays the same person with natural hands for the whole clip."))
    room = (take.get("soundscape") or "the room's own quiet natural tone throughout").strip().rstrip(".")
    mic = MIC + (" with a slightly compressed, room-echoey voice" if k == "speech" else "")
    room = room if re.search(r"\bmic", room, re.I) else f"{mic}, {room[:1].lower() + room[1:]}"
    sounds = " ".join(_cap(b["sound"].strip().rstrip(".")) + "." for _, _, b in tl if b.get("sound"))
    audio = " ".join(x for x in [room + ".", sounds, "No music, no subtitles, no text on screen."] if x)
    return "\n\n".join([" ".join(x for x in head if x), "\n".join(beats), audio])

def expected(spec, take):
    """(spoken lines, timeline text for the visual reviewer, kind, scripted non-verbal sounds) for review.py."""
    tl = timeline(spec, take)
    lines = [b["say"] for _, _, b in tl if "say" in b]
    text = " ".join(f"[{t0:.1f}s-{t1:.1f}s] " + (f'says "{b["say"]}" while {b.get("action", "")}.' if "say" in b else
                    ", then ".join(b.get("steps") or [b.get("action", "")]) + ".") for t0, t1, b in tl)
    if _scene(spec, take)[1] == "pov": text = "First-person view: only the hands and forearms are in frame the whole clip; a face or a person appearing is a POPIN defect. " + text
    return lines, text, kind(take), [b["sound"] for _, _, b in tl if b.get("sound")]

STILL = {   # capture anchor and framing per camera (references/stills.md); {he}/{his}/{him}/{where} filled in
    "selfie": ("A photorealistic iPhone front-camera selfie photo of {person}, in {setting}, captured as a casual, unposed phone snapshot",
               "Vertical 9:16 portrait, {he} holds the phone at arm's length at a slightly high selfie angle, head and shoulders in frame, slightly off-center, "
               "the surface in front of {him} visible in the foreground, slightly imperfect everyday composition"),
    "propped": ("A photorealistic iPhone photo of {person}, in {setting}, taken by a phone propped up on {where}, captured as a casual, unposed phone snapshot",
                "Vertical 9:16 portrait, straight-on at eye level from the propped phone, chest up, {he} looks into the lens, slightly off-center, slightly imperfect everyday composition"),
    "pov": ("A photorealistic first-person iPhone photo from {his} own eye line, in {setting}, captured as a casual, unposed phone snapshot",
            "Vertical 9:16 portrait, only {his} hands and forearms in view, the horizon very slightly tilted as if shot casually, slightly imperfect everyday composition"),
    "other": ("A photorealistic iPhone photo of {person}, in {setting}, captured as a casual, unposed phone snapshot",   # a custom camera text (x-ray push-in)
              "Vertical 9:16 portrait, slightly off-center, slightly imperfect everyday composition")}

def still_prompt(spec, take, base=None, end=False):
    """gpt-image prompt for a take's keyframe still (SKILL.md step 4, references/stills.md): the iPhone UGC formula in order, capture anchor,
    person and expression, hands and props, framing, place details, practical light, realism finish. Negations are fine here (an image model,
    not H3). base: the take whose still is passed as the reference image. Same place = an edit that keeps the face, room, light and framing;
    another place (a hook in the car) = a new photo of the same person. end: the take's last frame (its end_state) instead of its first."""
    he = spec.get("pronoun", "she"); his = {"he": "his", "she": "her"}.get(he, "their"); him = {"he": "him", "she": "her"}.get(he, "them")
    setting, cam, where = _scene(spec, take); pov = cam == "pov"
    fill = lambda s: s.format(person=_person(spec), setting=setting.strip().rstrip("."), where=where.strip().rstrip("."), he=he, his=his, him=him)
    sub = lambda s: re.sub(r"\s*\(<Picture 1>\)", "", s or "").replace("<Picture 1>", "the product")
    props, hands = _layout(spec, take)
    anchor, frame = STILL.get(cam, STILL["other"])
    face = "" if pov else (_cap(take["end_state"].strip().rstrip(".")) + "." if end and take.get("end_state") else
                           f"{he.capitalize()} has a tired, serious expression with {his} lips together, looking straight into the lens." if take.get("tone") == "negative" else
                           f"{he.capitalize()} has a relaxed, natural expression with {his} mouth closed, looking straight into the lens.")
    pk = "the second image" if base is not None else "the reference image"   # the packshot rides along as the last image (SKILL.md step 4)
    stuff = " ".join(x for x in [face, " ".join(f"{_cap(sub(p.get('desc') or p['name']).strip().rstrip(','))} {sub(p['where']).strip().rstrip('.')}." for p in props), sub(hands).strip(),
                                 f"The product is the {spec.get('product_name', 'product')} from {pk}, reproduced exactly (shape, colors, label text), label facing the camera, "
                                 "fingers wrapped naturally around it." if take.get("shows_product") else ""] if x)
    details = take.get("details", spec.get("details", []))
    light = take.get("light", spec.get("light")) or "the room's own practical lights, with neutral tones and soft natural shadows"
    finish = ("Everything is in sharp focus with crisp natural realism, like a real unedited iPhone photo: " + ("realistic skin texture on the hands, " if pov else
              "realistic skin texture with pores and small imperfections, natural eye reflections, ") + "true-to-life proportions and materials. "
              "An authentic unfiltered UGC snapshot, not a polished ad: no glossy commercial look, no cinematic color grading, no studio lighting, no text, captions, logos or graphic overlays.")
    if base is not None and _scene(spec, base) == (setting, cam, where):
        return (f"Edit the first image. Keep the same person, face, hair, clothes, room, lighting, framing and camera angle exactly. The new photo shows: {stuff} "
                "Keep the same unedited iPhone look, with no added text, captions or logos.")
    new = " ".join(x for x in [fill(anchor) + ".", stuff, fill(frame) + ".", f"Small real details around: {', '.join(details)}." if details else "",
                               f"Lighting: {fill(light).strip().rstrip('.')}.", finish] if x)
    return (f"Same person as in the first image: identical face, hair, skin and clothes. New photo: {new}" if base is not None and not pov else new)
