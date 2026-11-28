from pathlib import Path

from hinata.config import HinataConfig, SecurityConfig
from hinata.core.assistant import HinataAssistant


def make_assistant(
    directory: str, *, llm_enabled: bool = False, ultron_enabled: bool | None = False,
) -> HinataAssistant:
    # Legacy unit fixtures exercise base policies; pass None to test startup defaults.
    root = Path(directory)
    config = HinataConfig(
        data_dir=root / "data",
        security=SecurityConfig(allowed_roots=[root / "Documents", root / "Downloads"]),
    )
    config.ollama.enabled = llm_enabled
    config.validate()
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    (root / "Downloads").mkdir(parents=True, exist_ok=True)
    return HinataAssistant(config, llm_enabled=llm_enabled, ultron_enabled=ultron_enabled)
