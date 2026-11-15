"""One-shot offline SD-Turbo renderer. Only launched after owner approval."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def _disable_network() -> None:
    # Do not rely on library-specific offline switches alone; no Python socket
    # connection may leave this inference process. Setup downloads happen elsewhere.
    def no_connection(*_args: object, **_kwargs: object) -> None:
        raise OSError("Image inference is offline; network access is disabled")

    socket.socket.connect = no_connection  # type: ignore[method-assign]
    socket.socket.connect_ex = no_connection  # type: ignore[assignment]
    socket.create_connection = no_connection  # type: ignore[assignment]


def main() -> int:
    if len(sys.argv) != 5:
        raise ValueError("Expected model directory, output path, width and height")
    model_dir, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    width, height = int(sys.argv[3]), int(sys.argv[4])
    if not (256 <= width <= 768 and 256 <= height <= 768):
        raise ValueError("SD-Turbo output must be 256-768 pixels on each axis")
    if not model_dir.is_dir() or not (model_dir / "model_index.json").is_file():
        raise ValueError("SD-Turbo model cache is not available")
    prompt = sys.stdin.read(801).strip()
    if not prompt:
        raise ValueError("Image prompt is empty")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    _disable_network()
    import torch
    from diffusers import AutoPipelineForText2Image

    cuda = torch.cuda.is_available()
    pipe = AutoPipelineForText2Image.from_pretrained(
        str(model_dir), local_files_only=True,
        torch_dtype=torch.float16 if cuda else torch.float32,
        use_safetensors=True,
    )
    pipe.to("cuda" if cuda else "cpu")
    # SD-Turbo is trained for 1-4 steps and guidance_scale=0; it is NOT Qwen.
    result = pipe(
        prompt=prompt, num_inference_steps=2, guidance_scale=0.0,
        width=width, height=height,
    )
    result.images[0].save(output_path, format="PNG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
