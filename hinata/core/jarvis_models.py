"""Lenovo LOQ i5 multi-model AI arsenal, autonomous project builder, and visual studio.

Combines specialized local AI engines inside HINATA's 100% loopback-only security perimeter:
1. Curated Multi-Model Fleet Profiles (optimized for Lenovo LOQ Intel Core i5 + RTX/CPU tiers):
   - Primary Tactical Brain (Chat & Orchestration): ``qwen2.5:7b-instruct-q4_K_M`` (or 3B/1.5B fallback)
   - DeepSeek Reasoning & Automation Engine (Planner): ``deepseek-r1:7b``
   - DeepSeek / Qwen Autonomous Software Engineer (Coder): ``deepseek-coder-v2:lite`` / ``qwen2.5-coder:7b``
   - Qwen Multimodal Vision & Design Director (Vision): ``qwen2.5vl:7b`` (or ``moondream``)
   - Independent Code & Safety Auditor (Reviewer): ``llama3.2:3b``
   - High-Precision Semantic Memory & RAG (Embeddings): ``nomic-embed-text``
2. Qwen Visual Studio (``VisualStudioEngine``):
   - Uses Qwen to engineer rich visual blueprints / clean vector SVG art offline on any machine, OR
   - Drives a loopback-only image diffusion server (Automatic1111 / Forge / compatible ComfyUI wrapper
     on ``127.0.0.1``) when running locally on the Lenovo LOQ GPU to render high-res `.png` images!
3. DeepSeek Project Architect (``ProjectArchitectEngine``):
   - Plans, scaffolds, writes multi-file source + unit tests inside approved roots, and verifies syntax/tests
     in HINATA's isolated sandbox so HINATA can complete real software projects end-to-end.
"""

from __future__ import annotations

import ast
import base64
import html
import json
import keyword
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.workspace import WorkspaceManager
from hinata.core.llm_manager import LLMManager
from hinata.security.redaction import redact_text

RECOMMENDED_OLLAMA_VERSION = "0.6.2"

# Keywords routing user requests automatically to the right specialist model role
_CODING_KEYWORDS = frozenset(
    {
        "code",
        "python",
        "script",
        "project",
        "function",
        "class",
        "bug",
        "debug",
        "refactor",
        "typescript",
        "javascript",
        "rust",
        "golang",
        "api",
        "fastapi",
        "flask",
        "sql",
        "algorithm",
        "unittest",
        "pytest",
        "compile",
        "patch",
        "scaffold",
    }
)
_PLANNING_KEYWORDS = frozenset(
    {
        "automate",
        "automation",
        "workflow",
        "pipeline",
        "step by step",
        "plan",
        "architecture",
        "orchestrate",
        "schedule",
        "deploy",
        "strategy",
    }
)
_VISION_IMAGE_KEYWORDS = frozenset(
    {
        "image",
        "diagram",
        "poster",
        "logo",
        "icon",
        "illustration",
        "render",
        "artwork",
        "visual",
        "screenshot",
        "vision",
        "svg",
        "wallpaper",
    }
)


@dataclass(frozen=True, slots=True)
class FleetPack:
    pack_id: str
    title: str
    target_hardware: str
    recommended_ollama_version: str
    min_ram_gb: float
    min_vram_mb: int
    chat_model: str
    planner_model: str
    coding_model: str
    reviewer_model: str
    vision_model: str
    embedding_model: str
    recommended_context_tokens: int
    recommended_max_tokens: int
    recommended_threads: int
    local_image_recommended: str
    summary: str

    def all_ollama_models(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for m in (
            self.chat_model,
            self.planner_model,
            self.coding_model,
            self.reviewer_model,
            self.vision_model,
            self.embedding_model,
        ):
            if m and m not in ordered:
                ordered.append(m)
        return tuple(ordered)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "ollama_pull_commands": [f"ollama pull {m}" for m in self.all_ollama_models()],
        }


FLEET_CATALOG: dict[str, FleetPack] = {
    "lenovo_loq_i5_rtx_8gb": FleetPack(
        pack_id="lenovo_loq_i5_rtx_8gb",
        title="Lenovo LOQ i5 — Ultimate J.A.R.V.I.S. Arsenal (RTX 4050/4060 6-8 GB VRAM, 16+ GB RAM)",
        target_hardware="Lenovo LOQ Intel Core i5 (12th/13th/14th Gen H/HX) + NVIDIA RTX 6-8 GB VRAM + 16-32 GB RAM",
        recommended_ollama_version=RECOMMENDED_OLLAMA_VERSION,
        min_ram_gb=14.0,
        min_vram_mb=5500,
        chat_model="qwen2.5:7b-instruct-q4_K_M",
        planner_model="deepseek-r1:7b",
        coding_model="qwen2.5-coder:7b",
        reviewer_model="llama3.2:3b",
        vision_model="qwen2.5vl:7b",
        embedding_model="nomic-embed-text",
        recommended_context_tokens=8192,
        recommended_max_tokens=1536,
        recommended_threads=6,
        local_image_recommended=(
            "Qwen2.5-VL prompt director + local A1111/Forge-compatible SDXL-Turbo or Flux.1-schnell on http://127.0.0.1:7860 "
            "(plus built-in offline Qwen SVG/Canvas generator with zero extra setup)"
        ),
        summary=(
            "Full multi-AI J.A.R.V.I.S. stack for Lenovo LOQ i5 with RTX 6-8 GB GPU: "
            "Qwen 2.5 7B for tactical chat, DeepSeek-R1 7B for deep reasoning & automation planning, "
            "Qwen2.5-Coder 7B (or DeepSeek-Coder-V2) for building full software projects, "
            "Qwen2.5-VL 7B for visual intelligence & image prompt direction, Llama 3.2 3B for QA review, "
            "and Nomic-Embed-Text for instant semantic memory."
        ),
    ),
    "lenovo_loq_i5_rtx_4gb": FleetPack(
        pack_id="lenovo_loq_i5_rtx_4gb",
        title="Lenovo LOQ i5 — Balanced Tactical Arsenal (RTX 2050/3050 4 GB VRAM or 12-16 GB RAM)",
        target_hardware="Lenovo LOQ Intel Core i5 + RTX 4 GB VRAM (or 12-16 GB RAM)",
        recommended_ollama_version=RECOMMENDED_OLLAMA_VERSION,
        min_ram_gb=10.0,
        min_vram_mb=3500,
        chat_model="qwen2.5:3b",
        planner_model="deepseek-r1:1.5b",
        coding_model="deepseek-coder:6.7b",
        reviewer_model="llama3.2:3b",
        vision_model="qwen2.5vl:3b",
        embedding_model="nomic-embed-text",
        recommended_context_tokens=4096,
        recommended_max_tokens=1024,
        recommended_threads=6,
        local_image_recommended=(
            "Qwen2.5-VL 3B visual director + local SD-Turbo / SD 1.5 on http://127.0.0.1:7860 "
            "or instant offline Qwen SVG vector studio"
        ),
        summary=(
            "Fast, VRAM-friendly multi-AI stack for Lenovo LOQ i5 (4 GB VRAM / 16 GB RAM): "
            "Qwen 2.5 3B chat, DeepSeek-R1 1.5B fast reasoning/automation, DeepSeek-Coder 6.7B for full project "
            "engineering, Qwen2.5-VL 3B for vision & image creation, and Llama 3.2 3B reviewer."
        ),
    ),
    "lenovo_loq_i5_cpu_fast": FleetPack(
        pack_id="lenovo_loq_i5_cpu_fast",
        title="Lenovo LOQ i5 — Battery / Pure-CPU Featherweight Arsenal (8+ GB RAM)",
        target_hardware="Intel Core i5 on battery / iGPU-only mode (8+ GB RAM)",
        recommended_ollama_version=RECOMMENDED_OLLAMA_VERSION,
        min_ram_gb=7.0,
        min_vram_mb=0,
        chat_model="qwen2.5:1.5b",
        planner_model="deepseek-r1:1.5b",
        coding_model="qwen2.5-coder:1.5b",
        reviewer_model="qwen2.5:0.5b-instruct-q4_K_M",
        vision_model="moondream",
        embedding_model="nomic-embed-text",
        recommended_context_tokens=4096,
        recommended_max_tokens=768,
        recommended_threads=6,
        local_image_recommended="Built-in Qwen offline SVG/Vector studio (zero GPU required)",
        summary=(
            "Ultra-responsive battery-safe stack for Intel Core i5 CPU inference: "
            "Qwen 2.5 1.5B chat, DeepSeek-R1 1.5B reasoning, Qwen2.5-Coder 1.5B coding, and Moondream vision."
        ),
    ),
}


def select_optimal_fleet_pack(hardware_snapshot: dict[str, Any]) -> FleetPack:
    """Choose the best multi-AI fleet pack for the detected laptop (e.g. Lenovo LOQ i5)."""
    mem = hardware_snapshot.get("memory", {})
    total_ram = float(mem.get("total_gb") or 8.0)
    gpu = hardware_snapshot.get("gpu", {})
    vram_mb = int(gpu.get("vram_mb") or 0)
    power = hardware_snapshot.get("power", {})
    on_battery = bool(power.get("on_battery", False))

    if vram_mb >= 5500 and total_ram >= 14.0 and not on_battery:
        return FLEET_CATALOG["lenovo_loq_i5_rtx_8gb"]
    if (vram_mb >= 3500 or total_ram >= 11.5) and not on_battery:
        return FLEET_CATALOG["lenovo_loq_i5_rtx_4gb"]
    return FLEET_CATALOG["lenovo_loq_i5_cpu_fast"]


def classify_task_model_role(user_text: str) -> str:
    """Automatically route a request to the strongest specialist AI role."""
    lowered = user_text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    if words & _CODING_KEYWORDS or any(
        phrase in lowered for phrase in ("write code", "build a project", "create a python", "fix bug")
    ):
        return "coder"
    if words & _VISION_IMAGE_KEYWORDS or any(
        phrase in lowered for phrase in ("generate image", "make an image", "draw ", "design a logo")
    ):
        return "vision"
    if words & _PLANNING_KEYWORDS:
        return "planner"
    return "chat"


def strip_reasoning_traces(raw_text: str) -> str:
    """Remove DeepSeek/QwQ private reasoning; incomplete blocks fail closed."""
    if "<think>" not in raw_text.lower():
        return raw_text
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE)
    if "<think>" in cleaned.lower():
        return ""  # truncated reasoning must never be interpreted as an action
    return cleaned.strip()


def render_fleet_toml_snippet(pack: FleetPack) -> str:
    """Generate a ready-to-paste ``[ollama]`` config block for ``config.toml``."""
    return (
        f"# Recommended Ollama runtime: v{pack.recommended_ollama_version}+ (https://ollama.com/download/windows)\n"
        f"# Profile: {pack.title}\n"
        "[ollama]\n"
        "enabled = true\n"
        'host = "http://127.0.0.1:11434"\n'
        f'model = "{pack.chat_model}"\n'
        f'planner_model = "{pack.planner_model}"\n'
        f'coding_model = "{pack.coding_model}"\n'
        f'reviewer_model = "{pack.reviewer_model}"\n'
        f'vision_model = "{pack.vision_model}"\n'
        f'embedding_model = "{pack.embedding_model}"\n'
        'image_backend_url = "http://127.0.0.1:7860"\n'
        'hardware_profile = "lenovo_loq_i5"\n'
        f"context_tokens = {pack.recommended_context_tokens}\n"
        f"max_tokens = {pack.recommended_max_tokens}\n"
        f"threads = {pack.recommended_threads}\n"
        "allow_model_fallback = true\n"
    )


class _LoopbackNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _validate_loopback_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise AutomationError("Local image backend URL must be a plain HTTP loopback address (127.0.0.1).")
    return url.rstrip("/")


class VisualStudioEngine:
    """Qwen-directed local image & vector studio.

    Modes:
    1. Loopback Diffusion Engine (Automatic1111 / Forge ``/sdapi/v1/txt2img`` on ``127.0.0.1:7860``):
       Qwen expands the user's idea into a master photographic/artistic prompt + negative prompt,
       then renders a real `.png` via the local RTX GPU diffusion server.
    2. Offline Qwen Vector SVG Studio (works immediately on ANY laptop with zero extra servers):
       Qwen designs a crisp, scalable `.svg` illustration/diagram/poster (sanitized against scripts/external refs)
       or deterministic geometric HUD artwork when `--no-llm` is active.
    """

    STYLE_PALETTES: dict[str, tuple[str, str, str, str]] = {
        "cyberpunk": ("#070B19", "#00F0FF", "#FF2A85", "#E2F1FF"),
        "jarvis_hud": ("#041019", "#00D8FF", "#0088CC", "#D8F8FF"),
        "minimal": ("#F8FAFC", "#0F172A", "#3B82F6", "#1E293B"),
        "blueprint": ("#0A192F", "#64FFDA", "#112240", "#CCD6F6"),
        "sunset": ("#1A0B2E", "#FF6B6B", "#FFD93D", "#FFF5E4"),
    }

    def __init__(
        self,
        files: FileManager,
        llm: LLMManager | None = None,
        *,
        image_backend_url: str = "http://127.0.0.1:7860",
        image_model_dir: Path | None = None,
    ) -> None:
        self.files = files
        self.llm = llm
        self.image_model_dir = image_model_dir
        self.image_backend_url = _validate_loopback_url(image_backend_url) if image_backend_url else ""
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _LoopbackNoRedirect()
        )

    def expand_prompt_with_qwen(self, prompt: str, style: str = "jarvis_hud") -> dict[str, str]:
        """Use Qwen (vision/chat role) to craft a studio-grade image prompt & negative prompt."""
        clean_prompt = prompt.strip()[:600]
        base_positive = (
            f"{clean_prompt}, style={style}, ultra-detailed composition, dramatic lighting, "
            "sharp focus, high contrast, masterpiece quality"
        )
        base_negative = "blurry, low resolution, watermark, distorted text, artifacts, deformed"
        if self.llm is None:
            return {"positive_prompt": base_positive, "negative_prompt": base_negative, "director": "deterministic"}
        try:
            raw = self.llm.chat(
                [
                    {
                        "role": "user",
                        "content": json.dumps({"idea": clean_prompt, "style": style}, ensure_ascii=False),
                    }
                ],
                (
                    "You are Qwen Visual Director inside HINATA. Expand the user's image idea into "
                    'JSON with keys "positive_prompt" (under 90 words) and "negative_prompt" (under 30 words). '
                    "Output JSON only."
                ),
                connect=True,
                pull_missing=False,
                format="json",
                role="vision",
                num_predict=220,
            )
            parsed = json.loads(strip_reasoning_traces(raw))
            if isinstance(parsed, dict) and isinstance(parsed.get("positive_prompt"), str):
                return {
                    "positive_prompt": parsed["positive_prompt"].strip()[:800] or base_positive,
                    "negative_prompt": str(parsed.get("negative_prompt", base_negative)).strip()[:400],
                    "director": self.llm.last_model_identity or "qwen_local",
                }
        except Exception:
            pass
        return {"positive_prompt": base_positive, "negative_prompt": base_negative, "director": "fallback_template"}

    def _try_loopback_diffusion(
        self,
        positive_prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
    ) -> bytes | None:
        if not self.image_backend_url:
            return None
        payload = json.dumps(
            {
                "prompt": positive_prompt,
                "negative_prompt": negative_prompt,
                "steps": 20,
                "width": width,
                "height": height,
                "cfg_scale": 6.5,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.image_backend_url}/sdapi/v1/txt2img",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=45) as resp:
                body = json.loads(resp.read(25_000_000).decode("utf-8"))
            images = body.get("images")
            if isinstance(images, list) and images and isinstance(images[0], str):
                b64_str = images[0].split(",", 1)[-1]
                png = base64.b64decode(b64_str, validate=True)
                if len(png) <= 20_000_000 and png.startswith(b"\x89PNG\r\n\x1a\n"):
                    return png
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return None
        return None

    def _generate_qwen_svg(self, prompt: str, style: str, width: int, height: int) -> tuple[str, str]:
        """Ask Qwen to author clean standalone SVG markup, falling back to a crisp HUD vector canvas."""
        if self.llm is not None:
            try:
                raw = self.llm.chat(
                    [
                        {
                            "role": "user",
                            "content": (
                                f"Create a complete, visually striking standalone <svg> ({width}x{height}) "
                                f"illustrating: {prompt[:400]}. Style: {style}. "
                                "Use rich gradients, geometric layers, glowing accents, and clean typography. "
                                "Return ONLY valid <svg ...>...</svg> markup with no scripts or external URLs."
                            ),
                        }
                    ],
                    "You are Qwen Vector Artist. Return pure, valid, self-contained <svg> XML only. Never include <script> or http links.",
                    connect=True,
                    pull_missing=False,
                    role="chat",
                    num_predict=1400,
                )
                cleaned = strip_reasoning_traces(raw)
                match = re.search(r"<svg\b[^>]*>.*?</svg>", cleaned, flags=re.DOTALL | re.IGNORECASE)
                if match:
                    svg_candidate = match.group(0)
                    sanitized = self._sanitize_svg(svg_candidate)
                    return sanitized, f"qwen_svg:{self.llm.last_model_identity or 'local'}"
            except Exception:
                pass
        return self._deterministic_hud_svg(prompt, style, width, height), "hinata_vector_studio"

    @staticmethod
    def _sanitize_svg(svg_text: str) -> str:
        """Reject executable/remote SVG; don't try to clean untrusted markup by regex."""
        if len(svg_text) > 150_000 or re.search(r"<!|<\?|&[a-zA-Z#]", svg_text):
            raise ValueError("SVG contains declarations, entities, or unsupported markup")
        root = ET.fromstring(svg_text)
        allowed_tags = {
            "svg", "g", "defs", "rect", "circle", "ellipse", "line", "path",
            "polygon", "polyline", "text", "tspan", "linearGradient", "radialGradient", "stop",
        }
        allowed_attrs = {
            "xmlns", "width", "height", "viewBox", "id", "x", "y", "x1", "y1",
            "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points", "fill",
            "fill-opacity", "stroke", "stroke-width", "stroke-opacity", "stroke-dasharray",
            "opacity", "offset", "stop-color", "stop-opacity", "transform", "font-family",
            "font-size", "font-weight", "text-anchor", "letter-spacing",
        }
        for element in root.iter():
            tag = element.tag.removeprefix("{http://www.w3.org/2000/svg}")
            if tag not in allowed_tags or any(
                key not in allowed_attrs
                or ("url(" in value.lower() and not re.fullmatch(r"url\(#[a-zA-Z0-9_-]+\)", value))
                or re.search(r"(?:https?://|javascript:|data:|@import)", value, re.IGNORECASE)
                for key, value in element.attrib.items()
            ):
                raise ValueError("SVG contains unsafe elements or attributes")
        if root.tag.removeprefix("{http://www.w3.org/2000/svg}") != "svg":
            raise ValueError("Invalid SVG root")
        return svg_text

    def _deterministic_hud_svg(self, prompt: str, style: str, width: int, height: int) -> str:
        bg, primary, secondary, text_col = self.STYLE_PALETTES.get(
            style.lower(), self.STYLE_PALETTES["jarvis_hud"]
        )
        safe_title = html.escape(prompt.strip()[:68])
        safe_style = html.escape(style.upper())
        cx, cy = width // 2, height // 2
        r_outer = min(width, height) // 3
        r_inner = int(r_outer * 0.68)
        r_core = int(r_outer * 0.34)
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <radialGradient id="bgGrad" cx="50%" cy="50%" r="70%">
      <stop offset="0%" stop-color="{secondary}" stop-opacity="0.24"/>
      <stop offset="100%" stop-color="{bg}" stop-opacity="1"/>
    </radialGradient>
    <linearGradient id="accentGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{primary}"/>
      <stop offset="100%" stop-color="{secondary}"/>
    </linearGradient>
  </defs>
  <rect width="{width}" height="{height}" fill="{bg}"/>
  <rect width="{width}" height="{height}" fill="url(#bgGrad)"/>
  <g stroke="{primary}" stroke-opacity="0.16" stroke-width="1">
    <line x1="0" y1="{cy}" x2="{width}" y2="{cy}"/>
    <line x1="{cx}" y1="0" x2="{cx}" y2="{height}"/>
    <rect x="28" y="28" width="{width - 56}" height="{height - 56}" fill="none" rx="14"/>
  </g>
  <g transform="translate({cx},{cy})">
    <circle r="{r_outer}" fill="none" stroke="url(#accentGrad)" stroke-width="3" stroke-dasharray="18 10"/>
    <circle r="{r_inner}" fill="none" stroke="{primary}" stroke-width="2" stroke-opacity="0.75"/>
    <polygon points="0,-{r_inner} {int(r_inner * 0.86)},{r_inner // 2} -{int(r_inner * 0.86)},{r_inner // 2}"
             fill="none" stroke="{secondary}" stroke-width="2" stroke-opacity="0.65"/>
    <circle r="{r_core}" fill="{primary}" fill-opacity="0.18" stroke="{primary}" stroke-width="2.5"/>
    <circle r="10" fill="{primary}"/>
  </g>
  <text x="{cx}" y="{height - 76}" text-anchor="middle" fill="{text_col}"
        font-family="Segoe UI, Inter, sans-serif" font-size="22" font-weight="600">{safe_title}</text>
  <text x="{cx}" y="{height - 46}" text-anchor="middle" fill="{primary}"
        font-family="Consolas, monospace" font-size="13" letter-spacing="2">HINATA // QWEN VISUAL STUDIO // {safe_style}</text>
</svg>
"""

    def generate_image(
        self,
        prompt: str,
        output_path: str,
        *,
        style: str = "jarvis_hud",
        width: int = 1024,
        height: int = 768,
        llm_enabled: bool = True,
    ) -> dict[str, Any]:
        """Generate an image inside permitted roots via loopback diffusion (`.png`) or Qwen SVG studio (`.svg`)."""
        w = max(256, min(int(width), 2048))
        h = max(256, min(int(height), 2048))
        target = self.files.resolve(output_path, must_exist=False)
        suffix = target.suffix.lower()
        if suffix not in {".png", ".svg"}:
            raise AutomationError("Image output must end in .png or .svg")
        if target.exists():
            raise AutomationError(f"Image already exists (no overwrite): {target}")
        active_llm = self.llm if llm_enabled else None
        studio = VisualStudioEngine(
            self.files, active_llm, image_backend_url=self.image_backend_url,
            image_model_dir=self.image_model_dir,
        )
        direction = studio.expand_prompt_with_qwen(prompt, style=style)

        if suffix == ".png":
            png_bytes = studio._try_loopback_diffusion(
                direction["positive_prompt"], direction["negative_prompt"], w, h
            )
            if png_bytes is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with target.open("xb") as out:
                        out.write(png_bytes)
                except FileExistsError as exc:
                    raise AutomationError(f"Image already exists (no overwrite): {target}") from exc
                return {
                    "path": str(target),
                    "format": "png",
                    "backend": f"loopback_diffusion:{self.image_backend_url}",
                    "bytes": len(png_bytes),
                    "width": w,
                    "height": h,
                    "prompt_direction": direction,
                }
            # SD-Turbo is an independently trained local image model, cached only
            # after explicit setup consent. No network is used during generation.
            if self.image_model_dir is not None:
                from hinata.core.local_image import is_model_ready, render_offline_png

                if is_model_ready(self.image_model_dir):
                    actual_w = max(256, min(768, w // 8 * 8))
                    actual_h = max(256, min(768, h // 8 * 8))
                    rendered = render_offline_png(
                        self.image_model_dir, target, direction["positive_prompt"], actual_w, actual_h
                    )
                    return {
                        "path": str(target), "format": "png", "backend": "offline_sd_turbo",
                        "bytes": len(rendered), "width": actual_w, "height": actual_h,
                        "prompt_direction": direction,
                    }
            # Offline vector fallback is an SVG, never misrepresented as PNG.
            target = self.files.resolve(str(target.with_suffix(".svg")), must_exist=False)
            if target.exists():
                raise AutomationError(f"Fallback SVG already exists (no overwrite): {target}")

        svg_content, backend = studio._generate_qwen_svg(prompt, style, w, h)
        encoded = svg_content.encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as out:
                out.write(encoded)
        except FileExistsError as exc:
            raise AutomationError(f"Image already exists (no overwrite): {target}") from exc
        return {
            "path": str(target),
            "format": "svg",
            "backend": backend,
            "bytes": len(encoded),
            "width": w,
            "height": h,
            "prompt_direction": direction,
            "gpu_diffusion_tip": (
                "Run the optional image setup to cache SD-Turbo weights for real offline PNG generation "
                "(or connect an A1111/Forge-compatible loopback diffusion server)."
                if backend != f"loopback_diffusion:{self.image_backend_url}"
                else ""
            ),
        }


class ProjectArchitectEngine:
    """DeepSeek / Qwen-Coder autonomous software project builder.

    Plans, writes, syntax-checks, and sandbox-tests complete multi-file projects
    inside an owner-approved directory so HINATA can build real tools, CLIs, APIs,
    and automation scripts end-to-end.
    """

    SAFE_REL_FILE_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,120}$")
    ALLOWED_SCAFFOLD_SUFFIXES = frozenset(
        {
            ".py", ".toml", ".md", ".json", ".txt", ".html", ".css", ".js", ".ts",
            ".jsx", ".tsx", ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".rs", ".go",
            ".kt", ".swift", ".php", ".rb", ".sql", ".sh", ".ps1", ".dart", ".lua",
            ".r", ".scala", ".yaml", ".yml", ".ini", ".xml",
        }
    )

    def __init__(
        self,
        files: FileManager,
        workspaces: WorkspaceManager,
        llm: LLMManager | None = None,
    ) -> None:
        self.files = files
        self.workspaces = workspaces
        self.llm = llm

    def build_project(
        self,
        *,
        project_dir: str,
        specification: str,
        template: str = "python_cli",
        run_verification: bool = True,
        llm_enabled: bool = True,
    ) -> dict[str, Any]:
        target_root = self.files.resolve(project_dir, must_exist=False)
        if target_root.exists() and not target_root.is_dir():
            raise AutomationError(f"Project target is an existing file, not a directory: {target_root}")
        if target_root.exists() and any(target_root.iterdir()):
            raise AutomationError("Project builder requires an empty destination; refusing to overwrite existing files")
        spec = specification.strip()
        if not spec:
            raise AutomationError("Project specification must not be blank")
        if template not in {"python_cli", "web_dashboard"}:
            raise AutomationError("Unsupported project template")
        generated_files, generator_id = self._synthesize_files(
            project_name=target_root.name,
            specification=spec,
            template=template,
            llm_enabled=llm_enabled,
        )

        if any(p.startswith("tests/") and p.endswith(".py") for p in generated_files):
            generated_files.setdefault("tests/__init__.py", "")
        written_paths: list[str] = []
        syntax_checked: list[str] = []
        # Validate *all* generated paths and syntax before the first write.
        destinations: list[tuple[Path, str, str]] = []
        for rel_path, content in generated_files.items():
            clean_rel = rel_path.strip().replace("\\", "/")
            if (
                not self.SAFE_REL_FILE_RE.fullmatch(clean_rel)
                or clean_rel.startswith("/")
                or any(part in {"", ".", ".."} for part in clean_rel.split("/"))
                or Path(clean_rel).suffix.lower() not in self.ALLOWED_SCAFFOLD_SUFFIXES
            ):
                raise AutomationError(f"Refused unsafe generated project path: {rel_path!r}")
            dest = (target_root / clean_rel).resolve()
            if target_root not in dest.parents and dest != target_root:
                raise AutomationError(f"Project file escaped target root: {clean_rel}")
            if clean_rel.endswith(".py"):
                try:
                    ast.parse(content, filename=clean_rel)
                except SyntaxError as exc:
                    raise AutomationError(
                        f"Generated Python file '{clean_rel}' failed AST syntax verification at line {exc.lineno}: {exc.msg}"
                    ) from exc
                syntax_checked.append(clean_rel)
            if dest.exists():
                raise AutomationError(f"Generated file already exists: {clean_rel}")
            destinations.append((dest, clean_rel, content))
        target_root.mkdir(parents=True, exist_ok=True)
        try:
            for dest, clean_rel, content in destinations:
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Stage each file and atomically replace it.  The destination was
                # empty before validation, so an interrupted batch cannot overwrite
                # unrelated owner work.
                staged = dest.with_name(f".{dest.name}.hinata-stage-{uuid.uuid4().hex[:8]}")
                staged.write_text(content, encoding="utf-8")
                staged.replace(dest)
                written_paths.append(clean_rel)
        except OSError as exc:
            # Do not guess whether a failed filesystem call had an effect. Keep
            # the files for inspection and report the outcome as uncertain.
            write_verification: dict[str, Any] = {
                "executed": False,
                "tests_passed": None,
                "status": "UNKNOWN_OUTCOME",
                "checks": [{"name": "file_write", "status": "UNKNOWN_OUTCOME", "detail": str(exc)[:500]}],
            }
            result = {
                "project_root": str(target_root),
                "workspace_id": "",
                "generator": generator_id,
                "template": template,
                "files_created": written_paths,
                "python_ast_verified": syntax_checked,
                "status": "UNKNOWN_OUTCOME",
                "sandbox_verification": write_verification,
            }
            self._persist_project_record(target_root, spec, template, result)
            return result

        ws_info = self.workspaces.register_workspace(str(target_root))
        test_files = [p for p in written_paths if p.startswith("tests/") and p.endswith(".py")]
        verification: dict[str, Any] = {
            "executed": False,
            "tests_passed": None,
            "status": "PARTIAL",
            "test_count": 0,
            "checks": [
                {"name": "generated_paths", "status": "PASSED", "detail": f"{len(written_paths)} file(s) stayed in the workspace"},
                {"name": "python_syntax", "status": "PASSED", "detail": f"AST checked {len(syntax_checked)} Python file(s)"},
            ],
        }
        if not run_verification:
            verification["checks"].append({"name": "tests", "status": "NOT_RUN", "detail": "Verification was disabled by the caller"})
        elif not test_files:
            # A missing test suite is evidence of incomplete verification, never
            # an empty passing suite.
            verification["checks"].append({"name": "tests", "status": "NOT_RUN", "detail": "No test files were generated"})
        else:
            try:
                test_res = self.workspaces.run_tests(ws_info["workspace_id"])
                transcript = str(test_res.get("stdout", "")) + str(test_res.get("stderr", ""))
                ran_tests = re.search(r"\bRan (\d+) tests?\b", transcript)
                test_count = int(ran_tests.group(1)) if ran_tests else 0
                verification.update(
                    {
                        "executed": True,
                        "test_count": test_count,
                        "exit_code": test_res.get("exit_code"),
                        "isolation_backend": test_res.get("isolation_backend", ""),
                        "stdout": str(test_res.get("stdout", ""))[:4000],
                        "stderr": str(test_res.get("stderr", ""))[:4000],
                    }
                )
                if int(test_res.get("exit_code", 1)) == 0 and test_count > 0:
                    verification["status"] = "SUCCESS"
                    verification["tests_passed"] = True
                    verification["checks"].append({"name": "tests", "status": "PASSED", "detail": f"{test_count} test(s) executed and passed"})
                elif int(test_res.get("exit_code", 1)) == 0:
                    verification["checks"].append({"name": "tests", "status": "NOT_RUN", "detail": "Zero tests were discovered"})
                else:
                    verification["status"] = "FAILED"
                    verification["tests_passed"] = False
                    verification["checks"].append({"name": "tests", "status": "FAILED", "detail": f"Test command exited {test_res.get('exit_code')}"})
            except (AutomationError, OSError) as exc:
                verification["status"] = "BLOCKED"
                verification["checks"].append({"name": "tests", "status": "BLOCKED", "detail": str(exc)[:500]})

        if verification["status"] == "SUCCESS" and generator_id == "hinata_clean_scaffold":
            # Passing scaffold self-tests proves the scaffold is runnable, not
            # that the user's product requirements were implemented.
            verification["status"] = "PARTIAL"
            verification["checks"].append(
                {
                    "name": "product_scope",
                    "status": "NOT_RUN",
                    "detail": "Deterministic offline scaffold generated; owner acceptance and product requirements remain unverified",
                }
            )

        result = {
            "project_root": str(target_root),
            "workspace_id": ws_info["workspace_id"],
            "generator": generator_id,
            "template": template,
            "files_created": written_paths,
            "python_ast_verified": syntax_checked,
            "status": verification["status"],
            "sandbox_verification": verification,
        }
        self._persist_project_record(target_root, spec, template, result)
        return result

    def _persist_project_record(
        self, target_root: Path, specification: str, template: str, result: dict[str, Any]
    ) -> None:
        """Persist project status and iteration evidence without changing the outcome."""
        database = getattr(self.workspaces, "database", None)
        if database is None or not hasattr(database, "fetch_one") or not hasattr(database, "execute"):
            return
        try:
            ensure_migrated = getattr(database, "ensure_migrated", None)
            if callable(ensure_migrated):
                ensure_migrated()
            existing = database.fetch_one(
                "SELECT project_id FROM project_records WHERE root_path = ?", (str(target_root),)
            )
            project_id = str(existing["project_id"]) if existing else uuid.uuid4().hex[:16]
            safe_description = redact_text(specification)[:4000]
            verification = result.get("sandbox_verification", {})
            database.execute(
                """INSERT INTO project_records(
                    project_id, name, description, root_path, project_type, stack_json,
                    requirements_json, status, verification_json, file_inventory_json, known_issues_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_path) DO UPDATE SET
                    name=excluded.name, description=excluded.description, project_type=excluded.project_type,
                    stack_json=excluded.stack_json, requirements_json=excluded.requirements_json,
                    status=excluded.status, verification_json=excluded.verification_json,
                    file_inventory_json=excluded.file_inventory_json, known_issues_json=excluded.known_issues_json,
                    updated_at=datetime('now')""",
                (
                    project_id,
                    target_root.name[:160],
                    safe_description,
                    str(target_root),
                    template,
                    json.dumps({"template": template}),
                    json.dumps({"specification": safe_description}),
                    result.get("status", "UNKNOWN_OUTCOME"),
                    json.dumps(verification, ensure_ascii=False)[:12000],
                    json.dumps(result.get("files_created", []), ensure_ascii=False),
                    json.dumps(
                        [
                            check for check in verification.get("checks", [])
                            if check.get("status") not in {"PASSED", "SUCCESS"}
                        ],
                        ensure_ascii=False,
                    )[:4000],
                ),
            )
            database.execute(
                """INSERT INTO project_iterations(
                    iteration_id, project_id, phase, status, changed_files_json,
                    checks_json, diagnostics_json
                ) VALUES(?, ?, 'IMPLEMENT_VERIFY', ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex[:16],
                    project_id,
                    result.get("status", "UNKNOWN_OUTCOME"),
                    json.dumps(result.get("files_created", []), ensure_ascii=False),
                    json.dumps(verification.get("checks", []), ensure_ascii=False)[:8000],
                    json.dumps({"generator": result.get("generator", "")}, ensure_ascii=False),
                ),
            )
        except Exception:
            # Evidence persistence is supplemental; it must not turn a real
            # build result into an invented success or hide the filesystem result.
            return

    def _synthesize_files(
        self,
        *,
        project_name: str,
        specification: str,
        template: str,
        llm_enabled: bool,
    ) -> tuple[dict[str, str], str]:
        slug = re.sub(r"[^a-z0-9_]+", "_", project_name.lower()).strip("_") or "app"
        if slug[0].isdigit() or keyword.iskeyword(slug):
            slug = f"pkg_{slug}"

        if llm_enabled and self.llm is not None:
            # The reasoning specialist decomposes the request; the coding specialist
            # then writes files. Neither result bypasses path/AST/sandbox checks.
            implementation_plan = ""
            try:
                plan_raw = self.llm.chat(
                    [{"role": "user", "content": specification[:1500]}],
                    "You are HINATA's reasoning specialist. Give a concise implementation "
                    "checklist and test cases for a small, runnable software project. "
                    "Do not claim anything has been built or tested.",
                    connect=True, pull_missing=False, role="planner", num_predict=500,
                )
                implementation_plan = strip_reasoning_traces(plan_raw)[:1200]
            except Exception:
                pass  # A missing planner never authorizes writes or fabricates a plan.
            try:
                raw = self.llm.chat(
                    [
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "project_name": project_name,
                                    "package_slug": slug,
                                    "template": template,
                                    "specification": specification[:1500],
                                    "implementation_plan": implementation_plan,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    (
                        "You are HINATA's local coding specialist. Design a runnable, testable "
                        "project matching the user's specification. Return ONLY a JSON object with a single key "
                        '"files" mapping relative file paths (e.g. "README.md", "pyproject.toml", '
                        f'"{slug}/__init__.py", "{slug}/core.py", "tests/test_core.py") to their full UTF-8 string '
                        "contents. Every Python file MUST be syntactically valid stdlib-compatible Python 3.11+ "
                        "with real passing unittest tests in tests/test_core.py. Keep this response bounded to at most 12 files; larger projects require later iterations."
                    ),
                    connect=True,
                    pull_missing=False,
                    format="json",
                    role="coder",
                    num_predict=2800,
                )
                parsed = json.loads(strip_reasoning_traces(raw))
                files_obj = parsed.get("files") if isinstance(parsed, dict) else None
                if isinstance(files_obj, dict) and 2 <= len(files_obj) <= 12:
                    validated: dict[str, str] = {}
                    for k, v in files_obj.items():
                        if isinstance(k, str) and isinstance(v, str) and len(v) <= 100_000:
                            if k.endswith(".py"):
                                ast.parse(v, filename=k)
                            validated[k] = v
                    if validated:
                        return validated, f"local_coder:{self.llm.last_model_identity or 'local'}"
            except Exception:
                pass

        return self._deterministic_scaffold(project_name, slug, specification, template), "hinata_clean_scaffold"

    @staticmethod
    def _deterministic_scaffold(
        project_name: str, slug: str, specification: str, template: str
    ) -> dict[str, str]:
        safe_spec = specification.replace('"""', "'''")
        if template == "web_dashboard":
            return {
                "README.md": f"# {project_name}\n\n{specification}\n\nGenerated offline by HINATA Project Architect.\n",
                "index.html": (
                    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n"
                    f"  <title>{html.escape(project_name)}</title>\n"
                    "  <link rel=\"stylesheet\" href=\"styles.css\">\n</head>\n<body>\n"
                    f"  <main class=\"hud\">\n    <h1>{html.escape(project_name)}</h1>\n"
                    f"    <p id=\"spec\">{html.escape(specification)}</p>\n"
                    "    <button id=\"actionBtn\">Run Diagnostics</button>\n"
                    "    <pre id=\"output\">Ready.</pre>\n  </main>\n"
                    "  <script src=\"app.js\"></script>\n</body>\n</html>\n"
                ),
                "styles.css": (
                    "body { background: #06101a; color: #e2f1ff; font-family: system-ui, sans-serif; margin: 0; padding: 2rem; }\n"
                    ".hud { max-width: 760px; margin: 0 auto; border: 1px solid #00d8ff55; border-radius: 12px; padding: 1.5rem; background: #0a1929; }\n"
                    "button { background: #00d8ff; color: #041019; border: none; padding: 0.6rem 1.2rem; border-radius: 6px; font-weight: 700; cursor: pointer; }\n"
                ),
                "app.js": (
                    "document.getElementById('actionBtn').addEventListener('click', () => {\n"
                    "  document.getElementById('output').textContent = 'Diagnostics nominal at ' + new Date().toISOString();\n"
                    "});\n"
                ),
            }

        return {
            "README.md": (
                f"# {project_name}\n\n"
                f"## Specification\n{specification}\n\n"
                "## Quick Start\n```bash\npython -m unittest discover -s tests -v\n"
                f"python -m {slug} --help\n```\n"
            ),
            "pyproject.toml": (
                "[build-system]\nrequires = [\"setuptools>=68\"]\nbuild-backend = \"setuptools.build_meta\"\n\n"
                f"[project]\nname = \"{slug.replace('_', '-')}\"\nversion = \"0.1.0\"\n"
                f"description = {json.dumps(specification[:140])}\nrequires-python = \">=3.11\"\n"
            ),
            f"{slug}/__init__.py": f'"""{safe_spec}"""\n\n__version__ = "0.1.0"\n',
            f"{slug}/core.py": (
                '"""Core automation & domain logic."""\n\n'
                "from __future__ import annotations\n\n"
                "from dataclasses import asdict, dataclass\n"
                "from typing import Any\n\n\n"
                "@dataclass(slots=True)\n"
                "class TaskOutcome:\n"
                "    title: str\n"
                "    specification: str\n"
                "    status: str = \"scaffold_only\"\n\n"
                "    def to_dict(self) -> dict[str, Any]:\n"
                "        return asdict(self)\n\n\n"
                f"def run_workflow(task_name: str = {json.dumps(project_name)}) -> dict[str, Any]:\n"
                f"    outcome = TaskOutcome(title=task_name, specification={json.dumps(specification[:300])})\n"
                "    return outcome.to_dict()\n"
            ),
            f"{slug}/__main__.py": (
                "from __future__ import annotations\n\n"
                "import argparse\nimport json\n\n"
                f"from {slug}.core import run_workflow\n\n\n"
                "def main(argv: list[str] | None = None) -> int:\n"
                f"    parser = argparse.ArgumentParser(prog={json.dumps(slug)}, description={json.dumps(specification[:160])})\n"
                f"    parser.add_argument(\"--task\", default={json.dumps(project_name)}, help=\"Workflow task name\")\n"
                "    args = parser.parse_args(argv)\n"
                "    print(json.dumps(run_workflow(args.task), indent=2))\n"
                "    return 0\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    raise SystemExit(main())\n"
            ),
            "tests/test_core.py": (
                "import unittest\n\n"
                f"from {slug}.__main__ import main\n"
                f"from {slug}.core import run_workflow\n\n\n"
                "class WorkflowTests(unittest.TestCase):\n"
                "    def test_run_workflow_returns_completed_payload(self) -> None:\n"
                "        result = run_workflow(\"demo\")\n"
                "        self.assertEqual(result[\"status\"], \"scaffold_only\")\n"
                "        self.assertEqual(result[\"title\"], \"demo\")\n\n"
                "    def test_cli_entry_point_succeeds(self) -> None:\n"
                "        self.assertEqual(main([\"--task\", \"cli-check\"]), 0)\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    unittest.main()\n"
            ),
        }
