# Web mode: claude.ai chat and Cowork, through the Comfy Cloud connector

Use this when there is no `comfy` CLI, for example in claude.ai chat or Cowork with this skill installed as a plugin. Run commands from this skill's folder (the one holding `SKILL.md`).

What changes from the CLI path: every render, still and download goes through the Comfy Cloud connector's tools. The scripts that only compute run as they are in the code sandbox: the prompt compiler, the lint, still prompts, and `scripts/web_overrides.py`. Out of the box the sandbox has no `comfy` CLI and no Gemini key, so the scripts that call the CLI or ffmpeg (`take_graph.py --submit`, `heygen_take.py`, `media.py`, `review.py`, `fix.py`, `ledger.py`) don't run until plan B installs the CLI. The sandbox could reach Comfy Cloud on claude.ai in the 2026-10-01 test.

## Plan B: the comfy CLI in the sandbox (tested on claude.ai 2026-10-01)
When the connector can't do a step (HeyGen, uploading the packshot, downloads), install the CLI in the code sandbox and use the normal CLI path of SKILL.md from there. Tested: Claude web installed comfy-cli 1.22.0, signed in, and rendered a HeyGen take itself (job d80d66f8, 33 s, $0.32), with no Run click in the web UI.
1. Install: `pip install comfy-cli`, then `comfy --version`.
2. Sign in with OAuth. Run `comfy --json cloud login --no-browser --timeout 900` in the background; it prints a `login_url`. Give that link to the user to open and approve in their own browser.
   - Their browser then fails to load `http://127.0.0.1:<port>/callback?code=...`. That is expected: the port is in the sandbox, not on their computer.
   - Ask them to paste that full URL. While the login command is still waiting, complete it from inside the sandbox: `curl -s "<the pasted URL>"`.
   - The code works once and expires in minutes. Never store it, log it or repeat it.
   - Alternative: the user sets `COMFY_API_KEY` in this environment themselves, never through the chat.
3. Check `comfy --json cloud whoami`, then `python3 scripts/preflight.py`. Takes and stills need no Gemini key; only `review.py` does. `media.py` and `review.py` need ffmpeg: check `ffmpeg -version`.
4. From here on, follow SKILL.md steps 4-9 as on a computer:
   - `heygen_take.py --spec $W/spec.json --take N --still <still> --work $W --user-ok --cap <usd>` renders HeyGen directly.
   - `take_graph.py --submit` runs the H3 fallback.
   - The CLI uploads stills and the packshot and downloads the takes.
   - The quote and the user's explicit OK still come first.

Order to try: the connector for what it can do (stills, the H3 workflows), plan B for HeyGen and file uploads, and saving a workflow for the user to Run only when the CLI can't be installed.

## 0. Preflight
1. Call `get_server_info`. It must say `authenticated`. If the Comfy tools are missing, the user connects them once from the plugin's **Connectors** tab (Customize > Plugins > this plugin), which runs an OAuth sign-in.
2. Workflows: call `list_saved_workflows` with `name: "heygen-take"` (the default model) and `name: "speech-take"` (the H3 fallback). If they are missing, save each file in `workflows_ui/` with `save_workflow(name: <file name without .json>, workflow_json: <its JSON>)`. This is free; it stores the workflows and runs nothing.
3. Voice-reference LoRA (H3 fallback only): workflow 3 uses `lightx2v__Minimax-h3-Turbo__minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors`, which is not in Comfy's public catalog. Check with `search_models`. If it's missing, the user imports `lightx2v/Minimax-h3-Turbo` from Hugging Face in the Comfy Cloud web UI (Models > Import) before running any take with a voice reference or the product.

## 1-3. Brief, script, spec
Same as SKILL.md steps 2-3. Write `spec.json` in the sandbox, then lint every take for free:
`python3 scripts/web_overrides.py --spec spec.json --take N --first x.png` prints the take's errors (exit 1 when there are any).

## 4. Keyframe stills (gpt-image, through the connector)
1. Prompt: `python3 scripts/take_graph.py --spec spec.json --take N --print-still [--still-base M] [--still-end]` (references/stills.md).
2. Render: `partner_generate` with `model: "openai/images-generations"` and `params: {model: "gpt-image-2.5-sunburst", size: "1024x1536", quality: "high"}`.
   - For an edit of the base still, or the same person in a new place, add `medias: [{role: "image", prompt_id: <base still's prompt_id>}]`.
   - For a product still, also pass the packshot as `{role: "image", name: <packshot input name>}`.
3. Gate: show the still to the user and check it against the stills.md gates yourself (people, hands, props, camera, realism, same person). There is no Gemini still check in web mode.
4. Make it a take input: `use_previous_output(prompt_id)` returns the input file name for the take's FIRST and LAST frame. The H3 node center-crops it to 9:16 itself, so it needs no crop step.

## The packshot (the user's own file)
1. `upload_file`, then run the PUT command it returns in the code sandbox.
2. If the sandbox can't reach Comfy (`Host not in allowlist`) and the product has a public HTTPS image URL: call `partner_generate` (same gpt-image model) with `medias: [{role: "image", value: <URL>}]` and the prompt "This exact product photo on a plain white background, label and shape unchanged". Then `use_previous_output` gives the input name. Check the label by eye: it is a regenerated image.
3. Otherwise the user uploads it once in the Comfy Cloud web UI. Open workflow 1, drop the file on "SET FILE: the packshot", and save. Then read its input name with `get_saved_workflow`.

## 5. Quote and an explicit OK
Prices are in SKILL.md step 5. Never run a paid workflow without the user's yes to the quote. The connector's `confirm: true` only gates paid API nodes (gpt-image); the H3 workflows run on GPU time and are not gated by the connector, so the gate is you.

## 6-7. Takes
The default model is HeyGen Video 1 (workflow `8-heygen-take`, references/heygen.md). H3 (workflows 2-4) is the fallback for a creator whose voice changes between HeyGen clips.

**The connector can't submit HeyGen yet** (tested 2026-10-01, connector v0.62.1). Its pre-flight checks a bundled node catalog that lags Comfy Cloud: `submit_workflow` (even `dry_run`), `run_saved_workflow` and `partner_generate` answer "unknown node type" for `HeyGenImageToVideoNode` and `HeyGenReferenceToVideoNode`, which Comfy Cloud itself runs fine (the CLI path, `heygen_take.py`, works). Use **plan B** (the CLI in the sandbox, above). If the CLI can't be installed:
- Write the workflow with `python3 scripts/web_overrides.py --spec spec.json --take N --first <still> [--take M --first <still> ..] --write-workflow heygen-batch.json`. It writes a ready save-format workflow: one still → HeyGen → save chain per take, with the compiled prompt, length (5-15 s) and seed already in the widgets. It also prints the cost estimate and each take's lint errors. Never set HeyGen widgets by hand: the connector doesn't know the node, so it can't map them for you.
- Save it with `save_workflow(name: "<brand> HeyGen - <takes> (run once)", workflow_json: <the file's JSON>)`. Saving works and costs nothing.
- Ask the user to open the link and press **Run** in the Comfy Cloud web UI. That click is their spend confirmation.
- Collect the outputs by job id with `get_job_status` / `get_output`. The user gives the ids, or find them with `get_billing_activity`.
- Or use the H3 fallback, which the connector does run.
- Use `HeyGenImageToVideoNode`: the take starts exactly from its still. `HeyGenReferenceToVideoNode` is a different node: text-to-video that takes the still only as a reference (`@Image1`), so framing and identity can drift from the still. It also takes reference audio (`@Audio1`) to hold a creator's voice across clips (Nico confirmed this works, 2026-10-01; the skill does not use it yet, and the 13-clip batch from Claude web passed no audio).
1. `python3 scripts/web_overrides.py --spec spec.json --take N --first <still>` prints the workflow name and the `input_overrides`: the same prompt, length (5-15 s) and seed as `heygen_take.py`. The seed is the spec seed for every take of a creator, which keeps the voice closer. Don't edit the overrides by hand; fix the spec instead.
   H3 fallback: add `--model h3 [--last <still>] [--packshot <name>] [--voice <name>] [--draft]`. It builds the same prompt and settings as `take_graph.py`: HQ 768x1376, the 8-step turbo for speech takes, 30 steps for action takes.
2. Do the realism pass on the printed prompt before you spend (SKILL.md step 6).
3. Run `run_saved_workflow(filename: <workflow>, input_overrides: <overrides>)`.
4. Poll with `wait_for_job`. It returns after about 25 s; call it again until the job is done. A 15 s HeyGen take takes about 1-2 minutes; on H3 a 15 s speech take about 6 and an action take about 10. In chat, tell the user the job id and keep polling on their next message if the turn ends.
5. Get the file with `get_output(prompt_id, inline_urls: true)` and give the user the link exactly as returned.

## Not yet tested in web mode
These steps need a finished take as the next workflow's input. Test them once on claude.ai before relying on them:
- **Voice reference** for the H3 fallback: `use_previous_output(<take 1 prompt_id>)`, then that name as `--voice` (LoadAudio reads the mp4's audio track).
- **Review** with workflow 5 (Gemini QA on Comfy credits) and **assembly** with workflow 7: the same chaining, into their video inputs.
- **Room tone** (`media.py roomtone`) needs ffmpeg. In web mode, give the take as is and note the timestamp for the editor.

Until those pass, web mode delivers the stills and takes as links. The user reviews them by eye, and the editor assembles them (SKILL.md step 9).

## Cost
`get_billing_activity` lists each job: GPU seconds × 0.27313 credits per second, 211 credits = $1. API jobs (gpt-image) show their credits directly.
