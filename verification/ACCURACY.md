# Accuracy experiments

New hosted runs use candidate consolidation and nine-frame refinement, retaining
4-fps sampling, eight-window grouping, and four request workers. The recovery
threshold remains 0.05, missed-shot rescue is off, and extra rescue requests are
capped at zero. Application, CLI and new local-review projects use the same
refinement defaults; deployment overrides are no longer required to enable them.
Neither unit tests nor fixed-response replays establish real detection accuracy.

## Candidate refinement

Pipeline v2 can consolidate overlapping uncertain windows into one refinement
request. A cluster must share sampled evidence and span at most 0.5 seconds from
its first window center; disjoint evidence and longer chains remain separate.
The highest made-basket probability chooses the refinement center. Source-window
identities are saved with the result in the same journal transaction, so context
checks and resumed analyses retain their identities without repeating completed
requests. Uncertain refinements preserve the original review candidates.

Five refinement frames span 0.5 seconds, matching the existing refinement density.
Nine frames span one second at the same 0.125-second spacing. Sampling clamps to
the final source frame and removes duplicate timestamps near video boundaries.

Baseline uncertain candidates at probability >=0.05 are admitted first. Optional
lower-threshold and missed-shot rescues can be enabled with an explicit allowance
(the example below uses 16 additional candidate requests per run), ranked by
probability. The default allowance is zero. The cap counts candidates, not retry
attempts; the existing four-attempt reservation and 90% refinement budget ceiling
still apply. This request allowance is **not a guarantee of <=10% additional wall
time**. The benchmark must establish that limit on representative workers.

```sh
.venv/bin/python -m basketball_highlights analyze /path/to/game.mov \
  --output-dir /tmp/game-accuracy --pipeline-version 2 --group-windows 8 \
  --timestamp-decoder --prefetch-requests 4 --chronological-evidence \
  --decoder-thread-type AUTO --decoder-threads 2 --preview-max-dimension 720 \
  --candidate-refinement --refinement-frames 9 \
  --rescue-threshold .02 --rescue-missed --rescue-max-requests 16
```

Resume with the original options and `--resume`. Omitted CLI refinement options
inherit the saved signature, including old runs without candidate refinement.
Explicit incompatible options reject recovery. Hosted runs retain their pinned
settings. To enable more aggressive offline experiments, use these variables:

```dotenv
ANALYSIS_CANDIDATE_REFINEMENT=true
ANALYSIS_REFINEMENT_FRAMES=9
ANALYSIS_RESCUE_THRESHOLD=0.02
ANALYSIS_RESCUE_MISSED=true
ANALYSIS_RESCUE_MAX_REQUESTS=16
```

The application and deployment workflow default to consolidation and wider
evidence. This is not a measured accuracy or runtime improvement.
Lower thresholds and missed-shot rescue remain offline experiments. To roll back
new hosted runs, set HOSTED_ANALYSIS_CANDIDATE_REFINEMENT to false and
HOSTED_ANALYSIS_REFINEMENT_FRAMES to 5 in the workflow and redeploy. Existing queued
and active runs keep their pinned configuration.
For a new CLI run using the original refinement, pass `--no-candidate-refinement`.
Selecting pipeline v1 also retains its original refinement by default.

## Human labels and stage reports

Use at least three full games with distinct tuning and validation recordings.
Include different camera styles and difficult cases: distant hoops, occlusion,
free throws, buzzer-beaters, warm-ups, and closely spaced baskets. Label every
made basket, including practice makes, with its timestamp and context:

```csv
timestamp_seconds,game_context
45.25,game_play
73.5,game_play
890.0,warmup_or_break
```

Missed shots are negative examples in the footage; do not label them as made
baskets. Use humans, not model outputs, to establish ground truth. Keep manual
review edits out of the event files used for automatic-selection measurements.

```sh
.venv/bin/python -m basketball_highlights evaluate-accuracy \
  /tmp/game-accuracy/run_state.json /tmp/game-accuracy/events.json labels.csv
```

The report distinguishes all candidates before context, confirmed baskets before
context, confirmed baskets retained after context, and automatically selected
baskets. Each includes precision, recall, F1 and timestamp error. Context metrics
separately count genuine baskets excluded as practice or left for review.
The existing three-second one-to-one timestamp tolerance remains the default.

## Full-game comparison

Create a manifest with `label_source: "human"` and at least three `games` entries:
each needs `id`, `video`, `labels_csv`, `camera_style`, and `split` (`tuning` or
`validation`). Paths are relative to the manifest. The same recording must not
appear in both splits. Review the zero-request dry run first:

```sh
.venv/bin/python verification/benchmark_pipeline.py games manifest.json \
  --output /tmp/accuracy-matrix --profiles hosted --groups 8 --concurrencies 4 \
  --memory-gib 8 --accuracy-profiles baseline candidates wider rescue rescue-missed combined
```

Add `--execute` to make paid model requests. The profiles isolate clustering,
wider evidence, lower threshold, lower threshold plus missed-shot recovery, and
their combination. `hosted` uses prefetch=4, AUTO/2 decoding and chronological
evidence; rolling scheduling stays off. Every profile samples the original
source. Use actual hosted-equivalent worker resources for timing: CPU/memory
arguments describe cost estimates and do not allocate resources. Local matrix
elapsed time excludes hosted dispatch, download and upload; production validation
must also compare `end_to_end_seconds` and cumulative worker time.

`matrix.json` records stage metrics, duplicates, spend, timing ratio, lost labels,
and acceptance. A per-game experiment passes only with improved automatic-selection
recall, no lost baseline matches, no lower confirmed/selected precision, no more
duplicates, a completed run, and elapsed time <=1.10 times baseline. Choose settings
using tuning games, then evaluate once on held-out games. `accuracy-summary.json`
also evaluates each split: all games must pass quality and timing, with an overall
gain in automatically selected baskets. A game with perfect baseline recall can
therefore retain parity while another improves. These flags are evidence, not an
automatic production promotion; review all held-out results.

Do not add a full-game detector or increase global resolution/frame rate before
this comparison. If misses remain because the ball/net is unreadable at 768px,
inspect the corresponding original-resolution frames and evaluate hoop crops in
a separate experiment. Reliable crop localization and crop retention through
camera pans have not been established; no crop heuristic is enabled here.
