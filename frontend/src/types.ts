export type RuntimeMode = "hosted" | "local";

export type FirebasePublicConfig = {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
};

export type RuntimeConfig =
  | { mode: "local" }
  | { mode: "hosted"; configured: boolean; firebase: FirebasePublicConfig };

export type Progress = {
  phase?: string;
  completed?: number;
  total?: number;
  active_requests?: number;
  context_completed?: number;
  context_total?: number;
  stage_completed?: number;
  stage_total?: number;
  clips_completed?: number;
  clips_total?: number;
  uploads_completed?: number;
  uploads_total?: number;
  spend_usd?: number;
  estimated_compute_usd?: number;
};

export type ClipEvent = {
  event_id: string;
  timestamp_seconds: number;
  classification: string;
  shot_type: string;
  model_confidence: number | null;
  needs_review: boolean;
  game_context?: "game_play" | "warmup_or_break" | "uncertain" | null;
  included: boolean;
  clip_url?: string | null;
  clip_path?: string | null;
};

export type RunSummary = {
  is_admin?: boolean;
  id: string;
  name: string;
  status: string;
  created_at?: string | null;
  progress?: Progress;
  error?: string | null;
  spend_usd?: number;
  performance?: { estimated_compute_usd?: number } | null;
  event_count: number;
  included_count: number;
  media_expires_at?: string | null;
  media_available?: boolean;
  reel_job?: { id?: string; status?: "queued" | "rendering" | "ready" | "failed"; combine_overlaps?: boolean; exclude_free_throws?: boolean; error?: string | null };
  latest_reel?: { count: number; format: "vertical" | "landscape"; combine_overlaps?: boolean; exclude_free_throws?: boolean; generated_at?: string | null } | null;
  entitlements?: Entitlements;
  has_saved_csv?: boolean;
  reels_used?: number;
  analysis_config?: { stride_seconds?: number };
};

export type RunDetail = RunSummary & {
  events: ClipEvent[];
  example_reel_url?: string;
  sponsors?: { id: string; name: string; url: string | null }[];
};

export type PaidPlan = "pro" | "business";
export type BillingInterval = "monthly" | "annual";
export type UpgradeQuote = { quote_id: string; plan: PaidPlan; interval: BillingInterval; amount_due: number; currency: string; extra_minutes: number; allowance_end: string; expires_at: string };
export type SubscriptionChange = { scheduled_change: ScheduledChange | null; status?: "payment_required" | "confirming" | "upgraded"; payment_url?: string | null };
export type ScheduledChange = { plan: PaidPlan; interval: BillingInterval; effective_at: string };
export type Entitlements = { plan: "free" | PaidPlan; csv: boolean; sponsors: boolean; renders: number | null; max_minutes: number };
export type PlanCatalog = { enabled: boolean; currency: string; plans: { id: "free" | PaidPlan; name: string; monthly: number; annual: number; minutes: number; renders: number; csv: boolean; sponsors: boolean }[] };

export type Account = {
  is_admin?: boolean;
  plan?: "free" | PaidPlan;
  subscription_plan?: PaidPlan;
  status?: string;
  interval?: BillingInterval;
  renewal_at?: string | null;
  allowance_end?: string | null;
  grace_until?: string | null;
  cancel_at_period_end?: boolean;
  scheduled_change?: ScheduledChange | null;
  pending_upgrade?: { plan: PaidPlan; payment_url?: string | null; expires_at: string } | null;
  can_manage?: boolean;
  can_top_up?: boolean;
  has_subscription?: boolean;
  subscriptions_enabled?: boolean;
  subscription_minutes?: number;
  free_minutes?: number;
  prepaid_minutes?: number;
  legacy_minutes?: number;
  entitlements?: Entitlements;
  available_minutes: number;
  reserved_minutes: number;
};

export type CreditPack = {
  id: string;
  minutes: number;
};

export type ReelOptions = { format: "vertical" | "landscape"; sponsor_ids: string[]; combine_overlaps: boolean; exclude_free_throws?: boolean };

export type ReelResult = {
  status?: string;
  count: number;
  url: string | null;
};

export type UploadPhase =
  | "Reading video duration"
  | "Preparing private upload"
  | "Uploading video"
  | "Starting analysis";

export type ProductApi = {
  mode: RuntimeMode;
  demoSummary?: () => Promise<{ available: boolean; name?: string; event_count?: number }>;
  demoGame?: () => Promise<RunDetail>;
  listRuns: () => Promise<RunSummary[]>;
  getRun: (id: string) => Promise<RunDetail>;
  upload: (file: File, onPhase: (phase: UploadPhase) => void) => Promise<{ id: string }>;
  start?: (id: string) => Promise<void>;
  rerun: (id: string) => Promise<{ id: string }>;
  setIncluded: (runId: string, eventId: string, included: boolean) => Promise<RunDetail>;
  buildReel: (id: string, options?: ReelOptions) => Promise<ReelResult>;
  reelPreview?: (id: string) => Promise<ReelResult>;
  reelDownload?: (id: string) => Promise<ReelResult>;
  uploadSponsor?: (id: string, file: File) => Promise<RunDetail>;
  removeSponsor?: (id: string, sponsorId: string) => Promise<RunDetail>;
  downloadCsv: (id: string, name: string) => Promise<void>;
  clipUrl: (runId: string, clip: ClipEvent) => string;
  cancel?: (id: string) => Promise<void>;
  deleteRun?: (id: string) => Promise<void>;
  account?: () => Promise<Account>;
  creditPacks?: () => Promise<CreditPack[]>;
  checkout?: (packId: string) => Promise<void>;
  plans?: () => Promise<PlanCatalog>;
  subscribe?: (plan: PaidPlan, interval: BillingInterval) => Promise<void>;
  portal?: () => Promise<void>;
  previewUpgrade?: (plan: PaidPlan, interval: BillingInterval) => Promise<UpgradeQuote>;
  changeSubscription?: (plan: PaidPlan, interval: BillingInterval, quoteId?: string) => Promise<SubscriptionChange>;
};
