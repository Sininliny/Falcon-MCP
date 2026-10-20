import json

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp.reference import compare_profiles, extract_segment, profile
from falcon_mcp.server import create_server


def test_exact_segment_from_long_source_without_reading_whole_file(config, tmp_path):
    source = tmp_path / "upload.wav"
    audio = np.zeros(60 * 8000, dtype=np.float32)
    audio[35 * 8000:36 * 8000] = 0.25
    sf.write(source, audio, 8000, subtype="FLOAT")
    path, meta = extract_segment(config, str(source), 35, 36)
    selected, rate = sf.read(path)
    assert rate == 8000 and len(selected) == 8000
    assert np.all(selected == 0.25) and meta["start_seconds"] == 35
    assert meta["end_seconds"] == 36 and len(meta["sha256"]) == 64
    assert source.read_bytes() != path.read_bytes()


@pytest.mark.parametrize("start,end", [(-1, 1), (1, 1), (0, 31), (float("nan"), 1), (0, float("inf"))])
def test_invalid_reference_ranges_fail_before_audio_read(config, start, end):
    with pytest.raises(ValueError, match="timestamps"):
        extract_segment(config, "missing.wav", start, end)


def test_reference_rejects_end_beyond_file(config, tmp_path):
    path = tmp_path / "short.wav"
    sf.write(path, np.zeros(8000), 8000)
    with pytest.raises(ValueError, match="exceeds source duration"):
        extract_segment(config, str(path), 0.5, 1.5)


def test_comparison_separates_gain_from_timbre_and_handles_silence(config):
    config.audio_root.mkdir()
    t = np.arange(8000) / 8000
    r, c, silent = [config.audio_root / name for name in ("r.wav", "c.wav", "silent.wav")]
    sf.write(r, np.sin(t * 2 * np.pi * 440) * 0.4, 8000, subtype="FLOAT")
    sf.write(c, np.sin(t * 2 * np.pi * 440) * 0.1, 8000, subtype="FLOAT")
    sf.write(silent, np.zeros(8000), 8000)
    ref, candidate = profile(r), profile(c)
    result = compare_profiles(ref, candidate)
    assert result["available"]
    assert result["level_match_gain_db"] == pytest.approx(12.0412, abs=0.001)
    assert max(abs(b["candidate_minus_reference_db"]) for b in result["normalized_band_deltas"]) < 0.01
    assert not compare_profiles(ref, profile(silent))["available"]


async def test_reference_tools_preserve_goal_and_do_not_load_model_by_default(config, tmp_path):
    source = tmp_path / "attachment.wav"
    sf.write(source, np.sin(np.arange(16000) * 0.3) * 0.1, 8000)
    server = create_server(config)
    result = await server._tool_manager.get_tool("falcon_analyze_reference").fn(
        source=str(source), start_seconds=0.5, end_seconds=1.5,
        focus="Lead behind the vocal", requested_changes="Less bright, shorter tail",
    )
    assert result["requested_changes"] == "Less bright, shorter tail"
    assert "feedback" not in result
    candidate = config.audio_root / "candidate.wav"
    sf.write(candidate, np.sin(np.arange(8000) * 0.3) * 0.1, 8000)
    comparison = await server._tool_manager.get_tool("falcon_compare_reference").fn(
        reference_id=result["reference_id"], candidate_path=str(candidate),
    )
    assert comparison["focus"] == result["focus"] and comparison["comparison"]["available"]
    assert "reference_feedback" not in comparison
    status = server._tool_manager.get_tool("falcon_capture_status").fn()
    assert status["state"] == "idle"
    metadata = json.loads((config.audio_root / f"reference-{result['reference_id']}.json").read_text())
    assert metadata["focus"] == result["focus"]
    with pytest.raises(ValueError, match="reference_id"):
        await server._tool_manager.get_tool("falcon_compare_reference").fn(
            reference_id="../escape", candidate_path=str(candidate),
        )
