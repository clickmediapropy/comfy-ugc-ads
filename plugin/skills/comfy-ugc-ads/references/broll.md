# B-roll library

B-roll is short first-person footage laid over the creator's talking takes: the pain, a failed fix, the product in use, life after. It makes an ad feel filmed by a real person and gives the editor cuts to hide a glitch. Following the client's B-roll SOP (2026-10-01): first-person view, no face, about 4-5 s each, sorted by category, made once and reused across every ad and batch.

## Categories
| category | What it shows | Product | Example (magnesium) |
| --- | --- | --- | --- |
| `pain` | The problem as lived | never (`tone: negative`) | Phone glowing 3:12 AM on the nightstand; his hand pressing his stomach |
| `failed` | Fixes that did not work | never; a plain unlabeled jar or tub only | Shaking two capsules from a plain white jar into his palm |
| `discovery` | Finding the answer | after the reveal only | Scrolling a page about magnesium forms on a phone |
| `product_use` | The product in use | yes | Unscrewing the bottle, a capsule in the palm, a glass of water |
| `lifestyle` | Day-in-the-life texture | rarely | Morning coffee by a window, keys and a gym bag |
| `after` | Life after | rarely | Switching off an alarm in morning light, stretching |
| `xray` | Inside the body (medical-style overlay) | never | A skeleton and muscle overlay with a red glow over the calf (recipe below) |

Product rule: pain and failed b-roll never shows the product (lint `BROLL_CATEGORY` forces `tone: negative`, so `PRODUCT_IN_PAIN` applies; `broll.py add` refuses it). A product clip goes only over a range after the reveal (`broll.py list --no-product` before it). H3 invents label text: keep product clips short and never let the label be read; a readable label is a packshot cutaway (SKILL.md step 3).

## Writing a b-roll take
A b-roll take is an action take with a first-person camera and a category, in the library's own spec (`runs/broll-<brand>-<who>/spec.json`; `creator` = whose hands, sleeves and clothes; `pronoun`; `setting` per take):
```json
{"kind": "action", "camera": "pov", "category": "pain", "tone": "negative",
 "setting": "his dark bedroom at 3 AM, lying in bed", "props": [{"name": "phone", "desc": "his phone, screen glowing with the time 3:12", "where": "lies face up on the nightstand to the right"}],
 "hands": "His right hand rests on the blanket at the bottom of the frame.",
 "beats": [{"silent": 3.0, "steps": ["his right hand reaches to the nightstand and turns the phone over", "the screen lights his hand in cold blue light", "his hand lets go and drops back onto the blanket"]}],
 "soundscape": "Quiet bedroom at night, a faint fridge hum through the wall."}
```
- Only hands, forearms and objects: no face, eyes, lens or gaze words (lint `POV_FACE`), no spoken line (`POV_SPEECH`). In a first-person shot the phone is the viewer's eyes, not a prop held by the free hand: a phone on the nightstand is a prop like any other.
- Physical steps, 0.7 s or more each, as in any action take (`ACTION_TIME`, `FAST_ACTION`: base 25 steps).
- One large, visible motion per clip: a hand travelling across the frame, an object pushed, turned or tipped. A tiny motion (a finger on a trackpad, scrolling, a blink of the screen) comes back as a frozen clip.
- 4-5 s (model minimum 4 s); the clip is used whole or trimmed with `media.py cut`.
- Still: `take_graph.py --print-still` (first-person anchor, place details, practical light), gated with `review.py --still`.
- Render like any take: `take_graph.py --spec <lib>/spec.json --take N --guide <still> --submit ...` (no voice, fl path). Review with `review.py` (a face appearing is POPIN; no hair check).
- If H3 fails the same shot twice, render that shot from the same still with a partner image-to-video model (`partner_generate`, Kling or Seedance), record it in the library ledger, and add it with `--source kling|seedance`. Its look may differ from H3's: use it only where it is a separate cut.

## Library layout and index
```
runs/broll-<brand>-<who>/
  spec.json   stills/   takes/   index.json   jobs.jsonl   ledger.json
```
`python3 scripts/broll.py add <lib> <clip.mp4> --category pain --desc "phone glow 3 AM" [--product] [--who creator1] [--source h3] [--take 1]`, only after the clip passed review. `broll.py list <lib> [--category pain] [--no-product]` picks clips for an ad.

## Laying b-roll into an ad
Assembly order (SKILL.md step 9): `media.py concat` (hook, body, close) → `media.py insert` (b-roll) → `media.py overlay` (packshot cutaway, end card) → `media.py export`.
`python3 scripts/media.py insert ad.mp4 ad-broll.mp4 <lib>/takes/pain-01.mp4@2.4+2.0 <lib>/takes/failed-01.mp4@6.1+1.8`
Each clip fills the frame from t0 for its length (or `+dur`); the creator's voice keeps playing underneath, untouched; overlapping ranges are refused. Place a cut on the line it illustrates, about 1.5-2.5 s, never over the reveal or the packshot cutaway. Review the assembled ad with `review.py <ad.mp4> --expected <lines.txt> --cutaways 2.4-4.4,6.1-7.9` so the planned cuts are not reported. Check the 4:5 export: its crop removes the bottom of the frame, where first-person hands often are (`media.py export --y=`).

## X-ray b-roll (medical-style overlay)
1. Start still: a first-person or third-person still of the body part (`--print-still`), gated normally.
2. End still: an edit of the start still: "Keep the photo exactly. Add a translucent, glowing blue-white skeleton and muscle overlay on the calf, with a soft red glow around the muscle, like a medical scan seen through the skin. No text." Gate it with `--no-realism`.
3. Take: `"kind": "action", "category": "xray", "camera": "one continuous shot as the camera pushes in with small amplitude at slow speed toward his calf"`, one or two steps ("the camera moves slowly closer to the calf", "the red glow pulses once"), rendered with `--guide <start> --end-guide <end>` (fl path: H3 morphs from the photo into the scan).
4. Check it against the brief's Must-not: a scan implies a medical claim (cramps, inflammation). Use it only for what the label supports.
