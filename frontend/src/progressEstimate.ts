export type EstimateSample = { time: number; completed: number };
export type EstimateState = { phase: string; total: number; samples: EstimateSample[] };

export function observeProgress(state: EstimateState | null, phase: string, completed: number, total: number, time: number): EstimateState {
  if (!state || state.phase !== phase || state.total !== total || completed < state.samples.at(-1)!.completed) {
    return { phase, total, samples: [{ time, completed }] };
  }
  if (completed === state.samples.at(-1)!.completed) return state;
  return { ...state, samples: [...state.samples.filter(sample => time - sample.time <= 120), { time, completed }].slice(-32) };
}

export function estimateRemaining(state: EstimateState | null, time: number): number | null {
  if (!state || state.samples.length < 2) return null;
  const first = state.samples[0];
  const last = state.samples.at(-1)!;
  const elapsed = last.time - first.time;
  const advanced = last.completed - first.completed;
  const minimum = ["checking_game_context", "refining_candidates", "creating_clips", "uploading_clips"].includes(state.phase) ? 2 : 20;
  if (elapsed < 15 || advanced < minimum || time - last.time > 60 || state.total <= last.completed) return null;
  const seconds = (state.total - last.completed) * elapsed / advanced;
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}

export function formatEstimate(seconds: number): string {
  if (seconds < 60) return "Less than a minute";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `About ${minutes} minutes`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return `About ${hours} ${hours === 1 ? "hour" : "hours"}${remainder ? ` ${remainder} ${remainder === 1 ? "minute" : "minutes"}` : ""}`;
}
