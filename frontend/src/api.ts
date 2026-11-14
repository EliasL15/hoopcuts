import type { Auth } from "@firebase/auth";
import type {
  Account,
  PlanCatalog,
  SubscriptionChange,
  UpgradeQuote,
  ClipEvent,
  CreditPack,
  ProductApi,
  ReelResult,
  RunDetail,
  RunSummary,
  RuntimeConfig,
  UploadPhase,
} from "./types";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch("/config");
  if (!response.ok) throw new ApiError("Could not load application configuration.", response.status);
  return response.json() as Promise<RuntimeConfig>;
}

async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}

async function localJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new ApiError(await readError(response), response.status);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function videoDuration(file: File): Promise<number> {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    const url = URL.createObjectURL(file);
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      URL.revokeObjectURL(url);
      Number.isFinite(video.duration) && video.duration > 0
        ? resolve(video.duration)
        : reject(new Error("Could not read the video duration."));
    };
    video.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Choose a video the browser can read."));
    };
    video.src = url;
  });
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function createHostedApi(auth: Auth): ProductApi {
  async function request<T>(url: string, init: RequestInit = {}, retried = false): Promise<T> {
    if (!auth.currentUser) throw new ApiError("Sign in to continue.", 401);
    const token = await auth.currentUser.getIdToken(retried);
    const response = await fetch(url, {
      ...init,
      headers: {
        ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        Authorization: `Bearer ${token}`,
        ...(init.headers || {}),
      },
    });
    if (response.status === 401 && !retried) {
      await auth.currentUser.getIdToken(true);
      return request<T>(url, init, true);
    }
    if (!response.ok) throw new ApiError(await readError(response), response.status);
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }

  return {
    mode: "hosted",
    demoSummary: () => request("/api/demo"),
    demoGame: () => request<RunDetail>("/api/demo/game"),
    listRuns: () => request<RunSummary[]>("/api/runs"),
    getRun: (id) => request<RunDetail>(`/api/runs/${id}`),
    upload: async (file, onPhase) => {
      onPhase("Reading video duration");
      const duration = await videoDuration(file);
      onPhase("Preparing private upload");
      const created = await request<{ id: string; upload_url: string; required_headers: Record<string, string> }>(
        "/api/runs/upload",
        {
          method: "POST",
          body: JSON.stringify({ name: file.name, file_name: file.name, size_bytes: file.size, duration_seconds: duration }),
        },
      );
      onPhase("Uploading video");
      const upload = await fetch(created.upload_url, { method: "PUT", headers: created.required_headers, body: file });
      if (!upload.ok) throw new ApiError(`Storage upload failed (${upload.status}).`, upload.status);
      onPhase("Starting analysis");
      await request(`/api/runs/${created.id}/start`, { method: "POST" });
      return { id: created.id };
    },
    start: (id) => request(`/api/runs/${id}/start`, { method: "POST" }),
    rerun: async (id) => {
      const created = await request<{ id: string }>(`/api/runs/${id}/rerun`, { method: "POST" });
      await request(`/api/runs/${created.id}/start`, { method: "POST" });
      return created;
    },
    setIncluded: (runId, eventId, included) =>
      request<RunDetail>(`/api/runs/${runId}/events/${encodeURIComponent(eventId)}`, {
        method: "PATCH",
        body: JSON.stringify({ included }),
      }),
    buildReel: (id, options) => request<ReelResult>(`/api/runs/${id}/reel`, { method: "POST", body: JSON.stringify(options) }),
    reelPreview: (id) => request<ReelResult>(`/api/runs/${id}/reel/preview`),
    reelDownload: (id) => request<ReelResult>(`/api/runs/${id}/reel/download`),
    uploadSponsor: (id, file) => {
      const body = new FormData();
      body.append("file", file);
      return request<RunDetail>(`/api/runs/${id}/sponsors`, { method: "POST", body });
    },
    removeSponsor: (id, sponsorId) => request<RunDetail>(`/api/runs/${id}/sponsors/${encodeURIComponent(sponsorId)}`, { method: "DELETE" }),
    downloadCsv: async (id, name) => {
      if (!auth.currentUser) throw new ApiError("Sign in to continue.", 401);
      const token = await auth.currentUser.getIdToken();
      const response = await fetch(`/api/runs/${id}/csv`, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new ApiError(await readError(response), response.status);
      saveBlob(await response.blob(), `${name.replace(/[^A-Za-z0-9._-]+/g, "_")}-events.csv`);
    },
    clipUrl: (_runId, clip) => clip.clip_url || "",
    cancel: (id) => request(`/api/runs/${id}/cancel`, { method: "POST" }),
    deleteRun: (id) => request(`/api/runs/${id}`, { method: "DELETE" }),
    plans: async () => {
      const response = await fetch("/api/billing/plans");
      if (!response.ok) throw new ApiError(await readError(response), response.status);
      return response.json() as Promise<PlanCatalog>;
    },
    subscribe: async (plan, interval) => {
      const result = await request<{ url: string }>("/api/billing/subscription/checkout", { method: "POST", body: JSON.stringify({ plan, interval }) });
      window.location.assign(result.url);
    },
    portal: async () => {
      const result = await request<{ url: string }>("/api/billing/portal", { method: "POST" });
      window.location.assign(result.url);
    },
    previewUpgrade: (plan, interval) => request<UpgradeQuote>("/api/billing/subscription/preview", { method: "POST", body: JSON.stringify({ plan, interval }) }),
    changeSubscription: (plan, interval, quoteId) => request<SubscriptionChange>("/api/billing/subscription/change", { method: "POST", body: JSON.stringify({ plan, interval, quote_id: quoteId }) }),
    account: () => request<Account>("/api/account"),
    creditPacks: () => request<CreditPack[]>("/api/billing/packs"),
    checkout: async (packId) => {
      const result = await request<{ url: string }>("/api/billing/checkout", {
        method: "POST",
        body: JSON.stringify({ pack_key: packId }),
      });
      window.location.assign(result.url);
    },
  };
}

export function createLocalApi(): ProductApi {
  return {
    mode: "local",
    listRuns: () => localJson<RunSummary[]>("/api/projects"),
    getRun: (id) => localJson<RunDetail>(`/api/projects/${id}`),
    upload: async (file, onPhase: (phase: UploadPhase) => void) => {
      onPhase("Uploading video");
      const body = new FormData();
      body.append("file", file);
      return localJson<{ id: string }>("/api/projects", { method: "POST", body });
    },
    rerun: (id) => localJson<{ id: string }>(`/api/projects/${id}/rerun`, { method: "POST" }),
    setIncluded: (runId, eventId, included) =>
      localJson<RunDetail>(`/api/projects/${runId}/events/${encodeURIComponent(eventId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ included }),
      }),
    buildReel: (id, options) => localJson<ReelResult>(`/api/projects/${id}/reel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(options) }),
    reelPreview: (id) => localJson<ReelResult>(`/api/projects/${id}/reel/preview`),
    reelDownload: (id) => localJson<ReelResult>(`/api/projects/${id}/reel/download`),
    uploadSponsor: (id, file) => {
      const body = new FormData(); body.append("file", file);
      return localJson<RunDetail>(`/api/projects/${id}/sponsors`, { method: "POST", body });
    },
    removeSponsor: (id, sponsorId) => localJson<RunDetail>(`/api/projects/${id}/sponsors/${encodeURIComponent(sponsorId)}`, { method: "DELETE" }),
    downloadCsv: async (id, name) => {
      const response = await fetch(`/api/projects/${id}/csv`);
      if (!response.ok) throw new ApiError(await readError(response), response.status);
      saveBlob(await response.blob(), `${name.replace(/[^A-Za-z0-9._-]+/g, "_")}-events.csv`);
    },
    clipUrl: (runId, clip: ClipEvent) => `/api/projects/${runId}/clips/${encodeURIComponent(clip.event_id)}`,
  };
}
