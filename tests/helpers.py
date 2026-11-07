from pathlib import Path

from friday.config import FridayConfig, SecurityConfig
from friday.core.assistant import FridayAssistant


def make_assistant(directory: str, *, llm_enabled: bool = False) -> FridayAssistant:
    root = Path(directory)
    config = FridayConfig(
        data_dir=root / "data",
        security=SecurityConfig(allowed_roots=[root / "Documents", root / "Downloads"]),
    )
    config.ollama.enabled = llm_enabled
    config.validate()
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    (root / "Downloads").mkdir(parents=True, exist_ok=True)
    return FridayAssistant(config, llm_enabled=llm_enabled)
