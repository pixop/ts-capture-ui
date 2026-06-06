# ts-capture-ui

`ts-capture-ui` is a minimal internal FastAPI web app for capturing raw MPEG-TS snippets from live inputs (especially UDP MPEG-TS streams) on a Linux EC2 instance.

It is intentionally simple and focused on:

- raw `.ts` capture with `ffmpeg` (`-c copy`, no transcoding)
- lightweight keyframe thumbnail preview (`preview.jpg`)
- listing captured files
- direct download and deletion from the web UI
- basic interval-based mini scheduling for fixed-duration captures

No database, no React frontend, and no full DVR/calendar system are used.

## Requirements

- Python 3.10+ (or newer)
- Linux host (for `systemd` + server deployment)
- `ffmpeg` and `ffprobe`

## Install

```bash
git clone <your-repo-url> ts-capture-ui
cd ts-capture-ui
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

By default, captures are stored in `/data/captures`. You can override this with:

```bash
export TS_CAPTURE_DIR=/your/capture/path
```

Default runtime configuration values:

```bash
TS_CAPTURE_INSTANCE_NAME=default
TS_CAPTURE_DIR=/data/captures
TS_DEFAULT_INPUT_URL=udp://0.0.0.0:5000?fifo_size=10000000&overrun_nonfatal=1
TS_BIND_HOST=0.0.0.0
TS_BIND_PORT=8080
TS_RELAY_HOST=127.0.0.1
TS_RELAY_PREVIEW_PORT=5101
TS_RELAY_CAPTURE_PORT=5102
TS_PREVIEW_FILENAME=preview.jpg
TS_SCHEDULES_FILENAME=schedules.json
TS_THUMBNAILS_DIRNAME=thumbnails
```

Create the capture directory (default path example):

```bash
sudo mkdir -p /data/captures
sudo chown -R $USER:$USER /data/captures
```

## Install ffmpeg

On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

Verify:

```bash
ffmpeg -version
ffprobe -version
```

## Run locally

```bash
uvicorn app.main:app --host "${TS_BIND_HOST:-0.0.0.0}" --port "${TS_BIND_PORT:-8080}"
```

Or with a custom capture directory:

```bash
TS_CAPTURE_DIR=/your/capture/path TS_BIND_PORT=8080 uvicorn app.main:app --host "${TS_BIND_HOST:-0.0.0.0}" --port "${TS_BIND_PORT:-8080}"
```

Open:

- [http://localhost:8080](http://localhost:8080)

## Helper scripts (terminal + screen)

To avoid remembering the full env/uvicorn command, use the helper scripts:

```bash
# Foreground runs (Ctrl+C to stop)
scripts/run-primary.sh
scripts/run-processed.sh

# Detached screen runs
scripts/run-primary.sh --screen
scripts/run-processed.sh --screen
```

With the bundled env files, these start on:

- primary: `http://localhost:8180`
- processed: `http://localhost:8181`

Manage screen sessions:

```bash
scripts/screen-instance.sh primary status
scripts/screen-instance.sh primary attach
scripts/screen-instance.sh primary stop

scripts/screen-instance.sh processed status
scripts/screen-instance.sh processed attach
scripts/screen-instance.sh processed stop
```

## Run on EC2 (Linux)

1. Copy project to your instance (for example `/opt/ts-capture-ui`).
2. Create a virtual environment and install requirements.
3. Ensure `/data/captures` exists and is writable by the service user.
4. Start with Uvicorn directly or use `systemd`.

### Safe exposure recommendations

This app has no authentication in MVP, so restrict access:

- Prefer an SSH tunnel from your workstation:
  - `ssh -L 8080:127.0.0.1:8080 user@your-ec2-host`
- Or restrict EC2 security groups to trusted office/VPN IPs only.
- Avoid exposing port `8080` publicly to the internet.

## systemd setup example

A sample single-instance unit file is provided at:

- `systemd/ts-capture-ui.service`

A templated multi-instance unit file is also provided at:

- `systemd/ts-capture-ui@.service`

Install it:

```bash
sudo cp systemd/ts-capture-ui.service /etc/systemd/system/ts-capture-ui.service
sudo systemctl daemon-reload
sudo systemctl enable ts-capture-ui
sudo systemctl start ts-capture-ui
sudo systemctl status ts-capture-ui
```

Adjust these fields in the unit file as needed:

- `User` / `Group`
- `WorkingDirectory`
- `ExecStart` virtualenv path
- `TS_CAPTURE_DIR` environment value

## Running multiple instances

The same codebase can run multiple independent instances (for example `primary` and `processed`) by using different environment files.

- each instance needs a unique `TS_CAPTURE_DIR`
- each instance needs a unique `TS_BIND_PORT`
- if relay mode is used, each instance needs unique `TS_RELAY_PREVIEW_PORT` and `TS_RELAY_CAPTURE_PORT`
- each instance can set a different `TS_DEFAULT_INPUT_URL`
- schedules, thumbnails, preview image, and capture list are isolated per instance directory

| Instance | UI port | Capture dir | Input port | Relay ports |
| --- | --- | --- | --- | --- |
| primary | 8180 | /data/captures/primary | 5000 | 5101, 5102 |
| processed | 8181 | /data/captures/processed | 6000 | 5201, 5202 |

Example setup:

```bash
sudo mkdir -p /etc/ts-capture-ui
sudo cp examples/primary.env /etc/ts-capture-ui/primary.env
sudo cp examples/processed.env /etc/ts-capture-ui/processed.env

sudo mkdir -p /data/captures/primary /data/captures/processed
sudo chown -R tscapture:tscapture /data/captures

sudo cp systemd/ts-capture-ui@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ts-capture-ui@primary
sudo systemctl enable --now ts-capture-ui@processed
```

## Main routes

- `GET /` - web UI
- `GET /api/status` - live capture/preview/relay status JSON
- `POST /capture-fixed` - fixed-duration snippet capture
- `POST /manual/start` / `POST /manual/stop` - manual capture control
- `POST /preview/start` / `POST /preview/stop` - preview control
- `POST /relay/start` / `POST /relay/stop` - local relay/fanout control
- `POST /schedules/create` - create interval schedule
- `POST /schedules/delete/{schedule_id}` - delete schedule
- `POST /schedules/enable/{schedule_id}` / `POST /schedules/disable/{schedule_id}` - toggle schedule
- `POST /schedules/run-now/{schedule_id}` - run schedule immediately
- `POST /schedules/delayed-enable/{schedule_id}` / `POST /schedules/clear-delayed-enable/{schedule_id}` - schedule or clear future enable
- `POST /schedules/delayed-disable/{schedule_id}` / `POST /schedules/clear-delayed-disable/{schedule_id}` - schedule or clear future disable
- `GET /download/{filename}` - download `.ts` file
- `POST /delete/{filename}` - delete `.ts` file
- `GET /thumbnail/{size}/{filename}` - fetch `small`/`large` JPEG capture thumbnail
- `POST /thumbnails/regenerate/{filename}` - regenerate a capture's thumbnails
- `GET /probe/{filename}` - optional ffprobe metadata (JSON)

## Live capture monitoring

The status section in the web UI is progressively enhanced with lightweight polling:

- browser polls `GET /api/status` once per second
- if JavaScript is unavailable, the page still renders server-side status text
- live status updates include capture state, active capture filename, mode, elapsed time, file size, approximate bitrate, input URL, and start time in UTC
- for fixed-duration and scheduled captures, expected/remaining seconds are included when known
- preview and relay running state are included in the same payload

Monitoring metrics are intentionally simple:

- elapsed seconds are calculated from process start time (in-memory `started_at_utc`)
- file size is read from the active `.ts` file on disk
- approximate bitrate is computed from `file_size_bytes / elapsed_seconds`
- bitrate is an approximation, not parsed from the MPEG-TS stream
- ffmpeg logs are not parsed for bitrate in this version

State model:

- active capture process metadata is kept in memory only
- finished processes are cleaned up on status refresh and key route entry points
- in-memory monitoring state resets when the app restarts

### Live monitoring test checklist

- start manual capture and verify status updates every second
- verify elapsed seconds increase while running
- verify file size increases while running
- verify approximate bitrate looks plausible for your input
- stop manual capture and verify state returns to idle
- start fixed-duration capture (for example, 30s)
- verify elapsed and remaining seconds update during fixed capture
- verify fixed capture disappears from active list after completion
- verify finished file appears in capture list after refresh
- verify preview status still reflects running/stopped correctly
- verify relay status still reflects running/stopped correctly
- verify `/api/status` handles file-not-yet-created (`size=0`) safely
- verify completed processes are cleaned up without endpoint errors

## Mini Scheduler

The Mini Scheduler is an internal, interval-based scheduler built with APScheduler (`BackgroundScheduler`):

- schedules capture fixed-duration `.ts` snippets every N minutes
- supports one-time delayed start/stop (enable/disable) for each schedule
- stores schedules in `<TS_CAPTURE_DIR>/<TS_SCHEDULES_FILENAME>` (default `/data/captures/schedules.json`)
- uses UTC timestamps for schedule metadata
- reloads enabled schedules from JSON when the app starts
- only runs while the FastAPI service is running

This feature is intentionally basic. It is not a full DVR system and does not implement calendar rules, timezone UI, or advanced conflict resolution.

### Persistence and restart behavior

- schedule changes are persisted immediately using atomic JSON writes
- if the service restarts, enabled schedules are loaded and re-registered automatically
- delayed start/stop timers are stored in JSON and re-registered on restart (if still in the future)
- delayed start immediately kicks off a capture at the delayed time, then continues on interval
- active in-memory process handles are not restored across restart

### systemd log checks

If running under systemd, inspect service logs with:

```bash
sudo journalctl -u ts-capture-ui -f
```

For recent logs:

```bash
sudo journalctl -u ts-capture-ui -n 200 --no-pager
```

### Input stream warning

If your input is unicast UDP, multiple simultaneous consumers may not work unless you relay/fan out the stream first. Manual capture, scheduled capture, and preview can compete for the same source depending on your deployment. For more robust operation, place a relay/fanout process in front of the app.

## Relay / Fanout mode

Relay mode exists to avoid unicast UDP consumer conflicts when preview and capture run at the same time. Instead of having multiple ffmpeg processes bind the same input URL, a single relay ffmpeg process reads the source once and fans out to two localhost UDP outputs:

- preview reads from `udp://<TS_RELAY_HOST>:<TS_RELAY_PREVIEW_PORT>?...` (default `127.0.0.1:5101`)
- capture reads from `udp://<TS_RELAY_HOST>:<TS_RELAY_CAPTURE_PORT>?...` (default `127.0.0.1:5102`)
- multicast is not required for this local EC2 use case (`239.0.0.1` should not be needed)
- if nothing is listening on one relay output, UDP packets to that output are discarded
- capture starts from the point where that capture process begins listening

Example relay command:

```bash
ffmpeg \
  -hide_banner \
  -loglevel warning \
  -i "udp://0.0.0.0:5000?fifo_size=10000000&overrun_nonfatal=1" \
  -map 0 \
  -c copy \
  -f tee \
  "[f=mpegts]udp://127.0.0.1:5101?pkt_size=1316|[f=mpegts]udp://127.0.0.1:5102?pkt_size=1316"
```

The web UI includes a **Relay / Fanout** section to start/stop the relay and select relay usage for preview/capture/schedules.

### Start relay by default on startup

You can auto-start relay when FastAPI boots with:

- `TS_RELAY_AUTOSTART=1`
- optional `TS_RELAY_INPUT_URL` (defaults to the app `DEFAULT_INPUT_URL`)

Example:

```bash
TS_RELAY_AUTOSTART=1 \
TS_RELAY_INPUT_URL="udp://0.0.0.0:5000?fifo_size=10000000&overrun_nonfatal=1" \
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

For `systemd`, add the same environment variables in the unit file, then restart the service.

### Start preview by default on startup

You can also auto-start preview at app boot:

- `TS_PREVIEW_AUTOSTART=1`
- optional `TS_PREVIEW_INPUT_URL` (defaults to the app `DEFAULT_INPUT_URL`)
- optional `TS_PREVIEW_USE_RELAY=1` (default `1`; if relay is running, preview uses relay input)

Example:

```bash
TS_RELAY_AUTOSTART=1 \
TS_PREVIEW_AUTOSTART=1 \
TS_PREVIEW_USE_RELAY=1 \
TS_PREVIEW_INPUT_URL="udp://0.0.0.0:5000?fifo_size=10000000&overrun_nonfatal=1" \
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Capture thumbnails

For each captured `.ts` file, the app generates two JPEG thumbnails from the first decoded video frame:

- small thumbnail (width `120`) for the capture table
- large thumbnail (width `480`) for hover preview

Thumbnails are stored at:

- `<TS_CAPTURE_DIR>/<TS_THUMBNAILS_DIRNAME>` (default `/data/captures/thumbnails`)

The filenames are derived from the capture filename stem:

- `snippet_20260606T120000Z.ts`
- `snippet_20260606T120000Z.small.jpg`
- `snippet_20260606T120000Z.large.jpg`

Generation is best-effort and does not affect capture success. If ffmpeg cannot decode a usable first frame (for example, missing video stream), the capture still appears normally and UI may show "No thumbnail".

### Thumbnail testing checklist

- capture a short `.ts` file
- confirm both `*.small.jpg` and `*.large.jpg` are created
- confirm small thumbnail appears in the capture list
- confirm hovering the small thumbnail shows larger preview
- confirm download still works
- confirm delete removes the `.ts` file and both thumbnails
- confirm regenerate thumbnails works from the file action
- confirm a capture with no video stream still appears without breaking the page
- confirm path traversal attempts against thumbnail/download/delete routes fail
- confirm listing/startup remains responsive with many capture files

### Relay testing checklist

- start relay from the UI and confirm status shows running
- start preview with relay enabled and confirm thumbnails update
- start fixed-duration capture with relay enabled while preview is running
- confirm `.ts` file is created, download it, and verify it plays/probes
- stop preview while relay keeps running
- start manual capture while relay keeps running, then stop and confirm file finalizes
- stop relay and confirm direct input capture still works without relay
- start relay with a different input URL and confirm relay restarts cleanly
- restart the app and confirm it does not report relay running from stale in-memory state

## Multi-instance testing checklist

- run app without environment variables and confirm defaults still work
- run primary instance with `examples/primary.env`
- run processed instance with `examples/processed.env`
- confirm primary UI header shows `primary`
- confirm processed UI header shows `processed`
- confirm primary saves captures to `/data/captures/primary`
- confirm processed saves captures to `/data/captures/processed`
- confirm primary and processed use different preview images
- confirm primary and processed use different thumbnail directories
- confirm primary and processed use different schedules files
- confirm both instances run simultaneously on ports `8180` and `8181`
- confirm relay mode works in both instances without port collisions
- confirm downloads only work from the configured instance capture directory
- confirm deletes only affect files in the configured instance capture directory
- confirm thumbnail generation uses the configured instance thumbnail directory
- confirm path traversal attempts fail

## Notes

- Capture output files are generated server-side with UTC timestamps.
- File download/delete/probe operations are restricted to `TS_CAPTURE_DIR` (default `/data/captures`) and `.ts` filenames.
- Process state is in-memory only; active process references are not restored after app restart.
# ts-capture-ui
