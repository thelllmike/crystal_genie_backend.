# Crystal Genie trainer — turns labeled photos into a YOLO classifier.
#
# Waits for jobs queued from the admin panel's Training page, then:
#   1. downloads every labeled photo (crystals with >= min_images photos),
#   2. splits them 80/20 into train/val folders (one folder per crystal),
#   3. trains a YOLO11 classification model, reporting each epoch back,
#   4. uploads best.pt to the private `models` bucket.
# Deploying is a separate click in the admin panel; the API then picks the
# model up by itself (see model_manager.py).
#
# Run (from the backend folder):  python trainer/train_worker.py
# It can run on the VPS or on any faster machine — it only needs the .env.

import hashlib
import os
import shutil
import sys
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
WORK_DIR = Path(os.getenv("TRAIN_WORK_DIR", Path(__file__).resolve().parent / "work"))
DEVICE = os.getenv("TRAIN_DEVICE", "cpu")  # "cpu", "0" for the first GPU, "mps" on Apple silicon
BATCH = int(os.getenv("TRAIN_BATCH", "16"))
DATALOADER_WORKERS = int(os.getenv("TRAIN_WORKERS", "2"))
POLL_SECONDS = int(os.getenv("TRAIN_POLL_SECONDS", "15"))
# Photos uploaded to the VPS live here (same setting as the API)...
TRAINING_DIR = Path(os.getenv("TRAINING_DIR", Path(__file__).resolve().parent.parent / "training_data"))
# ...and are fetched over HTTPS when the trainer runs on another machine.
TRAINING_IMAGES_URL = os.getenv("TRAINING_IMAGES_URL", "https://srv1866657.hstgr.cloud/training-images")

IMAGES_BUCKET = "training-images"
MODELS_BUCKET = "models"
LOG_LINES = 200  # how much log the admin panel shows


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Job:
    """One training_jobs row plus a log that is mirrored to the admin panel."""

    def __init__(self, db: Client, row: dict):
        self.db = db
        self.id = row["id"]
        self.params = row.get("params") or {}
        self.lines: list[str] = []

    def update(self, **fields) -> None:
        self.db.table("training_jobs").update(fields).eq("id", self.id).execute()

    def log(self, message: str, flush: bool = True) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
        print(f"[job {self.id}] {message}", flush=True)
        self.lines = (self.lines + [line])[-LOG_LINES:]
        if flush:
            self.update(log="\n".join(self.lines))

    def canceled(self) -> bool:
        row = self.db.table("training_jobs").select("status").eq("id", self.id).single().execute().data
        return row["status"] == "canceled"


def claim_next_job(db: Client) -> Job | None:
    queued = (
        db.table("training_jobs").select("*").eq("status", "queued").order("created_at").limit(1).execute().data
    )
    if not queued:
        return None
    # Only one trainer may win the job: the update matches only while still queued.
    claimed = (
        db.table("training_jobs")
        .update({"status": "running", "started_at": now(), "error": None})
        .eq("id", queued[0]["id"])
        .eq("status", "queued")
        .execute()
        .data
    )
    return Job(db, claimed[0]) if claimed else None


def labeled_images(db: Client) -> list[dict]:
    rows, start = [], 0
    while True:
        page = (
            db.table("training_images")
            .select("id, storage_path, label, stored_on")
            .not_.is_("label", "null")
            .order("id")
            .range(start, start + 999)
            .execute()
            .data
        )
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


def folder_name(label: str) -> str:
    # Folder names become the model's class names, which the API matches
    # against crystal names — so keep them identical except for "/".
    return label.replace("/", "-").strip()


def is_val(image_id: int) -> bool:
    """Stable 80/20 split: a photo stays on the same side across runs."""
    return hashlib.md5(str(image_id).encode()).digest()[0] % 5 == 0


def build_dataset(job: Job) -> tuple[Path, list[str], int]:
    min_images = int(job.params.get("min_images", 20))
    by_label: dict[str, list[dict]] = {}
    for row in labeled_images(job.db):
        by_label.setdefault(row["label"], []).append(row)

    classes = sorted(label for label, rows in by_label.items() if len(rows) >= min_images)
    skipped = sorted(set(by_label) - set(classes))
    if len(classes) < 2:
        raise RuntimeError(
            f"Need at least 2 crystals with {min_images}+ photos each; found {len(classes)}."
        )
    job.log(f"{len(classes)} crystals qualify" + (f"; skipping {len(skipped)} with too few photos" if skipped else ""))

    cache = WORK_DIR / "cache"
    dataset = WORK_DIR / f"job-{job.id}" / "data"
    shutil.rmtree(dataset, ignore_errors=True)

    tasks = []
    for label in classes:
        rows = by_label[label]
        val_ids = {r["id"] for r in rows if is_val(r["id"])}
        if not val_ids:  # every class needs something to be tested on
            val_ids = {rows[0]["id"]}
        if len(val_ids) == len(rows):
            val_ids.discard(rows[0]["id"])
        for r in rows:
            split = "val" if r["id"] in val_ids else "train"
            dest = dataset / split / folder_name(label) / Path(r["storage_path"]).name
            tasks.append((r["storage_path"], r.get("stored_on"), dest))

    def fetch(task):
        path, stored_on, dest = task
        dest.parent.mkdir(parents=True, exist_ok=True)
        local = TRAINING_DIR / path
        if stored_on == "vps" and local.exists():  # trainer runs on the VPS
            shutil.copyfile(local, dest)
            return
        cached = cache / (stored_on or "supabase") / path  # photos never change
        if not cached.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            if stored_on == "vps":
                with urllib.request.urlopen(f"{TRAINING_IMAGES_URL}/{path}", timeout=60) as resp:
                    data = resp.read()
            else:  # uploaded before photos moved to the VPS
                data = job.db.storage.from_(IMAGES_BUCKET).download(path)
            tmp = cached.with_suffix(cached.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(cached)
        shutil.copyfile(cached, dest)

    job.log(f"Collecting {len(tasks)} photos…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch, tasks))
    return dataset, classes, len(tasks)


def train(job: Job) -> None:
    from ultralytics import YOLO  # slow import; only pay it when there's work

    dataset, classes, count = build_dataset(job)
    epochs = int(job.params.get("epochs", 30))
    imgsz = int(job.params.get("imgsz", 224))
    base = job.params.get("base_model", "yolo11n-cls.pt")
    job.update(classes=classes, image_count=count, progress={"epoch": 0, "epochs": epochs})
    job.log(f"Training {base} for {epochs} epochs at {imgsz}px on {DEVICE}")

    model = YOLO(base)
    finished = False  # set once the last epoch has run

    def on_epoch_end(trainer):
        nonlocal finished
        top1 = getattr(trainer.validator.metrics, "top1", None) if trainer.validator else None
        if finished:
            # Ultralytics fires this once more after training, for best.pt.
            if top1 is not None:
                job.log(f"Best model accuracy {top1:.1%}")
            return
        epoch = trainer.epoch + 1
        job.update(progress={"epoch": epoch, "epochs": epochs})
        job.log(f"Epoch {epoch}/{epochs}" + (f" — accuracy {top1:.1%}" if top1 is not None else ""))
        if job.canceled():
            job.log("Canceled from the admin panel — stopping after this epoch")
            trainer.stop = True
        finished = trainer.stop  # true on the last epoch, early stop, or cancel

    model.add_callback("on_fit_epoch_end", on_epoch_end)
    model.train(
        data=str(dataset),
        epochs=epochs,
        imgsz=imgsz,
        batch=BATCH,
        device=DEVICE,
        workers=DATALOADER_WORKERS,
        project=str(WORK_DIR / "runs"),
        name=f"job-{job.id}",
        exist_ok=True,
        patience=max(10, epochs // 3),
        plots=False,
        verbose=False,
    )

    if job.canceled():
        job.update(finished_at=now())
        return

    metrics = model.metrics
    top1, top5 = float(getattr(metrics, "top1", 0)), float(getattr(metrics, "top5", 0))
    best = Path(model.trainer.save_dir) / "weights" / "best.pt"
    remote = f"job-{job.id}/best.pt"
    job.log(f"Done — accuracy {top1:.1%} (top-5 {top5:.1%}). Uploading model…")
    job.db.storage.from_(MODELS_BUCKET).upload(
        remote, best.read_bytes(), {"content-type": "application/octet-stream", "upsert": "true"}
    )
    job.update(
        status="succeeded",
        metrics={"top1": top1, "top5": top5},
        model_path=remote,
        finished_at=now(),
    )
    job.log("Model uploaded. Deploy it from the Training page when you're happy with the accuracy.")
    shutil.rmtree(dataset, ignore_errors=True)  # photos stay in the cache


def main() -> None:
    if not SUPABASE_SERVICE_KEY:
        sys.exit("SUPABASE_SERVICE_KEY is not set in the backend's .env — the trainer needs it.")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # A job left "running" means a previous trainer died mid-run.
    db.table("training_jobs").update(
        {"status": "failed", "error": "The trainer stopped while this job was running. Start a new run.", "finished_at": now()}
    ).eq("status", "running").execute()

    print(f"Trainer ready (device={DEVICE}); checking for jobs every {POLL_SECONDS}s", flush=True)
    while True:
        try:
            job = claim_next_job(db)
        except Exception as e:  # noqa: BLE001 — network blips shouldn't kill the worker
            print(f"Could not check for jobs: {e}", flush=True)
            job = None
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        try:
            job.log("Picked up by trainer")
            train(job)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                job.log(f"Failed: {e}")
                job.update(status="failed", error=str(e), finished_at=now())
            except Exception:  # noqa: BLE001
                traceback.print_exc()


if __name__ == "__main__":
    main()
