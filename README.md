# picture-frame
Web controlled Full HD digital picture frame with video call functionality.

## Cloud

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

### Backend

FastAPI.

Setup:
```
python -m venv .venv && source .venv/bin/activate
pip install "fastapi>=0.115" "uvicorn[standard]" "SQLAlchemy>=2.0" pydantic pydantic-settings alembic "psycopg[binary]"
```

Run:
```
uvicorn main:app --reload --port 8001
```

### Frontend

Run `npm run dev` inside [*picture-frame-kiosk-frontend*](/apps/kiosk/frontend/picture-frame-kiosk-frontend/).

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