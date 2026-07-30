# Crystal Genie Backend

FastAPI service that runs the YOLO11 gem-detection model and looks up crystal
info (headline, description, star sign, chakras) from Supabase.

## Setup

1. **Create the Supabase tables** — open your project's SQL Editor and run
   `supabase_schema.sql`. Then fill the `crystals` table with one row per gem
   class (the `name` column must match your YOLO class names). You can import
   your existing CSV via Table Editor → `crystals` → Insert → Import data from CSV.

2. **Install dependencies** (Python 3.10+):

   ```bash
   cd backend
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Add your model weights** — copy your trained `best.pt` into this folder
   (or point `MODEL_PATH` in `.env` somewhere else).

4. **Configure environment** — copy `.env.example` to `.env` and fill in
   `SUPABASE_URL` and `SUPABASE_KEY` from Dashboard → Project Settings → API.

5. **Run the server**:

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

   `--host 0.0.0.0` is required so your phone can reach it over Wi-Fi.

6. **Point the app at your machine** — find your computer's LAN IP
   (`ipconfig getifaddr en0` on macOS) and set it in
   `lib/core/services/api_service.dart` (`_baseUrl`). Phone and computer must
   be on the same Wi-Fi network.

## Subscriptions (7-day trial, then $3.69/month)

1. Run `subscriptions.sql` in the Supabase SQL editor. It creates the
   `subscriptions` table, starts a 7-day trial on every new signup, and
   backdates trials for accounts that already exist.
2. Create the recurring price: `python create_stripe_price.py`. Paste the id it
   prints into `STRIPE_PRICE_ID` in `.env`.
3. Copy the **service_role** key from Supabase → Project Settings → API into
   `SUPABASE_SERVICE_KEY`. It bypasses RLS — backend only, never in the app.
4. Add a webhook in Stripe → Developers → Webhooks pointing at
   `https://<your-host>/stripe/webhook`, subscribed to
   `customer.subscription.*`, `invoice.paid` and `invoice.payment_failed`.
   Put its signing secret in `STRIPE_WEBHOOK_SECRET`.

The webhook is what grants paid access — the app can only read status, so a
tampered client can't unlock scanning. While developing, forward events with
`stripe listen --forward-to localhost:8000/stripe/webhook`.

**App store note:** Apple (3.1.1) and Google both require *in-app purchase* for
digital content sold inside an app. A Stripe-billed subscription that unlocks an
app feature will normally be rejected on review — this setup works fine for
Android sideloads and the web, but plan for StoreKit / Google Play Billing if
you're shipping to the stores.

## Endpoints

- `GET /health` — liveness check.
- `GET /subscription` — trial/subscription status for the signed-in user.
- `POST /subscription/subscribe` — starts the $3.69/month subscription and
  returns a `clientSecret` for the payment sheet.
- `POST /subscription/cancel` — cancels at the end of the paid period.
- `POST /stripe/webhook` — Stripe → us; the only thing that grants paid access.
- `POST /orders/{id}/confirmation-email` — emails the buyer their receipt.
- `POST /detect` — multipart upload with field `file` (jpeg/png). Returns:

  ```json
  {
    "detections": [
      {
        "class_id": 0,
        "class_name": "Amethyst",
        "confidence": 0.93,
        "box": {"x1": 12.0, "y1": 34.0, "x2": 300.0, "y2": 280.0},
        "description": {
          "headline": "The stone of calm",
          "description": "Amethyst is a violet quartz...",
          "star_sign": "Pisces",
          "chakras": "Crown, Third Eye"
        }
      }
    ]
  }
  ```

  Detections are sorted by confidence (highest first). The top detection is
  also logged to the `detections` table in Supabase.
