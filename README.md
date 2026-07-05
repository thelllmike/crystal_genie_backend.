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

## Endpoints

- `GET /health` — liveness check.
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
