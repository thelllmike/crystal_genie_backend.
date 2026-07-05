# Crystal Genie backend — YOLO11 detection + Supabase lookup
#
# Run with:  uvicorn main:app --host 0.0.0.0 --port 8000

import io
import os

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from supabase import Client, create_client
from ultralytics import YOLO

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
MODEL_PATH = os.getenv("MODEL_PATH", "best.pt")
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.25"))

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
