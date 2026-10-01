# Comfy UGC Ads

A Claude plugin that makes UGC-style video ads on Comfy Cloud. Give Claude a brand brief and a product packshot. It writes the script, makes the stills, renders the creator clips, checks them and assembles 9 ads (3 hooks x 1 shared body x 3 closes) in 9:16 and 4:5. A human editor does the final polish.

- **Model:** HeyGen Video 1 by default (about $0.32 per 15 s clip). MiniMax H3 is the fallback when a creator's voice needs a voice reference.
- **Spend:** nothing paid runs before a quote and an explicit yes. Every paid job goes into a ledger.
- **Assembly:** runs on Comfy Cloud (`scripts/assemble_graph.py`), with a local ffmpeg fallback (`scripts/media.py`).
- **Brand-neutral:** the plugin holds no client material. Your brief, packshot and runs stay in your own folders.

## Install (claude.ai, Cowork, Claude Code)

Needs a paid Claude plan and a Comfy account with credits.

1. In claude.ai open **Customize > Plugins > Add > Add marketplace**, enter `clickmediapropy/comfy-ugc-ads`, then install **Comfy UGC Ads**. Or download this repo, zip the `plugin/` folder and use **Add > Upload plugin**.
2. On the plugin's **Connectors** tab, connect **comfy-cloud** and sign in. Installing the plugin does not connect it.
3. Start a new chat and attach the brief and the packshot.

Without the `comfy` CLI (claude.ai chat, Cowork) the skill runs in web mode: `plugin/skills/comfy-ugc-ads/references/web-mode.md`. If the connector rejects HeyGen, that file describes the fallbacks, including installing the `comfy` CLI in the Claude sandbox.

## Where things are

- `plugin/skills/comfy-ugc-ads/SKILL.md`: the step-by-step the agent follows.
- `plugin/skills/comfy-ugc-ads/scripts/`: take rendering, prompt compiler and lint, review, fix, assembly, media, ledger.
- `plugin/skills/comfy-ugc-ads/references/`: failure classes, Comfy Cloud setup, QA rubric, spec example, brief template.
- `plugin/skills/comfy-ugc-ads/workflows_ui/`: the same steps as editable Comfy Cloud workflows.

Run `python3 plugin/skills/comfy-ugc-ads/scripts/preflight.py` first when working with the CLI. MiniMax H3 is open weights under a community licence that excludes use in the US, EU, UK and Korea.
