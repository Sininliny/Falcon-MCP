"""Run the real voice-trace kit (Lua 5.1) inside a small engine double, then analyze its log."""

import json

import pytest
from lupa.lua51 import LuaRuntime

from falcon_mcp import voice_trace

ENGINE = r"""
-- Like Falcon 27: no coroutine global for scripts, spawn defers the task, and playNote/releaseVoice
-- are Lua that call the global postEvent.
local co = coroutine
clock = 0
tasks = {}
saved = {}
calls = {}
function getTime() return clock end
function getSamplingRate() return 48000 end
local function newId() return newproxy(false) end
function postEvent(e, delta) calls[#calls+1] = "post" .. tostring(e.type); if e.type == 144 then return e.id or newId() end end
function playNote(note, vel, dur, layer) calls[#calls+1] = "play"; return postEvent({type=144, note=note, velocity=vel}) end
function releaseVoice(id) calls[#calls+1] = "release"; postEvent({type=128, id=id}); return true end
function changeTune(id, shift, relative) calls[#calls+1] = "tune" end
function fade(id, a, b, c) end
function fadeout(id, duration, kill, reset) calls[#calls+1] = "fadeout" end
function wait(ms) co.yield(clock + ms) end
local function engineSpawn(fn, ...)
    local args = {...}
    tasks[#tasks+1] = {co=co.create(function() fn(unpack(args)) end), wake=clock}
end
spawn = engineSpawn
function saveData(data, path, cb) saved[path] = data; if cb then cb(newproxy(false)) end end
function advance(ms)
    local target = clock + ms
    while true do
        local best = nil
        for i, task in ipairs(tasks) do if task.wake <= target and (not best or task.wake < tasks[best].wake) then best = i end end
        if not best then break end
        local task = table.remove(tasks, best)
        clock = math.max(clock, task.wake)
        local ok, wake = co.resume(task.co)
        assert(ok, wake)
        if co.status(task.co) ~= "dead" then tasks[#tasks+1] = {co=task.co, wake=wake} end
    end
    clock = target
end
function deliver(name, e) engineSpawn(function() (rawget(_G, "onEvent") or _G[name])(e) end) end
local ids = {}
function key(on, note, ch)
    ch = ch or 1
    if on then ids[note] = newId(); deliver("onNote", {type=144, note=note, velocity=90, channel=ch, id=ids[note]})
    else deliver("onRelease", {type=128, note=note, velocity=0, channel=ch, id=ids[note]}) end
end
coroutine = nil
"""

# A mono legato instrument with two classic bugs: the auxiliary wavetable voice is started with
# playNote(-1) on the FIRST key only, and after the last key is lifted a 40 ms pending window keeps
# the main voice for a fast return.
INSTRUMENT = r"""
local main, mainNote, aux = nil, nil, nil
local heldCount, pendingGen = 0, 0
function onNote(e)
    heldCount = heldCount + 1
    pendingGen = pendingGen + 1
    if main and mainNote then
        FVT.legato(true, heldCount > 1 and "overlap" or "pending window", {from=mainNote, to=e.note})
        FVT.envelope(main, false, "glide")
        changeTune(main, e.note - mainNote, true)
        if heldCount == 1 then FVT.pending("commit", {id="handoff"}) end
    else
        FVT.legato(false, "first note")
        main = playNote(e.note, e.velocity, 0, 1)
        aux = playNote(e.note, e.velocity, -1, 3)
    end
    mainNote = e.note
end
function onRelease(e)
    heldCount = heldCount - 1
    if heldCount == 0 and main then
        FVT.pending("open", {id="handoff", note=e.note})
        local gen = pendingGen
        spawn(function()
            wait(40)
            if gen == pendingGen and main then
                FVT.pending("expire", {id="handoff"})
                releaseVoice(main); main = nil; mainNote = nil
            end
        end)
    end
end
"""


def runtime(kit: str, instrument: str = INSTRUMENT):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(ENGINE)
    lua.execute(kit + "\n" + instrument + "\nFVT.install()")
    lua.execute("onInit()")
    return lua


def to_python(value):
    if hasattr(value, "items"):
        items = dict(value.items())
        if items and all(isinstance(k, int) for k in items):
            return [to_python(items[k]) for k in sorted(items)]
        return {k: to_python(v) for k, v in items.items()}
    return value


def trace_of(lua, path="C:/t/trace.json"):
    lua.execute("advance(250)")
    data = lua.eval("saved")[path]
    return to_python(data)


def play(lua, steps):
    for at, on, note in steps:
        lua.execute(f"advance({at})")
        lua.globals().key(on, note)


def test_kit_template_fills_config():
    source = voice_trace.kit_source("C:/t/trace.json", "dev", {3: "aux_wavetable"}, 100, 50, True)
    assert "__FVT_CONFIG__" not in source
    assert 'path="C:/t/trace.json"' in source and '["3"]="aux_wavetable"' in source
    assert "capacity=100" in source and "echo=true" in source


def test_kit_passes_calls_through_and_flushes_json():
    lua = runtime(voice_trace.kit_source("C:/t/trace.json", "dev", {3: "aux_wavetable"}))
    play(lua, [(0, True, 60), (100, False, 60)])
    data = trace_of(lua)
    assert data["format"] == voice_trace.FORMAT and data["dropped"] == 0
    kinds = [e["k"] for e in data["events"]]
    assert kinds[:4] == ["init", "key_on", "legato", "voice_start"]
    assert "voice_release" in kinds and "pending" in kinds
    calls = to_python(lua.eval("calls"))
    assert calls.count("play") == 2 and "release" in calls  # the instrument's own calls still happen
    starts = [e for e in data["events"] if e["k"] == "voice_start"]
    assert len(starts) == 2  # playNote's own internal postEvent is not logged as a second voice
    assert data.get("save_error") is None  # saveData's task is userdata in Falcon
    assert starts[1]["role"] == "aux_wavetable" and starts[1]["dur"] == -1 and starts[1]["cb"] == "onNote"
    # The expiry inside spawn() keeps the key context of the release that opened it.
    release = next(e for e in data["events"] if e["k"] == "voice_release")
    assert release["cb"] == "spawn" and release["from_cb"] == "onRelease" and release["dt"] == pytest.approx(40)


def test_legato_handoff_cut_aux_and_pending_reversal_are_explained():
    lua = runtime(voice_trace.kit_source("C:/t/trace.json", "dev", {3: "aux_wavetable"}))
    # A on, B on (overlap legato), A off (aux owned by A dies), B off (pending opens), A again 20 ms later.
    play(lua, [(0, True, 60), (100, True, 62), (50, False, 60), (100, False, 62), (20, True, 60), (300, False, 60)])
    report = voice_trace.analyze(voice_trace.parse_trace(json.dumps(trace_of(lua))))
    notes = report["notes"]
    assert [n["note"] for n in notes] == [60, 62, 60]
    first, second, third = notes
    assert first["legato"] == {"triggered": False, "reason": "first note", "source": "instrument_reported",
                               "from": None, "to": None}
    assert first["envelope"]["restarted"] is True and first["envelope"]["source"] == "inferred"
    assert second["legato"]["triggered"] is True and second["legato"]["reason"] == "overlap"
    assert second["envelope"]["restarted"] is False and second["envelope"]["source"] == "instrument_reported"
    assert second["input"]["keys_held_before"] == [60]
    aux = next(v for v in report["voices"] if v["role"] == "aux_wavetable")
    assert aux["owner"]["note"] == 60 and aux["mode"] == "owner_key" and aux["release_ms"] == pytest.approx(150)
    codes = {f["code"]: f for f in report["findings"]}
    cut = codes["voice_cut_by_original_key"]["items"][0]
    assert cut["continuing_key"] == 62 and cut["cut_after_legato_ms"] == pytest.approx(50)
    # Fast return to 60 inside the 40 ms pending window: legato without a held key.
    assert third["input"]["fast_reversal"]["path"] == [60, 62, 60]
    assert third["input"]["fast_reversal"]["gap_since_release_ms"] == pytest.approx(120)  # A lifted at 150, back at 270
    assert third["pending_windows"][0]["age_ms"] == pytest.approx(20)
    assert third["pending_windows"][0]["outcome"] == "commit"
    assert third["legato"]["reason"] == "pending window"
    assert "legato_without_held_key" in codes and "keys_in_pending_window" in codes
    assert codes["fast_reversals"]["items"][0]["legato"] is True
    assert "fast_reversals" not in {f["code"] for f in voice_trace.analyze(
        voice_trace.parse_trace(json.dumps(trace_of(lua))), reversal_ms=100)["findings"]}
    assert "pending window 'handoff'" in third["explanation"]
    # The main voice is finally released by the pending expiry after the last key.
    main = next(v for v in report["voices"] if v["role"] == "unlabelled")
    assert main["mode"] == "manual" and main["release_cause"] == "releaseVoice in spawn"
    assert "stuck_voices" not in codes


def test_reversal_window_and_stuck_voice_from_spawn():
    instrument = r"""
    function onNote(e)
        spawn(function() wait(5); playNote(e.note, e.velocity, -1, 2) end)
    end
    """
    lua = runtime(voice_trace.kit_source("C:/t/x.json", "dev"), instrument)
    play(lua, [(0, True, 60), (30, True, 64), (20, False, 60), (30, True, 60)])
    report = voice_trace.analyze(voice_trace.parse_trace(json.dumps(trace_of(lua, "C:/t/x.json"))),
                                 reversal_ms=200)
    codes = {f["code"]: f for f in report["findings"]}
    assert codes["never_auto_release_calls"]["count"] == 3
    assert codes["stuck_voices"]["count"] == 3
    assert report["voices"][0]["owner"]["callback"] == "spawn"
    assert report["voices"][0]["delay_after_callback_ms"] == pytest.approx(5)
    assert codes["fast_reversals"]["items"][0]["path"] == [60, 64, 60]


def test_forwarded_notes_follow_their_key_and_console_lines_parse():
    lua = runtime(voice_trace.kit_source(None, "fwd", echo=False), "")  # no onNote: default forwarding
    play(lua, [(0, True, 48), (100, False, 48)])
    lua.execute("advance(10)")
    rows = to_python(lua.eval("FVT._snapshot()"))["events"]
    text = "\n".join("[script] FVT " + json.dumps(r) for r in rows) + "\nunrelated line"
    report = voice_trace.analyze(voice_trace.parse_trace(text))
    voice = report["voices"][0]
    assert voice["mode"] == "forwarded_key" and voice["release_ms"] == pytest.approx(100)
    assert voice["release_cause"] == "forward_off in onRelease"
    assert report["summary"]["voices"] == 1 and not report["findings"]


def test_on_event_scripts_are_traced_too():
    instrument = r"""
    function onEvent(e)
        if e.type == 144 then playNote(e.note, e.velocity, 300) end
    end
    """
    lua = runtime(voice_trace.kit_source(None, "ev"), instrument)
    play(lua, [(0, True, 50), (100, False, 50)])
    lua.execute("advance(1)")
    rows = to_python(lua.eval("FVT._snapshot()"))["events"]
    report = voice_trace.analyze({"events": rows})
    assert report["notes"][0]["off_ms"] == pytest.approx(100)
    assert report["voices"][0]["mode"] == "timed" and report["voices"][0]["release_ms"] == pytest.approx(300)


def test_ring_overflow_is_reported():
    lua = runtime(voice_trace.kit_source(None, "small", capacity=5), "")
    for i in range(6):
        lua.globals().key(True, 40 + i)
        lua.execute("advance(10)")
    data = to_python(lua.eval("FVT._snapshot()"))
    assert len(data["events"]) == 5 and data["dropped"] > 0
    report = voice_trace.analyze(data)
    assert report["findings"][0]["code"] == "trace_overflow"


def test_release_mode_matches_documented_rules():
    assert voice_trace.release_mode(-1, "onNote", "playNote") == "owner_key"
    assert voice_trace.release_mode(None, "onRelease", "playNote") == "never_auto"
    assert voice_trace.release_mode(-1, "spawn", "playNote") == "never_auto"
    assert voice_trace.release_mode(-1, "onInit", "playNote") == "immediate"
    assert voice_trace.release_mode(0, "onNote", "playNote") == "manual"
    assert voice_trace.release_mode(250, "spawn", "playNote") == "timed"
    assert voice_trace.release_mode(0, "onNote", "postEvent") == "manual"


def test_midi_alignment_and_segments():
    events = [{"k": "init", "t": 1000, "s": 1},
              {"k": "key_on", "t": 1500, "s": 2, "kid": 1, "note": 60, "ch": 1},
              {"k": "transport", "t": 2000, "s": 3, "playing": True},
              {"k": "key_on", "t": 2100, "s": 4, "kid": 2, "note": 62, "ch": 1},
              {"k": "key_on", "t": 2600, "s": 5, "kid": 3, "note": 64, "ch": 1}]
    midi = [{"type": "note", "time": 0.1, "note": 62, "channel": 1, "duration": .2},
            {"type": "note", "time": 0.6, "note": 64, "channel": 1, "duration": .2},
            {"type": "note", "time": 0.9, "note": 67, "channel": 1, "duration": .2}]
    report = voice_trace.analyze({"events": events}, midi, segment="last_transport")
    assert [n["note"] for n in report["notes"]] == [62, 64]
    alignment = report["midi_alignment"]
    assert alignment["matched"] == 2 and alignment["offset_ms"] == pytest.approx(0)
    assert alignment["midi_notes_not_seen_by_script"][0]["note"] == 67
    with pytest.raises(ValueError):
        voice_trace.analyze({"events": events}, segment="mark:nope")
    compact = voice_trace.compact(report, 1)
    assert compact["notes_truncated"] and len(compact["notes"]) == 1


def test_parse_rejects_text_without_events():
    with pytest.raises(ValueError):
        voice_trace.parse_trace("hello")


def test_documented_auto_release_wins_over_engine_release_at_same_time():
    # Falcon releases a playNote(-1) voice with its own releaseVoice in an engine task that the kit
    # logs with a stale key context; the owner-key rule must still name the cause.
    events = [{"k": "key_on", "t": 0, "s": 1, "kid": 1, "note": 60, "ch": 1},
              {"k": "voice_start", "t": 0, "s": 2, "kid": 1, "cb": "onNote", "v": 1, "dur": -1, "via": "playNote"},
              {"k": "voice_release", "t": 50, "s": 3, "kid": 7, "cb": "spawn", "v": 1, "via": "releaseVoice"},
              {"k": "key_off", "t": 50, "s": 4, "kid": 1, "note": 60}]
    voice = voice_trace.analyze({"events": events})["voices"][0]
    assert voice["release_cause"] == "owner key 60 lifted (playNote -1)"


def test_last_render_segment_starts_where_the_engine_clock_restarts():
    events = [{"k": "init", "t": 0, "s": 1},
              {"k": "key_on", "t": 46.4, "s": 2, "kid": 1, "note": 60, "ch": 1},  # host load-path note
              {"k": "key_off", "t": 92.9, "s": 3, "kid": 1, "note": 60},
              {"k": "cc", "t": 0, "s": 4, "cc": 64, "value": 0},
              {"k": "key_on", "t": 200, "s": 5, "kid": 2, "note": 62, "ch": 1}]
    report = voice_trace.analyze({"events": events}, segment="last_render")
    assert [n["note"] for n in report["notes"]] == [62]
    assert len(voice_trace.analyze({"events": events}, segment="last_init")["notes"]) == 2
