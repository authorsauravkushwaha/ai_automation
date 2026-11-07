"""Owner voice embeddings with conservative matching and local-at-rest protection."""

from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hinata.config import SpeechConfig
from hinata.security.local_protection import read_private, write_private


class SpeakerError(RuntimeError):
    pass


@dataclass(slots=True)
class SpeakerVerification:
    status: str
    score: float | None
    match: bool | None
    required: bool


class SpeakerRecognition:
    def __init__(self, config: SpeechConfig, directory: Path) -> None:
        self.config = config
        self.directory = directory
        self._encoder: Any = None
        self._profile_path = self._path(config.speaker_profile)

    def _path(self, profile: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", profile):
            raise SpeakerError("Speaker profile names may contain only letters, digits, _, and -")
        return self.directory / f"{profile}.voice"

    @property
    def is_enrolled(self) -> bool:
        return self._profile_path.is_file()

    def _load_encoder(self) -> Any:
        if self._encoder is None:
            try:
                from resemblyzer import VoiceEncoder
            except ImportError as exc:
                raise SpeakerError(
                    "Speaker recognition is optional and memory-intensive. Install it with: "
                    "python -m pip install -e \".[speaker]\""
                ) from exc
            try:
                self._encoder = VoiceEncoder("cpu")
            except Exception as exc:
                raise SpeakerError(f"Could not load the local speaker encoder: {exc}") from exc
        return self._encoder

    def _embed(self, audio: Any) -> Any:
        np = self._numpy()
        samples = np.asarray(audio, dtype="float32").reshape(-1)
        if samples.size < self.config.sample_rate:
            raise SpeakerError("Each voice sample must contain at least one second of audio")
        if samples.size > self.config.sample_rate * 20:
            samples = samples[: self.config.sample_rate * 20]
        try:
            from resemblyzer import preprocess_wav

            encoder = self._load_encoder()
            embedding = np.asarray(
                encoder.embed_utterance(preprocess_wav(samples, source_sr=self.config.sample_rate)),
                dtype="float32",
            )
        except ImportError as exc:
            raise SpeakerError("Install the optional speaker extra before enrollment") from exc
        except Exception as exc:
            raise SpeakerError(f"Could not create a voice embedding: {exc}") from exc
        norm = float(np.linalg.norm(embedding))
        if embedding.ndim != 1 or not math.isfinite(norm) or norm == 0:
            raise SpeakerError("The speaker encoder produced an invalid embedding")
        return embedding / norm

    def enroll(self, samples: list[Any]) -> dict[str, Any]:
        if not 3 <= len(samples) <= 8:
            raise SpeakerError("Record between 3 and 8 separate voice samples")
        np = self._numpy()
        embeddings = [self._embed(sample) for sample in samples]
        matrix = np.stack(embeddings)
        centroid = matrix.mean(axis=0)
        centroid /= np.linalg.norm(centroid)
        stored = np.concatenate([centroid[None, :], matrix], axis=0).astype("float32")
        buffer = io.BytesIO()
        np.save(buffer, stored, allow_pickle=False)
        write_private(self._profile_path, buffer.getvalue())
        metadata = {
            "format": 1,
            "encoder": "resemblyzer",
            "samples": len(samples),
            "created_at": datetime.now(UTC).isoformat(),
            "raw_audio_stored": False,
        }
        write_private(self._profile_path.with_suffix(".json"), json.dumps(metadata).encode("utf-8"))
        self.unload()
        return {"samples": len(samples), "profile": self.config.speaker_profile}

    def verify(self, audio: Any) -> SpeakerVerification:
        if not self.is_enrolled:
            return SpeakerVerification("not_enrolled", None, None, False)
        np = self._numpy()
        try:
            stored = np.load(io.BytesIO(read_private(self._profile_path)), allow_pickle=False)
        except Exception as exc:
            raise SpeakerError(f"Could not read the protected speaker profile: {exc}") from exc
        if stored.ndim != 2 or stored.shape[0] < 4:
            raise SpeakerError("The speaker profile is invalid; enroll again")
        embedding = self._embed(audio)
        centroid_score = float(np.dot(embedding, stored[0]))
        sample_score = max(float(np.dot(embedding, row)) for row in stored[1:])
        score = 0.7 * centroid_score + 0.3 * sample_score
        self.unload()
        threshold = self.config.speaker_threshold
        if score >= threshold + 0.03:
            return SpeakerVerification("owner", score, True, True)
        if score < threshold:
            return SpeakerVerification("rejected", score, False, True)
        return SpeakerVerification("uncertain", score, False, True)

    def delete_profile(self) -> bool:
        deleted = False
        for path in (self._profile_path, self._profile_path.with_suffix(".json")):
            if path.exists():
                path.unlink()
                deleted = True
        return deleted

    def unload(self) -> None:
        self._encoder = None

    @staticmethod
    def _numpy() -> Any:
        try:
            import numpy as np
        except ImportError as exc:
            raise SpeakerError("NumPy is required for speaker recognition") from exc
        return np
