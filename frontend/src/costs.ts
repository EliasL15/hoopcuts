import type { RunSummary } from "./types";

export function cloudComputeCost(run: RunSummary): number | null {
  const active = ["running", "queued", "cancel_requested"].includes(run.status);
  const saved = run.performance?.estimated_compute_usd;
  const value = active ? run.progress?.estimated_compute_usd ?? saved : saved;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}
