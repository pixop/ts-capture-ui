import json
import os
import re
import signal
import subprocess
import threading
import uuid
from urllib.parse import quote_plus
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import settings


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


CAPTURE_DIR = settings.capture_dir
SCHEDULES_FILE = settings.schedules_path
DEFAULT_INPUT_URL = settings.default_input_url
RELAY_PREVIEW_URL = settings.relay_preview_url
RELAY_CAPTURE_URL = settings.relay_capture_url
RELAY_TEE_OUTPUT = settings.relay_tee_output
TS_RELAY_AUTOSTART = env_flag("TS_RELAY_AUTOSTART", "0")
TS_RELAY_INPUT_URL = os.getenv("TS_RELAY_INPUT_URL", DEFAULT_INPUT_URL).strip() or DEFAULT_INPUT_URL
TS_PREVIEW_AUTOSTART = env_flag("TS_PREVIEW_AUTOSTART", "0")
TS_PREVIEW_INPUT_URL = os.getenv("TS_PREVIEW_INPUT_URL", DEFAULT_INPUT_URL).strip() or DEFAULT_INPUT_URL
TS_PREVIEW_USE_RELAY = env_flag("TS_PREVIEW_USE_RELAY", "1")
THUMBNAIL_GENERATION_PER_LIST_LIMIT = 3
VALID_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.ts$")
VALID_SCHEDULE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

app = FastAPI(title=f"TS Capture UI - {settings.instance_name}")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# MVP in-memory process state.
manual_capture_process: Optional[subprocess.Popen] = None
manual_capture_file: Optional[str] = None
preview_process: Optional[subprocess.Popen] = None
preview_input_url: Optional[str] = None
relay_process: Optional[subprocess.Popen] = None
relay_input_url: Optional[str] = None

scheduler = BackgroundScheduler(timezone=timezone.utc)
scheduler_lock = threading.Lock()
schedules_lock = threading.Lock()
active_captures_lock = threading.Lock()


@dataclass
class ScheduleConfig:
    id: str
    name: str
    input_url: str
    duration_seconds: int
    interval_minutes: int
    enabled: bool
    use_relay: bool
    created_at_utc: str
    last_run_utc: Optional[str]
    last_output_file: Optional[str]
    last_error: Optional[str]
    delayed_enable_at_utc: Optional[str]
    delayed_disable_at_utc: Optional[str]


@dataclass
class ActiveCapture:
    id: str
    process: subprocess.Popen
    output_path: Path
    input_url: str
    mode: str  # "manual", "fixed-duration", "scheduled"
    started_at_utc: datetime
    expected_duration_seconds: Optional[int]
    schedule_id: Optional[str] = None


schedules_by_id: dict[str, ScheduleConfig] = {}
active_scheduled_processes: dict[str, subprocess.Popen] = {}
active_captures: dict[str, ActiveCapture] = {}
last_completed_capture: Optional[dict[str, Any]] = None
thumbnail_generation_lock = threading.Lock()
thumbnail_generation_in_progress: set[str] = set()


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_utc_with_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    normalized = dt.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def iso_utc_for_input(dt_iso: Optional[str]) -> str:
    if not dt_iso:
        return ""
    try:
        parsed = datetime.fromisoformat(dt_iso)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed_utc = parsed.astimezone(timezone.utc)
    return parsed_utc.strftime("%Y-%m-%dT%H:%M")


def parse_utc_datetime_form(raw_value: str) -> datetime:
    value = raw_value.strip()
    if not value:
        raise ValueError("Datetime cannot be empty.")
    # Browser datetime-local is naive; we intentionally interpret it as UTC.
    formats = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")
    parsed: Optional[datetime] = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Invalid datetime format. Use YYYY-MM-DDTHH:MM (UTC).")
    return parsed.replace(tzinfo=timezone.utc)


def sanitize_schedule_name(name: str) -> str:
    safe = VALID_SCHEDULE_NAME_RE.sub("_", name.strip().lower()).strip("_")
    return safe[:40] or "schedule"


def build_capture_command(
    input_url: str, output_file: Path, duration: Optional[int] = None
) -> list[str]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        input_url,
    ]
    if duration is not None:
        cmd.extend(["-t", str(duration)])
    cmd.extend(["-map", "0", "-c", "copy", "-f", "mpegts", str(output_file)])
    return cmd


def build_preview_command(input_url: str) -> list[str]:
    output_file = settings.preview_path
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-skip_frame",
        "nokey",
        "-i",
        input_url,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        "scale=480:-2",
        "-q:v",
        "5",
        "-update",
        "1",
        "-y",
        str(output_file),
    ]


def build_relay_command(input_url: str) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        input_url,
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "tee",
        RELAY_TEE_OUTPUT,
    ]


def is_process_running(process: Optional[subprocess.Popen]) -> bool:
    return process is not None and process.poll() is None


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 0:
        num_bytes = 0
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024**2:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024**3:
        return f"{num_bytes / (1024**2):.1f} MB"
    return f"{num_bytes / (1024**3):.1f} GB"


def is_relay_running() -> bool:
    return is_process_running(relay_process)


def stop_relay() -> None:
    global relay_process, relay_input_url
    if is_process_running(relay_process):
        stop_process(relay_process)
    relay_process = None
    relay_input_url = None


def start_relay(input_url: str) -> None:
    global relay_process, relay_input_url
    normalized_input = input_url.strip()
    if not normalized_input:
        raise ValueError("Relay input URL cannot be empty.")

    if is_relay_running() and relay_input_url == normalized_input:
        return
    if is_relay_running():
        stop_relay()

    relay_process = subprocess.Popen(build_relay_command(normalized_input))
    relay_input_url = normalized_input


def get_effective_preview_url(user_input_url: str, use_relay: bool) -> str:
    if use_relay and is_relay_running():
        return RELAY_PREVIEW_URL
    return user_input_url


def get_effective_capture_url(user_input_url: str, use_relay: bool) -> str:
    if use_relay and is_relay_running():
        return RELAY_CAPTURE_URL
    return user_input_url


def start_preview_process(input_url: str, use_relay: bool) -> None:
    global preview_process, preview_input_url
    normalized_input = input_url.strip()
    if not normalized_input:
        raise ValueError("Input URL cannot be empty.")
    if is_process_running(preview_process):
        raise RuntimeError("Preview is already running.")

    effective_input_url = get_effective_preview_url(normalized_input, use_relay)
    cmd = build_preview_command(effective_input_url)
    try:
        preview_process = subprocess.Popen(cmd)
        preview_input_url = effective_input_url
    except Exception as exc:
        preview_process = None
        preview_input_url = None
        raise RuntimeError("Failed to start preview process.") from exc


def validate_duration_seconds(duration_seconds: int) -> None:
    if duration_seconds < 1 or duration_seconds > 3600:
        raise ValueError("Duration must be between 1 and 3600 seconds.")


def validate_interval_minutes(interval_minutes: int) -> None:
    if interval_minutes < 1 or interval_minutes > 1440:
        raise ValueError("Interval must be between 1 and 1440 minutes.")


def safe_capture_path(filename: str) -> Path:
    if not VALID_FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = (settings.capture_dir / filename).resolve()
    capture_dir_resolved = settings.capture_dir.resolve()
    if not is_relative_to(path, capture_dir_resolved) or path.parent != capture_dir_resolved:
        raise HTTPException(status_code=400, detail="Invalid file path.")
    return path


def is_safe_ts_capture_path(capture_path: Path) -> bool:
    try:
        resolved_path = capture_path.resolve()
    except OSError:
        return False
    if resolved_path.suffix.lower() != ".ts":
        return False
    return is_relative_to(resolved_path, settings.capture_dir) and resolved_path.parent == settings.capture_dir.resolve()


def thumbnail_dir() -> Path:
    return settings.thumbnails_dir


def thumbnail_paths_for_capture(capture_path: Path) -> tuple[Path, Path]:
    if not is_safe_ts_capture_path(capture_path):
        raise ValueError("Invalid capture path for thumbnail generation.")
    stem = capture_path.stem
    thumbs_dir = thumbnail_dir()
    return thumbs_dir / f"{stem}.small.jpg", thumbs_dir / f"{stem}.large.jpg"


def is_safe_thumbnail_path(path: Path) -> bool:
    try:
        resolved_path = path.resolve()
    except OSError:
        return False
    return is_relative_to(resolved_path, settings.thumbnails_dir)


def thumbnail_urls_for_capture(filename: str) -> dict:
    safe_capture_path(filename)
    return {
        "small_thumbnail_url": f"/thumbnail/small/{filename}",
        "large_thumbnail_url": f"/thumbnail/large/{filename}",
    }


def capture_has_thumbnails(capture_path: Path) -> bool:
    try:
        small_thumb, large_thumb = thumbnail_paths_for_capture(capture_path)
    except ValueError:
        return False
    return small_thumb.exists() and large_thumb.exists()


def _generate_thumbnail_with_ffmpeg(capture_path: Path, width: int, output_path: Path) -> bool:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(capture_path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        f"yadif,scale={width}:-2",
        "-q:v",
        "5",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0


def generate_thumbnails_for_capture(capture_path: Path) -> None:
    if not is_safe_ts_capture_path(capture_path):
        return
    if not capture_path.exists() or not capture_path.is_file() or capture_path.stat().st_size <= 0:
        return

    thumbs_dir = thumbnail_dir()
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    small_thumb, large_thumb = thumbnail_paths_for_capture(capture_path)
    if small_thumb.exists() and large_thumb.exists():
        return

    try:
        if not small_thumb.exists():
            _generate_thumbnail_with_ffmpeg(capture_path, 120, small_thumb)
        if not large_thumb.exists():
            _generate_thumbnail_with_ffmpeg(capture_path, 480, large_thumb)
    except Exception:
        # Best-effort only; capture listing should continue even if ffmpeg fails.
        return


def generate_thumbnails_for_capture_async(capture_path: Path) -> None:
    if not is_safe_ts_capture_path(capture_path):
        return
    capture_key = str(capture_path.resolve())
    with thumbnail_generation_lock:
        if capture_key in thumbnail_generation_in_progress:
            return
        thumbnail_generation_in_progress.add(capture_key)

    def worker() -> None:
        try:
            generate_thumbnails_for_capture(capture_path)
        finally:
            with thumbnail_generation_lock:
                thumbnail_generation_in_progress.discard(capture_key)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def register_active_capture(metadata: ActiveCapture) -> None:
    with active_captures_lock:
        active_captures[metadata.id] = metadata


def build_capture_status(metadata: ActiveCapture) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    elapsed = max(0.0, (now - metadata.started_at_utc).total_seconds())
    try:
        file_size_bytes = metadata.output_path.stat().st_size
    except FileNotFoundError:
        file_size_bytes = 0
    except OSError:
        file_size_bytes = 0

    approx_bitrate_mbps: Optional[float] = None
    if elapsed >= 0.5:
        approx_bitrate_mbps = round(file_size_bytes * 8 / elapsed / 1_000_000, 1)

    expected = metadata.expected_duration_seconds
    remaining: Optional[float] = None
    if expected is not None:
        remaining = max(0.0, expected - elapsed)

    return {
        "id": metadata.id,
        "running": is_process_running(metadata.process),
        "filename": metadata.output_path.name,
        "input_url": metadata.input_url,
        "mode": metadata.mode,
        "started_at_utc": iso_utc_with_z(metadata.started_at_utc),
        "elapsed_seconds": round(elapsed, 1),
        "file_size_bytes": file_size_bytes,
        "file_size_human": format_bytes(file_size_bytes),
        "approx_bitrate_mbps": approx_bitrate_mbps,
        "expected_duration_seconds": expected,
        "remaining_seconds": round(remaining, 1) if remaining is not None else None,
    }


def process_finished_capture(capture_id: str, metadata: ActiveCapture, return_code: int) -> None:
    global manual_capture_process, manual_capture_file, last_completed_capture

    if capture_id == "manual":
        manual_capture_process = None
        manual_capture_file = None

    capture_size_bytes = 0
    try:
        capture_size_bytes = metadata.output_path.stat().st_size
    except OSError:
        capture_size_bytes = 0

    elapsed = max(0.0, (datetime.now(timezone.utc) - metadata.started_at_utc).total_seconds())
    approx_bitrate_mbps: Optional[float] = None
    if elapsed >= 0.5:
        approx_bitrate_mbps = round(capture_size_bytes * 8 / elapsed / 1_000_000, 1)

    if return_code == 0 and capture_size_bytes > 0:
        generate_thumbnails_for_capture_async(metadata.output_path)

    last_completed_capture = {
        "id": metadata.id,
        "mode": metadata.mode,
        "filename": metadata.output_path.name,
        "duration_seconds": round(elapsed, 1),
        "file_size_bytes": capture_size_bytes,
        "file_size_human": format_bytes(capture_size_bytes),
        "approx_bitrate_mbps": approx_bitrate_mbps,
        "completed_at_utc": iso_utc_with_z(datetime.now(timezone.utc)),
        "return_code": return_code,
    }

    if metadata.schedule_id is None:
        return

    should_persist = False
    with schedules_lock:
        schedule = schedules_by_id.get(metadata.schedule_id)
        if schedule is not None:
            if return_code == 0:
                schedule.last_output_file = metadata.output_path.name
                schedule.last_error = None
            else:
                schedule.last_error = "Scheduled capture failed. Check ffmpeg/logs."
            should_persist = True
        active_scheduled_processes.pop(metadata.schedule_id, None)
    if should_persist:
        write_schedules_atomic()


def cleanup_finished_captures() -> None:
    finished: list[tuple[str, ActiveCapture, int]] = []
    with active_captures_lock:
        for capture_id, metadata in list(active_captures.items()):
            return_code = metadata.process.poll()
            if return_code is None:
                continue
            active_captures.pop(capture_id, None)
            finished.append((capture_id, metadata, return_code))

    for capture_id, metadata, return_code in finished:
        process_finished_capture(capture_id, metadata, return_code)


def get_capture_state() -> str:
    cleanup_finished_captures()
    with active_captures_lock:
        has_capture = bool(active_captures)
    if has_capture:
        return "recording"
    if is_process_running(preview_process):
        return "preview running"
    if is_relay_running():
        return "relay running"
    return "idle"


def build_status_payload() -> dict[str, Any]:
    cleanup_finished_captures()
    with active_captures_lock:
        captures_snapshot = list(active_captures.values())

    active_capture_items = [build_capture_status(item) for item in captures_snapshot]
    active_capture_items.sort(key=lambda item: item["started_at_utc"], reverse=True)

    manual_item = next((item for item in active_capture_items if item["id"] == "manual"), None)
    if manual_item is None:
        manual_item = {
            "running": False,
            "filename": None,
            "input_url": None,
            "mode": "manual",
            "started_at_utc": None,
            "elapsed_seconds": 0.0,
            "file_size_bytes": 0,
            "file_size_human": format_bytes(0),
            "approx_bitrate_mbps": None,
            "expected_duration_seconds": None,
            "remaining_seconds": None,
        }
    else:
        manual_item = {k: v for k, v in manual_item.items() if k != "id"}

    relay_running = is_relay_running()
    preview_running = is_process_running(preview_process)
    return {
        "capture_state": get_capture_state(),
        "manual_capture": manual_item,
        "active_captures": active_capture_items,
        "preview": {
            "running": preview_running,
            "input_url": preview_input_url if preview_running else None,
        },
        "relay": {
            "running": relay_running,
            "input_url": relay_input_url if relay_running else None,
        },
        "last_completed_capture": last_completed_capture,
    }


def validate_schedule_model(schedule: ScheduleConfig) -> None:
    if not schedule.name.strip():
        raise ValueError("Schedule name cannot be empty.")
    if not schedule.input_url.strip():
        raise ValueError("Input URL cannot be empty.")
    validate_duration_seconds(schedule.duration_seconds)
    validate_interval_minutes(schedule.interval_minutes)


def schedule_job_id(schedule_id: str) -> str:
    return f"schedule:{schedule_id}"


def delayed_enable_job_id(schedule_id: str) -> str:
    return f"schedule-control:enable:{schedule_id}"


def delayed_disable_job_id(schedule_id: str) -> str:
    return f"schedule-control:disable:{schedule_id}"


def write_schedules_atomic() -> None:
    with schedules_lock:
        serialized = [asdict(item) for item in schedules_by_id.values()]
    tmp_file = SCHEDULES_FILE.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
    tmp_file.replace(SCHEDULES_FILE)


def load_schedules() -> None:
    if not SCHEDULES_FILE.exists():
        return
    try:
        raw = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
    except (json.JSONDecodeError, OSError):
        return

    loaded: dict[str, ScheduleConfig] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            schedule = ScheduleConfig(
                id=str(item["id"]),
                name=str(item["name"]),
                input_url=str(item["input_url"]),
                duration_seconds=int(item["duration_seconds"]),
                interval_minutes=int(item["interval_minutes"]),
                enabled=bool(item["enabled"]),
                use_relay=bool(item.get("use_relay", False)),
                created_at_utc=str(item["created_at_utc"]),
                last_run_utc=item.get("last_run_utc"),
                last_output_file=item.get("last_output_file"),
                last_error=item.get("last_error"),
                delayed_enable_at_utc=item.get("delayed_enable_at_utc"),
                delayed_disable_at_utc=item.get("delayed_disable_at_utc"),
            )
            validate_schedule_model(schedule)
        except (KeyError, TypeError, ValueError):
            continue
        loaded[schedule.id] = schedule

    with schedules_lock:
        schedules_by_id.clear()
        schedules_by_id.update(loaded)


def make_capture_file(prefix: str) -> Path:
    return CAPTURE_DIR / f"{prefix}_{timestamp_utc()}.ts"


def launch_fixed_duration_capture_process(
    input_url: str, duration_seconds: int, prefix: str = "snippet"
) -> tuple[subprocess.Popen, Path]:
    input_url = input_url.strip()
    if not input_url:
        raise ValueError("Input URL cannot be empty.")
    validate_duration_seconds(duration_seconds)

    output_file = make_capture_file(prefix)
    cmd = build_capture_command(input_url, output_file, duration=duration_seconds)
    process = subprocess.Popen(cmd)
    return process, output_file


def start_fixed_duration_capture(
    input_url: str, duration_seconds: int, prefix: str = "snippet"
) -> Path:
    process, output_file = launch_fixed_duration_capture_process(
        input_url=input_url,
        duration_seconds=duration_seconds,
        prefix=prefix,
    )
    capture_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    register_active_capture(
        ActiveCapture(
            id=capture_id,
            process=process,
            output_path=output_file,
            input_url=input_url.strip(),
            mode="fixed-duration",
            started_at_utc=datetime.now(timezone.utc),
            expected_duration_seconds=duration_seconds,
        )
    )
    return output_file


def register_schedule_job(schedule: ScheduleConfig) -> None:
    if not schedule.enabled:
        return
    job_id = schedule_job_id(schedule.id)
    with scheduler_lock:
        existing_job = scheduler.get_job(job_id)
        if existing_job is not None:
            scheduler.remove_job(job_id)
        scheduler.add_job(
            run_scheduled_capture,
            "interval",
            minutes=schedule.interval_minutes,
            id=job_id,
            args=[schedule.id],
        )


def register_delayed_enable_job(schedule: ScheduleConfig) -> None:
    job_id = delayed_enable_job_id(schedule.id)
    with scheduler_lock:
        existing_job = scheduler.get_job(job_id)
        if existing_job is not None:
            scheduler.remove_job(job_id)
    if not schedule.delayed_enable_at_utc:
        return
    try:
        run_at = datetime.fromisoformat(schedule.delayed_enable_at_utc)
    except ValueError:
        return
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    run_at = run_at.astimezone(timezone.utc)
    if run_at <= datetime.now(timezone.utc):
        return
    with scheduler_lock:
        scheduler.add_job(
            run_delayed_enable,
            "date",
            run_date=run_at,
            id=job_id,
            args=[schedule.id],
        )


def register_delayed_disable_job(schedule: ScheduleConfig) -> None:
    job_id = delayed_disable_job_id(schedule.id)
    with scheduler_lock:
        existing_job = scheduler.get_job(job_id)
        if existing_job is not None:
            scheduler.remove_job(job_id)
    if not schedule.delayed_disable_at_utc:
        return
    try:
        run_at = datetime.fromisoformat(schedule.delayed_disable_at_utc)
    except ValueError:
        return
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    run_at = run_at.astimezone(timezone.utc)
    if run_at <= datetime.now(timezone.utc):
        return
    with scheduler_lock:
        scheduler.add_job(
            run_delayed_disable,
            "date",
            run_date=run_at,
            id=job_id,
            args=[schedule.id],
        )


def unregister_delayed_control_jobs(schedule_id: str) -> None:
    for job_id in (delayed_enable_job_id(schedule_id), delayed_disable_job_id(schedule_id)):
        with scheduler_lock:
            existing_job = scheduler.get_job(job_id)
            if existing_job is not None:
                scheduler.remove_job(job_id)


def unregister_schedule_job(schedule_id: str) -> None:
    job_id = schedule_job_id(schedule_id)
    with scheduler_lock:
        existing_job = scheduler.get_job(job_id)
        if existing_job is not None:
            scheduler.remove_job(job_id)


def update_schedule(schedule_id: str, **updates) -> Optional[ScheduleConfig]:
    with schedules_lock:
        schedule = schedules_by_id.get(schedule_id)
        if schedule is None:
            return None
        for key, value in updates.items():
            setattr(schedule, key, value)
        updated = schedule
    write_schedules_atomic()
    return updated


def start_scheduled_capture(schedule: ScheduleConfig) -> bool:
    cleanup_finished_captures()
    should_persist = False
    with schedules_lock:
        existing_process = active_scheduled_processes.get(schedule.id)
        if is_process_running(existing_process):
            schedule.last_error = "Previous scheduled capture still running; skipped this run."
            should_persist = True
    if should_persist:
        write_schedules_atomic()
        return False

    safe_name = sanitize_schedule_name(schedule.name)
    prefix = f"scheduled_{safe_name}"
    effective_input_url = schedule.input_url
    warning_message: Optional[str] = None
    if schedule.use_relay:
        if is_relay_running():
            effective_input_url = RELAY_CAPTURE_URL
        else:
            warning_message = "Relay was not running; used direct input URL instead."

    try:
        process, output_file = launch_fixed_duration_capture_process(
            input_url=effective_input_url,
            duration_seconds=schedule.duration_seconds,
            prefix=prefix,
        )
    except Exception as exc:
        update_schedule(schedule.id, last_error=f"Failed to start scheduled capture: {exc}")
        return False

    with schedules_lock:
        refreshed = schedules_by_id.get(schedule.id)
        if refreshed is not None:
            refreshed.last_run_utc = iso_utc_now()
            refreshed.last_error = warning_message
            active_scheduled_processes[schedule.id] = process
    write_schedules_atomic()
    register_active_capture(
        ActiveCapture(
            id=f"scheduled:{schedule.id}",
            process=process,
            output_path=output_file,
            input_url=effective_input_url,
            mode="scheduled",
            started_at_utc=datetime.now(timezone.utc),
            expected_duration_seconds=schedule.duration_seconds,
            schedule_id=schedule.id,
        )
    )
    return True


def run_scheduled_capture(schedule_id: str) -> None:
    with schedules_lock:
        schedule = schedules_by_id.get(schedule_id)
        if schedule is None or not schedule.enabled:
            return
    start_scheduled_capture(schedule)


def run_delayed_enable(schedule_id: str) -> None:
    schedule = update_schedule(
        schedule_id,
        enabled=True,
        delayed_enable_at_utc=None,
        last_error=None,
    )
    if schedule is None:
        return
    register_schedule_job(schedule)
    # Delayed start should begin capture at the delayed timestamp,
    # not wait for the first interval boundary.
    start_scheduled_capture(schedule)


def run_delayed_disable(schedule_id: str) -> None:
    schedule = update_schedule(
        schedule_id,
        enabled=False,
        delayed_disable_at_utc=None,
        last_error=None,
    )
    if schedule is None:
        return
    unregister_schedule_job(schedule_id)


def list_captures() -> list[dict]:
    captures: list[dict] = []
    queued_generations = 0
    if not settings.capture_dir.exists():
        return captures
    for file_path in settings.capture_dir.glob("*.ts"):
        if file_path.is_file():
            stat = file_path.stat()
            captures.append(
                {
                    "filename": file_path.name,
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "modified_ts": stat.st_mtime,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "modified_time": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "download_url": f"/download/{file_path.name}",
                    "delete_url": f"/delete/{file_path.name}",
                }
            )
            urls = thumbnail_urls_for_capture(file_path.name)
            captures[-1].update(urls)
            has_thumbnails = capture_has_thumbnails(file_path)
            captures[-1]["has_thumbnail"] = has_thumbnails
            if not has_thumbnails and queued_generations < THUMBNAIL_GENERATION_PER_LIST_LIMIT:
                generate_thumbnails_for_capture_async(file_path)
                queued_generations += 1
    captures.sort(key=lambda item: item["modified_ts"], reverse=True)
    return captures


def redirect_with_message(message: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?msg={quote_plus(message)}", status_code=303)


def stop_process(process: subprocess.Popen) -> None:
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@app.on_event("startup")
def on_startup() -> None:
    if not is_relative_to(settings.preview_path, settings.capture_dir):
        raise RuntimeError("TS_PREVIEW_FILENAME must resolve inside TS_CAPTURE_DIR.")
    if not is_relative_to(settings.schedules_path, settings.capture_dir):
        raise RuntimeError("TS_SCHEDULES_FILENAME must resolve inside TS_CAPTURE_DIR.")
    if not is_relative_to(settings.thumbnails_dir, settings.capture_dir):
        raise RuntimeError("TS_THUMBNAILS_DIRNAME must resolve inside TS_CAPTURE_DIR.")
    settings.capture_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_dir().mkdir(parents=True, exist_ok=True)
    load_schedules()
    with scheduler_lock:
        if not scheduler.running:
            scheduler.start()
    with schedules_lock:
        all_schedules = list(schedules_by_id.values())
    for schedule in all_schedules:
        if schedule.enabled:
            register_schedule_job(schedule)
        register_delayed_enable_job(schedule)
        register_delayed_disable_job(schedule)
    if TS_RELAY_AUTOSTART:
        try:
            start_relay(TS_RELAY_INPUT_URL)
        except Exception as exc:
            print(f"Relay autostart failed: {exc}")
    if TS_PREVIEW_AUTOSTART:
        try:
            start_preview_process(
                input_url=TS_PREVIEW_INPUT_URL,
                use_relay=TS_PREVIEW_USE_RELAY,
            )
        except Exception as exc:
            print(f"Preview autostart failed: {exc}")


@app.on_event("shutdown")
def on_shutdown() -> None:
    with scheduler_lock:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    stop_relay()


def list_schedules() -> list[dict]:
    with schedules_lock:
        schedules = [asdict(item) for item in schedules_by_id.values()]
    for item in schedules:
        item["delayed_enable_input_utc"] = iso_utc_for_input(item.get("delayed_enable_at_utc"))
        item["delayed_disable_input_utc"] = iso_utc_for_input(item.get("delayed_disable_at_utc"))
    schedules.sort(key=lambda item: item["created_at_utc"], reverse=True)
    return schedules


@app.get("/")
def index(request: Request):
    global relay_process
    cleanup_finished_captures()
    msg = request.query_params.get("msg", "")
    preview_path = settings.preview_path
    preview_exists = preview_path.exists()
    preview_last_updated_utc = "-"
    if preview_exists:
        try:
            preview_last_updated_utc = datetime.fromtimestamp(
                preview_path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
        except FileNotFoundError:
            preview_exists = False
    relay_running = is_relay_running()
    if relay_process is not None and not relay_running:
        stop_relay()
        relay_running = False
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "msg": msg,
            "instance_name": settings.instance_name,
            "capture_dir": str(settings.capture_dir),
            "default_input_url": settings.default_input_url,
            "relay_preview_url": settings.relay_preview_url,
            "relay_capture_url": settings.relay_capture_url,
            "captures": list_captures(),
            "capture_status": get_capture_state(),
            "manual_capture_file": manual_capture_file if is_process_running(manual_capture_process) else "",
            "preview_status": "running" if is_process_running(preview_process) else "stopped",
            "preview_input_url": preview_input_url or DEFAULT_INPUT_URL,
            "preview_exists": preview_exists,
            "preview_last_updated_utc": preview_last_updated_utc,
            "preview_cache_buster": int(datetime.now(tz=timezone.utc).timestamp()),
            "relay_status": "running" if relay_running else "stopped",
            "relay_running": relay_running,
            "relay_input_url": relay_input_url if relay_running else "-",
            "relay_form_input_url": relay_input_url or DEFAULT_INPUT_URL,
            "schedules": list_schedules(),
        },
    )


@app.get("/api/captures")
def api_captures():
    cleanup_finished_captures()
    running = is_process_running(manual_capture_process)
    return JSONResponse(
        content={
            "captures": list_captures(),
            "capture_status": "recording" if running else "idle",
            "manual_capture_file": manual_capture_file if running else "",
        }
    )


@app.get("/api/status")
def api_status():
    return JSONResponse(content=build_status_payload())


@app.get("/api/schedules")
def api_schedules():
    return JSONResponse(content={"schedules": list_schedules()})


@app.post("/relay/start")
def relay_start(input_url: str = Form(...)):
    normalized_input = input_url.strip()
    if not normalized_input:
        return redirect_with_message("Relay input URL cannot be empty.")
    if is_relay_running() and relay_input_url == normalized_input:
        return redirect_with_message("Relay already running for this input URL.")
    try:
        start_relay(normalized_input)
    except Exception as exc:
        stop_relay()
        return redirect_with_message(f"Failed to start relay: {exc}")
    return redirect_with_message(f"Relay started: {normalized_input}")


@app.post("/relay/stop")
def relay_stop():
    if not is_relay_running():
        stop_relay()
        return redirect_with_message("Relay is not running.")
    stop_relay()
    return redirect_with_message("Relay stopped.")


@app.post("/capture-fixed")
def capture_fixed_duration(
    input_url: str = Form(...), duration: int = Form(...), use_relay: Optional[str] = Form(None)
):
    normalized_input = input_url.strip()
    use_relay_bool = use_relay is not None
    effective_input_url = get_effective_capture_url(normalized_input, use_relay_bool)
    try:
        output_file = start_fixed_duration_capture(
            input_url=effective_input_url,
            duration_seconds=duration,
            prefix="snippet",
        )
    except ValueError as exc:
        return redirect_with_message(str(exc))
    return redirect_with_message(f"Started fixed-duration capture: {output_file.name}")


@app.post("/manual/start")
def start_manual_capture(input_url: str = Form(...), use_relay: Optional[str] = Form(None)):
    global manual_capture_process, manual_capture_file
    cleanup_finished_captures()
    normalized_input = input_url.strip()
    if not normalized_input:
        return redirect_with_message("Input URL cannot be empty.")
    if is_process_running(manual_capture_process):
        return redirect_with_message("Manual capture is already running.")

    effective_input_url = get_effective_capture_url(normalized_input, use_relay is not None)
    output_file = settings.capture_dir / f"manual_{timestamp_utc()}.ts"
    cmd = build_capture_command(effective_input_url, output_file)
    manual_capture_process = subprocess.Popen(cmd)
    manual_capture_file = output_file.name
    register_active_capture(
        ActiveCapture(
            id="manual",
            process=manual_capture_process,
            output_path=output_file,
            input_url=effective_input_url,
            mode="manual",
            started_at_utc=datetime.now(timezone.utc),
            expected_duration_seconds=None,
        )
    )
    return redirect_with_message(f"Started manual capture: {output_file.name}")


@app.post("/schedules/create")
def create_schedule(
    name: str = Form(...),
    input_url: str = Form(...),
    duration_seconds: int = Form(...),
    interval_minutes: int = Form(...),
    enabled: Optional[str] = Form(None),
    use_relay_present: Optional[str] = Form(None),
    use_relay: Optional[str] = Form(None),
):
    name = name.strip()
    input_url = input_url.strip()
    if not name:
        return redirect_with_message("Schedule name cannot be empty.")
    if not input_url:
        return redirect_with_message("Input URL cannot be empty.")

    try:
        validate_duration_seconds(duration_seconds)
        validate_interval_minutes(interval_minutes)
    except ValueError as exc:
        return redirect_with_message(str(exc))

    use_relay_enabled = (use_relay is not None) if use_relay_present is not None else is_relay_running()
    schedule = ScheduleConfig(
        id=str(uuid.uuid4()),
        name=name,
        input_url=input_url,
        duration_seconds=duration_seconds,
        interval_minutes=interval_minutes,
        enabled=enabled is not None,
        use_relay=use_relay_enabled,
        created_at_utc=iso_utc_now(),
        last_run_utc=None,
        last_output_file=None,
        last_error=None,
        delayed_enable_at_utc=None,
        delayed_disable_at_utc=None,
    )
    with schedules_lock:
        schedules_by_id[schedule.id] = schedule
    write_schedules_atomic()
    if schedule.enabled:
        register_schedule_job(schedule)
    return redirect_with_message(f"Created schedule: {schedule.name}")


@app.post("/schedules/delete/{schedule_id}")
def delete_schedule(schedule_id: str):
    unregister_schedule_job(schedule_id)
    unregister_delayed_control_jobs(schedule_id)
    with schedules_lock:
        removed = schedules_by_id.pop(schedule_id, None)
        active_scheduled_processes.pop(schedule_id, None)
    if removed is None:
        return redirect_with_message("Schedule not found.")
    write_schedules_atomic()
    return redirect_with_message(f"Deleted schedule: {removed.name}")


@app.post("/schedules/enable/{schedule_id}")
def enable_schedule(schedule_id: str):
    schedule = update_schedule(schedule_id, enabled=True)
    if schedule is None:
        return redirect_with_message("Schedule not found.")
    register_schedule_job(schedule)
    return redirect_with_message(f"Enabled schedule: {schedule.name}")


@app.post("/schedules/disable/{schedule_id}")
def disable_schedule(schedule_id: str):
    schedule = update_schedule(schedule_id, enabled=False)
    if schedule is None:
        return redirect_with_message("Schedule not found.")
    unregister_schedule_job(schedule_id)
    return redirect_with_message(f"Disabled schedule: {schedule.name}")


@app.post("/schedules/delayed-enable/{schedule_id}")
def delayed_enable_schedule(schedule_id: str, run_at_utc: str = Form(...)):
    try:
        run_at = parse_utc_datetime_form(run_at_utc)
    except ValueError as exc:
        return redirect_with_message(str(exc))
    if run_at <= datetime.now(timezone.utc):
        return redirect_with_message("Delayed start must be in the future (UTC).")

    # Delayed start should pause interval runs until the delayed trigger fires.
    schedule = update_schedule(
        schedule_id,
        enabled=False,
        delayed_enable_at_utc=run_at.replace(microsecond=0).isoformat(),
        last_error=None,
    )
    if schedule is None:
        return redirect_with_message("Schedule not found.")
    unregister_schedule_job(schedule_id)
    register_delayed_enable_job(schedule)
    return redirect_with_message(
        f"Delayed start set for {schedule.name} at {schedule.delayed_enable_at_utc}. "
        "Schedule paused until then."
    )


@app.post("/schedules/delayed-disable/{schedule_id}")
def delayed_disable_schedule(schedule_id: str, run_at_utc: str = Form(...)):
    try:
        run_at = parse_utc_datetime_form(run_at_utc)
    except ValueError as exc:
        return redirect_with_message(str(exc))
    if run_at <= datetime.now(timezone.utc):
        return redirect_with_message("Delayed stop must be in the future (UTC).")

    schedule = update_schedule(schedule_id, delayed_disable_at_utc=run_at.replace(microsecond=0).isoformat())
    if schedule is None:
        return redirect_with_message("Schedule not found.")
    register_delayed_disable_job(schedule)
    return redirect_with_message(f"Delayed stop set for {schedule.name} at {schedule.delayed_disable_at_utc}")


@app.post("/schedules/clear-delayed-enable/{schedule_id}")
def clear_delayed_enable_schedule(schedule_id: str):
    schedule = update_schedule(schedule_id, delayed_enable_at_utc=None)
    if schedule is None:
        return redirect_with_message("Schedule not found.")
    register_delayed_enable_job(schedule)
    return redirect_with_message(f"Cleared delayed start for {schedule.name}")


@app.post("/schedules/clear-delayed-disable/{schedule_id}")
def clear_delayed_disable_schedule(schedule_id: str):
    schedule = update_schedule(schedule_id, delayed_disable_at_utc=None)
    if schedule is None:
        return redirect_with_message("Schedule not found.")
    register_delayed_disable_job(schedule)
    return redirect_with_message(f"Cleared delayed stop for {schedule.name}")


@app.post("/schedules/run-now/{schedule_id}")
def run_schedule_now(schedule_id: str):
    with schedules_lock:
        schedule = schedules_by_id.get(schedule_id)
    if schedule is None:
        return redirect_with_message("Schedule not found.")
    started = start_scheduled_capture(schedule)
    if not started:
        with schedules_lock:
            refreshed = schedules_by_id.get(schedule_id)
            last_error = refreshed.last_error if refreshed else "Failed to start scheduled capture."
        return redirect_with_message(last_error or "Failed to start scheduled capture.")
    return redirect_with_message(f"Started scheduled capture now: {schedule.name}")


@app.post("/manual/stop")
def stop_manual_capture():
    global manual_capture_process, manual_capture_file
    cleanup_finished_captures()
    if not is_process_running(manual_capture_process):
        manual_capture_process = None
        manual_capture_file = None
        return redirect_with_message("No manual capture is running.")

    stop_process(manual_capture_process)
    stopped_file = manual_capture_file or "unknown.ts"
    stopped_metadata: Optional[ActiveCapture] = None
    with active_captures_lock:
        stopped_metadata = active_captures.pop("manual", None)
    if stopped_metadata is not None:
        process_finished_capture("manual", stopped_metadata, manual_capture_process.poll() or 0)
    manual_capture_process = None
    manual_capture_file = None
    return redirect_with_message(f"Stopped manual capture: {stopped_file}")


@app.post("/preview/start")
def start_preview(input_url: str = Form(...), use_relay: Optional[str] = Form(None)):
    try:
        start_preview_process(input_url=input_url, use_relay=use_relay is not None)
    except ValueError as exc:
        return redirect_with_message(str(exc))
    except RuntimeError as exc:
        return redirect_with_message(str(exc))
    return redirect_with_message("Started preview.")


@app.post("/preview/stop")
def stop_preview():
    global preview_process, preview_input_url
    if not is_process_running(preview_process):
        preview_process = None
        preview_input_url = None
        return redirect_with_message("Preview is not running.")

    stop_process(preview_process)
    preview_process = None
    preview_input_url = None
    return redirect_with_message("Stopped preview.")


@app.get("/preview.jpg")
def get_preview_image():
    preview_path = settings.preview_path
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail="Preview unavailable.")
    try:
        content = preview_path.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Preview unavailable.")
    if not content:
        raise HTTPException(status_code=503, detail="Preview is updating. Try again.")
    preview_stat = preview_path.stat()
    updated_utc = datetime.fromtimestamp(preview_stat.st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={"X-Preview-Updated-UTC": updated_utc},
    )


@app.get("/download/{filename}")
def download_capture(filename: str):
    file_path = safe_capture_path(filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        file_path,
        media_type="video/MP2T",
        filename=file_path.name,
    )


@app.get("/thumbnail/{size}/{filename}")
def get_capture_thumbnail(size: str, filename: str):
    if size not in {"small", "large"}:
        raise HTTPException(status_code=400, detail="Invalid thumbnail size.")
    capture_path = safe_capture_path(filename)
    if not capture_path.exists() or not capture_path.is_file():
        raise HTTPException(status_code=404, detail="Capture file not found.")
    small_thumb, large_thumb = thumbnail_paths_for_capture(capture_path)
    thumb_path = small_thumb if size == "small" else large_thumb
    if not is_safe_thumbnail_path(thumb_path):
        raise HTTPException(status_code=400, detail="Invalid thumbnail path.")
    if not thumb_path.exists() or not thumb_path.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not found.")
    return FileResponse(thumb_path, media_type="image/jpeg")


@app.post("/thumbnails/regenerate/{filename}")
def regenerate_capture_thumbnails(filename: str):
    capture_path = safe_capture_path(filename)
    if not capture_path.exists() or not capture_path.is_file():
        return redirect_with_message("Capture file not found.")
    try:
        small_thumb, large_thumb = thumbnail_paths_for_capture(capture_path)
    except ValueError:
        return redirect_with_message("Invalid capture path.")
    for thumb_path in (small_thumb, large_thumb):
        if is_safe_thumbnail_path(thumb_path) and thumb_path.exists() and thumb_path.is_file():
            thumb_path.unlink()
    generate_thumbnails_for_capture_async(capture_path)
    return redirect_with_message(f"Thumbnail regeneration started for {capture_path.name}")


@app.post("/delete/{filename}")
def delete_capture(filename: str):
    file_path = safe_capture_path(filename)
    if not file_path.exists() or not file_path.is_file():
        return redirect_with_message("File not found.")
    try:
        small_thumb, large_thumb = thumbnail_paths_for_capture(file_path)
    except ValueError:
        small_thumb = None
        large_thumb = None
    file_path.unlink()
    for thumb_path in (small_thumb, large_thumb):
        if (
            thumb_path is not None
            and is_safe_thumbnail_path(thumb_path)
            and thumb_path.exists()
            and thumb_path.is_file()
        ):
            thumb_path.unlink()
    return redirect_with_message(f"Deleted {file_path.name}")


@app.get("/probe/{filename}")
def probe_capture(filename: str):
    file_path = safe_capture_path(filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    cmd = [
        "ffprobe",
        "-hide_banner",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(file_path),
    ]
    process = subprocess.run(cmd, capture_output=True, text=True)
    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"ffprobe failed: {process.stderr.strip()}",
        )

    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse ffprobe output.")
    return JSONResponse(content=data)
