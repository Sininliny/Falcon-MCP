"""Run the REAL Lua dispatcher against a small engine double with Lua 5.1.

These test validation, selector parsing and async scheduling; hardware integration is separate.
"""

from importlib.resources import files

import pytest
from lupa.lua51 import LuaError, LuaRuntime


def dispatcher():
    lua = LuaRuntime(unpack_returned_tuples=True)
    source = files("falcon_mcp").joinpath("lua/bridge.lua").read_text(encoding="utf8")
    source += "\n" + files("falcon_mcp").joinpath("lua/extended.lua").read_text(encoding="utf8")
    # The return is only appended in tests; production exposes no arbitrary code tool.
    setup = """
    package.preload["uvi.json"] = function() return {} end
    local values = {Gain=1,Pan=0,Bypass=false,Ratio=1}
    local defs = {
       {name="Gain",type="float",min=0,max=2,readOnly=false},
       {name="Pan",type="float",min=-1,max=1,readOnly=false},
       {name="Bypass",type="bool",readOnly=false},
       {name="Ratio",type="float",min=1,max=0,readOnly=false}
    }
    local node={name="Program",type="Program",parameterDefinitions=defs,
      inserts={},layers={},numParams=3}
    function node:hasParameter(name) return values[name] ~= nil end
    function node:getParameter(name) return values[name] end
    function node:setParameter(name,value) values[name]=value end
    function node:getParameterConnections(name) return {} end
    function node:insertEffect(type,slot,cb)
      self.inserts[#self.inserts+1]={name=type,type=type,numParams=0}
      cb({success=true}); return {finished=true,success=true}
    end
    Synth={parts={{program=node}}};Program=node;this={parent=node}
    function getSamplingRate() return 48000 end
    function getTempo() return 120 end
    off_audio_thread=true
    function getTime()
      if off_audio_thread then error("cannot access RenderContext outside of the audio thread") end
      return 0
    end
    function wait(ms) error("Unexpected yield across dispatcher") end
    """
    lua.execute(setup)
    # Test access to private local helpers, not a production API.
    dispatch = lua.execute(
        source + "\nreturn {process=process, resolve=resolve, pending=function() return pending end}"
    )
    lua.globals().off_audio_thread = False
    return lua, dispatch


def table(lua, obj):
    if isinstance(obj, dict):
        return lua.table_from({k: table(lua, v) for k, v in obj.items()})
    if isinstance(obj, list):
        return lua.table_from([table(lua, v) for v in obj])
    return obj


def request(lua, d, op, args):
    return d.process(table(lua, {"protocol": 1, "id": "test", "op": op, "args": args}))


def test_parameter_types_and_optimistic_guard():
    lua, d = dispatcher()
    result = request(
        lua,
        d,
        "set_parameters",
        {"selector": "Synth.parts[1].program", "changes": [{"name": "Gain", "value": 0.5, "expected": 1}]},
    )
    assert result.verified and result["items"][1].after == 0.5
    with pytest.raises(LuaError, match="Stale expected"):
        request(
            lua,
            d,
            "set_parameters",
            {"selector": "Program", "changes": [{"name": "Gain", "value": 0.7, "expected": 1}]},
        )


@pytest.mark.parametrize(
    "change",
    [
        {"name": "DoesNotExist", "value": 0.5},
        {"name": "Gain", "value": "0.5"},
        {"name": "Gain", "value": 3},
        {"name": "Bypass", "value": 1},
    ],
)
def test_whole_batch_preflight_prevents_partial_validation_writes(change):
    lua, d = dispatcher()
    with pytest.raises(LuaError):
        request(
            lua,
            d,
            "set_parameters",
            {"selector": "Program", "changes": [{"name": "Pan", "value": 0.4}, change]},
        )
    result = request(lua, d, "get_parameters", {"selector": "Program", "names": ["Pan"]})
    assert result["items"][1].value == 0


@pytest.mark.parametrize("selector", ["Program;os.execute('x')", "Synth.parts[0]", "Synth.parts[1].evil()"])
def test_selector_never_evaluates_lua(selector):
    _lua, d = dispatcher()
    with pytest.raises(LuaError):
        d.resolve(selector)


def test_async_effect_does_not_yield_inside_pcall():
    lua, d = dispatcher()
    request(lua, d, "edit_effect", {"selector": "Program", "action": "insert", "type": "Gain"})
    pending = d.pending()
    assert pending.done and pending.ok
    assert pending.result["items"][1].type == "Gain"


def test_uvi_unbounded_numeric_sentinel():
    lua, d = dispatcher()
    result = request(
        lua, d, "set_parameters", {"selector": "Program", "changes": [{"name": "Ratio", "value": 0.5}]}
    )
    assert result.verified and result["items"][1].after == 0.5


def test_panic_does_not_resurrect_cancelled_sequence():
    lua, d = dispatcher()
    lua.execute("""
      scheduled={};played={}
      function spawn(fn) scheduled[#scheduled+1]=coroutine.create(fn) end
      function wait(ms) coroutine.yield(ms) end
      function playNote(note) played[#played+1]=note; return #played end
      function releaseVoice(id) end
      function resume(i) local ok,e=coroutine.resume(scheduled[i]); if not ok then error(e) end end
    """)
    request(lua, d, "play", {"notes": [{"note": 48, "velocity": 80, "duration_ms": 100, "delay_ms": 500}]})
    lua.globals().resume(1)  # delayed old sequence
    request(lua, d, "panic", {})
    request(lua, d, "play", {"notes": [{"note": 60, "velocity": 80, "duration_ms": 100}]})
    lua.globals().resume(2)  # new note plays
    lua.globals().resume(1)  # old sequence wakes up after panic
    assert len(lua.globals().played) == 1 and lua.globals().played[1] == 60


def test_connection_false_values_are_not_dropped():
    lua, d = dispatcher()
    lua.execute("""
      local c={name="connection",path="/Program/c",type="SignalConnection",
               parameterDefinitions={{name="Bypass",type="bool",readOnly=false}}}
      function c:getParameter(name) return false end
      function Program:getParameterConnections(name) return {c} end
    """)
    result = request(lua, d, "connections", {"selector": "Program", "parameter": "Gain"})
    assert result["items"][1].parameters[1].value is False


def test_generic_multisample_purge_rejects_silent_noop():
    lua, d = dispatcher()
    lua.execute('Program.type="SampleMappingOscillator"')
    with pytest.raises(LuaError, match="silent no-op"):
        request(lua, d, "purge", {"selector": "Program"})


def test_raw_midi_uses_zero_based_channel_and_native_constructor():
    lua, d = dispatcher()
    lua.execute("""uvi={MidiEvent=function(t,ch,status,b1,b2) return {channel=ch,type=status} end}
                   function postMidiEvent(e) posted=e end""")
    request(
        lua,
        d,
        "midi_extra",
        {"kind": "raw", "timestamp": 0, "channel": 0, "status": 144, "byte1": 60, "byte2": 80},
    )
    assert lua.globals().posted.channel == 0 and lua.globals().posted.type == 144


def test_native_voice_identity_survives_json_boundary_and_release():
    lua, d = dispatcher()
    lua.execute("""
      nativeId=newproxy(true) -- userdata, matching Falcon's real VoiceId
      function playNote(...) return nativeId end
      function changeVolume(id,volume) assert(id==nativeId);lastVolume=volume end
      function setSampleOffset(id,offset) assert(id==nativeId);lastOffset=offset end
      function releaseVoice(id) assert(id==nativeId);released=true end
    """)
    result = request(lua, d, "voice_start", {"note": 60, "duration_ms": 1000, "sample_offset_ms": 120})
    handle = result.voice_id
    assert isinstance(handle, str) and handle.startswith("voice-")
    assert lua.globals().lastOffset == 120
    request(lua, d, "voice_api", {"method": "changeVolume", "arguments": [handle, 0.2]})
    assert lua.globals().lastVolume == 0.2
    request(lua, d, "voice_api", {"method": "releaseVoice", "arguments": [handle]})
    assert lua.globals().released
    with pytest.raises(LuaError, match="Unknown/expired"):
        request(lua, d, "voice_api", {"method": "releaseVoice", "arguments": [handle]})


def test_posted_voice_is_released_by_panic_and_handles_expire():
    lua, d = dispatcher()
    lua.execute("""
      nativeId=newproxy(true)
      function postEvent(e,delta) return nativeId end
      function releaseVoice(id) assert(id==nativeId);released=true end
    """)
    handle = request(lua, d, "post_event", {"event": {"type": 144}, "delta": 0}).voice_id
    request(lua, d, "panic", {})
    assert lua.globals().released
    with pytest.raises(LuaError, match="Unknown/expired"):
        request(lua, d, "voice_api", {"method": "releaseVoice", "arguments": [handle]})
