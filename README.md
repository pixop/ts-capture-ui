# ts-capture-ui

Web UI for capturing raw MPEG-TS snippets with `ffmpeg`.

## What It Does

- fixed-duration capture (`.ts`)
- manual start/stop capture
- preview thumbnail (`preview.jpg`)
- relay/fanout start/stop
- schedule-based fixed captures
- capture list with download, delete, probe, and thumbnails

No database and no authentication. Designed for private/internal deployments.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe`
- Linux for production/systemd (macOS works for local dev)

## Quick Start

```bash
git clone <repo-url> ts-capture-ui
cd ts-capture-ui
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install ffmpeg (Ubuntu/Debian):

```bash
sudo apt update
sudo apt install -y ffmpeg
```

Create capture directory:

```bash
sudo mkdir -p /data/captures
sudo chown -R "$USER":"$USER" /data/captures
```

Run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open: [http://localhost:8080](http://localhost:8080)

## Key Environment Variables

```bash
TS_CAPTURE_DIR=/data/captures
TS_BIND_HOST=0.0.0.0
TS_BIND_PORT=8080
TS_DEFAULT_INPUT_URL="udp://0.0.0.0:5000?fifo_size=10000000&overrun_nonfatal=1"

TS_RELAY_AUTOSTART=0
TS_RELAY_INPUT_URL="udp://0.0.0.0:5000?fifo_size=10000000&overrun_nonfatal=1"
TS_PREVIEW_AUTOSTART=0
TS_PREVIEW_USE_RELAY=1
```

For multiple instances, give each instance unique values for:

- `TS_CAPTURE_DIR`
- `TS_BIND_PORT`
- `TS_RELAY_PREVIEW_PORT`
- `TS_RELAY_CAPTURE_PORT`

Example env files:

- `examples/primary.env`
- `examples/processed.env`

## Helper Scripts

Foreground:

```bash
scripts/run-primary.sh
scripts/run-processed.sh
```

Detached `screen`:

```bash
scripts/run-primary.sh --screen
scripts/run-processed.sh --screen
```

Manage sessions:

```bash
scripts/screen-instance.sh primary status
scripts/screen-instance.sh primary attach
scripts/screen-instance.sh primary stop
```

## systemd

Provided unit files:

- `systemd/ts-capture-ui.service`
- `systemd/ts-capture-ui@.service`

Single-instance install:

```bash
sudo cp systemd/ts-capture-ui.service /etc/systemd/system/ts-capture-ui.service
sudo systemctl daemon-reload
sudo systemctl enable --now ts-capture-ui
sudo systemctl status ts-capture-ui
```

## Main Routes

- `GET /` - UI
- `GET /api/status` - live status JSON
- `GET /api/captures` - capture list/status JSON
- `POST /capture-fixed`
- `POST /manual/start`
- `POST /manual/stop`
- `POST /preview/start`
- `POST /preview/stop`
- `POST /relay/start`
- `POST /relay/stop`
- `POST /schedules/create`
- `POST /schedules/enable/{schedule_id}`
- `POST /schedules/disable/{schedule_id}`
- `POST /schedules/run-now/{schedule_id}`
- `POST /schedules/delete/{schedule_id}`
- `GET /download/{filename}`
- `POST /delete/{filename}`
- `GET /probe/{filename}`

## Security

- Keep service access private (VPN, SSH tunnel, restricted security groups).
- Do not expose publicly without adding authentication.
- Download/delete/probe are restricted to `TS_CAPTURE_DIR` and validated `.ts` filenames.
