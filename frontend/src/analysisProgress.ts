import type { Progress } from "./types";

export const analysisPasses = ["Scan footage", "Refine moments", "Check game context", "Prepare previews"];

export function analysisStage(status: string, progress: Progress = {}) {
  let phase = status === "queued" ? "queued" : progress.phase || "analyzing";
  if (phase === "analyzing" && progress.total && (progress.completed || 0) >= progress.total) phase = "finishing_analysis";
  const stages: Record<string, { title: string; description: string; step: number; unit?: string; next?: string }> = {
    queued: { title: "Waiting to start", description: "Your recording is queued. Analysis will begin when a worker is available.", step: -1 },
    starting: { title: "Preparing your video", description: "Opening the recording and checking that it is ready for analysis.", step: -1 },
    downloading_video: { title: "Loading your recording", description: "Loading the original video for processing. Larger recordings can take longer.", step: -1 },
    restoring_progress: { title: "Resuming saved progress", description: "Restoring the completed checks so analysis can continue where it left off.", step: -1 },
    preparing_video: { title: "Preparing your video", description: "Checking the recording and preparing it for analysis.", step: -1 },
    analyzing: { title: "Scanning the footage", description: "Looking through the full recording for possible made baskets.", step: 0, unit: "sections scanned", next: "Closer checks, game context and preview preparation follow." },
    refining_candidates: { title: "Taking a closer look", description: "Checking extra frames around moments where the shot outcome is unclear.", step: 1, unit: "moments checked", next: "Game-context checks and preview preparation follow." },
    checking_game_context: { title: "Checking game context", description: "Checking the surrounding play to filter out warm-up shooting. Game free throws and buzzer-beaters stay eligible.", step: 2, unit: "moments checked", next: "Preview preparation follows." },
    creating_clips: { title: "Preparing your previews", description: "Creating a video preview for each detected moment so you can review your selections.", step: 3, unit: "previews prepared", next: "Your results will open after the previews are saved." },
    uploading_clips: { title: "Saving your previews", description: "Uploading the finished previews to your private game library.", step: 3, unit: "previews uploaded", next: "Your results will open after the previews are saved." },
    saving_results: { title: "Saving your results", description: "Saving the detected moments and your preview links. Your review will open when everything is ready.", step: 3 },
    complete: { title: "Saving your results", description: "The checks are finished. Saving your previews and review results.", step: 3 },
    finishing_analysis: { title: "Finishing the analysis", description: "The footage scan is complete. Remaining moment checks and preview preparation are still in progress.", step: -1 },
  };
  const stage = stages[phase] || stages.finishing_analysis;
  let completed: number | undefined;
  let total: number | undefined;
  if (phase === "analyzing") {
    completed = progress.completed;
    total = progress.total;
  } else if (phase === "checking_game_context") {
    completed = progress.stage_completed ?? progress.context_completed;
    total = progress.stage_total ?? progress.context_total;
  } else if (phase === "refining_candidates") {
    completed = progress.stage_completed;
    total = progress.stage_total;
  } else if (phase === "creating_clips") {
    completed = progress.clips_completed ?? progress.stage_completed;
    total = progress.clips_total ?? progress.stage_total;
  } else if (phase === "uploading_clips") {
    completed = progress.uploads_completed ?? progress.stage_completed;
    total = progress.uploads_total ?? progress.stage_total;
  }
  const hasCount = completed != null && total != null && Number.isFinite(completed) && Number.isFinite(total) && total > 0;
  return { ...stage, phase, completed: hasCount ? Math.max(0, Math.min(completed!, total!)) : 0,
    total: hasCount ? total! : 0, hasCount };
}
