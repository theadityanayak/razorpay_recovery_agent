"""
config.py — centralized configuration for the AI Revenue Recovery Platform.

Every tunable and every secret the app needs lives here, loaded from the
environment (and from a local .env file, which is gitignored and must never
be committed). Copy .env.example to .env and fill in what you have.

Safe-by-default design:
  - DRY_RUN defaults to True. With no live credentials configured the whole
    pipeline still runs end-to-end: payment links, WhatsApp/SMS and email
    are logged (exact payload included) instead of sent.
  - The webhook signature bypass only ever applies to loopback clients, so
    a deployed instance cannot be fed unsigned webhooks from the internet.
  - Mutation endpoints (/simulate/event, /decisions/*/approve|reject) require
    ADMIN_API_KEY when one is configured; without one they only answer
    loopback clients. There is no configuration in which an anonymous
    internet client can mutate state.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    db_path: str = "AI Revenue Recovery Platform (SQLite).db"
    dry_run: bool = True
    app_version: str = "2.1.0"

    # CORS: comma-separated list of origins allowed to call the API.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Admin authentication for mutation endpoints ---
    # When set, /simulate/event, /decisions/{id}/approve and
    # /decisions/{id}/reject require an `X-Admin-API-Key: <key>` header.
    # When unset, those endpoints only respond to loopback clients.
    admin_api_key: Optional[str] = None

    # --- Razorpay ---
    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    razorpay_webhook_secret: Optional[str] = None
    # Local-testing escape hatch: accept unsigned webhooks, but ONLY from
    # loopback clients. Has no effect on requests from any other host.
    razorpay_webhook_bypass: bool = False
    # Merchant that webhook-ingested events are attributed to. If unset and
    # the database holds exactly one merchant, that merchant is used;
    # otherwise webhook ingestion refuses with a clear error.
    default_merchant_id: Optional[str] = None
    payment_link_expire_hours: int = 72

    # --- Twilio (WhatsApp / SMS dispatch) ---
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_whatsapp_from: Optional[str] = None  # e.g. whatsapp:+14155238886
    twilio_sms_from: Optional[str] = None       # e.g. +16505551234
    # Public URL Twilio should POST delivery receipts to (your deployed
    # /webhooks/twilio/status). Left unset, sends carry no status callback.
    twilio_status_callback_url: Optional[str] = None

    # --- Dispatch retry policy (live sends only; DRY_RUN never retries) ---
    # Bounded retries apply ONLY to connection-establishment failures, where
    # the request provably never reached the provider. A timeout or 5xx after
    # the request may have arrived is an uncertain delivery: it is recorded
    # for manual reconciliation, never auto-retried (duplicate-send risk).
    dispatch_max_attempts: int = 3
    dispatch_retry_backoff_seconds: float = 2.0

    # --- Resend (email dispatch) ---
    resend_api_key: Optional[str] = None
    resend_from_email: Optional[str] = None     # e.g. Recovery <recovery@yourdomain.com>

    # --- Gemini (message drafting) ---
    google_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.5-flash"

    # --- Agent behaviour ---
    # Decisions the policy guardrail approves but whose confidence is below
    # this threshold are held for a human instead of auto-dispatching.
    human_review_confidence_threshold: float = 0.5
    # Background worker: poll for undecided opportunities on this interval.
    worker_enabled: bool = True
    worker_poll_interval_seconds: int = 60

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def razorpay_live_ready(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def twilio_live_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def resend_live_ready(self) -> bool:
        return bool(self.resend_api_key and self.resend_from_email)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Module-level convenience handle. Tests monkeypatch attributes on this
# object; runtime code should treat it as read-only after startup.
settings = get_settings()
