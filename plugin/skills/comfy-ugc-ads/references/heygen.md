# HeyGen Video 1: the default model

HeyGen Video 1, run through the Comfy Cloud partner node, is the default model (Nico, 2026-10-01, until he says otherwise; `h3prompt.DEFAULT_MODEL`). It makes one clip of up to 15 s from our keyframe still, with native lip-synced speech. In his side-by-side of the same clip (H3 jobs ddc884bd and 6db161af vs HeyGen c7d95422), HeyGen looked better, cost $0.32 instead of $0.45 per 15 s and took about 1.3 minutes instead of 6.

What it doesn't have, and what the skill does about it:
- **No last frame:** each take starts exactly from its still and ends free. Keep `end_state` simple (the hand at rest, eyes on the lens); the cut to the next part is a jump cut.
- **No voice reference on the image-to-video node** (`HeyGenImageToVideoNode`). The reference-to-video node (`HeyGenReferenceToVideoNode`) does take `@Audio1` voice references (Nico, 2026-10-01; its node description lists up to 12 image, video and audio references), which is the route for holding a voice across clips; not yet wired into the skill or tested for voice match. With the image-to-video node: every take of a creator uses the same `voice` text and the same seed (the spec seed). Listen to a creator's takes one after another; if the voice changes, re-render the later speech takes with H3 using take 1's audio as the voice reference (SKILL.md step 6, the H3 fallback).
- **No product reference on the image-to-video node.** The product must already be in the take's still (a gpt-image edit with the packshot, references/stills.md). The reference-to-video node accepts several reference images (up to 12 references in all), so the packshot can go in as a second image, `@Image2`, named in the prompt as the product (Nico, 2026-10-01). Not yet wired into the skill or tested for label fidelity.
- **Not a `partner_generate` model:** on the web it runs as the saved workflow `8-heygen-take` (references/web-mode.md).

## Settings
- Node: `HeyGenImageToVideoNode`, model `heygen-video-1`.
- Inputs: `model.image` = the 864x1536 keyframe still; `model.duration` 5-15 s (from the take's length); `model.resolution` `768p` (its maximum: 768x1344, 24 fps); `model.seed` = the spec seed.
- Cost: $0.32 per 15 s at 768p (67.89 credits, from Comfy billing).
- Run: `python3 scripts/heygen_take.py --spec $W/spec.json --take N --still <still.png> --work $W --user-ok --cap <cap_usd>`. It compiles the prompt from the spec (`h3prompt.heygen_prompt`; `--print-prompt` shows it), runs the lint and the prompt check (`--check` is free and stops there), the spend guard, records the job in the ledger, and downloads `$W/takes/<name>-T<N>-heygen-768p-<try>.mp4`. `--prompt-file P.txt` runs a hand-written prompt instead.

## Prompt
`heygen_take.py --print-prompt` writes it from the spec; fix the spec, not the prompt. It is natural language, not H3's caption format (no `(S1)`, no `<d>`). In order:
1. The shot: "Live-action, realistic UGC iPhone selfie video, vertical 9:16, one continuous handheld front-camera shot with a subtle natural sway."
2. "It starts exactly from the reference image:" followed by the creator, the setting and light, the phone arm, and what the free hand holds.
3. "She is on screen and talks straight into the lens; her lips move in sync with every word, in <voice>."
4. One timed line per beat: `0-4.3s: <gesture> and says: "<line>"`. Time lines at about 2.5 words/s with a 0.3 s gap. Silent action beats come after the last line, one physical step each.
5. The last paragraph is the audio: room tone and ambience only ("Close phone-mic audio, quiet kitchen room tone in the morning, faint birds outside the window. No music, no subtitles, no text on screen.").

## Rules (each one cost a take)
- No impact sound in the prompt. HeyGen renders impacts badly: pills into a trash bag came out as pills on metal, and naming the plastic bag made it worse. A soft sound in place (a faint rattle in the palm, a fabric rustle) is fine.
- Small objects (pills, capsules, gummies) never fall on screen; they break up mid-air. Close them in a fist and drop the fist out of frame. Leave the bin out of the prompt, so the model doesn't aim the drop at it.
- HeyGen still adds a thud when the fist leaves the frame. After the render, replace everything after the last word with the take's own room tone: `media.py roomtone <take> <out> <t0>`, with t0 about 50 ms after the last word. Find the end of the last word with `ffmpeg -ss <t> -t 0.1 -i take.mp4 -vn -af volumedetect -f null -` (room tone is about -45 to -50 dB).
- After an action, the hand goes back to rest ("rests flat on the counter"). Nobody shows the result to the lens, such as an empty palm.

`heygen_take.py --check` enforces the first, second and fourth rules (`SOUND_EFFECT`, `SMALL_FALL`, `SHOW_RESULT`). The thud fix is a step after the render.

## Approved example (clip 1, 15 s, seed 42; the spoken lines are swapped for neutral ones, the rest is the approved prompt)
```
Live-action, realistic UGC iPhone selfie video, vertical 9:16, one continuous handheld front-camera shot with a subtle natural sway. It starts exactly from the reference image: a warm, relatable American woman in her fifties with shoulder-length brown hair worn down and a soft blue cardigan, standing at the counter of her bright kitchen in soft morning light. Her phone arm stretches toward the lens at the lower left edge of the frame. Her free hand rests palm up on the counter, loosely cupped around a small handful of plain white generic pills. She is on screen and talks straight into the lens; her lips move in sync with every word, in a warm, slightly husky female voice, calm, wry and honest, General American accent.

0-4.3s: She leans in toward the lens with a knowing look, head slightly tilted, and says: "Okay, honest opinion. The supplement wasn't the problem."
4.6-7s: She gives a dry, knowing smile and a small nod and says: "The cheap version was."
7.3-11.8s: She lifts the pills in her open palm up toward the lens and grimaces at them, saying: "Mine was the bargain bottle. Never again."
12.1-13s: Without a word, her fingers close into a tight fist around the pills.
13-14s: She lowers her closed fist straight down past the bottom right edge of the frame, out of view.
14-15s: Her hand comes back up and rests flat on the counter, and she looks into the lens with a dry, knowing look, lips together.

Close phone-mic audio, quiet kitchen room tone in the morning, faint birds outside the window. No music, no subtitles, no text on screen.
```
After the render: `media.py roomtone <take> <out> 12.15`.
