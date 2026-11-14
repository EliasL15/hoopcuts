# Basketball Highlights — Hosted App

This repository contains the hosted basketball highlights product. It includes a copy of the Python analysis package so Cloud Build can create the application image independently from the local-only repository.

The app uses Firebase Authentication and Cloud Firestore for accounts and saved runs, Cloud Run for the web service and video analysis jobs, Cloud Tasks for job dispatch, Cloudflare R2 for private video storage, and Stripe for subscriptions and prepaid video-minute credits.

The hosted product and local review studio share a React 19 and TypeScript frontend in `frontend/`. Node.js 24 LTS builds the application with Vite; FastAPI serves the production assets, while Vite provides local hot reload on port 8000 and proxies API requests to FastAPI on port 8001.

The optimized analysis pipeline, offline benchmark, experimental request grouping,
and staged rollout are documented in [PERFORMANCE.md](hosted/PERFORMANCE.md).
Default candidate refinement, optional bounded missed-basket recovery, and the human-label
accuracy comparison are documented in [ACCURACY.md](verification/ACCURACY.md).

Start with the full setup, local development, and deployment guide in [hosted/README.md](hosted/README.md). The code is not deployed until you configure Firebase, Google Cloud, R2, Stripe, and the OpenAI API key.

## Keep credentials private

Copy `hosted/.env.example` to `hosted/.env` for local development. Never commit the real `.env`, an OpenAI key, Stripe secrets, R2 credentials, service-account files, source videos, or generated output. Production secrets belong in Google Secret Manager.

## Halftime and warm-up filtering

After short-window basket detection and outcome refinement, each merged candidate receives one wider-context Decisions check. Up to seven frames span 10 seconds before and after the basket. Confident warm-up/practice detections are excluded; unclear context stays visible for review and starts unselected. Official game free throws and buzzer-beaters are explicitly protected in the instructions. This is a model judgment, not a guarantee that every break will be identified.

The initial context-confidence threshold is 0.90. Base detection uses at most 80% of the model budget, outcome refinement at most 90%, and context checks can use the remainder up to the existing total limit. Failed or unaffordable checks leave candidates unselected for review. Filtering does not reduce analyzed video duration or subscription-minute usage; trimming halftime before uploading still saves minutes.

Context results are checkpointed separately by source-window identity. New runs use analysis version 2; old checkpoints cannot resume under the new version. Completed historical games and manual selections are not migrated. Generated events/CSV include optional `game_context` metadata, and CSV-to-reel generation honors an explicit `included` column. CSV files without this column keep their previous behavior.

Original-source reel exports offer an overlap setting: combine overlapping or touching selected clip intervals into continuous sections (the default), or keep each selected clip separate and allow shared footage to repeat. Each selected source interval is retained in both modes. Individual review previews, basket counts, and manual selections remain independent. The setting is saved with each export job and included in cache matching. Existing downloaded reels are unchanged.

`processing_summary.json` records checked, excluded and unchecked context counts. Hosted workers also save these counts in `media_metrics.game_context`. Deploy matching web and worker images using the existing main-branch workflow; no balance or database migration is needed.

### Evaluate on human-labeled footage

Analyze a recording, then provide a CSV with `timestamp_seconds,game_context` for human-verified baskets. Label actual game baskets `game_play` and warm-up baskets `warmup_or_break`; include halftime, pregame, between-period and postgame practice plus genuine free throws and buzzer-beaters. Evaluate the saved checkpoint, which retains candidates removed from the final highlight list:

```sh
.venv/bin/python -m basketball_highlights evaluate-context outputs/recording/run_state.json labels.csv
```

For `--csv-only` analyses, the checkpoint is `outputs/recording/.state/run_state.json`. The report records correctly excluded warm-up baskets, genuine game baskets incorrectly excluded, game baskets retained for review, missing candidates and unmatched candidates. Do not use model-generated labels as ground truth or claim measured accuracy from mocked tests. No human-labeled context dataset is committed to this repository.

## License

Source code is available under the [MIT License](LICENSE).
