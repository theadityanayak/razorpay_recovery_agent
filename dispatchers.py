"""
dispatchers.py — pluggable outbound dispatch layer.

One public entry point per capability:

  create_payment_link(...)  -> Razorpay Payment Links API
  send_message(...)         -> WhatsApp / SMS via Twilio, email via Resend

DRY_RUN contract (the default): when settings.dry_run is True, or when the
credentials for a provider simply aren't configured, nothing is sent and
nothing is charged. The EXACT payload that would have gone out is logged
and returned in DispatchResult.payload_summary, so the pipeline executes
end-to-end and the audit trail shows precisely what live mode would do.

SDKs are imported lazily inside each function, so a missing package can
never break DRY_RUN operation or app startup.

Retry policy (live mode only): bounded exponential backoff applies ONLY to
connection-establishment failures, where the request provably never reached
the provider. A timeout or error status after the request may have arrived
is an uncertain delivery — recorded as failed for manual reconciliation,
never auto-retried. See config.py (dispatch_max_attempts,
dispatch_retry_backoff_seconds).
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from config import settings

logger = logging.getLogger("recovery_dispatch")

# Exception type names (requests- and httpx-based SDKs) that mean the
# request never reached the provider's servers — the ONLY failures that are
# safe to retry automatically. Anything else (a timeout waiting on a
# response, an HTTP error status) means the provider may have accepted the
# request, so retrying could double-send: those surface as failed with the
# error recorded, and recovery is a human reconciliation decision.
_CONNECTION_LEVEL_ERRORS = {
    "ConnectionError", "ConnectError", "ConnectTimeout",
    "NewConnectionError", "NameResolutionError", "MaxRetryError",
}


def _call_with_bounded_retries(fn, *, what: str, extra: dict):
    """Runs fn() with bounded exponential-backoff retries, but ONLY for
    connection-level failures (see _CONNECTION_LEVEL_ERRORS). Records the
    attempt count in `extra` so the dispatch audit row shows what happened.
    Any other exception propagates immediately — never auto-resent."""
    attempts = max(1, settings.dispatch_max_attempts)
    delay = settings.dispatch_retry_backoff_seconds
    for attempt in range(1, attempts + 1):
        try:
            result = fn()
            extra["attempts"] = attempt
            return result
        except Exception as e:
            if type(e).__name__ in _CONNECTION_LEVEL_ERRORS and attempt < attempts:
                logger.warning(
                    "%s: connection failure on attempt %d/%d (%s) — retrying in %.1fs",
                    what, attempt, attempts, e, delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            extra["attempts"] = attempt
            raise


@dataclass
class DispatchResult:
    channel: str                      # whatsapp | sms | email | payment_link | none
    provider: Optional[str]           # razorpay | twilio | resend | None
    status: str                       # sent | dry_run | skipped | failed
    dry_run: bool
    provider_message_id: Optional[str] = None
    payment_link_url: Optional[str] = None
    payment_link_id: Optional[str] = None
    payload_summary: Optional[str] = None
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def summary_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def create_payment_link(
    *,
    amount_inr: float,
    currency: str,
    reference_id: str,
    description: str,
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
    customer_contact: Optional[str] = None,
) -> DispatchResult:
    """Creates a Razorpay Payment Link for one-click retry.

    Razorpay amounts are in paise. reference_id ties the link back to the
    recovery opportunity so a later payment.captured webhook can be
    attributed to this outreach.
    """
    payload = {
        "amount": int(round(amount_inr * 100)),  # paise
        "currency": currency or "INR",
        "reference_id": reference_id,
        "description": description,
        "customer": {
            k: v
            for k, v in {
                "name": customer_name,
                "email": customer_email,
                "contact": customer_contact,
            }.items()
            if v
        },
        "notify": {"sms": False, "email": False},  # we do our own messaging
        "reminder_enable": False,
        "expire_by": None,  # set below when configured
    }
    if settings.payment_link_expire_hours:
        import time

        payload["expire_by"] = int(time.time()) + settings.payment_link_expire_hours * 3600

    if settings.dry_run or not settings.razorpay_live_ready:
        reason = "DRY_RUN=true" if settings.dry_run else "Razorpay credentials not configured"
        link_id = f"plink_dryrun_{uuid.uuid4().hex[:12]}"
        logger.info(
            "[DRY RUN] Razorpay payment_link.create payload=%s (%s)",
            json.dumps(payload),
            reason,
        )
        return DispatchResult(
            channel="payment_link",
            provider="razorpay",
            status="dry_run",
            dry_run=True,
            payment_link_url=f"https://dry-run.example.invalid/l/{uuid.uuid4().hex[:10]}",
            payment_link_id=link_id,
            payload_summary=json.dumps({"would_create_payment_link": payload, "reason": reason}),
        )

    try:
        import razorpay

        client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        retry_extra: dict = {}
        link = _call_with_bounded_retries(
            lambda: client.payment_link.create(payload),
            what="Razorpay payment link create",
            extra=retry_extra,
        )
        return DispatchResult(
            channel="payment_link",
            provider="razorpay",
            status="sent",
            dry_run=False,
            payment_link_url=link.get("short_url"),
            payment_link_id=link.get("id"),
            payload_summary=json.dumps({"created_payment_link": payload}),
        )
    except Exception as e:  # razorpay.errors.* all inherit from Exception
        logger.error("Razorpay payment link creation failed: %s", e)
        return DispatchResult(
            channel="payment_link",
            provider="razorpay",
            status="failed",
            dry_run=False,
            error=f"{type(e).__name__}: {e}",
            payload_summary=json.dumps({"attempted_payment_link": payload}),
        )


def send_message(
    *,
    channel: str,
    body: str,
    to_phone: Optional[str] = None,
    to_email: Optional[str] = None,
    subject: Optional[str] = None,
) -> DispatchResult:
    """Sends the recovery message over the chosen channel.

    whatsapp/sms -> Twilio; email -> Resend. 'none' (e.g. the no_action
    strategy) is a logged no-op, not an error.
    """
    if channel == "none" or not channel:
        return DispatchResult(
            channel="none",
            provider=None,
            status="skipped",
            dry_run=settings.dry_run,
            payload_summary=json.dumps({"reason": "strategy selected no customer contact"}),
        )

    if channel in ("whatsapp", "sms"):
        return _send_twilio(channel=channel, to=to_phone, body=body)
    if channel == "email":
        return _send_resend(to=to_email, subject=subject or "Complete your payment", body=body)

    return DispatchResult(
        channel=channel,
        provider=None,
        status="failed",
        dry_run=settings.dry_run,
        error=f"unsupported channel: {channel}",
    )


def _send_twilio(*, channel: str, to: Optional[str], body: str) -> DispatchResult:
    from_number = (
        settings.twilio_whatsapp_from if channel == "whatsapp" else settings.twilio_sms_from
    )
    payload = {
        "from_": from_number,
        "to": (f"whatsapp:{to}" if channel == "whatsapp" else to),
        "body": body,
    }
    if settings.twilio_status_callback_url:
        payload["status_callback"] = settings.twilio_status_callback_url
    if settings.dry_run or not (settings.twilio_live_ready and from_number and to):
        reason = (
            "DRY_RUN=true"
            if settings.dry_run
            else "Twilio credentials/sender/recipient not configured"
        )
        logger.info("[DRY RUN] Twilio messages.create (%s) payload=%s (%s)", channel, json.dumps(payload), reason)
        return DispatchResult(
            channel=channel,
            provider="twilio",
            status="dry_run",
            dry_run=True,
            provider_message_id=f"SM_dryrun_{uuid.uuid4().hex[:12]}",
            payload_summary=json.dumps({"would_send": payload, "reason": reason}),
        )

    try:
        from twilio.rest import Client

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        retry_extra: dict = {}
        message = _call_with_bounded_retries(
            lambda: client.messages.create(**payload),
            what=f"Twilio {channel} send",
            extra=retry_extra,
        )
        return DispatchResult(
            channel=channel,
            provider="twilio",
            status="sent",
            dry_run=False,
            provider_message_id=message.sid,
            payload_summary=json.dumps({"sent": payload}),
        )
    except Exception as e:
        logger.error("Twilio send failed: %s", e)
        return DispatchResult(
            channel=channel,
            provider="twilio",
            status="failed",
            dry_run=False,
            error=f"{type(e).__name__}: {e}",
            payload_summary=json.dumps({"attempted": payload}),
        )


def _send_resend(*, to: Optional[str], subject: str, body: str) -> DispatchResult:
    payload = {
        "from": settings.resend_from_email,
        "to": [to] if to else [],
        "subject": subject,
        "text": body,
    }
    if settings.dry_run or not (settings.resend_live_ready and to):
        reason = (
            "DRY_RUN=true"
            if settings.dry_run
            else "Resend credentials/sender/recipient not configured"
        )
        logger.info("[DRY RUN] Resend emails.send payload=%s (%s)", json.dumps(payload), reason)
        return DispatchResult(
            channel="email",
            provider="resend",
            status="dry_run",
            dry_run=True,
            provider_message_id=f"email_dryrun_{uuid.uuid4().hex[:12]}",
            payload_summary=json.dumps({"would_send": payload, "reason": reason}),
        )

    try:
        import resend

        resend.api_key = settings.resend_api_key
        retry_extra: dict = {}
        resp = _call_with_bounded_retries(
            lambda: resend.Emails.send(payload),
            what="Resend email send",
            extra=retry_extra,
        )
        return DispatchResult(
            channel="email",
            provider="resend",
            status="sent",
            dry_run=False,
            provider_message_id=(resp or {}).get("id") if isinstance(resp, dict) else getattr(resp, "id", None),
            payload_summary=json.dumps({"sent": payload}),
        )
    except Exception as e:
        logger.error("Resend send failed: %s", e)
        return DispatchResult(
            channel="email",
            provider="resend",
            status="failed",
            dry_run=False,
            error=f"{type(e).__name__}: {e}",
            payload_summary=json.dumps({"attempted": payload}),
        )
