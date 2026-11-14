# Analysis performance rollout and comparison

Version 2 keeps the existing 4-fps, 0.75-second-window, 0.25-second-stride coverage.
It journals state and budget transactions in SQLite, publishes immutable checkpoint
deltas, reuses decoder handles, indexes timestamps, pools refinement/context work,
and limits previews and uploads to two workers. Original reels continue to use the
original upload. Grouped Decisions requests and removing the proxy are separate
experiments; neither is required to enable the persistence improvements.

## Enable the optimized pipeline for testing

The optimized pipeline is now the default for new hosted analyses. The GitHub
deployment workflow explicitly enables eight-window grouping and original-source
sampling on both web and worker. The deployed settings are:

```dotenv
ANALYSIS_PIPELINE_VERSION=2
ANALYSIS_GROUP_WINDOWS=8
ANALYSIS_CONCURRENCY=4
ANALYSIS_DIRECT_SOURCE=true
ANALYSIS_TIMESTAMP_DECODER=true
ANALYSIS_PREFETCH_REQUESTS=4
ANALYSIS_ROLLING_SCHEDULER=false
ANALYSIS_CHRONOLOGICAL_EVIDENCE=true
ANALYSIS_DECODER_THREAD_TYPE=AUTO
ANALYSIS_DECODER_THREADS=2
ANALYSIS_OVERLAP_PREVIEW_UPLOADS=true
```

These settings apply when an analysis is first queued. Already-pinned runs resume
with their saved pipeline, source identity, image settings, grouping and budget.
New uploads and "Run again" analyses use the grouped settings; an existing active
run retains its previous grouping. Set the workflow's `HOSTED_ANALYSIS_GROUP_WINDOWS`
value to `1` and push to GitHub to return new hosted analyses to individual requests.
Checkpoints without a pipeline version still use the legacy implementation. Roll
back the entire pipeline by changing deployment overrides to version `1`, grouping
`1`, candidate refinement `false`, and refinement frames `5`; do not change the
saved settings or version of an active run.

### Original-source rollout (14 October 2026)

New runs skip the full-game 720p proxy encode. Version 2 samples the original at
the same evidence timestamps and JPEG dimensions/quality; review clips are encoded
individually with a 720p limit. Reel exports still use the original upload. Download
time remains, and decoding original-resolution frames can cost more than decoding
the proxy. This change removes preparation work, not all analysis work.

Offline verification uses the actual 106-minute, 1080p QuickTime-edited `.mov`
recording at its beginning, middle and end. It compares persistent samples with
independent timestamp seeks, checks preview duration/audio/dimensions, renders
vertical and original-resolution landscape reels, and measures proxy preparation
against bounded direct/proxy decode samples. It does not make model requests or
establish equal detection precision/recall across full games. Review the first new
production results for detection quality and total worker time.

On the local Mac, preparing each eight-second excerpt took 1.10–1.16 seconds;
decoding its 32 sampled frames took 0.42–0.48 seconds from the original-resolution
excerpt versus 0.22–0.24 seconds from the proxy. The actual edited full MOV uses
the variable-rate seek path: 32 samples near its midpoint took 3.81 seconds.
These microbenchmarks are not a Cloud Run throughput guarantee. The first live
direct run must be checked for total decoding time, not only preparation time.

Reproduce with a fresh output directory:

```sh
.venv/bin/python verification/verify_direct_source.py /path/to/game.mov --output /tmp/direct-source-check
```

Tests also cover variable-rate video, rotation, silent HD footage, MOV edit-list
trimming, direct-source worker completion, and preserving either saved source mode
when deployment defaults change. To roll back for new runs, set the workflow's
`HOSTED_ANALYSIS_DIRECT_SOURCE` to `"false"` and push. Already-queued/running analyses
retain their saved settings, including the current full-game proxy analysis.

### Timestamp decoder rollout (14 October 2026)

New hosted runs pin `timestamp_decoder=true`. Unrotated variable-frame-rate
sources use PyAV presentation timestamps to decode forward, retaining only the
two frames surrounding the requested time. The nearest frame is selected, with
midpoint ties selecting the later frame. Backward requests and jumps over two
seconds seek to the previous keyframe and decode forward again. Both base and
refinement/context sampling use this timestamp policy. No sampling interval,
window, JPEG quality, image dimension or model setting is reduced.

Constant-rate footage retains the existing OpenCV sequential path. Rotated
variable-rate footage retains OpenCV's display-matrix handling; missing or
non-monotonic frame timestamps fall back to the existing seek sampler. Decoder
threads are capped at two, and only sampled frames are converted to BGR/JPEG.

Tests independently index FFmpeg-decoded frames with ffprobe presentation times,
cover changing frame rates, B-frame reordering, MOV edit lists, nonzero timeline
origins, repeat/backward/distant requests, timestamp fallback and pinned recovery.
FFmpeg builds can round colour conversion slightly differently; image comparisons
allow small pixel rounding differences while requiring the exact expected PTS.

Runs saved before this rollout lack the flag and keep the legacy decoder; their
checkpoint signatures remain compatible. New checkpoint signatures include the
decoder version, so switching modes cannot silently mix frame policies. The
performance report includes `timestamp_decoder` for production comparisons.
Revert the workflow's `HOSTED_ANALYSIS_TIMESTAMP_DECODER` to `"false"` and push to
roll back new runs without altering saved analyses.

Benchmark the actual edited source (no model requests):

```sh
.venv/bin/python verification/benchmark_timestamp_decoder.py /path/to/game.mov --output /tmp/timestamp-report.json --frames 128
```

The local benchmark includes file opening and initial seek, compares three
32-second regions at four samples per second, and checks selected source frames
against FFmpeg. It measures decoder time only; it does not establish total Cloud
Run latency, model accuracy, upload speed or billing reductions.

For the 106-minute/2.9-GB QuickTime-edited broadcast on the local Mac, 128 samples
from the beginning, middle and end took 14.45/14.29/15.46 seconds with repeated
OpenCV seeking versus 5.25/5.09/5.30 seconds with forward PTS decoding, respectively:
2.75–2.92x faster decoding. Independent FFmpeg frame comparisons differed by an
average of 2.04–2.14 channel levels out of 255 from colour-conversion rounding.
No model calls were made; full-game detection precision/recall is unmeasured.

### Frame prefetch and upload overlap (14 October 2026)

New runs pin the execution settings above. A single frame-producer thread owns
decoder opening, sampling and closing. At most four prepared requests wait ahead
of dispatch. All budget reservations and journal writes remain on the coordinator;
up to 32 outstanding request reservations are durably published before dispatch.
Refinement/context preparation sorts unique timestamps within each preparation
chunk, then rebuilds original requests in admission order. Evidence timestamps,
window membership, image dimensions/quality and prompts are unchanged.

Unrotated VFR sources use PyAV AUTO threading with two decoder threads. CFR and
rotated sources retain their existing OpenCV sampling paths. Saved runs missing
these settings keep their original scheduler and SLICE decoder threading. New
execution settings enter the checkpoint signature; changing them during resume
is rejected. Rolling scheduling is implemented but disabled by default: it can
refill durable reservations before batch stragglers finish, still capped at 32.

Hosted review clips use two render workers and two upload workers. Only two renders
are submitted at once, and the upload queue is bounded at four files. Successful
uploads are ownership-checked before deleting temporary clips. Final completion
waits for uploads and event persistence. Local reels retain their preview files.
Preview uploads overlap clip rendering, so upload timings now represent cumulative
upload-call time and cannot be added to clip-rendering wall time.

The actual 106-minute / 2.9-GB recording was tested without model calls. Across
three 32-second regions, AUTO/2 was 1.43–1.53x faster than SLICE/2; AUTO/4 was
2.00–2.16x faster. Every sampled JPEG and selected PTS matched byte-for-byte across
threading modes. A 64-second replay of real footage with fixed model responses
took 24.39s with the current scheduler, 15.73s with prefetch, and 17.81s with rolling.
All profiles produced identical evidence, events and model usage/spend totals.
These Mac measurements do not establish Cloud Run throughput or model recall.
Keep rolling disabled until a hosted comparison justifies enabling it.

Reproduce with fresh output paths (no model requests):

```sh
.venv/bin/python verification/benchmark_timestamp_decoder.py /path/to/game.mov --output /tmp/threading.json --frames 128
.venv/bin/python verification/benchmark_pipeline.py scheduling /path/to/game.mov --output /tmp/scheduling --limit-seconds 64
```

The labeled-game matrix now explicitly sets the timestamp decoder and 720p preview
limit. By default it compares current, prefetch and rolling profiles with grouping
8 and concurrency 4/8. Review its dry-run output before `--execute`. To reproduce a
wider matrix add `--groups 1 4 8 --concurrencies 4 8 16`; for four decoder threads
add `--decoder-threads 4`. CPU/memory arguments still describe cost estimates only.
Use separate staging workers with actual 2-CPU/4-GiB, 2-CPU/8-GiB and 4-CPU/8-GiB
allocations for resource comparisons; do not resize production or increase API
concurrency based solely on the local threading result.

Performance reports add per-phase decode/JPEG time, decoder seek count, prepared
queue/outstanding request peaks, checkpoint blocking, and request-slot utilization.
Utilization measures coordinator-occupied futures, including rate waits, rather
than provider execution time. Linux cgroup current/limit/kernel-peak measurements
include tmpfs and FFmpeg memory; absent counters are omitted. Resume reports label
restored windows and compute throughput from newly processed windows only.

Rollback new runs by setting prefetch to 0, rolling/chronological/upload overlap to
false and decoder type to SLICE in the workflow. Already-pinned runs retain their
execution mode. Workers must be deployed before the web service pins new settings.

### Clip-rendering worker loss (14 October 2026)

The 2.9-GB run finished all analysis and context checks, then its first task attempt
was killed with signal 9 just after clip rendering began. Container memory was
86–87% of the 4-GiB limit before rendering. The immediate Cloud Run retry exited
without claiming work because the previous attempt's ten-minute lease remained
valid; the execution appeared successful while the run stayed running. This is
consistent with a rendering memory spike, although no explicit OOM log was emitted.

The workflow now allocates 8 GiB, retaining two CPUs and model concurrency four,
and aligns WORKER_MEMORY_GIB for cost reporting. Review clip FFmpeg input decoding
as well as output encoding is capped at one thread per render. New claims record
execution identity and attempt number. A later attempt of the same Cloud Run
execution can replace its terminated predecessor before lease expiry; different
executions and duplicate/older attempts remain fenced. Saved legacy runs without
execution metadata still require normal expiry recovery. Recovery keeps original
analysis settings, checkpoints and credit reservations.

For local analysis the CLI defaults to version 2 with individual requests; use
`--group-windows 8` to test grouping.

New application, CLI and local-review runs use candidate consolidation and
nine-frame refinement over one second. The made-basket threshold stays at 0.05,
missed-shot rescue stays off, and the additional rescue allowance is zero.
Hosted deployment settings explicitly match these application defaults.
Saved runs retain their original refinement settings; CLI resumes inherit saved
refinement options when omitted. Use `--no-candidate-refinement` to retain the
original refinement for a new v2 run. See [accuracy validation](../verification/ACCURACY.md).

Compare versions in separate output directories (these commands make paid API requests):

```sh
.venv/bin/python -m basketball_highlights analyze game.mp4 --pipeline-version 1 --output-dir outputs/baseline
.venv/bin/python -m basketball_highlights analyze game.mp4 --pipeline-version 2 --output-dir outputs/optimized
```

Add `--resume` to reuse an interrupted local journal. Normal JSON/CSV and budget
files are exported at completion or failure. During analysis the SQLite journal
is authoritative; do not copy only its JSON exports to resume a live process.
`--csv-only` stores journal and measurements in the existing hidden `.state` folder.

## Measurements

`performance.json` and `processing_summary.json.performance` contain stage timings,
request latency percentiles over a bounded rolling sample, throughput, CPU time,
child-process CPU, peak Python RSS, known model spend and the selected settings.
Stage durations can overlap; do not sum them to infer wall time. RSS does not include
all concurrent FFmpeg processes or Cloud Run's memory-backed filesystem; compare
against Cloud Monitoring's container CPU/memory metrics before resizing workers.

Hosted run details add `performance`, backed by `media_metrics.performance`.
It includes download/preparation/upload durations, checkpoint bytes/compactions,
analysis measurements, cumulative worker time across recoveries, and estimated
compute cost. For newly queued runs `end_to_end_seconds` includes dispatch/recovery
waiting from the start request to saved results. Compute estimates use configured
allocation/rates, exclude free-tier credits, startup overhead, network, storage,
Firestore and web-service costs, and should be reconciled with Cloud Billing.

The offline comparison on 13 October 2026 measured 1,024 synthetic windows:

| Component | Legacy | Version 2 |
| --- | ---: | ---: |
| Local persistence and checkpoint serialization | 9.00 s | 0.24 s |
| First / second half of persistence | 2.34 / 6.67 s | 0.12 / 0.12 s |
| Compressed checkpoint bytes | 683,601 | 154,478 |
| Decode 120 frames from a synthetic 30-second clip | 1.69 s | 0.11 s |

The two decoder paths produced identical JPEGs. This benchmark made no API calls
and excludes R2/Firestore latency. It does **not** establish full-game accuracy or
the 30-minute cloud target. Reproduce it with a fresh directory:

```sh
.venv/bin/python verification/benchmark_pipeline.py overhead --output outputs/overhead-test --requests 1024
```

## Full-game quality and resource matrix

Create a manifest alongside the videos/labels, with at least three human-labeled
games and separate tuning and validation sets. Labels have `timestamp_seconds`
and `game_context` (`game_play` or `warmup_or_break`). Record free throws, buzzer
beaters, pregame, halftime and postgame shooting; avoid model-generated ground truth.

```json
{
  "label_source": "human",
  "games": [
    {"id": "fixed-tuning", "video": "fixed.mp4", "labels_csv": "fixed.csv", "split": "tuning", "camera_style": "fixed"},
    {"id": "moving-validation", "video": "moving.mp4", "labels_csv": "moving.csv", "split": "validation", "camera_style": "moving"},
    {"id": "broadcast-validation", "video": "broadcast.mp4", "labels_csv": "broadcast.csv", "split": "validation", "camera_style": "broadcast"}
  ]
}
```

Preview the matrix before spending; execution requires `--execute`:

```sh
.venv/bin/python verification/benchmark_pipeline.py games /path/to/games.json --output outputs/game-matrix
.venv/bin/python verification/benchmark_pipeline.py games /path/to/games.json --output outputs/game-matrix --execute
```

The matrix tests a legacy baseline plus version 2 with grouping 1/4/8 and concurrency
4/8/16, records spend/timing/precision/recall and the exact additional missed labels.
It writes results after each case. It disables automatic reels but includes review
clip creation. This local matrix excludes cloud download/dispatch/uploads; repeat
selected cases as real hosted executions and use `performance.end_to_end_seconds`
for the final time gate.

On staging, compare actual 1-vCPU/4-GiB, 2-vCPU/4-GiB and 4-vCPU/8-GiB job settings;
update `WORKER_CPU`/`WORKER_MEMORY_GIB` to match each deployment. These variables and
the benchmark's `--cpu`/`--memory-gib` flags only describe allocations for cost
estimates; they do not resize resources. Account for source/proxy files held in
Cloud Run's memory-backed filesystem, especially near the 5-GiB upload limit.

Choose the lowest compute-cost configuration meeting all acceptance criteria:

- No newly missed labeled game baskets, lower precision, or duplicate-event regression.
- Every validation recording near 75 minutes finishes within 30 minutes including
  cloud download, dispatch waiting and uploaded review clips.
- At least 80% lower compute cost than its actual baseline; Decisions around $2 per
  footage hour, with retries and region premiums counted.
- Cancellation, worker replacement, corrupt/missing checkpoint segments, and
  interrupted requests preserve ownership, conservative spend and credit settlement.

Keep grouped requests disabled if human comparisons fail. Original-source sampling
passed source-timestamp and preview checks on real footage; keep monitoring model
quality separately. Do not discard intervals based on motion, a scoreboard
or inferred game breaks to satisfy the speed target.

## Coordinate concurrent games and production rollout

Set BOTH `ANALYSIS_RATE_RPM` and `ANALYSIS_RATE_TPM` to the Decisions limits actually
reported for the project. Version 2 leases small request/token blocks through a
shared Firestore token bucket with 20% headroom; unused leases expire locally and
remain charged against capacity until refill. Its token estimates are conservative
allowances, not billed usage. All test runs using that project must use consistent
limits. Other consumers of the same API project need additional headroom or an
isolated project. With both values zero, distributed coordination is disabled.
Per-worker rate-header pacing, shared Retry-After cooldowns and terminal billing
errors still apply. Read limits from the response headers/project console; do not
assume an account tier's advertised maximum is your project's current allowance.

Validate version 2/grouping 8 on the first ten new production test games.
Monitor time by stage, checkpoint bytes, errors/recoveries, peak container memory,
actual billing and user-reported missing/duplicate baskets. Stop enrolling new games
or revert new-run defaults on a failed gate; let already-pinned executions finish
with their original settings. Grouping is enabled for the user's requested test;
revert direct-source defaults if original decoding, previews or quality regress.
