# Subscriptions: 7-day free trial, then $3.69/month via Stripe.
#
# The trial is tracked in our own `subscriptions` table (see subscriptions.sql)
# and starts when the account is created. Paid status comes from Stripe and is
# written back by the webhook, so the client can never grant itself access.

import os
from datetime import datetime, timezone

import stripe
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
# Service role bypasses RLS. Required to write subscription rows — the webhook
# has no user session, and users only ever get SELECT on their own row.
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

TRIAL_DAYS = int(os.getenv("SUBSCRIPTION_TRIAL_DAYS", "7"))
PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PRICE_LABEL = os.getenv("SUBSCRIPTION_PRICE_LABEL", "$3.69/month")

_admin: Client | None = None


def is_configured() -> bool:
    return bool(SUPABASE_SERVICE_KEY and PRICE_ID and stripe.api_key)


def admin() -> Client:
    """Service-role Supabase client (bypasses RLS). Created on first use."""
    global _admin
    if not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_KEY is not set — subscriptions cannot be written"
        )
    if _admin is None:
        _admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _admin


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _epoch(seconds: int | None) -> str | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def get_or_create_row(user_id: str) -> dict:
    """The user's subscription row, starting a trial if they somehow have none
    (e.g. the account predates the signup trigger)."""
    rows = (
        admin()
        .table("subscriptions")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
    )
    if rows:
        return rows[0]

    trial_end = _now().timestamp() + TRIAL_DAYS * 86400
    return (
        admin()
        .table("subscriptions")
        .insert(
            {
                "user_id": user_id,
                "status": "trialing",
                "trial_ends_at": _epoch(int(trial_end)),
            }
        )
        .execute()
        .data[0]
    )


def has_access(row: dict) -> bool:
    """Mirrors has_app_access() in subscriptions.sql.

    `past_due` still counts: Stripe retries a failed charge for days, and
    locking someone out mid-retry over a card blip is worse than a short grace.
    """
    status = row.get("status")
    if status == "trialing":
        end = _parse(row.get("trial_ends_at"))
        return end is not None and end > _now()
    if status in ("active", "past_due"):
        end = _parse(row.get("current_period_end"))
        return end is None or end > _now()
    return False


def status_payload(row: dict) -> dict:
    trial_end = _parse(row.get("trial_ends_at"))
    days_left = 0
    if trial_end:
        remaining = (trial_end - _now()).total_seconds()
        days_left = max(0, -(-remaining // 86400))  # ceil, so today counts

    return {
        "status": row.get("status", "trialing"),
        "hasAccess": has_access(row),
        "trialEndsAt": row.get("trial_ends_at"),
        "trialDaysLeft": int(days_left),
        "currentPeriodEnd": row.get("current_period_end"),
        "cancelAtPeriodEnd": bool(row.get("cancel_at_period_end")),
        "priceLabel": PRICE_LABEL,
    }


def _ensure_customer(user, row: dict) -> str:
    """Reuse the user's Stripe customer, creating one on first subscribe."""
    existing = row.get("stripe_customer_id")
    if existing:
        return existing

    customer = stripe.Customer.create(
        email=user.email or None,
        metadata={"supabase_user_id": user.id},
    )
    admin().table("subscriptions").update(
        {"stripe_customer_id": customer.id, "updated_at": _now().isoformat()}
    ).eq("user_id", user.id).execute()
    return customer.id


def _client_secret(invoice) -> str | None:
    """Pull the payment client secret out of a subscription's first invoice.

    Stripe moved this between API versions: newer accounts expose
    `confirmation_secret`, older ones a `payment_intent`. Try both so this
    keeps working across a version bump.
    """
    if invoice is None:
        return None
    secret = (invoice.get("confirmation_secret") or {}).get("client_secret")
    if secret:
        return secret
    intent = invoice.get("payment_intent")
    if isinstance(intent, dict):
        return intent.get("client_secret")
    if isinstance(intent, str):
        return stripe.PaymentIntent.retrieve(intent).client_secret
    return None


def start_subscription(user, row: dict) -> dict:
    """Create (or reuse) an incomplete subscription and return what the app
    needs to confirm the first payment in the Stripe payment sheet."""
    customer_id = _ensure_customer(user, row)

    subscription = stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": PRICE_ID}],
        payment_behavior="default_incomplete",
        payment_settings={"save_default_payment_method": "on_subscription"},
        expand=["latest_invoice.confirmation_secret"],
        metadata={"supabase_user_id": user.id},
    )

    secret = _client_secret(subscription.get("latest_invoice"))
    if not secret:
        raise RuntimeError(
            "Stripe did not return a client secret for the first invoice"
        )

    admin().table("subscriptions").update(
        {
            "stripe_subscription_id": subscription.id,
            "updated_at": _now().isoformat(),
        }
    ).eq("user_id", user.id).execute()

    return {"clientSecret": secret, "subscriptionId": subscription.id}


def _period_end(subscription) -> str | None:
    """Renewal date. Lives on the subscription in older API versions and on
    the subscription item in newer ones."""
    end = subscription.get("current_period_end")
    if end:
        return _epoch(end)
    items = (subscription.get("items") or {}).get("data") or []
    if items:
        return _epoch(items[0].get("current_period_end"))
    return None


# Stripe's statuses -> the four we store.
_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "canceled": "canceled",
    "incomplete_expired": "canceled",
}


def apply_subscription(subscription) -> None:
    """Write a Stripe subscription's state onto the matching user's row."""
    stripe_status = subscription.get("status")
    # `incomplete` = first payment not confirmed yet; leave the row alone so an
    # abandoned payment sheet doesn't end someone's trial early.
    mapped = _STATUS_MAP.get(stripe_status)
    if mapped is None:
        return

    user_id = (subscription.get("metadata") or {}).get("supabase_user_id")
    query = admin().table("subscriptions").update(
        {
            "status": mapped,
            "current_period_end": _period_end(subscription),
            "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
            "stripe_subscription_id": subscription.get("id"),
            "updated_at": _now().isoformat(),
        }
    )
    if user_id:
        query.eq("user_id", user_id).execute()
    else:
        # Fall back to the customer when metadata is missing (e.g. a
        # subscription created from the Stripe dashboard).
        query.eq("stripe_customer_id", subscription.get("customer")).execute()


def cancel_subscription(row: dict) -> dict:
    """Cancel at period end — the user keeps access until they've used up
    what they already paid for."""
    subscription_id = row.get("stripe_subscription_id")
    if not subscription_id:
        raise RuntimeError("No active subscription to cancel")

    subscription = stripe.Subscription.modify(
        subscription_id, cancel_at_period_end=True
    )
    apply_subscription(subscription)
    return {"cancelAtPeriodEnd": True, "currentPeriodEnd": _period_end(subscription)}
