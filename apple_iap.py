# Subscriptions bought on iOS through Apple In-App Purchase.
#
# The App Store requires digital subscriptions sold inside an iOS app to go
# through Apple, so the iPhone app can't use the Stripe payment sheet. The
# flow mirrors Stripe's: the app only reports a transaction id, we ask Apple
# for the subscription's real state, and write it onto the same
# `subscriptions` row the rest of the backend already reads. The client can
# never grant itself access.
#
# Needs (App Store Connect -> Users and Access -> Integrations -> In-App
# Purchase): APPLE_IAP_ISSUER_ID, APPLE_IAP_KEY_ID and the .p8 key's contents
# in APPLE_IAP_PRIVATE_KEY (or its path in APPLE_IAP_PRIVATE_KEY_PATH), plus
# APPLE_APP_ID (App Store Connect -> App Information -> Apple ID).

import os
from datetime import datetime, timezone
from pathlib import Path

from appstoreserverlibrary.api_client import APIException, AppStoreServerAPIClient
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
)

import subscription_service

BUNDLE_ID = os.getenv("APPLE_BUNDLE_ID", "com.crystalgenie.app")
ISSUER_ID = os.getenv("APPLE_IAP_ISSUER_ID", "")
KEY_ID = os.getenv("APPLE_IAP_KEY_ID", "")
APP_APPLE_ID = int(os.getenv("APPLE_APP_ID", "0") or 0)

_CERT_DIR = Path(__file__).parent / "apple_certs"

# Apple's subscription statuses (App Store Server API `status`).
_ACTIVE, _EXPIRED, _BILLING_RETRY, _GRACE_PERIOD, _REVOKED = 1, 2, 3, 4, 5


class AppleIAPError(Exception):
    """A purchase that can't be applied; the message is safe to show."""


def _private_key() -> bytes:
    inline = os.getenv("APPLE_IAP_PRIVATE_KEY", "")
    if inline:
        # .env files often hold the key on one line with literal "\n"s.
        return inline.replace("\\n", "\n").encode()
    path = os.getenv("APPLE_IAP_PRIVATE_KEY_PATH", "")
    return Path(path).read_bytes() if path else b""


def is_configured() -> bool:
    return bool(ISSUER_ID and KEY_ID and APP_APPLE_ID and _private_key())


_clients: dict[Environment, AppStoreServerAPIClient] = {}
_verifiers: dict[Environment, SignedDataVerifier] = {}


def _client(env: Environment) -> AppStoreServerAPIClient:
    if env not in _clients:
        _clients[env] = AppStoreServerAPIClient(
            _private_key(), KEY_ID, ISSUER_ID, BUNDLE_ID, env
        )
    return _clients[env]


def _verifier(env: Environment) -> SignedDataVerifier:
    if env not in _verifiers:
        roots = [p.read_bytes() for p in sorted(_CERT_DIR.glob("*.cer"))]
        _verifiers[env] = SignedDataVerifier(
            roots, True, env, BUNDLE_ID, APP_APPLE_ID or None
        )
    return _verifiers[env]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _statuses(transaction_id: str):
    """All subscription statuses for the customer behind a transaction.

    Production first: App Review and TestFlight buy in the sandbox, so a
    "not found" there means retry against the sandbox (Apple's own advice).
    """
    try:
        return Environment.PRODUCTION, _client(
            Environment.PRODUCTION
        ).get_all_subscription_statuses(transaction_id)
    except APIException as e:
        if e.http_status_code != 404:
            raise
    return Environment.SANDBOX, _client(
        Environment.SANDBOX
    ).get_all_subscription_statuses(transaction_id)


def _latest(env: Environment, response):
    """(status, transaction, renewal_info) of the subscription that expires
    last — there is only one product today, but don't assume it."""
    best = None
    for group in response.data or []:
        for item in group.lastTransactions or []:
            txn = _verifier(env).verify_and_decode_signed_transaction(
                item.signedTransactionInfo
            )
            renewal = (
                _verifier(env).verify_and_decode_renewal_info(item.signedRenewalInfo)
                if item.signedRenewalInfo
                else None
            )
            if best is None or (txn.expiresDate or 0) > (best[1].expiresDate or 0):
                best = (item.rawStatus, txn, renewal)
    if best is None:
        raise AppleIAPError("No subscription found for this purchase")
    return best


def _row_update(status: int | None, txn, renewal) -> dict:
    """Apple's view of a subscription -> the columns we store."""
    period_end = _ms_to_iso(txn.expiresDate)
    if status == _ACTIVE:
        mapped = "active"
    elif status == _GRACE_PERIOD:
        # Apple is still retrying the charge and the user keeps access until
        # the grace period runs out.
        mapped = "past_due"
        period_end = _ms_to_iso(renewal and renewal.gracePeriodExpiresDate) or period_end
    elif status == _BILLING_RETRY:
        # Retrying without grace: the paid period is over, so no access.
        mapped = "past_due"
    else:  # expired / revoked (refunded)
        mapped = "canceled"

    return {
        "status": mapped,
        "current_period_end": period_end,
        # rawAutoRenewStatus 0 = the user turned off auto-renew.
        "cancel_at_period_end": bool(renewal and renewal.rawAutoRenewStatus == 0),
        "apple_original_transaction_id": txn.originalTransactionId,
        "updated_at": _now().isoformat(),
    }


def verify_purchase(user_id: str, transaction_id: str) -> dict:
    """Check a transaction the app just bought or restored with Apple and
    apply it to the signed-in user's row. Returns the updated row."""
    try:
        env, response = _statuses(transaction_id)
        status, txn, renewal = _latest(env, response)
    except APIException as e:
        raise AppleIAPError(f"App Store rejected the purchase ({e.http_status_code})")
    except VerificationException:
        raise AppleIAPError("The App Store response could not be verified")

    # The app tags each purchase with the account id. A different id means
    # this Apple ID's subscription was bought for another account.
    token = (txn.appAccountToken or "").lower()
    if token and token != user_id.lower():
        raise AppleIAPError(
            "This subscription belongs to a different Crystal Genie account"
        )

    # Restores have no tag to check, so make sure one Apple subscription
    # can't unlock several accounts.
    owner = (
        subscription_service.admin()
        .table("subscriptions")
        .select("user_id")
        .eq("apple_original_transaction_id", txn.originalTransactionId)
        .neq("user_id", user_id)
        .limit(1)
        .execute()
        .data
    )
    if owner:
        raise AppleIAPError(
            "This subscription is already linked to a different Crystal Genie account"
        )

    subscription_service.get_or_create_row(user_id)
    return (
        subscription_service.admin()
        .table("subscriptions")
        .update(_row_update(status, txn, renewal))
        .eq("user_id", user_id)
        .execute()
        .data[0]
    )


def handle_notification(signed_payload: str) -> None:
    """App Store Server Notifications V2: renewals, expiries, refunds and
    auto-renew changes. Re-reads the subscription so we store Apple's current
    state rather than inferring it from the event type."""
    decoded = None
    env = None
    for candidate in (Environment.PRODUCTION, Environment.SANDBOX):
        try:
            decoded = _verifier(candidate).verify_and_decode_notification(
                signed_payload
            )
            env = candidate
            break
        except VerificationException:
            continue
    if decoded is None:
        raise AppleIAPError("Invalid notification signature")

    data = decoded.data
    if data is None or not data.signedTransactionInfo:
        return  # e.g. TEST notifications

    txn = _verifier(env).verify_and_decode_signed_transaction(
        data.signedTransactionInfo
    )
    response = _client(env).get_all_subscription_statuses(txn.originalTransactionId)
    status, latest, renewal = _latest(env, response)

    update = _row_update(status, latest, renewal)
    linked = (
        subscription_service.admin()
        .table("subscriptions")
        .update(update)
        .eq("apple_original_transaction_id", latest.originalTransactionId)
        .execute()
        .data
    )
    # The app's own verify call may never have arrived (killed mid-purchase,
    # offline); the account tag on the purchase still says whose it is.
    if not linked and latest.appAccountToken:
        subscription_service.admin().table("subscriptions").update(update).eq(
            "user_id", latest.appAccountToken.lower()
        ).is_("apple_original_transaction_id", "null").execute()
