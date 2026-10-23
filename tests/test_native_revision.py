"""Instrument scripts that write parameters while the owned host processes audio (Polar Current EP, Glacier)."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from falcon_mcp.native_revision import MAX_ALIASES, RevisionAliases, settle

SESSION = "b" * 32

# A stand-in for pedalboard: Falcon whose instrument script writes envelope/pan values on its first process
# blocks after a state load ("init"), or a counter on every render ("every").
FAKE_PEDALBOARD = textwrap.dedent('''
    import os
    import struct
    import xml.etree.ElementTree as ET
    import zlib

    import numpy as np

    from falcon_mcp.native_state import encode_memory, replace_state_xml, state_xml

    MULTI = (b'<UVI4><Engine><Synth><Children><Part><Program DisplayName="Init"/></Part></Children></Synth>'
             b'</Engine></UVI4>')


    def host_state(xml):
        payload = b"UVI4\\x01\\0\\0\\0" + struct.pack("<I", len(xml)) + zlib.compress(xml) + b"controller-tail"
        component = bytearray(176) + payload
        component[:4] = b"VstW"
        component[16:20] = b"CcnK"
        component[24:28] = b"FBCh"
        struct.pack_into(">I", component, 20, len(component) - 24)
        struct.pack_into(">I", component, 172, len(component) - 176)
        root = ET.Element("VST3PluginState")
        ET.SubElement(root, "IComponent").text = encode_memory(component)
        outer = ET.tostring(root, encoding="utf-8")
        return b"VC2!" + struct.pack("<I", len(outer)) + outer + b"\\0"


    class Plugin:
        name = "Falcon"

        def __init__(self):
            self.mode = os.environ.get("FAKE_SCRIPT_MODE", "init")
            self._state = host_state(MULTI)
            self.pending_init = True
            self.processed = 0

        @property
        def raw_state(self):
            return self._state

        @raw_state.setter
        def raw_state(self, value):
            self._state = value
            self.pending_init = True  # Falcon re-runs the instrument script after a state load.

        def _script_write(self, **attributes):
            tree = ET.fromstring(state_xml(self._state))
            for program in tree.iter("Program"):
                for name, value in attributes.items():
                    program.set(name, value)
            self._state = replace_state_xml(self._state, ET.tostring(tree, encoding="utf-8"))

        def __call__(self, midi, duration, sample_rate, num_channels=2, buffer_size=512, reset=True):
            self.processed += 1
            if self.pending_init:
                self.pending_init = False
                self._script_write(ScriptAttack="0.012", ScriptPan="-0.2")
            if self.mode == "every":
                self._script_write(ScriptCounter=str(self.processed))
            return np.zeros((num_channels, int(duration * sample_rate)), dtype="float32")


    def load_plugin(path):
        return Plugin()
''')


def multi(name: str) -> bytes:
    return (f'<UVI4><Engine><Synth><Children><Part><Program DisplayName="{name}"/></Part></Children></Synth>'
            f'</Engine></UVI4>').encode()


class Engine:
    """The real native worker engine loop with the fake plugin, spoken to over its JSON line protocol."""

    def __init__(self, tmp_path: Path, mode: str):
        fake = tmp_path / "fake"
        (fake / "pedalboard").mkdir(parents=True)
        (fake / "pedalboard" / "__init__.py").write_text(FAKE_PEDALBOARD)
        plugin = tmp_path / "Falcon.vst3"
        plugin.write_bytes(b"not a real plugin")
        root = tmp_path / "native"
        root.mkdir()
        src = Path(__file__).resolve().parents[1] / "src"
        env = {**os.environ, "FALCON_NATIVE_ROOT": str(root), "FALCON_NATIVE_SESSION": SESSION,
               "FALCON_VST3_PATH": str(plugin), "FAKE_SCRIPT_MODE": mode,
               "PYTHONPATH": os.pathsep.join([str(fake), str(src), os.environ.get("PYTHONPATH", "")])}
        self.tmp = tmp_path
        self.process = subprocess.Popen([sys.executable, "-m", "falcon_mcp.native_worker", "engine"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env=env)
        self.ready = json.loads(self.process.stdout.readline())
        self.count = 0

    def __call__(self, op, **args):
        self.count += 1
        self.process.stdin.write((json.dumps({"id": str(self.count), "session_id": SESSION, "op": op,
                                              "args": args}) + "\n").encode())
        self.process.stdin.flush()
        reply = json.loads(self.process.stdout.readline())
        if not reply["ok"]:
            raise ValueError(reply["error"])
        return reply["result"]

    def render(self, revision, name="render"):
        return self("render", path=str(self.tmp / f"{name}-{self.count}.wav"), seconds=1, sample_rate=48000,
                    notes=[{"note": 60, "velocity": 100, "start_seconds": 0.1, "duration_seconds": 0.5}],
                    expected_revision=revision)

    def load(self, name, **extra):
        path = self.tmp / f"{name}.uvim"
        path.write_bytes(multi(name))
        return self("load", kind="multi", path=str(path), part=1, **extra)

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


@pytest.fixture
def engine(tmp_path, request):
    worker = Engine(tmp_path, getattr(request, "param", "init"))
    yield worker
    worker.close()


def test_load_returns_the_revision_after_the_instrument_script_has_written_its_parameters(engine):
    assert engine.ready["ready"] and engine.ready["settle"]["changed"]
    loaded = engine.load("Polar Current EP")
    assert loaded["settle"]["changed"] and loaded["settle"]["stable"] and loaded["settle"]["passes"] >= 3
    revision = loaded["revision"]
    # Every cell of a sweep is pinned to the load revision; none of them sees a "changed" state.
    for cell in range(4):
        result = engine.render(revision, f"cell{cell}")
        assert result["revision"] == revision and not result["state_changed_by_render"]
    status = engine("status")
    assert status["revision"] == revision and not status["loaded_source"]["session_modified_since_load"]
    session = (engine.tmp / "native" / "session.uvim").read_bytes()
    assert b'ScriptAttack="0.012"' in session


def test_unsettled_first_render_still_lets_later_cells_use_the_pinned_revision(engine):
    # settle_seconds=0 reproduces the old worker: the script writes during the first sweep cell.
    loaded = engine.load("Glacier", settle_seconds=0)
    revision = loaded["revision"]
    first = engine.render(revision, "first")
    assert first["state_changed_by_render"] and first["revision_after"] != revision
    second = engine.render(revision, "second")  # failed with "Native revision changed" before the fix
    assert second["revision"] == first["revision_after"] and not second["state_changed_by_render"]
    status = engine("status")
    assert status["equivalent_revisions"] == [revision]
    assert not status["loaded_source"]["session_modified_since_load"]


def test_a_real_change_after_the_pinned_revision_is_still_rejected(engine):
    revision = engine.load("Glacier", settle_seconds=0)["revision"]
    engine.render(revision)
    engine.load("Another instrument")
    with pytest.raises(ValueError, match="revision changed"):
        engine.render(revision)


@pytest.mark.parametrize("engine", ["every"], indirect=True)
def test_scripts_that_write_on_every_render_keep_the_original_revision_valid(engine):
    loaded = engine.load("Polar Current EP")
    assert not loaded["settle"]["stable"]  # the state never stops changing; say so instead of hiding it
    revision = loaded["revision"]
    results = [engine.render(revision, f"cell{i}") for i in range(5)]
    assert all(r["state_changed_by_render"] for r in results)
    assert revision in engine("status")["equivalent_revisions"]


class FakePlugin:
    def __init__(self, writes):
        self.writes, self.calls, self.state = list(writes), 0, 0

    def __call__(self, midi, **kwargs):
        assert midi == [] and kwargs["duration"] == 0.5
        self.calls += 1
        if self.writes and self.writes[0] == self.calls:
            self.writes.pop(0)
            self.state += 1


def test_settle_waits_for_two_quiet_passes_so_a_late_script_write_is_caught():
    plugin = FakePlugin(writes=[2])  # sample loading delays the script to the second pass
    outcome = settle(plugin, lambda: str(plugin.state))
    assert outcome["changed"] and outcome["stable"] and plugin.calls == 4 and outcome["revision"] == "1"
    quiet = FakePlugin(writes=[])
    assert settle(quiet, lambda: str(quiet.state)) == {"passes": 2, "seconds": 1.0, "changed": False,
                                                       "stable": True, "revision": "0"}
    assert settle(quiet, lambda: "x", seconds=0)["passes"] == 0


def test_revision_aliases_follow_render_chains_and_clear_on_real_edits():
    aliases = RevisionAliases()
    assert aliases.matches("a", "a") and not aliases.matches("a", "b")
    assert not aliases.rendered("a", "a")
    assert aliases.rendered("a", "b") and aliases.rendered("b", "c")
    assert aliases.matches("a", "c") and aliases.matches("b", "c") and not aliases.matches("a", "b")
    for index in range(MAX_ALIASES * 2):
        aliases.rendered(aliases.current, f"r{index}")
    assert len(aliases.of(aliases.current)) == MAX_ALIASES and aliases.matches("a", aliases.current)
    aliases.clear()
    assert not aliases.matches("a", "c")
