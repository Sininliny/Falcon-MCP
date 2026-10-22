"""Performance-UI checks against small UVIScript UIs run in the offline double, plus fake bridge/native hosts."""

import json
from importlib.resources import files
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from mcp.server.fastmcp import FastMCP

from falcon_mcp.audit import AuditLog
from falcon_mcp.ui_check import check_preset, compare_programs, contrast_ratio, parse_colour
from falcon_mcp.ui_tools import register_ui_checks

SESSION = "b" * 32
OSC = "Synth.parts[1].program.layers[1].keygroups[1].oscillators[1]"


def program_xml(script: str, script_data: str = "<ScriptData />") -> str:
    multi = json.loads(files("falcon_mcp").joinpath("data/builder-v1.json").read_text(encoding="utf-8"))["multi_xml"]
    program = multi[multi.find("<Program"):multi.find("</Program>") + len("</Program>")]
    processor = (f'<EventProcessors><ScriptProcessor Name="EventProcessor0" Bypass="0" API_version="23">'
                 f'<Properties ScriptPath="ui.lua" /><script>{escape(script)}</script>{script_data}'
                 f'</ScriptProcessor></EventProcessors>')
    return '<?xml version="1.0" encoding="UTF-8"?><UVI4>' + program.replace("</Program>", processor + "</Program>") + "</UVI4>"


def run(tmp_path, script, **kwargs):
    path = tmp_path / "ui.uvip"
    path.write_text(program_xml(script), encoding="utf-8")
    report = check_preset(path, **kwargs)
    assert report["source"]["scripts"] == 1
    return report["scripts"][0]


def codes(report, widget=None):
    return {f["code"] for f in report["findings"] if widget is None or f.get("widget", {}).get("name") == widget}


def test_parent_panel_blocking_mouse_is_an_error_even_though_callbacks_work(tmp_path):
    report = run(tmp_path, """
makePerformanceView()
local main = Panel("Main"); main.bounds = {0, 0, 720, 200}; main.interceptsMouseClicks = false
local osc = Program.layers[1].keygroups[1].oscillators[1]
for i = 1, 8 do
  local k = main:Knob("Macro" .. i, 0.5, 0, 1)
  k.bounds = {(i - 1) * 85 + 5, 10, 80, 50}
  k.changed = function(self) osc:setParameter("Pwm", 0.1 + self.value * 0.8) end
end
""")
    blocked = [f for f in report["findings"] if f["code"] == "parent_blocks_mouse"]
    assert len(blocked) == 8 and all(f["severity"] == "error" and f["confidence"] == "high" for f in blocked)
    assert all(c["engine_writes"][0]["parameter"] == "Pwm" for c in report["controls"])
    assert report["status"]["mouse"] == "not_tested" and "not_tested" in report["scope"]["mouse"]
    assert "occluded" not in codes(report)


def test_later_panel_occludes_earlier_widget_but_children_and_earlier_panels_do_not(tmp_path):
    report = run(tmp_path, """
makePerformanceView()
local back = Panel("Back"); back.bounds = {0, 0, 720, 100}
local inside = back:Knob("Inside", 0, 0, 1); inside.bounds = {10, 10, 100, 50}
inside.changed = function() end
local k = Knob("Under", 0, 0, 1); k.bounds = {300, 10, 100, 50}; k.changed = function() end
local free = Knob("Free", 0, 0, 1); free.bounds = {500, 120, 100, 50}; free.changed = function() end
local glass = Panel("Glass"); glass.bounds = {250, 0, 200, 80}; glass.backgroundColour = "00000000"
""")
    occluded = [f for f in report["findings"] if f["code"] == "occluded"]
    assert [f["widget"]["name"] for f in occluded] == ["Under"]
    assert occluded[0]["severity"] == "error" and occluded[0]["evidence"]["occluder"] == "Glass"
    assert "occluded" not in codes(report, "Inside") and "occluded" not in codes(report, "Free")


def test_label_contrast_and_colour_parsing(tmp_path):
    assert parse_colour("white")[:3] == (255, 255, 255)
    assert parse_colour("80ff0000") == (255, 0, 0, 128 / 255)
    assert parse_colour("#00ff00")[:3] == (0, 255, 0) and parse_colour("nonsense") is None
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21, rel=1e-3)
    report = run(tmp_path, """
makePerformanceView()
setBackgroundColour("101010")
local dark = Label("Dark"); dark.text = "Body"; dark.textColour = "202020"; dark.bounds = {10, 10, 100, 20}
local fine = Label("Fine"); fine.text = "Room"; fine.textColour = "f0f0f0"; fine.bounds = {10, 40, 100, 20}
local faded = Label("Faded"); faded.text = "Air"; faded.textColour = "ffffff"; faded.alpha = 0.1
faded.bounds = {10, 70, 100, 20}
""")
    low = {f["widget"]["name"]: f for f in report["findings"] if f["code"] == "low_contrast"}
    assert set(low) == {"Dark", "Faded"}
    assert low["Dark"]["severity"] == "error" and low["Dark"]["confidence"] == "high"
    assert low["Dark"]["evidence"]["ratio"] < 1.5


def test_duplicate_entry_points_labels_and_dead_controls(tmp_path):
    report = run(tmp_path, """
makePerformanceView()
local a = Button("Start"); a.changed = function() end
local b = Button("Resume"); b.changed = function() end
local c = Button("Explore"); c.changed = function() end
local d = Knob("Tone", 0, 0, 1); d.displayName = "Tone"
local e = Knob("Tone2", 0, 0, 1); e.displayName = "Tone"; e.changed = function() end
""")
    entry = next(f for f in report["findings"] if f["code"] == "multiple_entry_points")
    assert set(entry["evidence"]["widgets"]) == {"Start", "Resume", "Explore"}
    assert "duplicate_label" in codes(report)
    assert "no_callback" in codes(report, "Tone")
    assert "no_observable_effect" in codes(report, "Start")


def test_simulated_writes_flag_unknown_parameters_ranges_and_persistence(tmp_path):
    report = run(tmp_path, """
makePerformanceView()
local osc = Program.layers[1].keygroups[1].oscillators[1]
local env = Program.layers[1].keygroups[1].modulations[1]
local g = Knob("Level", 0.5, 0, 1); g.changed = function(self) osc:setParameter("Gain", self.value * 3) end
local t = Knob("Typo", 0, 0, 1); t.changed = function(self) osc:setParameter("Gian", self.value) end
local r = Knob("Release", 0.01, 0.01, 4); r.persistent = false
r.changed = function(self) env:setParameter("ReleaseTime", self.value) end
local m = Menu("Wave", {"Saw", "Square", "Sine"}); m.changed = function(self) osc:setParameter("Waveform", self.value) end
""")
    assert "write_out_of_range" in codes(report, "Level")
    assert "unknown_engine_parameter" in codes(report, "Typo")
    assert "not_persistent_but_drives_engine" in codes(report, "Release")
    wave = next(c for c in report["controls"] if c["widget"] == "Wave")
    assert wave["tested_values"] == [1, 2, 3]
    assert wave["engine_writes"][0]["selector"] == OSC
    assert wave["engine_writes"][0]["native"].endswith("Oscillators/MinBlepGenerator[1]")
    assert "no_stored_widget_state" in codes(report)
    # Driving restores each control; the double does not leak test values into the widget table.
    level = next(w for w in report["widgets"] if w["name"] == "Level")
    assert level["value"] == 0.5


def test_tabs_revealed_by_a_control_are_info_but_never_shown_controls_warn(tmp_path):
    report = run(tmp_path, """
makePerformanceView()
local page = Panel("Page2"); page.visible = false; page.bounds = {0, 100, 720, 100}
local deep = page:Knob("Deep", 0, 0, 1); deep.changed = function() end
local tab = OnOffButton("Tab2", false); tab.bounds = {600, 10, 100, 20}
tab.changed = function(self) page.visible = self.value end
local ghost = Knob("Ghost", 0, 0, 1); ghost.visible = false; ghost.bounds = {300, 10, 100, 50}
ghost.changed = function() end
""")
    assert "hidden_initially" in codes(report, "Deep") and "never_visible" not in codes(report, "Deep")
    assert "never_visible" in codes(report, "Ghost")
    tab = next(c for c in report["controls"] if c["widget"] == "Tab2")
    assert tab["ui_changes"][0] == {"target": "Page2", "property": "visible", "value": True, "test_value": True}


def test_script_errors_loops_and_missing_performance_view_are_reported(tmp_path):
    broken = run(tmp_path, "local k = Knob('A', 0, 0, 1)\nlocal x = nil + 1\nlocal later = Knob('B', 0, 0, 1)")
    assert broken["status"]["script_ran"] == "error"
    assert [w["name"] for w in broken["widgets"]] == ["A"]
    assert "no_performance_view" in codes(broken)
    looping = run(tmp_path, "makePerformanceView()\nlocal k = Knob('A', 0, 0, 1)\n"
                            "k.changed = function() while true do end end")
    assert "callback_error" in codes(looping, "A")
    assert "instruction budget" in next(f for f in looping["findings"] if f["code"] == "callback_error")["message"]
    sandboxed = run(tmp_path, "makePerformanceView()\nos.execute('echo no')\nio.open('x')")
    assert sandboxed["status"]["script_ran"] == "error"


def test_auto_layout_overlap_is_low_confidence_and_ascii_layout_lists_widgets(tmp_path):
    report = run(tmp_path, """
makePerformanceView()
local a = Knob("A", 0, 0, 1); a.changed = function() end
local b = Knob("B", 0, 0, 1); b.changed = function() end
local over = Panel("Over"); over.bounds = {0, 0, 300, 60}
""")
    occluded = [f for f in report["findings"] if f["code"] == "occluded"]
    assert {f["widget"]["name"] for f in occluded} == {"A", "B"}
    assert all(f["confidence"] == "low" for f in occluded)
    assert [w["position"] for w in report["widgets"][:2]] == ["auto_layout_estimate"] * 2
    assert any(line.startswith("A=Knob:A") for line in report["layout_ascii"])


def test_bare_lua_uses_placeholders_and_still_catches_name_typos(tmp_path):
    path = tmp_path / "ui.lua"
    path.write_text("makePerformanceView()\nlocal k = Knob('K', 0, 0, 1)\n"
                    "k.changed = function(self) Program.layers['Body'].keygroups[1].oscillators[1]"
                    ":setParameter('Gian', self.value) end", encoding="utf-8")
    report = check_preset(path)
    assert report["status"]["script_ran"] == "ok"
    assert "parameter_name_not_indexed" in codes(report, "K")
    assert "unknown_engine_parameter" not in codes(report)


def test_compare_programs_reports_engine_and_script_state_changes():
    before = program_xml("makePerformanceView()", '<ScriptData><W Name="Tone" Value="0.5"/></ScriptData>')
    after = before.replace('Value="0.5"', 'Value="0.2"').replace('Pwm="0.5"', 'Pwm="0.25"')
    same = compare_programs(before.encode(), before.encode())
    diff = compare_programs(before.encode(), after.encode())
    assert same["identical"] and not diff["identical"]
    assert any(c["key"].endswith("@Pwm") for c in diff["parameter_changes"])
    assert diff["script_state_changes"][0]["script"].endswith("ScriptProcessor[1]")


class FakeBridge:
    def __init__(self, drives=True, fail_on=None):
        self.values = {"Synth.parts[1].program.eventProcessors[1]": {"Tone": 0.5}, OSC: {"Pwm": 0.5}}
        self.drives, self.fail_on, self.calls = drives, fail_on, []

    async def request(self, op, args=None, *, mutation=False):
        self.calls.append((op, args, mutation))
        node = self.values[args["selector"]]
        if op == "get_parameters":
            return {"items": [{"name": n, "value": node[n]} for n in args["names"]]}
        assert op == "set_parameters" and mutation
        change = args["changes"][0]
        if self.fail_on is not None and change["value"] == self.fail_on:
            raise RuntimeError("bridge timeout")
        if "expected" in change:
            assert node[change["name"]] == change["expected"]
        node[change["name"]] = change["value"]
        if self.drives:
            self.values[OSC]["Pwm"] = 0.1 + change["value"] * 0.8
        return {"items": [{"name": change["name"], "after": change["value"], "verified": True}], "verified": True}


def ui_tools(config, bridge=None, host=None):
    mcp = FastMCP("ui-test")
    register_ui_checks(mcp, config, AuditLog(config.log_root), bridge=bridge, native_host=host)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


@pytest.mark.asyncio
async def test_falcon_ui_check_tool_saves_full_report_and_leads_with_scope(config, tmp_path):
    path = tmp_path / "ui.uvip"
    path.write_text(program_xml("makePerformanceView()\nlocal k = Knob('K', 0, 0, 1)"), encoding="utf-8")
    tools = ui_tools(config)
    assert set(tools) == {"falcon_ui_check"}
    result = await tools["falcon_ui_check"](path=str(path))
    assert next(iter(result)) == "scope"
    saved = Path(result["full_report"]).read_text(encoding="utf-8")
    assert json.loads(saved)["scripts"][0]["widgets"]
    with pytest.raises(ValueError):
        await tools["falcon_ui_check"](path=str(tmp_path / "missing.uvip"))


@pytest.mark.asyncio
async def test_live_probe_observes_targets_and_always_restores(config):
    bridge = FakeBridge()
    probe = ui_tools(config, bridge=bridge)["falcon_ui_live_probe"]
    watch = [{"selector": OSC, "names": ["Pwm"]}]
    from falcon_mcp.ui_tools import Watch
    result = await probe(script_selector="Synth.parts[1].program.eventProcessors[1]", widget="Tone",
                         values=[0.0, 1.0], watch=[Watch(**w) for w in watch], settle_ms=0)
    assert result["status"]["control_to_engine"] == "observed" and result["restored"]
    assert result["steps"][1]["targets_changed"][f"{OSC}:Pwm"]["after"] == pytest.approx(0.9)
    assert bridge.values["Synth.parts[1].program.eventProcessors[1]"]["Tone"] == 0.5
    assert result["status"]["mouse"] == "not_tested"

    silent = FakeBridge(drives=False)
    result = await ui_tools(config, bridge=silent)["falcon_ui_live_probe"](
        script_selector="Synth.parts[1].program.eventProcessors[1]", widget="Tone", values=[1.0],
        watch=[Watch(**w) for w in watch], settle_ms=0)
    assert result["status"]["control_to_engine"] == "inconclusive"

    failing = FakeBridge(fail_on=0.7)
    with pytest.raises(RuntimeError):
        await ui_tools(config, bridge=failing)["falcon_ui_live_probe"](
            script_selector="Synth.parts[1].program.eventProcessors[1]", widget="Tone", values=[0.2, 0.7],
            watch=[Watch(**w) for w in watch], settle_ms=0)
    assert failing.values["Synth.parts[1].program.eventProcessors[1]"]["Tone"] == 0.5


class FakeNativeHost:
    """Writes session.uvim like the worker; reload optionally loses script state."""

    def __init__(self, config, lose_state=False):
        self.root = config.runtime.parent / "native-host" / SESSION
        self.root.mkdir(parents=True)
        self.lose_state = lose_state
        self.program = program_xml("makePerformanceView()\nlocal k = Knob('Tone', 0.5, 0, 1)",
                                   '<ScriptData><W Name="Tone" Value="0.3"/></ScriptData>')
        self.ops = []

    def multi(self):
        inner = self.program[self.program.find("<Program"):self.program.find("</UVI4>")]
        return ('<UVI4><Engine><Synth><Children><Part Name="Part 0">' + inner
                + "</Part></Children></Synth></Engine></UVI4>")

    async def request(self, session_id, op, args):
        assert session_id == SESSION
        self.ops.append(op)
        if op == "render":
            (self.root / "session.uvim").write_text(self.multi(), encoding="utf-8")
            return {"path": args["path"]}
        if op == "save":
            Path(args["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(args["path"]).write_text(self.program, encoding="utf-8")
            return {"path": args["path"], "sha256": "0" * 64}
        if op == "load":
            if self.lose_state:
                self.program = self.program.replace('Value="0.3"', 'Value="0.5"')
            return {"revision": "1" * 64}
        raise AssertionError(op)


@pytest.mark.asyncio
async def test_native_roundtrip_reports_kept_or_changed_state(config):
    host = FakeNativeHost(config)
    roundtrip = ui_tools(config, host=host)["falcon_native_ui_roundtrip"]
    result = await roundtrip(session_id=SESSION)
    assert host.ops == ["render", "save", "load", "render"]
    assert result["status"]["save_reload"] == "kept"
    assert result["stored_widget_state"][0]["widgets"] == {"Tone": True}
    assert "not_tested" in result["scope"]["mouse"]

    (config.runtime.parent / "native-host" / SESSION / "session.uvim").unlink()
    (config.runtime.parent / "native-host" / SESSION).rmdir()
    lossy = FakeNativeHost(config, lose_state=True)
    result = await ui_tools(config, host=lossy)["falcon_native_ui_roundtrip"](session_id=SESSION)
    assert result["status"]["save_reload"] == "changed"
    assert result["difference"]["script_state_changes"]
