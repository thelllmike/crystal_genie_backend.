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
import json
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


def fetch_rows(db: Client, column: str) -> list[dict]:
    """Every photo where `column` (label or boxes) has been filled in."""
    rows, start = [], 0
    while True:
        page = (
            db.table("training_images")
            .select("id, storage_path, label, stored_on, boxes")
            .not_.is_(column, "null")
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


def split_ids(rows: list[dict]) -> set[int]:
    """Validation photo ids: ~20%, but never none and never all."""
    val = {r["id"] for r in rows if is_val(r["id"])}
    if not val:
        val = {rows[0]["id"]}
    if len(val) == len(rows) and len(rows) > 1:
        val.discard(rows[0]["id"])
    return val


def collect_photos(job: Job, tasks: list[tuple[str, str | None, Path]]) -> None:
    """Copies (or downloads) each (storage_path, stored_on, dest) photo."""
    cache = WORK_DIR / "cache"

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


def build_classify_dataset(job: Job) -> tuple[Path, list[str], int]:
    """ImageFolder layout: data/{train,val}/<crystal>/<photo>."""
    min_images = int(job.params.get("min_images", 20))
    by_label: dict[str, list[dict]] = {}
    for row in fetch_rows(job.db, "label"):
        by_label.setdefault(row["label"], []).append(row)

    classes = sorted(label for label, rows in by_label.items() if len(rows) >= min_images)
    skipped = sorted(set(by_label) - set(classes))
    if len(classes) < 2:
        raise RuntimeError(f"Need at least 2 crystals with {min_images}+ photos each; found {len(classes)}.")
    job.log(f"{len(classes)} crystals qualify" + (f"; skipping {len(skipped)} with too few photos" if skipped else ""))

    dataset = WORK_DIR / f"job-{job.id}" / "data"
    shutil.rmtree(dataset, ignore_errors=True)
    tasks = []
    for label in classes:
        rows = by_label[label]
        val = split_ids(rows)
        for r in rows:
            split = "val" if r["id"] in val else "train"
            tasks.append((r["storage_path"], r.get("stored_on"), dataset / split / folder_name(label) / Path(r["storage_path"]).name))
    collect_photos(job, tasks)
    return dataset, classes, len(tasks)


def build_detect_dataset(job: Job) -> tuple[Path, list[str], int]:
    """YOLO detection layout: images/ + labels/ (one .txt of boxes per photo) + data.yaml."""
    min_images = int(job.params.get("min_images", 20))
    rows = fetch_rows(job.db, "boxes")

    photos_per_class: dict[str, int] = {}
    for r in rows:
        for label in {b["label"] for b in r["boxes"]}:
            photos_per_class[label] = photos_per_class.get(label, 0) + 1
    classes = sorted(label for label, n in photos_per_class.items() if n >= min_images)
    if not classes:
        raise RuntimeError(f"Need at least 1 crystal boxed in {min_images}+ photos; none qualify yet.")
    skipped = sorted(set(photos_per_class) - set(classes))

    # A photo with a skipped crystal is left out entirely: keeping it with that
    # box removed would teach the model the crystal is background.
    keep = [r for r in rows if all(b["label"] in classes for b in r["boxes"])]
    empty = sum(1 for r in keep if not r["boxes"])
    job.log(
        f"{len(classes)} crystals qualify; {len(keep)} photos ({empty} with no crystals)"
        + (f"; leaving out {len(rows) - len(keep)} photos containing {len(skipped)} rarer crystals" if skipped else "")
    )

    dataset = WORK_DIR / f"job-{job.id}" / "data"
    shutil.rmtree(dataset, ignore_errors=True)
    index = {label: i for i, label in enumerate(classes)}
    val = split_ids([r for r in keep if r["boxes"]] or keep)
    tasks = []
    for r in keep:
        split = "val" if r["id"] in val else "train"
        name = Path(r["storage_path"]).name
        tasks.append((r["storage_path"], r.get("stored_on"), dataset / "images" / split / name))
        lines = []
        for b in r["boxes"]:
            x, y = max(0.0, b["x"]), max(0.0, b["y"])
            w, h = min(b["w"], 1 - x), min(b["h"], 1 - y)
            if w > 0 and h > 0:  # YOLO wants centre x/y + width/height, all 0..1
                lines.append(f"{index[b['label']]} {x + w / 2:.6f} {y + h / 2:.6f} {w:.6f} {h:.6f}")
        label_file = dataset / "labels" / split / (Path(name).stem + ".txt")
        label_file.parent.mkdir(parents=True, exist_ok=True)
        label_file.write_text("\n".join(lines))
    collect_photos(job, tasks)

    # JSON is valid YAML, and quotes crystal names with odd characters safely.
    config = dataset / "data.yaml"
    config.write_text(
        json.dumps({"path": str(dataset), "train": "images/train", "val": "images/val", "names": classes})
    )
    return config, classes, len(keep)


def score(metrics) -> dict[str, float]:
    """The numbers we report: top1/top5 for classifiers, mAP for detectors."""
    box = getattr(metrics, "box", None)
    if box is not None:
        return {"map50": float(box.map50), "map": float(box.map)}
    return {"top1": float(getattr(metrics, "top1", 0)), "top5": float(getattr(metrics, "top5", 0))}


def describe(s: dict[str, float]) -> str:
    if "map50" in s:
        return f"box accuracy (mAP50) {s['map50']:.1%}, strict mAP {s['map']:.1%}"
    return f"accuracy {s['top1']:.1%} (top-5 {s['top5']:.1%})"


def train(job: Job) -> None:
    from ultralytics import YOLO  # slow import; only pay it when there's work

    detect = job.params.get("task", "classify") == "detect"
    data, classes, count = build_detect_dataset(job) if detect else build_classify_dataset(job)
    epochs = int(job.params.get("epochs", 30))
    imgsz = int(job.params.get("imgsz", 640 if detect else 224))
    base = job.params.get("base_model", "yolo11n.pt" if detect else "yolo11n-cls.pt")
    job.update(classes=classes, image_count=count, progress={"epoch": 0, "epochs": epochs})
    job.log(f"Training {base} ({'boxes' if detect else 'whole photo'}) for {epochs} epochs at {imgsz}px on {DEVICE}")

    model = YOLO(base)
    finished = False  # set once the last epoch has run

    def on_epoch_end(trainer):
        nonlocal finished
        s = score(trainer.validator.metrics) if trainer.validator else None
        if finished:
            # Ultralytics fires this once more after training, for best.pt.
            if s:
                job.log(f"Best model: {describe(s)}")
            return
        epoch = trainer.epoch + 1
        job.update(progress={"epoch": epoch, "epochs": epochs})
        job.log(f"Epoch {epoch}/{epochs}" + (f" — {describe(s)}" if s else ""))
        if job.canceled():
            job.log("Canceled from the admin panel — stopping after this epoch")
            trainer.stop = True
        finished = trainer.stop  # true on the last epoch, early stop, or cancel

    model.add_callback("on_fit_epoch_end", on_epoch_end)
    model.train(
        data=str(data),
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

    result = score(model.metrics)
    best = Path(model.trainer.save_dir) / "weights" / "best.pt"
    remote = f"job-{job.id}/best.pt"
    job.log(f"Done — {describe(result)}. Uploading model…")
    job.db.storage.from_(MODELS_BUCKET).upload(
        remote, best.read_bytes(), {"content-type": "application/octet-stream", "upsert": "true"}
    )
    job.update(status="succeeded", metrics=result, model_path=remote, finished_at=now())
    job.log("Model uploaded. Deploy it from the Training page when you're happy with the accuracy.")
    shutil.rmtree(WORK_DIR / f"job-{job.id}", ignore_errors=True)  # photos stay in the cache


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
