from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class Settings:
    firebase_project_id: str = os.getenv("FIREBASE_PROJECT_ID", os.getenv("GCP_PROJECT_ID", ""))
    firebase_api_key: str = os.getenv("FIREBASE_API_KEY", "")
    firebase_auth_domain: str = os.getenv("FIREBASE_AUTH_DOMAIN", "")
    firebase_app_id: str = os.getenv("FIREBASE_APP_ID", "")
    r2_account_id: str = os.getenv("R2_ACCOUNT_ID", "")
    r2_access_key_id: str = os.getenv("R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    r2_bucket: str = os.getenv("R2_BUCKET", "")
    r2_endpoint: str = os.getenv("R2_ENDPOINT", "")
    stripe_mode: str = os.getenv("STRIPE_MODE", "test").lower()
    stripe_live_enabled: bool = os.getenv("STRIPE_LIVE_ENABLED", "false").lower() == "true"
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe_price_60_minutes: str = os.getenv("STRIPE_PRICE_60_MINUTES", "")
    stripe_credit_packs: str = os.getenv("STRIPE_CREDIT_PACKS", "")
    app_base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000")
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024**3)))
    max_reserved_minutes: int = int(os.getenv("MAX_RESERVED_MINUTES", "180"))
    model_budget_usd: float = float(os.getenv("MODEL_BUDGET_USD", "5"))
    analysis_concurrency: int = int(os.getenv("ANALYSIS_CONCURRENCY", "4"))
    analysis_pipeline_version: int = int(os.getenv("ANALYSIS_PIPELINE_VERSION", "2"))
    analysis_group_windows: int = int(os.getenv("ANALYSIS_GROUP_WINDOWS", "1"))
    analysis_candidate_refinement: bool = os.getenv("ANALYSIS_CANDIDATE_REFINEMENT", "true").lower() == "true"
    analysis_refinement_frames: int = int(os.getenv("ANALYSIS_REFINEMENT_FRAMES", "9"))
    analysis_rescue_threshold: float = float(os.getenv("ANALYSIS_RESCUE_THRESHOLD", "0.05"))
    analysis_rescue_missed: bool = os.getenv("ANALYSIS_RESCUE_MISSED", "false").lower() == "true"
    analysis_rescue_max_requests: int = int(os.getenv("ANALYSIS_RESCUE_MAX_REQUESTS", "0"))
    analysis_direct_source: bool = os.getenv("ANALYSIS_DIRECT_SOURCE", "false").lower() == "true"
    analysis_timestamp_decoder: bool = os.getenv("ANALYSIS_TIMESTAMP_DECODER", "true").lower() == "true"
    analysis_prefetch_requests: int = int(os.getenv("ANALYSIS_PREFETCH_REQUESTS", "4"))
    analysis_rolling_scheduler: bool = os.getenv("ANALYSIS_ROLLING_SCHEDULER", "false").lower() == "true"
    analysis_chronological_evidence: bool = os.getenv("ANALYSIS_CHRONOLOGICAL_EVIDENCE", "true").lower() == "true"
    analysis_decoder_thread_type: str = os.getenv("ANALYSIS_DECODER_THREAD_TYPE", "AUTO")
    analysis_decoder_threads: int = int(os.getenv("ANALYSIS_DECODER_THREADS", "2"))
    analysis_overlap_preview_uploads: bool = os.getenv("ANALYSIS_OVERLAP_PREVIEW_UPLOADS", "true").lower() == "true"
    analysis_rate_rpm: int = int(os.getenv("ANALYSIS_RATE_RPM", "0"))
    analysis_rate_tpm: int = int(os.getenv("ANALYSIS_RATE_TPM", "0"))
    worker_cpu: float = float(os.getenv("WORKER_CPU", "2"))
    worker_memory_gib: float = float(os.getenv("WORKER_MEMORY_GIB", "4"))
    worker_cpu_usd_per_second: float = float(os.getenv("WORKER_CPU_USD_PER_SECOND", "0.000018"))
    worker_memory_usd_per_second: float = float(os.getenv("WORKER_MEMORY_USD_PER_SECOND", "0.000002"))
    gcp_project_id: str = os.getenv("GCP_PROJECT_ID", os.getenv("FIREBASE_PROJECT_ID", ""))
    gcp_region: str = os.getenv("GCP_REGION", "europe-west1")
    gcp_run_job: str = os.getenv("GCP_RUN_JOB", "highlights-worker")
    gcp_tasks_location: str = os.getenv("GCP_TASKS_LOCATION", "europe-west1")
    gcp_tasks_queue: str = os.getenv("GCP_TASKS_QUEUE", "highlights-dispatch")
    dispatch_url: str = os.getenv("DISPATCH_URL", "")
    dispatch_service_account: str = os.getenv("DISPATCH_SERVICE_ACCOUNT", "")
    internal_audience: str = os.getenv("INTERNAL_AUDIENCE", "")

    billing_migration_mode: bool = os.getenv("BILLING_MIGRATION_MODE", "false").lower() == "true"
    subscriptions_enabled: bool = os.getenv("SUBSCRIPTIONS_ENABLED", "false").lower() == "true"
    stripe_price_pro_monthly: str = os.getenv("STRIPE_PRICE_PRO_MONTHLY", "")
    stripe_price_pro_annual: str = os.getenv("STRIPE_PRICE_PRO_ANNUAL", "")
    stripe_price_business_monthly: str = os.getenv("STRIPE_PRICE_BUSINESS_MONTHLY", "")
    stripe_price_business_annual: str = os.getenv("STRIPE_PRICE_BUSINESS_ANNUAL", "")
    stripe_price_topup: str = os.getenv("STRIPE_PRICE_TOPUP", "")
    stripe_portal_configuration_id: str = os.getenv("STRIPE_PORTAL_CONFIGURATION_ID", "")

    media_cleanup_enabled: bool = os.getenv("MEDIA_CLEANUP_ENABLED", "false").lower() == "true"

    @property
    def configured_auth(self) -> bool:
        return bool(self.firebase_project_id and self.firebase_api_key and self.firebase_app_id)

    @property
    def configured_storage(self) -> bool:
        return all((self.r2_access_key_id, self.r2_secret_access_key, self.r2_bucket, self.r2_endpoint))


def analysis_settings(settings: Settings) -> dict:
    from types import SimpleNamespace
    from basketball_highlights.refinement import settings as refinement_settings
    refinement = refinement_settings(SimpleNamespace(
        candidate_refinement=settings.analysis_candidate_refinement,
        refinement_frames=settings.analysis_refinement_frames,
        rescue_threshold=settings.analysis_rescue_threshold,
        rescue_missed=settings.analysis_rescue_missed,
        rescue_max_requests=settings.analysis_rescue_max_requests))
    if refinement["candidate_refinement"] and settings.analysis_pipeline_version != 2:
        raise ValueError("Candidate refinement requires pipeline version 2")
    if settings.analysis_pipeline_version not in (1, 2) or settings.analysis_group_windows not in (1, 4, 8):
        raise ValueError("Invalid analysis pipeline settings")
    if settings.analysis_pipeline_version == 1 and settings.analysis_group_windows != 1:
        raise ValueError("Grouped requests require pipeline version 2")
    if not 1 <= settings.analysis_concurrency <= 16:
        raise ValueError("Analysis concurrency must be 1-16")
    if (not 0 <= settings.analysis_prefetch_requests <= 16
            or settings.analysis_decoder_thread_type not in ("SLICE", "AUTO", "FRAME")
            or settings.analysis_decoder_threads not in (1, 2, 4)
            or (settings.analysis_rolling_scheduler and not settings.analysis_prefetch_requests)):
        raise ValueError("Invalid analysis execution settings")
    config = {"sampling_fps": 4.0, "window_seconds": 0.75, "stride_seconds": 0.25,
            "max_image_dimension": 768, "jpeg_quality": 85, "pre_padding": 5.0,
            "post_padding": 5.0, "budget_usd": settings.model_budget_usd,
            "max_concurrency": settings.analysis_concurrency,
            "pipeline_version": settings.analysis_pipeline_version, "group_windows": settings.analysis_group_windows,
            "direct_source": settings.analysis_direct_source,
            "timestamp_decoder": settings.analysis_timestamp_decoder,
            "rate_rpm": settings.analysis_rate_rpm, "rate_tpm": settings.analysis_rate_tpm}
    if settings.analysis_pipeline_version == 2:
        config.update(prefetch_requests=settings.analysis_prefetch_requests,
            rolling_scheduler=settings.analysis_rolling_scheduler,
            chronological_evidence=settings.analysis_chronological_evidence,
            decoder_thread_type=settings.analysis_decoder_thread_type,
            decoder_threads=settings.analysis_decoder_threads,
            overlap_preview_uploads=settings.analysis_overlap_preview_uploads)
    if refinement["candidate_refinement"]:
        config.update(refinement)
    return config


settings = Settings()
