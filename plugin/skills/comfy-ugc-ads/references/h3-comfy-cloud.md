# MiniMax H3 on Comfy Cloud: what works (field-tested 2026-09-25/28)

**Since 2026-10-01 H3 is the fallback model.** HeyGen Video 1 is the default (references/heygen.md); H3 renders a creator's later speech takes when the HeyGen voice drifts, using take 1's audio as the voice reference.

Source of truth for these numbers: our live Comfy Cloud runs, 2026-09-25 to 2026-10-01. Re-verify against the live node schema (`comfy --json` node info / `get_node`) before relying on a limit.

## Method (default since 2026-09-30, approved by Nico)
**Every take starts from keyframe stills.** One base still per scene from gpt-image-2.5-sunburst (Comfy partner node `openai/images-generations`, `params.model: "gpt-image-2.5-sunburst"`, size 1024x1536, quality high; Nico 2026-09-30, after the live Artificial Analysis ranking: Sunburst #1 in text-to-image 1197 and image editing 1182, Flare 1190/1162, gpt-image-2 1172/1122; OpenAI lists the same token prices for all three; the first tests used gpt-image-2); every other still of the same person is an edit of that base (`medias: [{role: "image", prompt_id: <base>}]`) so the face and room stay the same; `scripts/media.py fitframe` crops each to 576x1024. Each take gets `--guide <first still> --end-guide <last still>`; consecutive takes share the still at their seam, except the hook-to-body cut where the product is revealed (no product in a pain take or its stills). The still must show every prop the take handles, visible and within reach of the free hand (`review.py --still`). Without a keyframe H3 drifted off-prompt (other people, food).
Takes are split by kind (`references/failure-classes.md`): **speech** takes (lines plus small gestures) and **action** takes (one physical action, no speech; non-verbal sounds such as a yawn or a gulp only live here, cut in during assembly). Takes 2+ of a speaker use take 1's audio as `<Audio 1>` (voice reference); action takes never do.
Default setting (Nico, 2026-10-01, after the Cowork take af42f2a7): HQ 768x1376, dense "comfy kitchen attention", text encoder `qwen3vl_32b_minimax_h3_int8_convrot`, and for speech takes the 8-step turbo LoRA with res_multistep/simple on the fl path: 342 GPU-s, $0.44 per 15 s. `take_graph.py` and the UI workflows (HQ? switch on) start there. Speed: speech takes run the 8-step turbo LoRA (Nico approved it on a 15 s keyframed take, two seeds, 2026-09-30). Action takes with hands and objects run the base model at 25 steps (turbo has weaker motion).

Phase-1 method, superseded: text-to-video with only the packshot as a reference, H3 inventing the woman; take N+1 guided by the last frame of take N. The invented person changes with resolution or settings even at the same seed.

Drafts: 576x1024, 20 steps, euler+beta, Kitchen then sparse attention. About $0.30 per 15 s take at 20 steps, about half that at 8 turbo steps.
Finals (HQ, the default): 768x1376, dense attention (no BlockSparseAttention), text encoder `qwen3vl_32b_minimax_h3_int8_convrot`; speech takes 8 turbo steps ($0.44 per 15 s), action takes 30 base steps (the price scales with steps). Drafts and finals differ in look: decide the setting before the sample take.

## Graph (API format)
Built for you by `scripts/take_graph.py`. It picks one of two paths per take:
- **fl** (keyframed take without packshot or voice reference: action takes, a speaker's first take): UNET `minimax_h3_fl2va_pruned_int8_convrot.safetensors` -> attention nodes as below -> `MiniMaxH3ImageToVideo {clip, vae, prompt, width, height, length, first_frame, last_frame}` -> `KSamplerSelect res_multistep`, `BasicScheduler simple` (the official Comfy I2V template). This node also hands the keyframes to the text encoder; `MiniMaxH3AddGuide` only pins them in the latent, so on this path the prompt can say "Picture 1 aligns with the 0.00-second mark". Turbo LoRA: `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` (catalog).
- **ref** (product or voice-referenced take): the steps below (verified node-for-node against a proven HQ take graph), with `AddGuide` at frame 0 and frame -1 for the keyframes. Turbo LoRA: LightX2V `minimax_h3_ref2v_turbo_8step_v1.0_768p` (import it from huggingface.co/lightx2v/Minimax-h3-Turbo; imported files submit through POST /prompt).
Loaders (public catalog, no upload): UNETLoader `minimax_h3_ref2va_pruned_int8_convrot.safetensors`; CLIPLoader `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` type `minimax` (HQ: int8_convrot file); VAELoader video `minimax_h3_video_vae_fp16.safetensors`; VAELoader audio `minimax_h3_audio_vae_fp32.safetensors`. No LoRA.
1. UNET -> `ModelAttentionBackend {attention: "comfy kitchen attention"}` (BEFORE sparse).
2. Drafts: -> `BlockSparseAttention {selection: "sol-attn", "selection.tau": 1.15, start_percent: 0.2, end_percent: 0.9, dense_blocks: "", min_tokens: 12288, extra_tokens: 256, sink_conditioning: "exact_kv_and_rows", verbose: false}`. HQ: skip this node.
3. Model feeds `BasicScheduler {scheduler: beta, steps: 20|30, denoise: 1}` and `BasicGuider.model`.
4. `MiniMaxH3ReferenceToVideo {clip, vae, audio_vae, prompt, width, height, length, ref_image_size: "match", "ref_images.ref_image_0": LoadImage(packshot)}` plus `"ref_audios.ref_audio_0"` (take 1's audio) for takes 2+. Output 0 = conditioning, output 1 = latent.
5. `MiniMaxH3AddGuide {positive: [R2V,0], latent: [R2V,1], vae, image: LoadImage(prev last frame), frame_idx: 0}` for takes 2+ only. Guider conditioning = AddGuide output 0 (take 1: R2V output 0).
6. `SamplerCustomAdvanced {noise: RandomNoise(INT seed), guider, sampler: KSamplerSelect euler, sigmas, latent_image: [R2V,1]}`. Latent comes from R2V, not AddGuide.
7. `VAEDecode` + `VAEDecodeAudio` -> `CreateVideo {fps: 24}` -> `SaveVideo`.

Limits: 9 images, 3 videos, 3 audios per R2V. Prompt tags `<Picture i>`, `<Video k>`, `<Audio j>` in connection order. Frame grid 17k+5 at 24 fps; the model card supports 4-15 s of output (15 s = 362 frames), so a short insert renders at 4 s or more and the action is cut out in assembly. Sizes: multiples of 32 (576x1024 draft, 768x1376 HQ). Long, concrete descriptions are fine (the official full-reference guide asks for 350-500 words).

## Submit and collect
- `comfy --json run --workflow f.json --where cloud --allow-spend` -> `.data.prompt_id`. Record it in the ledger immediately (`scripts/ledger.py record`).
- Poll `comfy --json jobs status <id> --where cloud` (`jobs watch` sometimes prints nothing; `history_v2` 429s when hammered). Then `comfy --json download <id> --where cloud --out-dir <dir>`.
- Upload refs: `comfy --json upload <file> --where cloud` -> `.data.uploads[0].cloud_name`. A media loader's file name must be a literal widget value, not a link.
- Set `SaveVideo.filename_prefix` to `<ad>-<take>-<res>-<try>`: never reuse one prefix.
- A draft take takes ~3-10 min; an HQ speech take at 8 turbo steps ~6 min (342 GPU-s for 15 s), at 30 steps ~20 min. Comfy Cloud kills jobs at 30 min (Team/Pro 1 h): one take per job.

## Cost
- About $0.02 per finished second at draft size, $0.03 per second at HQ with 8 turbo steps ($0.10 at 30 steps). `comfy cloud status` balance never moves on TEAM plans: use `scripts/ledger.py` (billing events, matched by job id). Events lag; re-run before quoting a final total.
- Check in-flight jobs before any resume; a blind resume paid for a duplicate.
- `comfy --json jobs cancel <id> --where cloud` stops a job.
- Identical partner-node calls can be served from cache at no cost.

## Prompting (`scripts/h3prompt.py` writes it; this is what it encodes)
H3 does not follow instructions: it reads every word as something seen or heard (no negative prompt, cfg 1). Write the spec accordingly; `take_graph.py --lint` enforces it and `references/failure-classes.md` has the evidence.
- Official MiniMax format: FL2VA (`How the reference pictures align…`, `integrated_multimodal_description: [Shot 1] …`, `overall_soundscape`, `non_diegetic_music`) on the fl path; full-reference sections (`subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, …) on the ref path.
- Speech only where it is spoken: speaker `(S1)` with a voice description, the line inside `<d>[English] …</d>`, then "he closes his lips". A take without lines has no speaker and no speech words at all.
- Only positives. Describe the state you want ("his face stays tired and serious", "the counter holds only the glass"), never the one you don't ("no smile", "nothing floats").
- Physical actions as timed steps: reach and grip ("his fingers and thumb close firmly around the glass"), path ("lifts it to his mouth until the rim rests on his lower lip"), use, put down, release, 0.7 s or more each. Every handled object is a declared prop, visible within reach in the first frame.
- Non-verbal human sounds (yawn, gulp, sigh) go in their action beat and in `overall_soundscape`, and only in action takes.
- Time lines at 2.5 words/s plus 0.3 s breaths; `"seconds": "auto"` keeps the idle tail short (H3 fills idle time with speech).
- Still valid from phase 1: one timed line per beat; never put quoted words inside an action; don't pass storyboard stills as timed `<Picture N>` refs (H3 jump-cuts to them); don't repeat a finished action at the start of the next take; transcribe every take.
- Legacy template (`"prompt_style": "legacy"` in the spec): the pre-2026-09-30 wording, kept only to compare.

## Known defects to check in every take
Shrunken pillarboxed frame (`scripts/media.py cropcheck`), wrong/extra/doubled spoken words, speech in an action take, props floating or popping in, ponytail/bun pop-in for ~1 s, label text changing, hard cut, identity change. See `qa-rubric.md` and `failure-classes.md`.

## Not proven / avoid
- Superseded 2026-09-30: "8-step or HyperFlow LoRA drops actions or does nothing in ComfyUI". The LightX2V 8-step LoRAs work for speech takes (A/B below); hand-object action takes still run the base model.
- Generative video repair (Kling edit etc.): rejected, restyles the whole piece. Fixes are re-render of a take or ffmpeg cuts.
- Whole-video Gemini review misses short glitches; frame-grid reviews hallucinate. Use transcript diff + dedicated hair check + human final look.
- Other models (Kling 3 $1.68, Grok 1.5 $3.05, Seedance 2.5 $4.97 per 15 s) are cleaner but 1-3x the HQ H3 price; not the default.

## License
The H3 community license excludes US/EU/UK/KR use, outputs included. Tests only until the client has a license from MiniMax (api@minimax.io). Tell the user before any commercial run.


## Speed/quality A/B (2026-09-30, a test hook, draft 576x1024, same seed, prompt of that day; Nico reviewed by eye, Gemini review not run)
Cost = Comfy billing events per 15 s take. Times are Nico's stopwatch where given. Baseline = 20 steps, sparse attention, no LoRA.

| Test | Cost | Time | Nico's verdict |
| --- | --- | --- | --- |
| base20 (baseline) | $0.284 | 219 s | good, but has cuts |
| LightX2V `ref2v_turbo_8step_v1.0_768p` LoRA, 8 steps | $0.136 | n/a | good |
| larryvrh `minimax_h3_turbo_v4_step600_ema_pruned` LoRA, 8 steps | $0.140 | n/a | good |
| `ref2v_turbo_4step_v0.1` LoRA, 4 steps | $0.090 | 70 s | good but repeats a line ("staring at the ceiling" at ~6 s) |
| int8 video VAE (`minimax_h3_video_vae_int8_convrot`), 20 steps | $0.279 | 215 s | good; no measurable gain (decode is a small slice) |
| official prompt schema (`<d>[English] …</d>`, `(S1)`, soundscape fields), 20 steps | $0.289 | slowest | good |
| fal Realism People LoRA (`r34l1sm`), 20 steps | $0.288 | n/a | bad: different person (liked) but said nonsense at times |

Takeaways: an 8-step distillation LoRA (LightX2V or larryvrh) halves the cost with no visible quality loss in this single-clip test; 4 steps is fastest/cheapest but repeats lines; int8 VAE and the prompt rewrite bought nothing here; the realism LoRA broke speech. One clip, one seed: confirm on the body and close takes before making 8-step the default. Imported LoRAs are not in the catalog the CLI validates: `take_graph.py --lora <name>` submits through POST /prompt.

Follow-up 2026-09-30, keyframed (first/last still) 15 s take: 8-step LightX2V re-run on two seeds, Nico: good, so speech takes default to `--turbo`. The same evening the hand-object inserts that failed (floating glass, babble) were re-rendered through the new compiler, the fl path and base 25 steps: exact lines, a real grip on the glass, no stray speech (Nico: "salió bien").

## Official prompt rules we follow (MiniMax `h3-prompt-writing` skill, github.com/minimax-ai/minimax-h3, 2026-08-15)
The compiler (`scripts/h3prompt.py`) writes these for you. They matter when you edit a prompt by hand or write one for a new format:
- **Speaker identity at first appearance:** say who the speaker is, including that they are on screen: "The woman on screen, with a warm, slightly husky voice (S1), ...". The compiler does this. An on-screen speaker is what fixed the 2K car take's voice-over-with-closed-lips.
- **Delivery outside `<d>`, words inside:** tone, breath and gesture go before `<d>`; inside it go only `[English]` and the exact line.
- **Camera terms the model was trained on:** motion type + amplitude + speed, written as a sentence. Types: Push In / Pull Out, Zoom In / Out, Pan, Truck, Tilt, Pedestal, Arc, Tracking, Static Shot, Shake Slightly / Strongly, POV, Roll. Examples: "the camera pushes in with small amplitude at slow speed toward his calf", "a static shot". The selfie sentence stays our approved one (Cowork take af42f2a7).
- **Voiceover** (a format with no lip-sync, e.g. hands cutting fruit while the creator talks): `(S1) says in an off-screen voiceover: <d>[English] ...</d> while her lips remain completely closed.` The compiler doesn't write this yet; the b-roll lint `POV_SPEECH` still refuses lines in a first-person take.
- **Ends and cuts:** `<cutoff>` when the video ends mid-line, and `<scenetrans>` for a line that crosses a cut. We avoid both: one shot per take, with lines that end before the take does.
- **Length:** for full-reference prompts, the official guide puts the description at 350-500 words. Ours run 300-490; Cowork's approved one was 412.
- **Concrete over abstract:** "a faint fridge hum", never "cinematic" or "beautiful".
- **Where we deviate:** the guide puts visible on-screen text in double quotes. We keep quotes out of speech-take actions (lint `QUOTE_IN_ACTION`), because H3 spoke quoted words. The guide also lists impact sounds as soundscape content; we leave them out (lint `SOUND_EFFECT`).
