"""Native write readback: what the plugin kept after the write, after scripts ran, and after a reload."""

import pytest
from test_native_revision import Engine

from falcon_mcp.native_edit import edit_node
from falcon_mcp.native_readback import classify, report, unit_hints
from falcon_mcp.native_state import parse_xml

PROGRAM = "Engine/Synth/Children/Part[1]/Program[1]"
GAIN = {"name": "Gain", "type": "float", "min": "0.000", "max": "1.995", "unit": "linear gain (1 is 0 dB)"}


def test_classes_name_rounding_clamping_truncation_unit_slips_and_ignored_writes():
    assert classify(0.5, "1", "0.5", GAIN) == "exact"
    assert classify(0.3, "1", "0.30000001", GAIN) == "rounded"
    assert classify(0.5, "1", "1", GAIN) == "not_kept"
    assert classify(2.5, "1", "1.995", GAIN) == "clamped_max"
    assert classify(-1.0, "1", "0", GAIN) == "clamped_min"
    assert classify(3.7, "1", "3", None) == "truncated"
    assert classify(1.9, "1", "1.5", GAIN) == "limited"
    assert classify(250.0, "1", "0.25", None) == "unit_mismatch_suspected"
    assert classify(0.5, "1", None, GAIN) == "missing"
    assert classify(True, "0", "1", None) == "exact" and classify(True, "0", "0", None) == "not_kept"


def test_unit_hints_catch_decibels_for_linear_gain_percent_and_milliseconds():
    assert "decibels" in unit_hints(-6.0, GAIN)[0] and "0.5012" in unit_hints(-6.0, GAIN)[0]
    assert "silence" in unit_hints(0.0, GAIN, before="1")[0]
    mix = {"name": "Mix", "min": "0", "max": "1", "unit": "% normalized"}
    assert "use 0.35" in unit_hints(35.0, mix)[0] and unit_hints(0.35, mix) == []
    decay = {"name": "DecayTime", "min": "0.1", "max": "10", "unit": "second"}
    assert "use 1.2" in unit_hints(1200.0, decay)[0]
    makeup = {"name": "GainOut", "min": "-30", "max": "30", "unit": "decibel"}
    assert "linear gain" in unit_hints(0.5, makeup, before="-6")[0] and unit_hints(-3.0, makeup, "-6") == []


def test_out_of_range_error_names_the_range_unit_and_the_likely_conversion():
    root = parse_xml(b'<UVI4><Program Gain="1"/></UVI4>')
    with pytest.raises(ValueError, match=r"outside the indexed range 0.000..1.995 linear gain.*0.5012"):
        edit_node(root, "Program", [{"name": "Gain", "value": -6.0, "expected": "1"}])


def test_report_marks_script_rewrites_and_reload_changes_separately():
    changes = [{"name": "Gain", "value": 1.2}, {"name": "Pan", "value": 0.1}]
    result = report(changes, {"Gain": "1", "Pan": "0"},
                    {"write": {"Gain": "1.2", "Pan": "0.1"}, "settle": {"Gain": "0.25", "Pan": "0.1"},
                     "reload": {"Gain": "0.25", "Pan": "0"}}, {"Gain": GAIN})
    gain, pan = result["items"]
    assert gain["status"] == "rewritten_by_script" and gain["write_class"] == "exact"
    assert pan["status"] == "changed_after_reload" and pan["settle_class"] == "exact"
    assert result["status"] == "changed" and result["changed"] == 2 and len(result["warnings"]) == 2


def _gain_engine(tmp_path, mode):
    worker = Engine(tmp_path, mode)
    path = tmp_path / "gain.uvim"
    path.write_bytes(b'<UVI4><Engine><Synth><Children><Part><Program DisplayName="G" Gain="1" Pan="0"/>'
                     b'</Part></Children></Synth></Engine></UVI4>')
    loaded = worker("load", kind="multi", path=str(path), part=1)
    return worker, loaded


def test_edit_reports_kept_values_through_settle_and_reload(tmp_path):
    worker, loaded = _gain_engine(tmp_path, "init")
    try:
        result = worker("edit", selector=PROGRAM, expected_revision=loaded["revision"], verify="reload",
                        changes=[{"name": "Gain", "value": 0.8, "expected": "1"}])
        assert result["verified"] and result["readback_report"]["stages"] == ["write", "settle", "reload"]
        assert result["readback_report"]["items"][0]["status"] == "kept" and result["warnings"] == []
        assert result["reload"]["passes"] >= 2
    finally:
        worker.close()


def test_edit_flags_a_value_the_instrument_script_rewrites_after_the_write(tmp_path):
    worker, loaded = _gain_engine(tmp_path, "rewrite")
    try:
        current = worker("edit", selector=PROGRAM)["attributes"]["Gain"]
        result = worker("edit", selector=PROGRAM, expected_revision=loaded["revision"], verify="settle",
                        changes=[{"name": "Gain", "value": 1.2, "expected": current}])
        item = result["readback_report"]["items"][0]
        assert not result["verified"] and item["write_class"] == "exact"
        assert item["status"] == "rewritten_by_script" and item["settle"] == "0.25"
        assert "rewritten_by_script" in result["warnings"][0]
        # verify="write" keeps the old, faster behaviour: the script has not run yet.
        quick = worker("edit", selector=PROGRAM, expected_revision=result["revision"], verify="write",
                       changes=[{"name": "Gain", "value": 1.1, "expected": "0.25"}])
        assert quick["verified"] and quick["readback_report"]["stages"] == ["write"]
    finally:
        worker.close()


def test_edit_error_names_the_clamp_when_the_plugin_refuses_the_written_value(tmp_path):
    worker, loaded = _gain_engine(tmp_path, "clamp")
    try:
        with pytest.raises(ValueError, match=r"readback mismatch: Gain: requested 1.9, write reads 1.5 \(limited\)"):
            worker("edit", selector=PROGRAM, expected_revision=loaded["revision"],
                   changes=[{"name": "Gain", "value": 1.9, "expected": "1"}])
    finally:
        worker.close()
