import type { ClipEvent, RunSummary } from "./types";

export function defaultFreeThrowFilter(run: Pick<RunSummary, "reel_job" | "latest_reel">): boolean {
  return run.reel_job?.exclude_free_throws ?? run.latest_reel?.exclude_free_throws ?? true;
}

export function includedInReel(clip: Pick<ClipEvent, "included" | "shot_type">, excludeFreeThrows: boolean): boolean {
  return clip.included && !(excludeFreeThrows && clip.shot_type === "free_throw");
}
