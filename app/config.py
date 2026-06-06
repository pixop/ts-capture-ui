from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    instance_name: str
    capture_dir: Path
    default_input_url: str
    bind_host: str
    bind_port: int
    relay_host: str
    relay_preview_port: int
    relay_capture_port: int
    preview_filename: str
    schedules_filename: str
    thumbnails_dirname: str

    @property
    def preview_path(self) -> Path:
        return self.capture_dir / self.preview_filename

    @property
    def schedules_path(self) -> Path:
        return self.capture_dir / self.schedules_filename

    @property
    def thumbnails_dir(self) -> Path:
        return self.capture_dir / self.thumbnails_dirname

    @property
    def relay_preview_url(self) -> str:
        return (
            f"udp://{self.relay_host}:{self.relay_preview_port}"
            "?fifo_size=10000000&overrun_nonfatal=1"
        )

    @property
    def relay_capture_url(self) -> str:
        return (
            f"udp://{self.relay_host}:{self.relay_capture_port}"
            "?fifo_size=10000000&overrun_nonfatal=1"
        )

    @property
    def relay_tee_output(self) -> str:
        return (
            f"[f=mpegts]udp://{self.relay_host}:{self.relay_preview_port}?pkt_size=1316"
            f"|[f=mpegts]udp://{self.relay_host}:{self.relay_capture_port}?pkt_size=1316"
        )


def get_env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def get_env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    raw_value = raw_value.strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def load_settings() -> Settings:
    return Settings(
        instance_name=get_env_str("TS_CAPTURE_INSTANCE_NAME", "default"),
        capture_dir=Path(get_env_str("TS_CAPTURE_DIR", "/data/captures")).expanduser().resolve(),
        default_input_url=get_env_str(
            "TS_DEFAULT_INPUT_URL",
            "udp://0.0.0.0:5000?fifo_size=10000000&overrun_nonfatal=1",
        ),
        bind_host=get_env_str("TS_BIND_HOST", "0.0.0.0"),
        bind_port=get_env_int("TS_BIND_PORT", 8080),
        relay_host=get_env_str("TS_RELAY_HOST", "127.0.0.1"),
        relay_preview_port=get_env_int("TS_RELAY_PREVIEW_PORT", 5101),
        relay_capture_port=get_env_int("TS_RELAY_CAPTURE_PORT", 5102),
        preview_filename=get_env_str("TS_PREVIEW_FILENAME", "preview.jpg"),
        schedules_filename=get_env_str("TS_SCHEDULES_FILENAME", "schedules.json"),
        thumbnails_dirname=get_env_str("TS_THUMBNAILS_DIRNAME", "thumbnails"),
    )


settings = load_settings()
