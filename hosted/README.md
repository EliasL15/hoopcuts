# Hosted Basketball Highlights

This directory contains the hosted product, separate from the local CLI and browser review app in the parent directory. It reuses the Python analysis pipeline and adds Firebase accounts, cloud run history, private media storage, isolated Cloud Run jobs, and Stripe subscriptions and minute credits.

## Architecture

- **Firebase Authentication** provides email/password signup and sign-in, plus Google sign-in. Registration asks users to confirm their password. Users must verify their email before using the API; Firebase handles verification and password-reset flows.
- **Cloud Firestore** stores run metadata, candidate events, clip selections, account balances, and an append-only credit ledger. The backend and workers access Firestore using the Firebase Admin SDK. Client-side Firestore access is denied by `firestore.rules`.
- **Cloud Run** hosts the FastAPI app and runs one isolated job per analysis.
- **Cloud Tasks** dispatches jobs. A scheduled recovery endpoint redelivers work when a dispatch or worker lease expires.
- **Cloudflare R2** stores source videos, clips, and reels in a private bucket. The API grants short-lived signed upload and playback URLs after checking ownership.
- **Stripe** supports Free/Pro/Business subscriptions, monthly minute allowances, Customer Portal, and subscriber-only top-ups after rollout; existing one-time packs remain compatible before rollout.
- **OpenAI** classifies sampled frames. Keep the API key in Secret Manager; it is never sent to the browser.

The project is configured for Europe by default. The recommended pairing is Firestore `eur3` with Cloud Run, Cloud Tasks, and Artifact Registry in `europe-west1` (Belgium), one of the regions backing `eur3`. Choose a Firestore location when creating the database; its location cannot be changed later.

## Run locally

Prerequisites: Python 3.11, Node.js 24 LTS, Docker (for deployment), FFmpeg, a Firebase project with a registered Web app, a Firestore database, and `gcloud` configured with credentials that can access Firebase Authentication and Firestore.

1. In Firebase Console, enable **Email/Password** and **Google** authentication (set a support email for Google) and configure authorized domains and email action templates for verification and password reset.
2. Create Firestore in the `eur3` multi-region. Deploy the default-deny rules from this directory:

   ```bash
   firebase deploy --only firestore:rules --project YOUR_FIREBASE_PROJECT_ID
   ```

3. Copy `.env.example` to `.env` in this directory. Fill in the Firebase Web app values, R2 settings, Stripe test values, and GCP project/region values. The Firebase Web API key is public app configuration; keep the Stripe, R2, and OpenAI credentials private.
4. For local server access to Firebase, authenticate Application Default Credentials:

   ```bash
   gcloud auth application-default login
   gcloud config set project YOUR_FIREBASE_PROJECT_ID
   ```

5. From the repository root, install the Python and frontend dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r hosted/requirements.txt

   export PATH="/opt/homebrew/opt/node@24/bin:$PATH"
   cd frontend
   npm ci
   npm run build
   ```

6. For frontend development with hot reload, use two terminals. Run FastAPI on port 8001 from the repository root:

   ```bash
   source .venv/bin/activate
   uvicorn hosted.app.main:app --reload --port 8001
   ```

   Run Vite on port 8000 from `frontend/`:

   ```bash
   export PATH="/opt/homebrew/opt/node@24/bin:$PATH"
   npm run dev
   ```

Open <http://127.0.0.1:8000>. Vite proxies API requests to FastAPI on port 8001. The web app uses Firebase Authentication; signup sends an email verification link, and the FastAPI service validates the verified Firebase ID token on every private request.

To inspect the production build without Vite, run `npm run build`, start FastAPI on port 8000, and open <http://127.0.0.1:8000>.

### Local-only review studio

The same React frontend also powers the local review tool. Start its API on port 8001:

```bash
source .venv/bin/activate
python -m basketball_highlights ui --port 8001
```

Then run `npm run dev` from `frontend/` and open <http://127.0.0.1:8000>. The local runtime skips Firebase and credits, and reads projects from `outputs/ui_projects`.

## Firestore layout and concurrency

- `users/{uid}` stores the account identity metadata.
- `users/{uid}/account/credits` stores integer milli-minute available and reserved balances.
- `users/{uid}/runs/{runId}` stores run status, progress, owner, source object key, reservation, and result counts.
- `users/{uid}/runs/{runId}/events/{eventId}` stores a candidate payload, inclusion state, and optional clip object key.
- `users/{uid}/credit_ledger/{eventKey}` stores immutable purchase, reserve, settle, and release records.
- `dispatch_outbox/{runId}` records Cloud Tasks dispatch attempts and delivery state.

Reservation, cancellation, credit settlement, selection counts, and Stripe grants use Firestore transactions. Each worker claims an expiring run lease before processing and renews it every minute independently of progress updates, including downloads and clip uploads. Analysis executions explicitly allow six hours; a newly claimed retry clears the previous lease warning. Hosted analysis publishes compressed immutable checkpoints to private R2 storage and fences their Firestore pointers with the worker token. Each snapshot contains detections, context checks, pinned settings, source identity, and the model-budget ledger. Base requests run in batches of at most 32; their maximum costs are saved before dispatch, with results saved after the batch or a 30-second drain boundary. Interrupted requests retain their conservative reservations on resume. Temporary API failures and expired executions allow three recoveries after the initial execution, keeping the original credit reservation. Cancelled orphaned workers are settled by scheduled recovery. Transient rate limits share a cooldown and reduce concurrency to one; four request attempts are allowed, with five-second exponential backoff, a 60-second backoff cap, and longer Retry-After headers honored. Explicit credit-balance exhaustion, organization/project spend limits, organization usage limits, other insufficient-quota errors, and invalid credentials fail immediately with an actionable message. Progress spend excludes unsent batch reservations; budget enforcement still includes them. Workers without Cloud Tasks dispatch configuration leave recovery to the scheduled outbox dispatcher. Only successful base classifications count as processed footage; incomplete base analysis fails with partial results rather than appearing complete. Completed/cancelled checkpoints are deleted after settlement; failed-run checkpoints and orphaned objects are removed by media retention cleanup or run deletion. An expired worker at its recovery limit can publish event data from the last checkpoint without making model requests; video clips may be unavailable in that terminal case.

Duplicate Cloud Tasks use deterministic task IDs; a worker that no longer owns the lease cannot settle credits. Keep application data access on the server: Firestore Admin SDK bypasses Firestore Security Rules, so server routes must always scope lookups and object keys to the verified Firebase `uid`.

## Automated image builds with GitHub Actions

`.github/workflows/build-and-push.yml` builds the image on pushes to `main`, publishes a commit SHA tag plus `staging` to Artifact Registry, deploys that immutable SHA image to `highlights-web`, and updates `highlights-worker` to the same image. It authenticates with Workload Identity Federation; do not create or upload a service-account JSON key. The workflow stays skipped until its GitHub repository variables are configured.

Run this one-time setup in Google Cloud Shell. Replace the project ID, project number, and repository placeholders with your own values.

```bash
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project=YOUR_PROJECT_ID

gcloud artifacts repositories create highlights \
  --repository-format=docker \
  --location=europe-west1 \
  --project=YOUR_PROJECT_ID

gcloud iam workload-identity-pools create github-pool \
  --project=YOUR_PROJECT_ID \
  --location=global \
  --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project=YOUR_PROJECT_ID \
  --location=global \
  --workload-identity-pool=github-pool \
  --display-name="Hosted highlights repository" \
  --issuer-uri="https://token.actions.githubusercontent.com/" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository == 'YOUR_GITHUB_OWNER/YOUR_GITHUB_REPO' && assertion.ref == 'refs/heads/main'"

gcloud iam service-accounts create github-image-publisher \
  --project=YOUR_PROJECT_ID \
  --display-name="GitHub image publisher"

gcloud artifacts repositories add-iam-policy-binding highlights \
  --project=YOUR_PROJECT_ID \
  --location=europe-west1 \
  --member="serviceAccount:github-image-publisher@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"

gcloud iam service-accounts add-iam-policy-binding \
  github-image-publisher@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --project=YOUR_PROJECT_ID \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/YOUR_GITHUB_OWNER/YOUR_GITHUB_REPO"

gcloud run services add-iam-policy-binding highlights-web \
  --project=YOUR_PROJECT_ID \
  --region=europe-west1 \
  --member="serviceAccount:github-image-publisher@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.developer"

gcloud run jobs add-iam-policy-binding highlights-worker \
  --project=YOUR_PROJECT_ID \
  --region=europe-west1 \
  --member="serviceAccount:github-image-publisher@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.developer"

gcloud iam service-accounts add-iam-policy-binding \
  highlights-runtime@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --project=YOUR_PROJECT_ID \
  --member="serviceAccount:github-image-publisher@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

In the GitHub repository, open **Settings → Secrets and variables → Actions → Variables** and add:

- `GCP_PROJECT_ID` = your Google Cloud project ID
- `GCP_WORKLOAD_IDENTITY_PROVIDER` = `projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider`
- `GCP_DEPLOY_SERVICE_ACCOUNT` = `github-image-publisher@YOUR_PROJECT_ID.iam.gserviceaccount.com`

These identifiers are not secrets. After saving them, run **Actions → Build and publish hosted image → Run workflow** once. Later pushes to `main` build, publish, and deploy automatically.

## Deploy to Google Cloud

1. Enable Cloud Run, Cloud Tasks, Cloud Scheduler, Secret Manager, Artifact Registry, and Cloud Build in the Firebase project's backing Google Cloud project. Create Firestore and deploy `firestore.rules` before accepting users.
2. Create a private R2 bucket with the **EU jurisdiction** (not just a Western Europe location hint), then configure CORS for the exact deployed app origin, `PUT`, and `Content-Type`. Use the EU S3 endpoint format `https://ACCOUNT_ID.eu.r2.cloudflarestorage.com`, create a least-privilege bucket key, and store the R2 credentials in Secret Manager. The R2 bucket jurisdiction cannot be changed after creation.
3. Create Stripe one-time prices for credit packs. Configure `STRIPE_CREDIT_PACKS` as a JSON object, for example `{"60":{"price_id":"price_...","minutes":60},"300":{"price_id":"price_large","minutes":300}}`. Set a webhook for `/api/stripe/webhook` and store its signing secret in Secret Manager.
4. Configure the automated image build above. It publishes `europe-west1-docker.pkg.dev/YOUR_PROJECT_ID/highlights/hosted:staging` and a commit-specific tag on pushes to `main`.
5. Deploy the FastAPI web service and the Cloud Run Job from that image in `europe-west1`. Set `GCP_REGION=europe-west1` and `GCP_TASKS_LOCATION=europe-west1` from `.env.example`. Attach a dedicated service account to each resource and grant only the needed Firestore, Cloud Tasks, Cloud Run, and Secret Manager permissions. The Firebase Admin SDK uses the attached Google service identity; do not upload service-account key files.
6. Give the web service permission to launch the analysis job and create tasks. Give the Cloud Tasks OIDC service account `roles/run.invoker` on the web service and permission to mint its OIDC token. Create the queue in `GCP_TASKS_LOCATION`.
7. Set `DISPATCH_URL`, `INTERNAL_AUDIENCE`, and `DISPATCH_SERVICE_ACCOUNT` to the deployed service and queue identity. Configure Cloud Scheduler to call `/internal/dispatch-pending` with OIDC at least once per minute.
8. Add the production web hostname to Firebase Authentication's authorized domains. Set `APP_BASE_URL` to the HTTPS origin and configure Firebase email-action redirect URLs and Stripe's webhook URL.

The Docker image includes FFmpeg. Configure enough worker memory and a job timeout that covers long games. Before enabling paid access, use staging to confirm verified signup, account isolation, Firestore transactions, duplicate dispatch handling, cancellation and credit release, media cleanup, CSV export, and chronological reel generation. Set upload, run-duration, and `MODEL_BUDGET_USD` limits for the pilot.

## Pricing notes

Firebase Authentication's basic service currently includes up to 50,000 monthly active users at no charge. Firestore includes daily free quotas, including 1 GiB stored, 50,000 document reads, and 20,000 writes. Usage beyond those quotas is billed. Check [Firebase pricing](https://firebase.google.com/pricing) and [Firestore pricing](https://firebase.google.com/docs/firestore/pricing) before opening the beta. Model API calls will likely remain the main variable cost per game.

## Security and operations

- `firestore.rules` denies all direct browser reads and writes. The service uses Firebase Admin SDK from trusted server-side processes.
- All API routes except `/health`, `/config`, and the Stripe webhook require a Firebase ID token with a verified email. The backend validates the token and derives the owner ID from its verified `uid`.
- R2 objects remain private. Signed upload and playback URLs are short-lived and are created only after the API confirms run ownership.
- Stripe webhooks are signature-verified and idempotent by Checkout session ID. Minute-pack IDs and minute amounts come from server-side configuration.
- `MAX_UPLOAD_BYTES` defaults to 5 GiB. `MAX_RESERVED_MINUTES` limits video duration. `MODEL_BUDGET_USD` caps model spend for each run.
- Users can cancel queued jobs or request cancellation while a worker runs. Active runs must be stopped before deletion. Run deletion removes nested event documents and attempts to delete the run's R2 prefix; schedule periodic cleanup for orphaned objects after transient failures.
- Keep `.env`, API keys, R2 credentials, service-account files, videos, virtual environments, and generated outputs out of Git.

## API surface

- `GET /api/account`, `GET /api/runs`, `POST /api/runs/upload`, `POST /api/runs/{id}/start`, `GET /api/runs/{id}`
- `PATCH /api/runs/{id}/events/{event_id}`, `POST /api/runs/{id}/reel`, `GET /api/runs/{id}/csv`
- `POST /api/runs/{id}/rerun`, `POST /api/runs/{id}/cancel`, `DELETE /api/runs/{id}`
- `GET /api/billing/packs`, `POST /api/billing/checkout`, `POST /api/stripe/webhook`

Internal dispatcher endpoints require a Google-issued OIDC token from the configured task service account.


## Media retention and social exports

New hosted games retain their original upload and review clips for 14 days after analysis completes (including cancelled runs with partial results). Failed analyses and abandoned uploads expire 14 days after creation. Analysis records, selections, CSV, and the latest successful reel remain until the user deletes the game. Active analysis is never cleaned up. API playback links expire within the media deadline; new exports and reruns are blocked after it. A reel that started before expiry may finish afterward, while its export lease protects the source. Downloads of saved reels request a fresh signed URL.

The worker preserves the original object and uses a temporary H.264 copy capped at 1280×720 for high-resolution analysis and review clips. `media_metrics` records original/proxy sizes, preparation time, analysis time, and stored preview bytes. Model images already have a 768-pixel cap; compression does not change credit accounting or guarantee lower model charges.

Reels are separate asynchronous executions of the same Cloud Run Job, selected through the `REEL_ID` environment override. The request transaction snapshots event timings and sponsor object keys. The worker cuts from the original, renders a vertical 1080×1920 canvas (full footage with blurred background) or source-dimension landscape, normalizes to 30 fps, blends short fades with audio crossfades, and optionally adds a three-second sponsor card. Up to two PNG/JPEG/WebP logos are decoded and normalized to private PNG objects; each upload is limited to 5 MB and 16 million source pixels. No music is added. A failed or duplicate job cannot replace a successful reel. Local review supports the same export options but does not expire media.

### Rollout procedure

1. Deploy the web service and worker from the same image with `MEDIA_CLEANUP_ENABLED=false` (the default). Keep the existing minute-by-minute `/internal/dispatch-pending` schedule: it also recovers delayed reel launches and marks expired export leases failed. Each reel execution receives a 100-minute Cloud Run timeout and a 110-minute fencing lease; rendering itself is bounded to 90 minutes. Ensure the worker's disk and memory can hold the original, normalized clips, and final export.
2. Using the existing dispatcher service account's Google OIDC token and `INTERNAL_AUDIENCE`, POST `{}` to `/internal/media/migrate`. It assigns a fixed rollout grace deadline to existing games and checks private storage for legacy `highlights_reel.mp4` exports. Storage failures fail the pass instead of enabling cleanup; retries never extend the grace deadline. The one-time scan requires a service request timeout sufficient for the existing library size.
3. Inspect the response, recovered reel downloads, and game expiry dates. POST `{"verify":true}` to the same endpoint as a separate verification pass. It must return `verified: true` and `missing_owners: 0`; a pass which still assigns deadlines does not verify the migration. The marker is stored in `maintenance/media-retention-v1`.
4. Set `MEDIA_CLEANUP_ENABLED=true` on the web service only after that verification. Cleanup requires both the environment flag and the verified marker.
5. Create an hourly Cloud Scheduler HTTP job targeting `APP_BASE_URL/internal/media/cleanup`, using the same OIDC service account and audience as the dispatcher. Set its schedule to `0 * * * *` and timezone `Etc/UTC`. Keep the bucket private; do not apply a whole-run bucket lifecycle rule, which would remove retained reels.
6. Monitor `Media cleanup failed`, `Reel generation failed`, and `Reel redispatch failed` logs. Cleanup returns deleted/failed counts, preserves the latest export, and retries partial storage deletions on the next hourly call. Source expiry is enforced immediately by the API even if storage cleanup has not run yet. The hourly call also retries deletion of known superseded reels before source expiry.

Additional authenticated routes:

- `POST /api/runs/{id}/reel`: optional JSON `{ "format": "vertical" | "landscape", "sponsor_ids": [] }`; returns HTTP 202 with queued job ID/status and selected clip count.
- `GET /api/runs/{id}/reel/download`: fresh signed URL for the latest successful export, available after footage expiry.
- `POST /api/runs/{id}/sponsors`: multipart `file`; returns updated run details.
- `DELETE /api/runs/{id}/sponsors/{sponsor_id}`: removes a logo when no export is active.
- Run summaries expose `media_expires_at`, `media_available`, `reel_job`, and `latest_reel`; details also expose sponsor previews.

Run verification with `python -m pytest tests -q` (FFmpeg required for rendered export tests) and `npm run build` in `frontend`. Tests use synthetic video and isolated storage/database doubles; staging verification of actual Firestore transactions, Cloud Run dispatch, and R2 is required before enabling cleanup.

## Subscription billing

Follow [BILLING.md](BILLING.md) for the approved catalog, environment variables, Stripe configuration, legacy migration, rollout and verification. The default `SUBSCRIPTIONS_ENABLED=false` keeps the existing prepaid offering active.

### Analysis recovery rollout

Deploy the same image to web and worker: scheduled lease recovery and the worker share the recovery limit and settlement path. Existing runs without a durable checkpoint must restart analysis; their lost local state cannot be recovered. Source identity, settings, budget, and analysis-version mismatches fail clearly without silently resetting spend. Do not automatically restart historical failed runs.

Before production rollout, interrupt a staging full-game execution after a checkpoint, let scheduled recovery dispatch it, and verify the resumed-window log count, cumulative budget, unchanged credit reservation, and final events against an uninterrupted run. Exercise transient 429s and explicit quota errors separately. Watch invocation/execution identity, checkpoint age, recovery count/reason, saved-window count, and model cooldown logs. No new public endpoints or status values are required.


## Authenticated sample game

An operator can publish a completed game with playable clips and a saved reel using `.venv/bin/python -m verification.publish_demo --owner FIREBASE_UID --run RUN_UUID --apply` from the repo root. Omit `--apply` to inspect readiness. Use the live R2 endpoint and operator credentials, and only publish footage approved for demonstration. The CLI checks source ownership and media availability, copies only clips and the latest reel to a versioned `demos/` prefix, then atomically replaces `product_demos/basketball` after every copy succeeds. It does not copy the full recording, costs, sponsors, source filenames, or account data.

`GET /api/demo` and `GET /api/demo/game` require a verified account. There are no demo mutation or export endpoints. Free and other non-subscriber accounts see the sample entry in their library; any signed-in account can open `/demo`. Selections are browser state and reset on leaving. The retained example reel is explicitly independent of practice selections. Source run cleanup does not affect the demo, and viewing the sample never touches billing or worker dispatch. Superseded demo versions are retained for active playback and operator rollback; they can be removed by an operator once no longer needed.
