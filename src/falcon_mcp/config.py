from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    runtime: Path
    audio_root: Path
    model_cache: Path
    audio_python: str = sys.executable
    device: str = "cpu"
    model_id: str = "laion/clap-htsat-unfused"
    local_files_only: bool = True
    bridge_timeout: float = 15.0
    model_timeout: float = 180.0

    @property
    def report_root(self) -> Path:
        return self.runtime.parent / "reports"

    @property
    def log_root(self) -> Path:
        return self.runtime.parent / "logs"

    @property
    def asset_root(self) -> Path:
        return self.runtime.parent / "assets"

    def asset_path(self, name: str, *, must_exist: bool = True) -> Path:
        candidate = (self.asset_root / name).resolve()
        if not candidate.is_relative_to(self.asset_root.resolve()):
            raise ValueError("Asset path must stay inside <FALCON_MCP_HOME>/assets")
        if must_exist and not candidate.is_file():
            raise ValueError(f"Missing asset: {candidate}")
        return candidate

    @classmethod
    def from_env(cls) -> Config:
        base = Path(os.environ.get("FALCON_MCP_HOME", Path.home() / ".falcon-mcp")).resolve()
        return cls(
            runtime=base / "bridge",
            audio_root=Path(os.environ.get("FALCON_AUDIO_ROOT", base / "audio")).resolve(),
            model_cache=Path(os.environ.get("FALCON_MODEL_CACHE", base / "models")).resolve(),
            audio_python=os.environ.get("FALCON_AUDIO_PYTHON", sys.executable),
            device=os.environ.get("FALCON_MODEL_DEVICE", "cpu"),
            model_id=os.environ.get("FALCON_MODEL_ID", "laion/clap-htsat-unfused"),
            local_files_only=os.environ.get("FALCON_MODEL_OFFLINE", "1") != "0",
            bridge_timeout=float(os.environ.get("FALCON_BRIDGE_TIMEOUT", "15")),
            model_timeout=float(os.environ.get("FALCON_MODEL_TIMEOUT", "180")),
        )

    def audio_path(self, path: str, *, must_exist: bool = True) -> Path:
        candidate = Path(path)
        candidate = (candidate if candidate.is_absolute() else self.audio_root / candidate).resolve()
        if not candidate.is_relative_to(self.audio_root):
            raise ValueError(f"Audio path must stay inside FALCON_AUDIO_ROOT: {self.audio_root}")
        if must_exist and not candidate.is_file():
            raise ValueError(f"Audio file does not exist: {candidate}")
        if candidate.suffix.lower() not in {".wav", ".flac", ".aiff", ".aif", ".ogg"}:
            raise ValueError("Use WAV, FLAC, AIFF or OGG audio.")
        return candidate
