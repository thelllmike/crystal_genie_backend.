# Which YOLO model /detect uses.
#
# Starts with MODEL_PATH (the original detector). If the admin panel has
# deployed a trained model (training_jobs.deployed_at), a background thread
# downloads its weights from the private `models` bucket and swaps it in — no
# restart needed. Clearing every deployed_at switches back to MODEL_PATH.

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from supabase import create_client
from ultralytics import YOLO

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
MODEL_PATH = os.getenv("MODEL_PATH", "best.pt")
MODELS_DIR = Path(os.getenv("MODELS_DIR", "models"))
POLL_SECONDS = int(os.getenv("MODEL_POLL_SECONDS", "60"))

_model = YOLO(MODEL_PATH)
_name = os.path.basename(MODEL_PATH)
_job_id: int | None = None  # None = the original MODEL_PATH model


def get() -> YOLO:
    return _model


def name() -> str:
    return _name


def _deployed_job(client) -> dict | None:
    rows = (
        client.table("training_jobs")
        .select("id, model_path")
        .not_.is_("deployed_at", "null")
        .order("deployed_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _sync(client) -> None:
    global _model, _name, _job_id
    job = _deployed_job(client)

    if job is None:
        if _job_id is not None:  # a deployment was rolled back
            _model, _name, _job_id = YOLO(MODEL_PATH), os.path.basename(MODEL_PATH), None
            print(f"[model] switched back to {MODEL_PATH}")
        return
    if job["id"] == _job_id or not job.get("model_path"):
        return

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    local = MODELS_DIR / f"job-{job['id']}.pt"
    if not local.exists():
        data = client.storage.from_("models").download(job["model_path"])
        tmp = local.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(local)

    new_model = YOLO(str(local))  # load fully before swapping it in
    _model, _name, _job_id = new_model, local.name, job["id"]
    print(f"[model] now serving training job #{job['id']} ({new_model.task}, {len(new_model.names)} classes)")


_test_cache: "OrderedDict[int, YOLO]" = OrderedDict()


def for_job(job_id: int, model_path: str, client) -> YOLO:
    """A trained job's model, for the admin Test page (not served to the app).

    `client` must be allowed to read the private `models` bucket (an admin's
    session is). Keeps the last few loaded, since classifiers are small.
    """
    if job_id == _job_id:
        return _model
    if job_id in _test_cache:
        _test_cache.move_to_end(job_id)
        return _test_cache[job_id]
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    local = MODELS_DIR / f"job-{job_id}.pt"
    if not local.exists():
        tmp = local.with_suffix(".part")
        tmp.write_bytes(client.storage.from_("models").download(model_path))
        tmp.replace(local)
    model = YOLO(str(local))
    _test_cache[job_id] = model
    while len(_test_cache) > 3:
        _test_cache.popitem(last=False)
    return model


def top_predictions(model: YOLO, image, k: int = 5) -> list[dict]:
    """Best guesses with confidences, for either model type, no threshold."""
    result = model.predict(image, verbose=False, conf=0.01)[0]
    if model.task == "classify":
        return [
            {"class_name": model.names[int(i)], "confidence": float(c)}
            for i, c in zip(result.probs.top5[:k], result.probs.top5conf.tolist()[:k])
        ]
    best: dict[str, float] = {}  # detector: highest box per crystal
    for box in result.boxes:
        name = model.names[int(box.cls[0])]
        best[name] = max(best.get(name, 0.0), float(box.conf[0]))
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:k]
    return [{"class_name": n, "confidence": c} for n, c in ranked]


def _loop() -> None:
    client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    while True:
        try:
            _sync(client)
        except Exception as e:  # noqa: BLE001 — keep serving the current model
            print(f"[model] could not check for a deployed model: {e}")
        time.sleep(POLL_SECONDS)


def start() -> None:
    """Begin watching for deployed models (needs SUPABASE_SERVICE_KEY)."""
    if not SUPABASE_SERVICE_KEY:
        print("[model] SUPABASE_SERVICE_KEY not set — trained models won't be picked up")
        return
    threading.Thread(target=_loop, name="model-sync", daemon=True).start()
