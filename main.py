# Crystal Genie backend — YOLO11 detection + Supabase lookup
#
# Run with:  uvicorn main:app --host 0.0.0.0 --port 8000

import io
import os

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from PIL import Image
from supabase import Client, create_client
from ultralytics import YOLO

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
MODEL_PATH = os.getenv("MODEL_PATH", "best.pt")
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.25"))

# Stripe. Kept optional so detection still runs without payment configured.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "usd")
stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="Crystal Genie API")
model = YOLO(MODEL_PATH)
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
    return {"status": "ok", "model": os.path.basename(MODEL_PATH)}


def _cart_total_for_token(token: str) -> tuple[str, float]:
    """Validate the Supabase JWT and return (user_id, cart total in dollars).

    The amount is computed server-side from the user's own cart under RLS, so
    the client can never dictate what it pays.
    """
    try:
        user_resp = supabase.auth.get_user(token)
        user = user_resp.user
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # Scope a client to the caller's JWT so RLS returns only their cart rows.
    scoped = create_client(SUPABASE_URL, SUPABASE_KEY)
    scoped.postgrest.auth(token)
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
    return user.id, total


@app.post("/create-payment-intent")
def create_payment_intent(authorization: str | None = Header(default=None)):
    """Create a Stripe PaymentIntent for the signed-in user's current cart."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Missing Authorization bearer token"
        )

    token = authorization.split(" ", 1)[1].strip()
    user_id, total = _cart_total_for_token(token)
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


@app.post("/detect")
async def detect(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="File is not a valid image")

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
