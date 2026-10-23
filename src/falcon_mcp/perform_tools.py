"""Performance renders on the owned native VST3 session: timestamped notes, sustain, CC, bend and pressure."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import listen
from .audio import read_audio
from .bridge import atomic_json
from .perform import (
    MAX_SECONDS,
    PHRASE_CHECKS,
    PHRASES,
    TimelineError,
    compile_events,
    sounding_windows,
    summarize,
    timeline_end,
    write_timeline,
)
from .perform import phrase as build_phrase
from .perform_listen import analyze

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Time = Annotated[float, Field(ge=0, le=MAX_SECONDS)]
Channel = Annotated[int, Field(ge=1, le=16, description="MIDI channel 1..16 (one-based, like falcon_native_render)")]
Byte = Annotated[int, Field(ge=0, le=127)]
Label = Annotated[str, Field(max_length=64)]
PhraseName = Literal["legato", "retrigger", "sustain_pedal", "pedal_wash", "dense_chords", "polyphony",
                     "cross_channel", "expression"]
DEFAULT_SUITE = ["retrigger", "legato", "sustain_pedal", "dense_chords", "polyphony", "expression"]
SCOPE = ("Offline render of the MCP-owned VST3 instance (pedalboard, non-realtime processing); "
         "not the open standalone/DAW instance and not live keyboard input")
LIMITATIONS = (
    "Offline (non-realtime) render: disk streaming waits and CPU load cannot cause dropouts here, so realtime "
    "glitches need the realtime benchmark or a live capture. MIDI is injected at the plugin input, so the "
    "Multi's Part channel routing applies but external keyboard/host routing is not tested. Checks compare "
    "parts of the same render with each other and use heuristic thresholds; they steer edits and are not a "
    "perceptual verdict. Pitch-based checks need a pitched, mostly monophonic tone.")


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    time: Time
    channel: Channel = 1
    section: Label | None = Field(default=None, description="Optional label carried into the analysis table")


class NoteEvent(_Event):
    type: Literal["note"]
    note: Byte
    velocity: Annotated[int, Field(ge=1, le=127)] = 96
    duration: Annotated[float, Field(gt=0, le=MAX_SECONDS)]
    release_velocity: Byte = 0


class _Ramp(_Event):
    end: Time | None = Field(default=None, description="Ramp end time; requires `to`")


class CCEvent(_Ramp):
    type: Literal["cc"]
    controller: Byte = Field(description="1 mod wheel, 2 breath, 4 foot, 7 volume, 11 expression, 64 sustain, "
                                         "66 sostenuto, 67 soft pedal")
    value: Byte
    to: Byte | None = None


class SustainEvent(_Event):
    type: Literal["sustain"]
    down: bool = True


class BendEvent(_Ramp):
    type: Literal["pitch_bend"]
    value: Annotated[float, Field(ge=-1, le=1, description="-1..1; 0 is centre; range is set by the instrument")]
    to: Annotated[float, Field(ge=-1, le=1)] | None = None


class PressureEvent(_Ramp):
    type: Literal["channel_pressure"]
    value: Byte
    to: Byte | None = None


class PolyPressureEvent(_Ramp):
    type: Literal["poly_pressure"]
    note: Byte
    value: Byte
    to: Byte | None = None


PerformEvent = Annotated[NoteEvent | CCEvent | SustainEvent | BendEvent | PressureEvent | PolyPressureEvent,
                         Field(discriminator="type")]


def _timeline(phrase_name, events, root, velocity, channel, channels, voices, seconds, tail_seconds):
    if (phrase_name is None) == (not events):
        raise ValueError("Pass exactly one of phrase or events")
    if phrase_name:
        built = build_phrase(phrase_name, root, velocity, channel, channels, voices)
        events_out, sections, length = built["events"], built["sections"], built["seconds"]
        meta = {k: built[k] for k in ("name", "description", "checks", "parameters")}
    else:
        events_out = [e.model_dump(exclude_none=True) if hasattr(e, "model_dump") else dict(e) for e in events]
        sections, length, meta = None, timeline_end(events_out) + tail_seconds, {"name": "custom"}
    length = max(length, seconds or 0)
    if length > MAX_SECONDS:
        raise ValueError(f"Timeline needs {length:.1f} s; limit {MAX_SECONDS:.0f} s")
    if timeline_end(events_out) + 0.3 > length:
        raise ValueError("Allow at least 0.3 s after the last event (seconds or tail_seconds)")
    return events_out, sections, round(max(1.0, length), 3), meta


def _trim(check_list):
    return [c for c in check_list if c["status"] != "ok"] or [{"status": "ok", "detail": "all checks passed"}]


def _declare_scopes():
    try:  # Result-scope profiles; absent in trees without result_scope.py.
        from .result_scope import declare
    except ImportError:
        return
    for name in ("falcon_native_perform", "falcon_native_perform_suite"):
        declare(name, "owned_vst3_host", ["offline_render", "dsp_measured", "cc_pedal_expression", "legato_repetition"],
                ["realtime_cpu", "external_midi_input", "open_instance", "perceptual_quality"],
                note="Events are injected at the owned plugin's input; the Multi's Part channels decide what sounds.")
    declare("falcon_perform_analyze", "audio_file_analysis", ["dsp_measured"], ["part_routing", "perceptual_quality"])
    declare("falcon_perform_phrases", "offline_reference")


def register_perform(mcp, config, audit, host):
    _declare_scopes()

    async def render(session_id, events, seconds, sample_rate, buffer_size, reset_controllers, ramp_rate_hz,
                     expected_revision, tag):
        compiled = compile_events(events, reset_controllers, ramp_rate_hz)
        config.audio_root.mkdir(parents=True, exist_ok=True)
        stem = f"perform-{tag}-{uuid.uuid4().hex}"
        wav = config.audio_path(f"{stem}.wav", must_exist=False)
        files = await asyncio.to_thread(write_timeline, compiled, wav.with_suffix(".events.json"),
                                        wav.with_suffix(".mid"))
        result = await host.request(session_id, "render", {
            "path": str(wav), "seconds": seconds, "sample_rate": sample_rate, "notes": [],
            "midi_path": files["midi_path"], "midi_sha256": files["midi_sha256"], "buffer_size": buffer_size,
            "expected_revision": expected_revision})
        if result.get("midi_sha256") != files["midi_sha256"]:
            raise RuntimeError("The native worker did not confirm the event timeline (midi_sha256 missing); it "
                               "predates performance rendering. Restart falcon_native_host after updating.")
        audio, rate = await asyncio.to_thread(read_audio, wav, MAX_SECONDS + 1)
        return compiled, files, result, audio, rate, wav

    async def evaluate(events, sections, seconds, meta, compiled, audio, rate, wav, include_table):
        report = await asyncio.to_thread(analyze, audio, rate, events, seconds, meta["name"] if sections else None,
                                         sections)
        # Pedal-extended note windows: held-pedal phrases are chords to a pitch tracker.
        voices = listen.max_overlap([(w["on"], w["sound_off"]) for w in sounding_windows(events, seconds)])
        heard = await asyncio.to_thread(listen.listen, audio, rate, None, None, None, False, voices)
        full = {"phrase": meta, "events": events, "sections": sections, "summary": summarize(events, compiled),
                **report, "listening": {k: heard.get(k) for k in ("level", "description", "findings")}}
        saved = wav.with_suffix(".perform.json")
        await asyncio.to_thread(atomic_json, saved, full)
        view = {"status": report["status"], "checks": report["checks"], "timeline": report["timeline"],
                "summary": full["summary"], "listening": {"description": heard.get("description"),
                                                         "findings": heard.get("findings")},
                "full_report": str(saved)}
        if include_table:
            view["table"] = report["table"]
        return view

    @mcp.tool(annotations=READ)
    async def falcon_perform_phrases(
        name: PhraseName | None = None, root: Byte = 60, velocity: Annotated[int, Field(ge=1, le=127)] = 96,
        channel: Channel = 1, channels: Annotated[list[Channel], Field(min_length=1, max_length=16)] | None = None,
        voices: Annotated[int, Field(ge=4, le=128)] = 32,
    ) -> dict[str, Any]:
        """List the built-in performance test phrases, or expand one into its editable event list.

        Without name: each phrase with what it checks. With name: the exact events (time/duration in seconds,
        channel 1..16), labelled sections and render length; edit them and pass to falcon_native_perform(events=...).
        """
        if name is None:
            return {"phrases": {n: {"checks": PHRASE_CHECKS[n],
                                    "description": build_phrase(n)["description"]} for n in PHRASES},
                    "event_types": {
                        "note": "time, note, velocity 1..127, duration, channel, release_velocity",
                        "sustain": "time, down (CC64 127/0)",
                        "cc": "time, controller, value, optional ramp to+end",
                        "pitch_bend": "time, value -1..1, optional ramp to+end",
                        "channel_pressure": "time, value, optional ramp to+end",
                        "poly_pressure": "time, note, value, optional ramp to+end"},
                    "ordering": "at equal times: note-off, then controllers, then note-on"}
        return build_phrase(name, root, velocity, channel, channels, voices)

    @mcp.tool(annotations=WRITE)
    async def falcon_native_perform(
        session_id: Session,
        phrase: PhraseName | None = None,
        events: Annotated[list[PerformEvent], Field(max_length=2000)] | None = None,
        root: Byte = 60, velocity: Annotated[int, Field(ge=1, le=127)] = 96, channel: Channel = 1,
        channels: Annotated[list[Channel], Field(min_length=1, max_length=16)] | None = None,
        voices: Annotated[int, Field(ge=4, le=128)] = 32,
        seconds: Annotated[float, Field(ge=1, le=MAX_SECONDS)] | None = None,
        tail_seconds: Annotated[float, Field(ge=0.3, le=10)] = 1.5,
        sample_rate: Literal[44100, 48000, 96000] = 48000,
        buffer_size: Annotated[int, Field(ge=32, le=4096)] = 512,
        reset_controllers: bool = True,
        ramp_rate_hz: Annotated[float, Field(ge=10, le=1000)] = 100,
        expected_revision: Revision | None = None,
        include_table: bool = False,
    ) -> dict[str, Any]:
        """Render a timestamped performance (notes, sustain pedal, CC, pitch bend, pressure, several channels)
        from the OWNED VST3 session and report performance checks as text.

        Pass a built-in phrase (see falcon_perform_phrases) or your own events. Phrases: legato, retrigger,
        sustain_pedal, pedal_wash, dense_chords, polyphony, cross_channel, expression. Checks cover re-strike
        clicks and missing re-attacks, tail cuts, legato joins, pedal sustain/release, held keys at pedal-up,
        chord level growth and missing chord notes, voice stealing, channel routing, CC1/CC11/pressure response
        and the measured pitch-bend range. reset_controllers clears pedal/bend (and used CCs) at t=0.
        Also writes a .mid of the exact timeline for replay in the standalone or a DAW. Offline render: not a
        realtime-performance test. Results steer edits; they are not a perceptual verdict.
        """
        try:
            timeline, sections, length, meta = _timeline(phrase, events, root, velocity, channel, channels,
                                                          voices, seconds, tail_seconds)
            audit.event("native_perform", session_id=session_id, phrase=meta["name"], seconds=length)
            compiled, files, result, audio, rate, wav = await render(
                session_id, timeline, length, sample_rate, buffer_size, reset_controllers, ramp_rate_hz,
                expected_revision, meta["name"])
        except TimelineError as exc:
            raise ValueError(str(exc)) from exc
        view = await evaluate(timeline, sections, length, meta, compiled, audio, rate, wav, include_table)
        return {"scope": SCOPE, "phrase": meta, "path": str(wav), "mid_path": files["mid_path"],
                "revision": result.get("revision"), "revision_number": result.get("revision_number"),
                "render": {"seconds": length, "sample_rate": sample_rate, "buffer_size": result.get("buffer_size"),
                           "midi_events": result.get("midi_events"), "peak": result.get("peak")},
                **view, "limitations": LIMITATIONS,
                "next": "falcon_listen(path) for one note in detail; change one parameter and re-run the same "
                        "phrase to compare checks"}

    @mcp.tool(annotations=WRITE)
    async def falcon_native_perform_suite(
        session_id: Session,
        phrases: Annotated[list[PhraseName], Field(min_length=1, max_length=8)] | None = None,
        root: Byte = 60, velocity: Annotated[int, Field(ge=1, le=127)] = 96, channel: Channel = 1,
        channels: Annotated[list[Channel], Field(min_length=1, max_length=16)] | None = None,
        voices: Annotated[int, Field(ge=4, le=128)] = 32,
        sample_rate: Literal[44100, 48000, 96000] = 48000,
        buffer_size: Annotated[int, Field(ge=32, le=4096)] = 512,
        expected_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Run several built-in performance phrases on the OWNED VST3 session and return one scorecard.

        Default: retrigger, legato, sustain_pedal, dense_chords, polyphony, expression (about 70 s of audio,
        one render each). The state revision is pinned after the first render, so a concurrent edit aborts the
        suite. Only non-ok checks are listed per phrase; each phrase's full report path is included.
        """
        names = phrases or DEFAULT_SUITE
        if len(set(names)) != len(names):
            raise ValueError("phrases must not repeat")
        revision, rows = expected_revision, []
        for name in names:
            timeline, sections, length, meta = _timeline(name, None, root, velocity, channel, channels, voices,
                                                         None, 1.5)
            compiled, files, result, audio, rate, wav = await render(
                session_id, timeline, length, sample_rate, buffer_size, True, 100, revision, name)
            revision = revision or result.get("revision")
            view = await evaluate(timeline, sections, length, meta, compiled, audio, rate, wav, False)
            rows.append({"phrase": name, "status": view["status"], "issues": _trim(view["checks"]),
                         "path": str(wav), "mid_path": files["mid_path"], "full_report": view["full_report"]})
        order = {"fail": 0, "warn": 1, "info": 2, "unknown": 2, "ok": 3}
        overall = min((r["status"] for r in rows), key=lambda s: order.get(s, 3))
        return {"scope": SCOPE, "revision": revision, "status": overall,
                "scorecard": [f"{r['phrase']:<14} {r['status']}" for r in rows], "phrases": rows,
                "limitations": LIMITATIONS}

    @mcp.tool(annotations=READ)
    async def falcon_perform_analyze(
        path: str,
        phrase: PhraseName | None = None,
        events: Annotated[list[PerformEvent], Field(max_length=2000)] | None = None,
        root: Byte = 60, velocity: Annotated[int, Field(ge=1, le=127)] = 96, channel: Channel = 1,
        channels: Annotated[list[Channel], Field(min_length=1, max_length=16)] | None = None,
        voices: Annotated[int, Field(ge=4, le=128)] = 32,
        offset_seconds: Annotated[float, Field(ge=-10, le=30)] | None = None,
        include_table: bool = False,
    ) -> dict[str, Any]:
        """Analyze an existing recording of a performance timeline (for example a capture of the standalone
        playing the .mid that falcon_native_perform wrote) with the same checks.

        offset_seconds is where timeline t=0 sits in the file; omitted, the first audible onset is aligned to
        the first note-on. Live captures include driver latency and other system audio; say so when reporting.
        """
        try:
            timeline, sections, length, meta = _timeline(phrase, events, root, velocity, channel, channels,
                                                          voices, None, 1.5)
        except TimelineError as exc:
            raise ValueError(str(exc)) from exc
        target = config.audio_path(path)
        audio, rate = await asyncio.to_thread(read_audio, target, MAX_SECONDS + 10)
        aligned = await asyncio.to_thread(_align, audio, rate, timeline, offset_seconds)
        audio, applied = aligned
        compiled = compile_events(timeline)
        report = await asyncio.to_thread(analyze, audio, rate, timeline, length,
                                         meta["name"] if sections else None, sections)
        out = {"scope": "Analysis of an existing file; the source and routing of the recording are not verified",
               "path": str(target), "phrase": meta, "offset_seconds": round(applied, 4),
               "status": report["status"], "checks": report["checks"], "timeline": report["timeline"],
               "summary": summarize(timeline, compiled), "limitations": LIMITATIONS}
        if include_table:
            out["table"] = report["table"]
        return out



def _align(audio, rate, timeline, offset_seconds):
    """Shift audio so timeline t=0 is at sample 0; pad or trim to keep times aligned."""
    import numpy as np

    from .perform import note_windows

    if offset_seconds is None:
        first = note_windows(timeline)[0]["on"]
        times, env = listen.fine_envelope(audio, rate, window_ms=5.0, hop_ms=1.0)
        audible = np.where(env > env.max() - 40)[0]
        onset = float(times[audible[0]]) if len(audible) else first
        offset_seconds = onset - first
    shift = round(offset_seconds * rate)
    if shift > 0:
        audio = audio[shift:]
    elif shift < 0:
        audio = np.concatenate([np.zeros((-shift, audio.shape[1]), dtype=audio.dtype), audio])
    return audio, offset_seconds
