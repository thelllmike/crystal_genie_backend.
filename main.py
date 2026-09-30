# Crystal Genie backend — YOLO11 detection + Supabase lookup
#
# Run with:  uvicorn main:app --host 0.0.0.0 --port 8000

import io
import os

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from supabase import Client, create_client

load_dotenv()

# Both need the .env values loaded first.
import email_service  # noqa: E402
import model_manager  # noqa: E402
import training_api  # noqa: E402
import subscription_service  # noqa: E402

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.25"))
# Classifiers always name *something*, so they need a stricter bar than boxes.
CLS_CONF_THRESHOLD = float(os.getenv("CLS_CONF_THRESHOLD", "0.5"))
# Label for non-crystal photos; winning it means "nothing found".
NOT_A_CRYSTAL = "not a crystal"

# Stripe. Kept optional so detection still runs without payment configured.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "usd")
stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="Crystal Genie API")
# The admin panel (Vercel / localhost) calls the training-photo endpoints from
# the browser. Requests still need an admin's bearer token; this only lets the
# browser send them.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=os.getenv(
        "ADMIN_ORIGIN_REGEX", r"https://[a-z0-9-]+\.vercel\.app|http://localhost:\d+"
    ),
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
model_manager.start()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# class name (lowercase) -> row from the crystals table
_crystal_cache: dict[str, dict] = {}


def get_crystal_info(class_name: str) -> dict:
    """Fetch headline/description/star sign/chakras for a crystal from Supabase."""
    key = class_name.strip().lower()
    if key in _crystal_cache:
        return _crystal_cache[key]

    try:
        resp = (
            supabase.table("crystals")
            .select("headline, description, star_sign, chakras")
            .ilike("name", class_name.strip())
            .limit(1)
            .execute()
        )
        row = resp.data[0] if resp.data else {}
    except Exception as e:  # noqa: BLE001 — detection should work even if the table is missing
        print(f"Warning: could not fetch crystal info for '{class_name}': {e}")
        return {"headline": "", "description": "", "star_sign": "", "chakras": ""}
    info = {
        "headline": row.get("headline") or "",
        "description": row.get("description") or "",
        "star_sign": row.get("star_sign") or "",
        "chakras": row.get("chakras") or "",
    }
    # Only cache real hits, so a crystal added to the DB later is picked up
    # without needing a server restart.
    if info["description"] or info["headline"]:
        _crystal_cache[key] = info
    return info


def save_detection(class_name: str, confidence: float) -> None:
    """Best-effort insert into detection history; never fails the request."""
    try:
        supabase.table("detections").insert(
            {"crystal_name": class_name, "confidence": confidence}
        ).execute()
    except Exception as e:  # noqa: BLE001
        print(f"Warning: could not save detection history: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "model": model_manager.name()}


def _authed(authorization: str | None):
    """Validate the Authorization header and return (user, RLS-scoped client).

    The returned client carries the caller's JWT, so every query it runs sees
    only that user's own rows.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Missing Authorization bearer token"
        )
    token = authorization.split(" ", 1)[1].strip()

    try:
        user = supabase.auth.get_user(token).user
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    scoped = create_client(SUPABASE_URL, SUPABASE_KEY)
    scoped.postgrest.auth(token)
    # Storage reads these headers when first used; without this it would act
    # as the anonymous role instead of the signed-in user.
    scoped.options.headers["Authorization"] = f"Bearer {token}"
    return user, scoped


app.include_router(training_api.build_router(_authed))


def _cart_total(scoped: Client) -> float:
    """Cart total in dollars, computed server-side so the client can never
    dictate what it pays."""
    rows = (
        scoped.table("cart_items")
        .select("quantity, products(price)")
        .execute()
        .data
    )

    total = 0.0
    for row in rows:
        product = row.get("products") or {}
        price = float(product.get("price") or 0)
        total += price * int(row.get("quantity") or 0)
    return total


@app.post("/create-payment-intent")
def create_payment_intent(authorization: str | None = Header(default=None)):
    """Create a Stripe PaymentIntent for the signed-in user's current cart."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    user, scoped = _authed(authorization)
    user_id = user.id
    total = _cart_total(scoped)
    if total <= 0:
        raise HTTPException(status_code=400, detail="Cart is empty")

    amount = int(round(total * 100))  # Stripe expects the smallest currency unit
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=STRIPE_CURRENCY,
            automatic_payment_methods={"enabled": True},
            metadata={"user_id": user_id},
        )
    except stripe.StripeError as e:  # noqa: BLE001
        detail = getattr(e, "user_message", None) or str(e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {detail}")

    return {
        "clientSecret": intent.client_secret,
        "publishableKey": STRIPE_PUBLISHABLE_KEY,
        "amount": amount,
        "currency": STRIPE_CURRENCY,
    }


@app.get("/subscription")
def get_subscription(authorization: str | None = Header(default=None)):
    """Trial/subscription state for the signed-in user."""
    user, _ = _authed(authorization)
    row = subscription_service.get_or_create_row(user.id)
    return subscription_service.status_payload(row)


@app.post("/subscription/subscribe")
def subscribe(authorization: str | None = Header(default=None)):
    """Start a $3.69/month subscription; returns a client secret for the
    payment sheet. Access is only granted once Stripe confirms the payment
    through the webhook."""
    if not subscription_service.is_configured():
        raise HTTPException(
            status_code=500,
            detail="Subscriptions are not configured "
            "(need STRIPE_PRICE_ID and SUPABASE_SERVICE_KEY)",
        )

    user, _ = _authed(authorization)
    row = subscription_service.get_or_create_row(user.id)
    if row.get("status") == "active" and subscription_service.has_access(row):
        raise HTTPException(status_code=400, detail="Already subscribed")

    try:
        result = subscription_service.start_subscription(user, row)
    except stripe.StripeError as e:  # noqa: BLE001
        detail = getattr(e, "user_message", None) or str(e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {detail}")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    result["publishableKey"] = STRIPE_PUBLISHABLE_KEY
    return result


@app.post("/subscription/cancel")
def cancel_subscription(authorization: str | None = Header(default=None)):
    """Stop the subscription renewing; access lasts until the period ends."""
    user, _ = _authed(authorization)
    row = subscription_service.get_or_create_row(user.id)
    try:
        return subscription_service.cancel_subscription(row)
    except stripe.StripeError as e:  # noqa: BLE001
        detail = getattr(e, "user_message", None) or str(e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {detail}")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/stripe/webhook")
async def stripe_webhook(
    request: Request, stripe_signature: str | None = Header(default=None)
):
    """Stripe tells us when a subscription starts, renews, lapses or is
    canceled. This is the only thing that grants paid access."""
    if not subscription_service.WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, subscription_service.WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    obj = event["data"]["object"]
    if event["type"].startswith("customer.subscription."):
        subscription_service.apply_subscription(obj)
    elif event["type"] in ("invoice.paid", "invoice.payment_failed"):
        # Re-read the subscription so we store its authoritative state rather
        # than inferring it from the invoice.
        sub_id = obj.get("subscription") or (
            (obj.get("parent") or {}).get("subscription_details") or {}
        ).get("subscription")
        if sub_id:
            subscription_service.apply_subscription(
                stripe.Subscription.retrieve(sub_id)
            )

    return {"received": True}


@app.post("/orders/{order_id}/confirmation-email")
def send_order_confirmation(
    order_id: int, authorization: str | None = Header(default=None)
):
    """Email the buyer their order confirmation from the shop's admin mailbox.

    Called by the app right after a successful payment. RLS means a caller can
    only ever trigger the email for one of their own orders, and it is sent to
    their account's own address — never one supplied by the client.
    """
    if not email_service.is_configured():
        raise HTTPException(status_code=500, detail="Email is not configured")

    user, scoped = _authed(authorization)
    if not user.email:
        raise HTTPException(status_code=400, detail="Account has no email address")

    orders = (
        scoped.table("orders").select("*").eq("id", order_id).limit(1).execute().data
    )
    if not orders:
        raise HTTPException(status_code=404, detail="Order not found")
    order = orders[0]

    # Skip if a previous call already sent it (column added by
    # order_confirmation_email.sql; absent until that migration is run).
    if order.get("confirmation_email_sent_at"):
        return {"sent": False, "reason": "already sent", "to": user.email}

    items = (
        scoped.table("order_items")
        .select("product_name, unit_price, quantity")
        .eq("order_id", order_id)
        .execute()
        .data
    )

    subject, text_body, html_body = email_service.build_order_confirmation(order, items)
    try:
        email_service.send_email(user.email, subject, text_body, html_body)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: could not send confirmation for order {order_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Could not send email: {e}")

    try:
        scoped.rpc("mark_order_email_sent", {"p_order_id": order_id}).execute()
    except Exception as e:  # noqa: BLE001 — the email already went out
        print(f"Warning: could not mark order {order_id} as emailed: {e}")

    return {"sent": True, "to": user.email}


@app.post("/detect")
async def detect(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="File is not a valid image")

    model = model_manager.get()
    if model.task == "classify":
        detections = _classify(model, image)
        if detections:
            save_detection(detections[0]["class_name"], detections[0]["confidence"])
        return {"detections": detections}

    results = model.predict(image, conf=CONF_THRESHOLD, verbose=False)

    detections = []
    for box in results[0].boxes:
        class_id = int(box.cls[0])
        class_name = model.names[class_id]
        confidence = float(box.conf[0])
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
        detections.append(
            {
                "class_id": class_id,
                "class_name": class_name,
                "confidence": confidence,
                "box": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "description": get_crystal_info(class_name),
            }
        )

    detections.sort(key=lambda d: d["confidence"], reverse=True)
    if detections:
        save_detection(detections[0]["class_name"], detections[0]["confidence"])

    return {"detections": detections}


def _classify(model, image: Image.Image) -> list[dict]:
    """Top guesses from a classification model, shaped like detections.

    The app expects boxes, so each guess gets one covering the whole photo.
    """
    probs = model.predict(image, verbose=False)[0].probs
    width, height = image.size
    out = []
    for class_id, confidence in zip(probs.top5, probs.top5conf.tolist()):
        if confidence < CLS_CONF_THRESHOLD or len(out) == 3:
            break
        class_name = model.names[int(class_id)]
        if class_name.strip().lower() == NOT_A_CRYSTAL:
            break  # the model is confident this isn't a crystal at all
        out.append(
            {
                "class_id": int(class_id),
                "class_name": class_name,
                "confidence": float(confidence),
                "box": {"x1": 0.0, "y1": 0.0, "x2": float(width), "y2": float(height)},
                "description": get_crystal_info(class_name),
            }
        )
    return out
