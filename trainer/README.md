# Crystal Genie trainer

Trains a YOLO11 **classification** model from the photos labeled in the admin
panel (Dataset page) whenever a run is started on the Training page.

```
admin panel ──upload/label──▶ Supabase (training_images + photos)
     │ "Start training"                        │
     ▼                                         ▼
training_jobs (queued) ──▶ train_worker.py ──▶ models/job-N/best.pt
     │ "Deploy to app"
     ▼
API (model_manager.py) downloads it and serves /detect with it — no restart
```

## One-time setup

1. Run `training.sql` in the Supabase SQL editor (after `admin_setup.sql`).
2. `SUPABASE_SERVICE_KEY` must be set in `the backend's .env` — both the trainer and
   the API use it. (Supabase dashboard → Project Settings → API → `service_role`.
   Keep it secret; never put it in the app or the admin panel.)
3. Deploy the updated API (`main.py`, `model_manager.py`) and restart it, so it
   starts watching for deployed models.

## Run the trainer on the VPS

Uses the API's virtualenv (it already has ultralytics + CPU torch). The unit
file is tuned for the 1-core server: the API always gets the CPU first, and a
run that uses too much memory is killed at 2 GB instead of the API.

```sh
cp /opt/crystalgenie/app/trainer/crystalgenie-trainer.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crystalgenie-trainer
journalctl -u crystalgenie-trainer -f      # watch it
```

With one CPU core, expect a few minutes per epoch per ~1,000 photos with the
Nano model, and slower scans while a run is going. Training on a faster
machine (below) avoids both.

## Or run it on a faster machine

Anything with the backend's `.env` and requirements works — only one trainer
needs to run, and whichever is running picks the job up.

```sh
cd "/Users/yuvin/Desktop/cristal geine/crystal_genie_backend"
TRAIN_DEVICE=mps .venv/bin/python trainer/train_worker.py   # Apple silicon Mac
TRAIN_DEVICE=0   .venv/bin/python trainer/train_worker.py   # NVIDIA GPU
```

## Settings (env vars)

| Variable | Default | |
|---|---|---|
| `TRAIN_DEVICE` | `cpu` | `cpu`, `mps`, or a GPU index like `0` |
| `TRAIN_BATCH` | `16` | lower if the machine runs out of memory |
| `TRAIN_WORKERS` | `2` | data-loading processes |
| `TRAIN_WORK_DIR` | `trainer/work` | photo cache + training runs |
| `CLS_CONF_THRESHOLD` (API) | `0.5` | below this, a scan returns "no crystal found" |

## Good results

- Aim for 50+ photos per crystal, from different angles, lighting and backgrounds.
- The model only knows crystals it was trained on. Deploying replaces the
  original 5-crystal detector, so include photos of those 5 as well.
- A classifier always names *some* crystal, even for a photo of a cup. Add a
  crystal named exactly `Not a crystal` on the Crystals page and label random
  non-crystal photos (hands, tables, rooms) with it: when it wins, scans
  return "nothing found". (It will also be listed in the app's library.)
- Check the accuracy on the Training page before clicking Deploy; you can
  switch back to an older model, or the original, at any time.
