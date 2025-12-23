# picture-frame
Web controlled Full HD digital picture frame with video call functionality.

## Developer info

**Adam** is `maggis-frame-1` and **Steve** is `maggis-frame-2`.

Frame URLs:
- adam-frame.maggisnorra.is (React)
- adam-frame.maggisnorra.is/api (FastAPI)
- steve-frame.maggisnorra.is (React)
- steve-frame.maggisnorra.is/api (FastAPI)

Cloud URLs:
- frame.maggisnorra.is/adam (React remote controller)
- frame.maggisnorra.is/steve (React remote controller)
- frame.maggisnorra.is/api (FastAPI remote controller in cloud)

## Remote controller

### Hetzner server

To get onto Hetzner server:
```
ssh -i ~/.ssh/id_ed25519_hetzner -o IdentitiesOnly=yes -v root@157.180.127.214
```

When setting up:
```
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo systemctl enable --now docker
docker version
docker compose version
```

Build the docker:
```
docker compose up -d --build
docker compose logs -f
```

Install `cloudflared`:
```
sudo apt-get update
sudo apt-get install -y curl

sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-public-v2.gpg >/dev/null

echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list

sudo apt-get update
sudo apt-get install -y cloudflared
cloudflared --version
```

Set up tunnel:
```
cloudflared tunnel login
cloudflared tunnel create remote-vps
cloudflared tunnel route dns remote-vps frame.maggisnorra.is
```

Create the tunnel config (using tunnel uuid from last step):
```
sudo mkdir -p /etc/cloudflared

sudo tee /etc/cloudflared/config.yml >/dev/null <<'YAML'
tunnel: <TUNNEL_UUID>
credentials-file: /etc/cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: frame.maggisnorra.is
    service: http://localhost:8000
  - service: http_status:404
YAML

sudo cp /root/.cloudflared/*.json /etc/cloudflared/
sudo chmod 600 /etc/cloudflared/*.json
```

Make it a service:
```
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

Rebuild docker:
```
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Other (obsolete?)

FastAPI.

Setup:
```
python -m venv .venv && source .venv/bin/activate
pip install "fastapi>=0.115" "uvicorn[standard]" "SQLAlchemy>=2.0" pydantic pydantic-settings alembic "psycopg[binary]"
```

Run:
```
uvicorn main:app --reload --port 8000
```

## Kiosk

Create and enable service to run on frame:
```
sudo tee /etc/systemd/system/kiosk-api.service >/dev/null <<'EOF'
[Unit]
Description=Kiosk FastAPI
After=network-online.target
Wants=network-online.target

[Service]
User=maggisnorrason
WorkingDirectory=/home/maggisnorrason/picture-frame/apps/kiosk/backend
ExecStart=/home/maggisnorrason/picture-frame/apps/kiosk/backend/.venv/bin/python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=2
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now kiosk-api
sudo systemctl status kiosk-api --no-pager
```

Install and set up Cloudflare tunnel:
```
sudo apt-get update
sudo apt-get install -y curl

sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-public-v2.gpg >/dev/null

echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list

sudo apt-get update
sudo apt-get install -y cloudflared
cloudflared --version

cloudflared tunnel login
cloudflared tunnel create <frame-adam|frame-steve>
cloudflared tunnel route dns <frame-adam|frame-steve> <frame-adam|frame-steve>.maggisnorra.is
```

Create the tunnel config (using tunnel uuid from last step):
```
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/config.yml >/dev/null <<'YAML'
tunnel: <TUNNEL_UUID>
credentials-file: /etc/cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: <frame-adam|steve-adam>.maggisnorra.is
    service: http://127.0.0.1:8000
  - service: http_status:404
YAML

sudo cp ~/.cloudflared/*.json /etc/cloudflared/
sudo chmod 600 /etc/cloudflared/*.json
```

Make it a service:
```
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```


### Backend

FastAPI.

Setup:
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Run:
```
uvicorn main:app --reload --port 8000
```

### Frontend

Run `npm run dev` inside [*picture-frame-kiosk-frontend*](/apps/kiosk/frontend/picture-frame-kiosk-frontend/).

To build:
```
npm ci
npm run build
```

Remember to set the Chromium version.


### WiFi Connect

Using [belena-os/wifi-connect](https://github.com/balena-os/wifi-connect) to connect to WiFi when needed.

To download on 64-bit Raspberry Pi OS (aarch64):
```
set -e
URL="https://github.com/balena-os/wifi-connect/releases/download/v4.11.84/wifi-connect-aarch64-unknown-linux-gnu.tar.gz"

# Temporary workspace
TMP=$(mktemp -d)
cd "$TMP"

# Download & unpack
curl -fL "$URL" -o wifi-connect.tgz
tar -xzf wifi-connect.tgz

# Install binary
sudo install -m 0755 wifi-connect /usr/local/sbin/wifi-connect

# Fetch and install the UI files (served by the captive portal)
curl -fL https://github.com/balena-os/wifi-connect/releases/download/v4.11.84/wifi-connect-ui.tar.gz -o ui.tgz
tar -xzf ui.tgz
sudo mkdir -p /usr/local/share/wifi-connect
sudo rm -rf /usr/local/share/wifi-connect/ui
sudo cp -a ui /usr/local/share/wifi-connect/ui

# Verify
/usr/local/sbin/wifi-connect --version
```


Settings:
```
sudo tee /etc/default/wifi-connect >/dev/null <<'EOF'
PORTAL_SSID=Picture-Frame-1-Setup
PORTAL_PASSPHRASE=Akureyri600
PORTAL_INTERFACE=wlan0
PORTAL_LISTENING_PORT=80
EOF
```

Create `systemd` service:
```
WIFICONNECT_BIN="$(command -v wifi-connect)"
sudo tee /etc/systemd/system/wifi-connect.service >/dev/null <<EOF
[Unit]
Description=WiFi Connect captive portal
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
EnvironmentFile=/etc/default/wifi-connect
ExecStart=$WIFICONNECT_BIN
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
EOF
```


Only run when offline:
```
sudo tee /usr/local/bin/wifi-provision-check.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

conn="$(nmcli -t -f CONNECTIVITY general 2>/dev/null || echo none)"

if [ "$conn" = "full" ]; then
  systemctl stop wifi-connect.service >/dev/null 2>&1 || true
  exit 0
fi

systemctl start wifi-connect.service
EOF

sudo chmod +x /usr/local/bin/wifi-provision-check.sh

sudo tee /etc/systemd/system/wifi-provision-check.service >/dev/null <<'EOF'
[Unit]
Description=Start WiFi Connect only when offline

[Service]
Type=oneshot
ExecStart=/usr/local/bin/wifi-provision-check.sh
EOF

sudo tee /etc/systemd/system/wifi-provision-check.timer >/dev/null <<'EOF'
[Unit]
Description=Periodic WiFi provisioning check

[Timer]
OnBootSec=20
OnUnitActiveSec=2min
Unit=wifi-provision-check.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wifi-provision-check.timer
```


### I2C sound

Enable sound in `/boot/firmware/config.txt` by adding `dtoverlay=max98357a` under `[all]` section.

Find sinks:
```
pactl list short sinks
```

Set default sink:
´´´
pactl set-default-sink alsa_output.platform-soc_107c000000_sound.stereo-fallback
´´´

Make sure it always sets it as default:
```
mkdir -p ~/bin
tee ~/bin/audio-default.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SINK="alsa_output.platform-soc_107c000000_sound.stereo-fallback"

for i in {1..40}; do
  pactl info >/dev/null 2>&1 && break
  sleep 0.1
done

pactl set-default-sink "$SINK"
pactl set-sink-mute "$SINK" 0
pactl set-sink-volume "$SINK" 40%
EOF

chmod +x ~/bin/audio-default.sh

mkdir -p ~/.config/systemd/user
tee ~/.config/systemd/user/audio-default.service >/dev/null <<'EOF'
[Unit]
Description=Set default audio sink to MAX98357A
After=pipewire.service pipewire-pulse.service wireplumber.service
Wants=pipewire-pulse.service wireplumber.service

[Service]
Type=oneshot
ExecStart=%h/bin/audio-default.sh

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now audio-default.service

systemctl --user status audio-default.service --no-pager
```