from pathlib import Path

import pytest

from falcon_mcp.config import Config


@pytest.fixture
def config(tmp_path: Path):
    return Config(
        runtime=tmp_path / "bridge",
        audio_root=tmp_path / "audio",
        model_cache=tmp_path / "models",
        bridge_timeout=0.15,
    )
