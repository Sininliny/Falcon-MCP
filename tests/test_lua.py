"""Run the REAL Lua dispatcher against a small engine double with Lua 5.1.

These test validation, selector parsing and async scheduling; hardware integration is separate.
"""

import uuid
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
    engine = d.process(table(lua, {"protocol": 1, "id": uuid.uuid4().hex, "op": "ping"}))
    return d.process(
        table(
            lua, {"protocol": 1, "id": uuid.uuid4().hex, "op": op, "args": args, "session": engine.instance}
        )
    )


def test_reload_rejects_stale_mutation_before_and_after_handshake():
    lua, d = dispatcher()
    stale = table(
        lua,
        {
            "protocol": 1,
            "id": "stale",
            "op": "set_parameters",
            "session": "previous",
            "args": {"selector": "Program", "changes": [{"name": "Gain", "value": 0.2}]},
        },
    )
    with pytest.raises(LuaError, match="session changed"):
        d.process(stale)
    assert (
        request(lua, d, "get_parameters", {"selector": "Program", "names": ["Gain"]})["items"][1].value == 1
    )
    with pytest.raises(LuaError, match="session changed"):
        d.process(stale)


@pytest.mark.parametrize("op", ["ping", "set_parameters"])
def test_first_mailbox_poll_accepts_ping_and_never_replays_stale_write(op):
    lua, d = dispatcher()
    lua.globals().BRIDGE_ROOT = "/test"
    lua.globals().incoming = table(
        lua,
        {
            "protocol": 1,
            "id": "first-request",
            "op": op,
            "session": "old-instance",
            "args": {"selector": "Program", "changes": [{"name": "Gain", "value": 0.2}]},
        },
    )
    lua.execute("""
      require("uvi.json").decode=function(text) return incoming end
      function loadTextData(path) return {finished=true,data="request"} end
      function saveData(value,path,cb) written=value;cb({success=true}) end
      function wait(ms) error("STOP AFTER FIRST POLL") end
    """)
    with pytest.raises(LuaError, match="STOP AFTER FIRST POLL"):
        lua.globals().onInit()
    assert lua.globals().written is not None
    assert lua.globals().written.ok is (op == "ping")
    assert (
        request(lua, d, "get_parameters", {"selector": "Program", "names": ["Gain"]})["items"][1].value == 1
    )


def test_script_instance_is_a_stable_host_nonce_not_a_reusable_lua_address():
    lua, d = dispatcher()
    first = d.process(table(lua, {"protocol": 1, "id": "unique-host-nonce", "op": "ping"}))
    second = request(lua, d, "ping", {})
    assert first.instance == "unique-host-nonce" and second.instance == first.instance


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


def test_expected_float_accepts_uvi_json_rounding_but_rejects_stale_values():
    lua, d = dispatcher()
    lua.execute('Program:setParameter("Gain",0.100000001490116)')
    result = request(
        lua,
        d,
        "set_parameters",
        {"selector": "Program", "changes": [{"name": "Gain", "expected": 0.10000000149012, "value": 0.2}]},
    )
    assert result.verified
    with pytest.raises(LuaError, match="Stale expected"):
        request(
            lua,
            d,
            "set_parameters",
            {
                "selector": "Program",
                "changes": [{"name": "Gain", "expected": 0.10000000149012, "value": 0.3}],
            },
        )


def test_expected_float_tolerance_does_not_mask_near_zero_edits():
    lua, d = dispatcher()
    lua.execute('Program:setParameter("Gain",1e-20)')
    with pytest.raises(LuaError, match="Stale expected"):
        request(
            lua,
            d,
            "set_parameters",
            {"selector": "Program", "changes": [{"name": "Gain", "expected": 2e-20, "value": 0.3}]},
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
      function c:hasParameter(name) return name=="Bypass" end
      function Program:getParameterConnections(name) return {c} end
    """)
    result = request(lua, d, "connections", {"selector": "Program", "parameter": "Gain"})
    assert result["items"][1].parameters[1].value is False


def modulation_dispatcher():
    lua, d = dispatcher()
    lua.execute('''
      local defs={{name="Source",type="string"},{name="Destination",type="string"},
                  {name="Ratio",type="float",min=1,max=0},{name="Bypass",type="bool"}}
      function makeConnection(name,source,ratio)
        local values={Source=source,Destination="Gain",Ratio=ratio,Bypass=false}
        local c={name=name,path="/Program/"..name,type="SignalConnection",parameterDefinitions=defs}
        function c:getParameter(key) return values[key] end
        function c:hasParameter(key) return values[key]~=nil end
        function c:setParameter(key,value) values[key]=value end
        return c
      end
      wheel=makeConnection("Pitch Wheel","@PitchBend",2)
      drift=makeConnection("Organic Drift","Organic Drift",0.01)
      paths={wheel,drift}
      function Program:getParameterConnections(name) if name=="Gain" then return paths else return {} end end
    ''')
    return lua, d


def test_modulation_requires_exact_preview_identity_expected_and_expiring_structure_guard():
    lua, d = modulation_dispatcher()
    read = request(lua, d, "connections", {"selector": "Program", "parameter": "Gain"})
    guard = read["items"][2].guard
    args = {"selector": "Program", "parameter": "Gain", "index": 2, "guard": guard,
            "allow_protected": True, "changes": [{"name": "Ratio", "expected": 0.01, "value": 0.02}]}
    with pytest.raises(LuaError, match="Preview"):
        request(lua, d, "set_connection", {**args, "dry_run": False})
    preview = request(lua, d, "set_connection", args)
    assert preview.source == "Organic Drift" and lua.globals().drift.getParameter(None, "Ratio") == 0.01
    lua.execute("paths={drift,wheel}")
    with pytest.raises(LuaError, match="identity/structure changed"):
        request(lua, d, "set_connection", {**args, "dry_run": False})
    lua.execute("paths={wheel,drift}")
    result = request(lua, d, "set_connection", {**args, "dry_run": False})
    assert result.verified and lua.globals().wheel.getParameter(None, "Ratio") == 2
    with pytest.raises(LuaError, match="missing/expired"):
        request(lua, d, "set_connection", {**args, "dry_run": False})


def test_modulation_protects_wheel_and_gain_and_invalidates_other_connection_changes():
    lua, d = modulation_dispatcher()
    read = request(lua, d, "connections", {"selector": "Program", "parameter": "Gain"})
    args = {"selector": "Program", "parameter": "Gain", "index": 2, "guard": read["items"][2].guard,
            "changes": [{"name": "Ratio", "expected": 0.01, "value": 0.02}]}
    with pytest.raises(LuaError, match="allow_protected"):
        request(lua, d, "set_connection", args)
    request(lua, d, "set_connection", {**args, "allow_protected": True})
    lua.execute('wheel:setParameter("Ratio",12)')
    with pytest.raises(LuaError, match="structure changed"):
        request(lua, d, "set_connection", {**args, "allow_protected": True, "dry_run": False})


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


def test_failed_sample_offset_releases_voice_created_before_error():
    lua, d = dispatcher()
    lua.execute("""
      nativeId=newproxy(true);releases=0
      function playNote(...) return nativeId end
      function setSampleOffset(...) error("offset failure") end
      function releaseVoice(id) assert(id==nativeId);releases=releases+1 end
    """)
    with pytest.raises(LuaError, match="offset failure"):
        request(lua, d, "voice_start", {"note": 60, "duration_ms": 1000, "sample_offset_ms": 120})
    assert lua.globals().releases == 1
    request(lua, d, "panic", {})
    assert lua.globals().releases == 1


def test_noteoff_and_controller_still_work_at_voice_capacity():
    lua, d = dispatcher()
    lua.execute("""
      function playNote(...) return newproxy(true) end
      function postEvent(e,delta) lastEvent=e;return e.id end
    """)
    for _ in range(256):
        handle = request(lua, d, "voice_start", {"note": 60, "duration_ms": 1000}).voice_id
    result = request(lua, d, "post_event", {"event": {"type": 128, "id": handle}, "delta": 0})
    assert result.posted
    assert request(lua, d, "post_event", {"event": {"type": 176}, "delta": 0}).posted


def test_sequence_nil_voice_does_not_leave_playing_stuck():
    lua, d = dispatcher()
    lua.execute("""
      scheduled={}
      function spawn(fn) scheduled[#scheduled+1]=coroutine.create(fn) end
      function playNote(...) return nil end
      function releaseVoice(id) end
    """)
    request(lua, d, "play", {"notes": [{"note": 60, "velocity": 1, "duration_ms": 100}]})
    ok, error = lua.execute("local ok,err=coroutine.resume(scheduled[1]);return ok,err")
    assert ok, error
    result = request(lua, d, "ping", {})
    assert not result.sequence.playing and "no voice" in result.sequence.error


def test_setter_readback_failure_keeps_applied_outcome():
    lua, d = dispatcher()
    lua.execute("""
      local get=Program.getParameter; local set=Program.setParameter;local changed=false
      function Program:getParameter(name)
        if changed then error("readback unavailable") end
        return get(self,name)
      end
      function Program:setParameter(name,value) set(self,name,value);changed=true end
    """)
    result = request(
        lua, d, "set_parameters", {"selector": "Program", "changes": [{"name": "Gain", "value": 0.2}]}
    )
    assert not result.verified and result.partial
    assert result["items"][1].applied and "readback unavailable" in result["items"][1].error


def create_midi_double(lua, d):
    lua.execute("""
      midi={numTracks=1,division=960,ppq=960,format=1}
      function midi:getNumEventsForTrack(track) nativeTrackCalled=true;return 0 end
      function createMidiFile(tracks,events,cb) cb({success=true,midi=midi});return {} end
    """)
    request(lua, d, "midi_file", {"action": "create", "max_tracks": 1, "max_events": 16})
    return d.pending().result.handle


def test_midi_handles_do_not_alias_across_script_instances():
    lua1, d1 = dispatcher()
    handle1 = create_midi_double(lua1, d1)
    lua2, d2 = dispatcher()
    handle2 = create_midi_double(lua2, d2)
    assert handle1 != handle2
    with pytest.raises(LuaError, match="expired"):
        request(lua2, d2, "midi_file", {"action": "events", "handle": handle1, "track": 1})


def test_midi_track_is_checked_before_native_access():
    lua, d = dispatcher()
    handle = create_midi_double(lua, d)
    with pytest.raises(LuaError, match="track index"):
        request(lua, d, "midi_file", {"action": "events", "handle": handle, "track": 2})
    assert not lua.globals().nativeTrackCalled


def test_readonly_midi_query_needs_no_mutation_session():
    lua, d = dispatcher()
    handle = create_midi_double(lua, d)
    result = d.process(
        table(
            lua,
            {
                "protocol": 1,
                "id": "read-only",
                "op": "midi_file",
                "args": {"action": "events", "handle": handle, "track": 1},
            },
        )
    )
    assert result.total_count == 0
