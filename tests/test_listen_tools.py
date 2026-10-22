import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP

from falcon_mcp.listen_tools import register_listening

RATE = 48000


def tone(path, frequency=220.0, harmonics=30, attack=0.01, seconds=2.0, release_at=1.4, level=0.3):
    t = np.arange(int(RATE * seconds)) / RATE
    wave = sum(np.sin(2 * np.pi * k * frequency * t) / k for k in range(1, harmonics + 1))
    envelope = np.minimum(t / attack, 1) * np.where(t < release_at, 1.0, np.exp(-(t - release_at) / 0.08))
    audio = (level * wave * envelope / np.abs(wave).max())[:, None].repeat(2, axis=1)
    sf.write(path, audio.astype(np.float32), RATE, subtype="FLOAT")


@pytest.fixture
def server(config):
    config.audio_root.mkdir()
    mcp = FastMCP("test")
    register_listening(mcp, config)
    return mcp


async def call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    return result[1] if isinstance(result, tuple) else result


async def test_listen_summary_reports_tuning_envelope_and_ascii(server, config):
    tone(config.audio_root / "a.wav", frequency=220 * 2 ** (12 / 1200))
    result = await call(server, "falcon_listen", {"path": "a.wav", "expected_note": 57,
                                                  "note_on_seconds": 0, "note_off_seconds": 1.4})
    assert abs(result["pitch"]["cents_error"] - 12) < 2
    assert result["envelope"]["shape"] == "sustained"
    assert 3 < result["envelope"]["attack_ms"] < 15
    assert "|" in result["ascii"]["spectrogram"] and "#" in result["ascii"]["envelope"]
    assert any("sharp by 12 cents" in w for w in result["description"])
    assert result["full_report"].endswith(".listen.json")


async def test_diff_ranks_brightness_and_target_points_direction(server, config):
    tone(config.audio_root / "dark.wav", harmonics=3)
    tone(config.audio_root / "bright.wav", harmonics=40)
    diff = await call(server, "falcon_audio_diff", {"before_path": "dark.wav", "after_path": "bright.wav",
                                                    "expected_note": 57})
    features = [row["feature"] for row in diff["diff"]["significant_changes"]]
    assert "centroid_hz" in features
    assert any(p.startswith("brighter") for p in diff["diff"]["summary"])
    match = await call(server, "falcon_match_target", {"path": "dark.wav", "reference_path": "bright.wav",
                                                       "features": ["centroid_hz", "attack_ms"]})
    rows = {row["feature"]: row for row in match["evaluation"]["features"]}
    assert rows["centroid_hz"]["direction"] == "increase" and rows["attack_ms"]["status"] == "ok"
    assert not match["evaluation"]["passed"]
    exact = await call(server, "falcon_match_target", {"path": "bright.wav", "target": {
        "attack_ms": {"min": 2, "max": 20}, "sustain_level_db": {"value": 0, "tolerance": 1.5}}})
    assert exact["evaluation"]["passed"], exact["evaluation"]


async def test_match_target_requires_exactly_one_goal(server, config):
    tone(config.audio_root / "a.wav")
    with pytest.raises(Exception, match="either target"):
        await call(server, "falcon_match_target", {"path": "a.wav"})
