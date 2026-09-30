# Deploying the Crystal Genie backend to a VPS

Target: Ubuntu 24.04, `root@187.127.213.241`.

Run steps 1–3 **on the server**, step 4 **on your Mac**, and 5–11 back on the
server. Anything in `<angle brackets>` is a value you supply.

---

## 1. Log in and create a service user

Never run a public-facing app as root — a bug in a dependency becomes a full
box compromise.

```bash
ssh root@187.127.213.241

adduser --system --group --home /opt/crystalgenie crystalgenie
```

## 2. Add swap (skip if the VPS has 8 GB+ RAM)

PyTorch briefly needs a lot of memory while `pip` unpacks its wheels, and the
model sits in RAM during inference. On a 4 GB box, swap is what stops the
install from being OOM-killed halfway.

```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h        # should now show 4 Gi of swap
```

## 3. Install system packages

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip nginx ufw rsync \
               libgl1 libglib2.0-0
```

`libgl1` and `libglib2.0-0` are needed by OpenCV, which ultralytics pulls in.
Without them the app imports fine locally but dies on the server with
`libGL.so.1: cannot open shared object file`.

## 4. Copy the code up (run this on your Mac)

The model weights and `.env` are gitignored, so `git clone` alone won't give you
a working server — copy the directory directly.

```bash
cd "/Users/yuvin/Desktop/cristal geine/crystal_genie_backend"

# Backend code (excluding the local venv, caches and local training files)
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude 'detect' \
  --exclude 'trainer/work' --exclude 'models' --exclude 'training_data' \
  ./ root@187.127.213.241:/opt/crystalgenie/app/

# The trained YOLO weights — MODEL_PATH=best.pt expects this filename
rsync -av detect/train/weights/best.pt \
  root@187.127.213.241:/opt/crystalgenie/app/best.pt
```

This first copy includes `.env` with your live Stripe secret, the Supabase
key and the mailbox password. That's intended — the server needs them — but it
means the transfer is only as safe as your SSH setup. Finish step 10 the same
day.

## 4b. Fix `MODEL_PATH` in the server's `.env`

Your local `.env` has `MODEL_PATH=detect/train/weights/best.pt`, which is
correct on your Mac but wrong on the server — step 4 copies the weights to
`/opt/crystalgenie/app/best.pt`, so the relative path resolves to a file that
isn't there and `main.py` dies at import time (it calls `YOLO(MODEL_PATH)` at
module scope, so systemd sees a crash-loop, not a runtime error).

```bash
ssh root@187.127.213.241 \
  'sed -i "s|^MODEL_PATH=.*|MODEL_PATH=best.pt|" /opt/crystalgenie/app/.env'
```

Leave your local `.env` alone — it needs the relative path for local dev. The
redeploy command at the bottom excludes `.env`, so this fix survives.

## 5. Build the virtualenv

Back on the server:

```bash
cd /opt/crystalgenie/app
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
```

**Install CPU-only PyTorch first.** This matters: `pip install ultralytics` on a
fresh box pulls the CUDA build — about 2.5 GB of NVIDIA libraries that are
useless on a GPU-less VPS and will fill a 20 GB disk. Pinning the CPU index
first brings it down to roughly 200 MB.

```bash
.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.txt
```

Then check it loads (first run takes ~30s while it initializes):

```bash
.venv/bin/python -c "from ultralytics import YOLO; YOLO('best.pt'); print('model OK')"
```

## 6. Fix ownership and the ultralytics config dir

ultralytics writes a settings file on first use. As a `--system` user with no
writable home, that crashes the service on startup with a permissions error.

```bash
mkdir -p /opt/crystalgenie/.config/Ultralytics
chown -R crystalgenie:crystalgenie /opt/crystalgenie
chmod 600 /opt/crystalgenie/app/.env
```

## 7. Run it under systemd

```bash
cat > /etc/systemd/system/crystalgenie.service <<'EOF'
[Unit]
Description=Crystal Genie API
After=network.target

[Service]
Type=simple
User=crystalgenie
Group=crystalgenie
WorkingDirectory=/opt/crystalgenie/app
Environment="YOLO_CONFIG_DIR=/opt/crystalgenie/.config/Ultralytics"
Environment="HOME=/opt/crystalgenie"
ExecStart=/opt/crystalgenie/app/.venv/bin/uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now crystalgenie
systemctl status crystalgenie --no-pager
```

Two deliberate choices:

- **`--workers 1`.** Each worker loads its own copy of the model into memory.
  On a 4 GB box, 4 workers means 4 copies and an OOM kill under load. Add
  workers only after watching `free -h` during real traffic.
- **`--host 127.0.0.1`.** uvicorn is not exposed directly; nginx is the only
  thing that can reach it.

Check it: `curl localhost:8000/health` → `{"status":"ok","model":"best.pt"}`

## 8. nginx reverse proxy

```bash
cat > /etc/nginx/sites-available/crystalgenie <<'EOF'
server {
    listen 80;
    server_name api.crystalgenie.com;

    # Crystal photos from a modern phone camera are several MB; nginx's
    # 1 MB default would reject them with a 413 before they reach the app.
    client_max_body_size 25M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # YOLO inference on CPU can take a while on a cold cache.
        proxy_read_timeout 120s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/crystalgenie /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

## 9. DNS, then HTTPS

**You have to do the DNS part yourself**, wherever crystalgenie.com's records
are managed. Add an A record:

```
api.crystalgenie.com.   A   187.127.213.241
```

Leave the root domain and `mail.` pointing at `45.84.120.170` — that's your
mail server, and repointing it would break email.

Wait for it to resolve (`dig +short api.crystalgenie.com`), then:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d api.crystalgenie.com --agree-tos -m admin@crystalgenie.com
```

certbot edits the nginx config and installs a renewal timer. HTTPS is not
optional here: Stripe refuses plain-http webhook endpoints, and both Android
and iOS block cleartext traffic in release builds.

## 10. Firewall and SSH hardening

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

passwd    # change the root password — the old one was shared in a chat
```

Once your SSH key works, turn off password logins entirely. A public IP on
port 22 gets brute-forced within hours of going live.

```bash
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

Do this only after confirming key login works from a second terminal — locking
yourself out means a rescue-console trip.

## 11. Point the app at the server

On your Mac, edit `lib/core/constants/api_config.dart`:

```dart
static const String baseUrl = 'https://api.crystalgenie.com';
```

That single constant feeds detection, payments, order emails and subscriptions.
Rebuild the app.

---

## Still to configure

The subscription endpoints return a clear "not configured" error until these
three land in `/opt/crystalgenie/app/.env`:

| Variable | Where it comes from |
|---|---|
| `STRIPE_PRICE_ID` | run `.venv/bin/python create_stripe_price.py` |
| `SUPABASE_SERVICE_KEY` | Supabase → Project Settings → API → `service_role` |
| `STRIPE_WEBHOOK_SECRET` | Stripe → Webhooks → add `https://api.crystalgenie.com/stripe/webhook`, subscribe to `customer.subscription.*`, `invoice.paid`, `invoice.payment_failed`, then copy the `whsec_...` |

After editing: `systemctl restart crystalgenie`

Also run these once in the Supabase SQL editor if you haven't:
`supabase_schema.sql`, `add_shipping_to_orders.sql`,
`order_confirmation_email.sql`, `subscriptions.sql`.

## Verifying

```bash
curl https://api.crystalgenie.com/health
```

Expected: `{"status":"ok","model":"best.pt"}`

```bash
# Detection, end to end
curl -F "file=@/path/to/crystal.jpg" https://api.crystalgenie.com/detect
```

## Day-to-day

```bash
journalctl -u crystalgenie -f          # live logs
systemctl restart crystalgenie         # after an .env change
free -h                                # watch memory during scans
```

**Redeploying after a code change** — normally just push to `main`; GitHub Actions deploys it (see `deploy/README.md`). Manual fallback, from your Mac:

```bash
cd "/Users/yuvin/Desktop/cristal geine/crystal_genie_backend"
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude '.env' --exclude 'detect' \
  --exclude 'trainer/work' --exclude 'models' --exclude 'training_data' \
  ./ root@187.127.213.241:/opt/crystalgenie/app/
ssh root@187.127.213.241 'chown -R crystalgenie:crystalgenie /opt/crystalgenie/app && systemctl restart crystalgenie'
```

Note `--exclude '.env'` — the server's copy has the real values and must not be
overwritten by your local one.

## When something breaks

| Symptom | Cause |
|---|---|
| `502 Bad Gateway` | app isn't running — `journalctl -u crystalgenie -n 50` |
| `libGL.so.1: cannot open shared object file` | missing `libgl1` (step 3) |
| Service dies during a scan, no error | out of memory — check swap is on, keep `--workers 1` |
| `413 Request Entity Too Large` | `client_max_body_size` missing from nginx |
| Permission error mentioning `Ultralytics` | step 6 skipped |
| Webhook shows 400 in Stripe's dashboard | `STRIPE_WEBHOOK_SECRET` wrong or unset |
