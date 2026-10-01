# QA rubric (every take and every final ad)

Score per take: 0 when clean; otherwise (number of real issues) + 0.5 per second of affected video. Lower is better. A fix is kept only if re-review score is strictly lower than the original's.

| Check | How | Blocking when |
| --- | --- | --- |
| Words | Transcribe the take (Gemini audio with word timestamps, or Whisper/Scribe word timings) and diff against the lines quoted in the H3 prompt | any wrong content word, 2+ extra/missing words, any repeated line, vocal audio event over 0.5 s (laughter, mumble, hum). Non-vocal sounds (drawer, clatter) are fine |
| Frame | `scripts/media.py cropcheck` | any crop smaller than the full frame |
| Cuts/identity | contact sheet (`media.py strip`) + viewing; model reviewers only find candidates | hard cut, face/identity change, fused fingers |
| Hair | head-only crops (top 45% of frame), per-frame level 0 short/swept, 1 bump/bun, 2 tall ponytail standing up; flag runs of level 2 | any level-2 run |
| Pop-in | bottle, hand or prop appears/vanishes/changes shape and the timeline does not explain it | visible at normal speed for 0.25 s or more |
| Label | packshot cutaway: label text identical to the real pack and readable; no invented label text elsewhere | any label text differs or is unreadable |
| Claims | script lines vs the brief's Must-not list | any violation: never ship, rewrite the script |
| Actions | staged actions land where the timeline says | action missing or floating object |
| Silent action take | an action take (`kind: action`) is transcribed against NONE; its scripted non-verbal sound (yawn, gulp) is expected | any spoken, mumbled or whispered word |
| Props | `review.py` props check on the declared props: each one rests on a surface or is gripped (fingers around it) | FLOAT, APPEAR, VANISH, DUPLICATE, SHAPE or THROUGH visible at normal speed |
| Keyframe still (before rendering) | `review.py <still.png> --still --spec S.json` | a declared prop missing or out of reach of the free hand, hands not as scripted |

Model reviewers (Gemini) are candidate finders: they hallucinate and miss short glitches. Treat "clean" as "no defects found", never as proof. The human editor does the final look.

Fix policy per defect, max 2 attempts:
1. Removable spoken defect (extra/doubled words) or pop-in under 1.5 s outside any spoken line: ffmpeg cut of the range (`media.py cut`), or re-render.
2. Anything overlapping a spoken line, 3+ issues, or a non-removable audio defect: re-render the take (new seed, positive one-line feedback in the prompt that says what stays the same and never names the unwanted thing).
3. Re-review the fixed clip. Keep the fix only if its score is lower. After 2 attempts still failing: ship the original and mark it `NEEDS EDITOR` in the report with timestamps.

Report line format: `ad3 / take 2 / 00:07.4-00:09.0 / AUDIO EXTRA "formulate it" / fix: cut / kept (score 2.5 -> 0)` or `... / NEEDS EDITOR`.
