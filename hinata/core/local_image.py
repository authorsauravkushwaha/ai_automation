"""Optional SD-Turbo image weights: explicit setup download, offline inference.

This module remains stdlib-only at import time; optional PyTorch/diffusers packages
are imported only in the consented setup action or isolated image subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hinata.automation.app_control import AutomationError

IMAGE_REPO = "stabilityai/sd-turbo"
IMAGE_MODEL_NAME = "sd-turbo"


def model_directory(data_dir: Path) -> Path:
    return data_dir.expanduser().resolve() / "image_models" / IMAGE_MODEL_NAME


def is_model_ready(model_dir: Path) -> bool:
    return all(
        (model_dir / name).is_file()
        for name in (
            "model_index.json",
            "unet/config.json",
            "unet/diffusion_pytorch_model.safetensors",
            "vae/config.json",
            "text_encoder/config.json",
            "text_encoder/model.safetensors",
            "tokenizer/vocab.json",
            "tokenizer/merges.txt",
        )
    )


def download_image_model(data_dir: Path) -> Path:
    """Explicit connected-setup operation; never called during image inference."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise AutomationError(
            "Image packages are missing. Install .[image] before downloading SD-Turbo."
        ) from exc
    folder = model_directory(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=IMAGE_REPO,
        local_dir=str(folder),
        allow_patterns=["*.json", "*.txt", "*.safetensors", "tokenizer/*"],
        ignore_patterns=["*.bin", "*.ckpt", "*.h5", "*.onnx", "*.msgpack"],
    )
    if not is_model_ready(folder):
        raise AutomationError("SD-Turbo weights are incomplete. Retry image setup while connected.")
    return folder


def render_offline_png(
    model_dir: Path, target: Path, prompt: str, width: int, height: int
) -> bytes:
    """Run a separate local-only inference process; never overwrite owner files.

    The child writes a temporary PNG in the approved target directory. The parent
    checks its size/signature and reports only a bounded error excerpt.
    """
    if not is_model_ready(model_dir):
        raise AutomationError("SD-Turbo weights are not installed; run optional image setup first.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".hinata-image-", suffix=".png", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    env = os.environ.copy()
    env.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        PYTHONNOUSERSITE="1",
        HTTP_PROXY="",
        HTTPS_PROXY="",
        ALL_PROXY="",
        http_proxy="",
        https_proxy="",
        all_proxy="",
    )
    try:
        try:
            run = subprocess.run(
                [sys.executable, "-s", "-m", "hinata.core.image_worker", str(model_dir),
                 str(temp_path), str(width), str(height)],
                input=prompt[:800],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=240,
                shell=False,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutomationError(f"Local SD-Turbo inference did not complete: {exc}") from exc
        if run.returncode != 0:
            raise AutomationError(
                "Local SD-Turbo inference failed; check GPU VRAM, image dependencies, and "
                f"the setup model cache. Details: {run.stderr[-1000:]}"
            )
        if temp_path.stat().st_size > 20_000_000 or temp_path.stat().st_size < 32:
            raise AutomationError("Local image output is empty or exceeds the 20 MB limit")
        png = temp_path.read_bytes()
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or b"IEND" not in png[-32:]:
            raise AutomationError("Local image process did not produce a complete PNG")
        # Exclusive create: do not clobber an existing file created during inference.
        with target.open("xb") as output:
            output.write(png)
        return png
    except FileExistsError as exc:
        raise AutomationError(f"Image already exists (no overwrite): {target}") from exc
    finally:
        temp_path.unlink(missing_ok=True)
