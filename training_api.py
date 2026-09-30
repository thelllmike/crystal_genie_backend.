# Training photos stored on this server's disk (not Supabase Storage).
#
# The admin panel uploads here; files land in TRAINING_DIR and a row goes into
# `training_images` (stored_on='vps') so labeling and training work as before.
# The trainer, running on the same box, reads the files straight from disk.

import io
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

import model_manager

TRAINING_DIR = Path(
    os.getenv("TRAINING_DIR", Path(__file__).resolve().parent / "training_data")
).resolve()
MAX_BYTES = 10 * 1024 * 1024
FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


class DeleteRequest(BaseModel):
    ids: list[int]


def _safe_path(relative: str) -> Path:
    """Resolve a stored path inside TRAINING_DIR, refusing ../ tricks."""
    path = (TRAINING_DIR / relative).resolve()
    if not path.is_relative_to(TRAINING_DIR):
        raise HTTPException(status_code=404, detail="Not found")
    return path


def build_router(authed) -> APIRouter:
    """`authed` is main._authed: validates the bearer token -> (user, scoped client)."""
    router = APIRouter()
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)

    def require_admin(authorization: str | None):
        user, scoped = authed(authorization)
        if scoped.rpc("is_admin").execute().data is not True:
            raise HTTPException(status_code=403, detail="Admins only")
        return user, scoped

    @router.post("/admin/training-images")
    async def upload(
        file: UploadFile = File(...),
        label: str | None = Form(default=None),
        authorization: str | None = Header(default=None),
    ):
        user, scoped = require_admin(authorization)
        raw = await file.read()
        if len(raw) > MAX_BYTES:
            raise HTTPException(status_code=413, detail="Photo is over 10 MB")
        try:
            image = Image.open(io.BytesIO(raw))
            image.verify()  # rejects truncated/non-image files
            ext = FORMATS[image.format]
        except Exception:
            raise HTTPException(status_code=400, detail="Only JPG, PNG or WebP photos")

        relative = f"{datetime.now(timezone.utc):%Y-%m-%d}/{uuid.uuid4()}.{ext}"
        dest = _safe_path(relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)

        label = (label or "").strip() or None
        try:
            row = (
                scoped.table("training_images")
                .insert(
                    {
                        "storage_path": relative,
                        "stored_on": "vps",
                        "label": label,
                        "labeled_at": datetime.now(timezone.utc).isoformat() if label else None,
                        "labeled_by": user.id if label else None,
                    }
                )
                .execute()
                .data[0]
            )
        except Exception as e:  # noqa: BLE001 — don't leave an orphaned file
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Could not save photo: {e}")
        return row

    @router.post("/admin/training-images/delete")
    def delete(body: DeleteRequest, authorization: str | None = Header(default=None)):
        _, scoped = require_admin(authorization)
        if not body.ids:
            return {"deleted": 0}
        rows = (
            scoped.table("training_images")
            .select("id, storage_path, stored_on")
            .in_("id", body.ids)
            .execute()
            .data
        )
        scoped.table("training_images").delete().in_("id", body.ids).execute()
        for r in rows:
            if r.get("stored_on") == "vps":
                _safe_path(r["storage_path"]).unlink(missing_ok=True)
        # Photos uploaded before the move to this server still live in Supabase.
        legacy = [r["storage_path"] for r in rows if r.get("stored_on") != "vps"]
        if legacy:
            try:
                scoped.storage.from_("training-images").remove(legacy)
            except Exception as e:  # noqa: BLE001 — rows are gone; a stray file is harmless
                print(f"Warning: could not remove old Supabase photos: {e}")
        return {"deleted": len(rows)}

    @router.post("/admin/test-model")
    async def test_model(
        file: UploadFile = File(...),
        job_id: int | None = Form(default=None),
        authorization: str | None = Header(default=None),
    ):
        """Top guesses for one photo from the live model or a training run's model."""
        _, scoped = require_admin(authorization)
        try:
            image = Image.open(io.BytesIO(await file.read())).convert("RGB")
        except Exception:
            raise HTTPException(status_code=400, detail="File is not a valid image")

        if job_id is None:
            model, label = model_manager.get(), f"Live model ({model_manager.name()})"
        else:
            rows = (
                scoped.table("training_jobs")
                .select("id, status, model_path")
                .eq("id", job_id)
                .execute()
                .data
            )
            if not rows or rows[0]["status"] != "succeeded" or not rows[0]["model_path"]:
                raise HTTPException(status_code=404, detail=f"Training run #{job_id} has no finished model")
            try:
                model = model_manager.for_job(job_id, rows[0]["model_path"], scoped)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"Could not load model #{job_id}: {e}")
            label = f"Training run #{job_id}"

        return {
            "model": label,
            "task": model.task,
            "predictions": model_manager.top_predictions(model, image),
        }

    @router.get("/training-images/{relative:path}")
    def serve(relative: str):
        # Public like the old bucket: crystal photos, random unguessable names.
        path = _safe_path(relative)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    return router
