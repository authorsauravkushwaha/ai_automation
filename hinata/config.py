"""Validated, offline-only configuration for HINATA.

Configuration is read from ``config.toml`` in the checkout, or from the path in
``HINATA_CONFIG``. No network location other than loopback Ollama is accepted.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe."""


@dataclass(slots=True)
class OllamaConfig:
    enabled: bool = True
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:0.5b-instruct-q4_K_M"
    timeout_seconds: int = 90
    max_tokens: int = 256
    context_tokens: int = 2048
    threads: int = 2
    temperature: float = 0.2
    # Start the loopback Ollama service automatically when it is not running.
    auto_start: bool = True
    # Download the configured model once if it is genuinely missing.
    auto_pull: bool = False

    def validate(self) -> None:
        parsed = urlsplit(self.host)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError("ollama.host must be a plain HTTP loopback URL")
        # An empty model means "use whichever model is already present locally",
        # which keeps a fresh install usable before the configured model downloads.
        if len(self.model) > 150 or ("\x00" in self.model) or self.model != self.model.strip():
            raise ConfigError("ollama.model must be a local model name of at most 150 characters")
        if not 1 <= self.timeout_seconds <= 600:
            raise ConfigError("ollama.timeout_seconds must be between 1 and 600")
        if not 16 <= self.max_tokens <= 4096:
            raise ConfigError("ollama.max_tokens must be between 16 and 4096")
        if not 512 <= self.context_tokens <= 8192:
            raise ConfigError("ollama.context_tokens must be between 512 and 8192 on this profile")
        if not 1 <= self.threads <= 16:
            raise ConfigError("ollama.threads must be between 1 and 16")


@dataclass(slots=True)
class SpeechConfig:
    microphone: str | int | None = None
    sample_rate: int = 16000
    stt_backend: str = "faster-whisper"
    whisper_model: str = "tiny"
    whisper_compute_type: str = "int8"
    vosk_model_path: str = ""
    tts_backend: str = "pyttsx3"
    tts_voice: str = ""
    tts_rate: int = 175
    piper_executable: str = ""
    piper_model: str = ""
    piper_config: str = ""
    speaker_profile: str = "owner"
    speaker_threshold: float = 0.75

    def validate(self) -> None:
        if self.stt_backend not in {"faster-whisper", "vosk"}:
            raise ConfigError("speech.stt_backend must be 'faster-whisper' or 'vosk'")
        if self.tts_backend not in {"pyttsx3", "piper"}:
            raise ConfigError("speech.tts_backend must be 'pyttsx3' or 'piper'")
        if not 100 <= self.tts_rate <= 250:
            raise ConfigError("speech.tts_rate must be between 100 and 250")
        if self.sample_rate != 16000:
            raise ConfigError("Only a 16000 Hz microphone stream is supported by this build")
        if not 0.0 <= self.speaker_threshold <= 1.0:
            raise ConfigError("speech.speaker_threshold must be between 0 and 1")


@dataclass(slots=True)
class SecurityConfig:
    allowed_roots: list[Path] = field(default_factory=list)
    text_extensions: list[str] = field(
        default_factory=lambda: [".txt", ".md", ".log", ".json", ".csv", ".py"]
    )
    max_read_bytes: int = 1_000_000
    search_timeout_seconds: float = 5.0
    search_max_directories: int = 10_000

    def validate(self) -> None:
        if not self.allowed_roots:
            home = Path.home()
            self.allowed_roots = [home / "Desktop", home / "Documents", home / "Downloads"]
        self.allowed_roots = [Path(p).expanduser().resolve() for p in self.allowed_roots]
        self.text_extensions = [
            suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
            for suffix in self.text_extensions
        ]
        if not 1 <= self.max_read_bytes <= 10_000_000:
            raise ConfigError("security.max_read_bytes must be between 1 and 10000000")
        if not 0.1 <= self.search_timeout_seconds <= 30:
            raise ConfigError("security.search_timeout_seconds must be between 0.1 and 30")


@dataclass(slots=True)
class WakeWordConfig:
    enabled: bool = False
    model_path: str = ""
    threshold: float = 0.5
    cooldown_seconds: float = 2.0


@dataclass(slots=True)
class InterfaceConfig:
    dark_theme: bool = True
    start_minimized: bool = False
    speak_responses: bool = False


@dataclass(slots=True)
class MemoryConfig:
    # Applies to conversation messages, not commands, results or preferences.
    conversation_limit: int = 1000

    def validate(self) -> None:
        if isinstance(self.conversation_limit, bool) or not isinstance(self.conversation_limit, int):
            raise ConfigError("memory.conversation_limit must be an integer")
        if not 1 <= self.conversation_limit <= 100000:
            raise ConfigError("memory.conversation_limit must be between 1 and 100000")


@dataclass(slots=True)
class PersonalityConfig:
    mode: str = "friend"

    def validate(self) -> None:
        if self.mode not in {"friend", "advisor", "technical", "strategist", "professional"}:
            raise ConfigError(
                "personality.mode must be friend, advisor, technical, strategist, or professional"
            )


@dataclass(slots=True)
class Application:
    command: list[str]
    process_names: list[str]


@dataclass(slots=True)
class HinataConfig:
    data_dir: Path
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    interface: InterfaceConfig = field(default_factory=InterfaceConfig)
    personality: PersonalityConfig = field(default_factory=PersonalityConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    applications: dict[str, Application] = field(default_factory=dict)

    def validate(self) -> None:
        self.data_dir = self.data_dir.expanduser().resolve()
        self.ollama.validate()
        self.speech.validate()
        self.security.validate()
        self.personality.validate()
        self.memory.validate()
        if not self.applications:
            self.applications = default_applications()
        normalized: dict[str, Application] = {}
        for name, app in self.applications.items():
            key = name.strip().lower()
            if not key or not app.command or any(not part for part in app.command):
                raise ConfigError(f"Application {name!r} needs a non-empty command")
            if any("\x00" in part for part in app.command + app.process_names):
                raise ConfigError(f"Application {name!r} contains an invalid null byte")
            normalized[key] = app
        self.applications = normalized

    @property
    def database_path(self) -> Path:
        preferred, legacy = self.data_dir / "hinata.db", self.data_dir / "friday.db"
        return legacy if not preferred.exists() and legacy.exists() else preferred

    @property
    def speaker_directory(self) -> Path:
        return self.data_dir / "speaker_profiles"

    @property
    def log_path(self) -> Path:
        preferred = self.data_dir / "logs" / "hinata.log"
        legacy = self.data_dir / "logs" / "friday.log"
        return legacy if not preferred.exists() and legacy.exists() else preferred

    def ensure_private_directories(self) -> None:
        for directory in (self.data_dir, self.speaker_directory, self.log_path.parent):
            directory.mkdir(parents=True, exist_ok=True)
            restrict_private_path(directory)


def default_applications() -> dict[str, Application]:
    return {
        "notepad": Application(["notepad.exe"], ["notepad.exe"]),
        "calculator": Application(["calc.exe"], ["Calculator.exe", "calc.exe"]),
        # Closing the Windows shell is intentionally disabled; use open_folder instead.
        "explorer": Application(["explorer.exe"], []),
    }


def default_data_dir() -> Path:
    # Honor the new variable first; retain the old variable for existing installs.
    if override := os.environ.get("HINATA_DATA_DIR") or os.environ.get("FRIDAY_DATA_DIR"):
        return Path(override)
    if os.name == "nt" and (local := os.environ.get("LOCALAPPDATA")):
        preferred, legacy = Path(local) / "HINATA", Path(local) / "FRIDAY"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        preferred, legacy = base / "hinata", base / "friday"
    # Reuse existing owner data in place rather than risk a lossy DB/WAL copy.
    return legacy if not preferred.exists() and legacy.exists() else preferred


T = TypeVar("T")


def _section(cls: type[T], values: dict[str, Any], name: str) -> T:
    allowed = {item.name for item in fields(cls)}  # type: ignore[arg-type]
    unknown = set(values) - allowed
    if unknown:
        raise ConfigError(f"Unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}")
    return cls(**values)


def restrict_private_path(path: Path) -> None:
    """Best-effort owner-only ACLs; embeddings are never uploaded by HINATA."""
    try:
        if os.name == "nt":
            import subprocess

            user = os.environ.get("USERNAME")
            if user:
                domain = os.environ.get("USERDOMAIN")
                account = f"{domain}\\{user}" if domain else user
                permission = "(OI)(CI)F" if path.is_dir() else "F"
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:{permission}"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                    shell=False,
                )
        else:
            path.chmod(0o700 if path.is_dir() else 0o600)
    except OSError:
        # A clear warning remains in the UI status if ACL inspection is needed.
        pass


def load_config(path: str | Path | None = None) -> HinataConfig:
    config_path = (
        Path(path)
        if path is not None
        else Path(
            os.environ.get("HINATA_CONFIG", os.environ.get("FRIDAY_CONFIG", str(PROJECT_ROOT / "config.toml")))
        )
    )
    raw: dict[str, Any] = {}
    if config_path.exists():
        try:
            with config_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Cannot read {config_path}: {exc}") from exc

    known = {
        "data_dir", "ollama", "speech", "security", "wake_word", "interface",
        "personality", "memory", "applications",
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Unknown configuration section(s): {', '.join(sorted(unknown))}")

    security_values = dict(raw.get("security", {}))
    if "allowed_roots" in security_values:
        security_values["allowed_roots"] = [Path(p) for p in security_values["allowed_roots"]]
    apps: dict[str, Application] = {}
    for name, values in raw.get("applications", {}).items():
        if not isinstance(values, dict) or set(values) - {"command", "process_names"}:
            raise ConfigError(f"Invalid [applications.{name}] definition")
        apps[name] = Application(
            command=list(values.get("command", [])),
            process_names=list(values.get("process_names", [])),
        )

    config = HinataConfig(
        data_dir=Path(raw.get("data_dir", default_data_dir())),
        ollama=_section(OllamaConfig, raw.get("ollama", {}), "ollama"),
        speech=_section(SpeechConfig, raw.get("speech", {}), "speech"),
        security=_section(SecurityConfig, security_values, "security"),
        wake_word=_section(WakeWordConfig, raw.get("wake_word", {}), "wake_word"),
        interface=_section(InterfaceConfig, raw.get("interface", {}), "interface"),
        personality=_section(PersonalityConfig, raw.get("personality", {}), "personality"),
        memory=_section(MemoryConfig, raw.get("memory", {}), "memory"),
        applications=apps,
    )
    try:
        config.validate()
    except ConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value in {config_path}: {exc}") from exc
    return config
