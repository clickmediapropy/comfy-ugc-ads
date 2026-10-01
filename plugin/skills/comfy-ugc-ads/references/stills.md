# Keyframe stills that pass as real iPhone photos

Every take starts (and usually ends) on a still made with `gpt-image-2.5-sunburst` (SKILL.md step 4). The still sets the face, the room and the light of the whole take, so a glossy, AI-looking still gives a glossy, AI-looking video. The client's bar: actors and b-roll must "feel and look real". This formula follows the client's own UGC photo prompt doc (2026-10-01), mapped onto the spec.

## The prompt comes from the spec
`python3 scripts/take_graph.py --spec $W/spec.json --take N --print-still [--still-base M] [--still-end]` prints it (free; `h3prompt.still_prompt`). It writes, in this order:

1. **Capture anchor**, by camera: "A photorealistic iPhone front-camera selfie photo of …" (`selfie`), "… taken by a phone propped up on {camera_where}" (`propped`), "A photorealistic first-person iPhone photo from his own eye line" (`pov`, b-roll).
2. **Person and expression**: `creator` (age, hair, clothes) and the face from the take's `tone` (negative = tired and serious, lips together), or its `end_state` with `--still-end`. No face for `pov`.
3. **Hands and props**: the take's first frame (`props`, `hands`, take-level when it starts from another still). With `shows_product` the packshot goes along as the last image, and the prompt says "reproduced exactly (shape, colors, label text)".
4. **Framing**: vertical 9:16, shot size and angle per camera, slightly off-center, the surface in front visible, "slightly imperfect everyday composition".
5. **Place details** (`details`, spec or take): 2-4 small, concrete things that sell a real place: fridge magnets, a dish rack, a cable on the counter, a parking lot through the windshield, a coffee cup in the cup holder, toothbrush holder and a smudged mirror. Not in the H3 prompt (stills only).
6. **Practical light** (`light`, spec or take): a source a phone would really see: "warm under-cabinet lights", "flat overcast daylight through the windshield", "slightly harsh bathroom vanity light", "soft daylight from a window on the left". Default: the room's own practical lights, neutral tones, soft shadows.
7. **Realism finish** (fixed): sharp focus, crisp natural realism, a real unedited iPhone photo, realistic skin texture with pores and small imperfections, natural eye reflections, true-to-life materials; not a polished ad, no glossy look, no cinematic grading, no studio light, no text or logos.

Negations are fine in still prompts (an image model follows them). They stay forbidden in H3 prompts (lint `NEGATION`).

Words to keep out of a still prompt (they pull toward ads and stock photos): cinematic, masterpiece, luxury, award-winning, dramatic volumetric light, fashion editorial, perfect symmetry, glow, premium campaign.

## Base still and edits
- **One base still per person** (the first take of the body, usually). Text-to-image, no reference.
- **Same place, new pose or prop** (the bottle in hand, the last frame of a take): `--still-base M` prints an edit: "Edit the first image. Keep the same person, face, hair, clothes, room, lighting, framing and camera angle exactly. The new photo shows: …". Pass the base still as the first image (`medias: [{role: "image", prompt_id: <base>}]`), the packshot as the second when the product is in hand.
- **Another place, same person** (a hook in the car): the take has its own `setting` / `camera` / `camera_where` / `props` / `hands`. `--still-base M` then prints "Same person as in the first image: identical face, hair, skin and clothes. New photo: <the full formula>". The body's base still goes along as the first image.
- **First-person b-roll**: no base (no face to keep); the hands and clothes come from the b-roll spec's `creator`/`hands`.

## Example (car hook, propped)
Spec take: `"part": "hook2", "setting": "the driver's seat of his parked car on a gray afternoon", "camera": "propped", "camera_where": "the dashboard in front of him", "props": [], "hands": "Both his hands rest on the bottom of the steering wheel.", "details": ["a parking-lot view through the windshield", "a paper coffee cup in the cup holder"], "light": "flat overcast daylight through the windshield"`.
`--print-still --still-base 11` gives: "Same person as in the first image: … New photo: A photorealistic iPhone photo of a tired, relatable American man, 33 … in the driver's seat of his parked car on a gray afternoon, taken by a phone propped up on the dashboard in front of him, captured as a casual, unposed phone snapshot. He has a tired, serious expression … Both his hands rest on the bottom of the steering wheel. Vertical 9:16 portrait, straight-on at eye level from the propped phone, chest up … Small real details around: … Lighting: flat overcast daylight through the windshield. Everything is in sharp focus …".

## Gate every still before a take uses it
`python3 scripts/review.py <still.png> --still --spec $W/spec.json --take N [--same-as <base still.png>] [--no-realism]` (one Gemini call). It fails (exit 1) when:
- a prop is missing or out of reach of a free hand, the hands differ, or an undeclared product is within reach (product rule);
- `camera_ok` is false (a selfie arm in a propped shot, a face in a first-person still);
- `realistic` is false: `realism_issues` names what gives it away (plastic or airbrushed skin, studio light, stock-photo look, an empty showroom, CGI objects, malformed hands);
- with `--same-as`, `same_person` is not true (a new place changed the face).
`--no-realism` only for an x-ray end still (a graphic on purpose; `references/broll.md`). Regenerate a failed still; never start a take from it.
