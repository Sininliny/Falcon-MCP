"""Reproducible A/B/C auditions: bound inputs, loudness-matched files, recorded choices, traced migration.

An audition session compares two to six candidate renders of the same musical input. Each candidate is
bound to its event timeline (hash of the .events.json/.mid it was rendered from), declared controller
values, file versions (sha256 of the presets that produced it) and sample rate. Candidates that do not
share a binding are reported as not comparable instead of silently compared.

Two candidate kinds are kept apart on purpose:
- ``integrated_preset``: rendered from a saved preset, i.e. what a user loading that file hears.
- ``offline_mix``: audio files summed outside Falcon (stems, layers with gains). It shows a direction;
  it is not a preset until the direction has been migrated and the integrated render matches it.

The user's choice becomes the next round's target (binding + loudness + chosen audio + feature ranges).
Metrics never stand in for the choice: a choice records who decided (user or agent) and why.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import string
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import parity

FORMAT = "falcon-mcp-audition-1"
TARGET_FORMAT = "falcon-mcp-audition-target-1"
KINDS = ("integrated_preset", "offline_mix")
ID_PATTERN = r"^aud-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$"
MAX_SECONDS = 61
DURATION_TOLERANCE_SECONDS = 0.25
PEAK_CEILING_DBFS = -1.0
GAP_SECONDS = 0.75
KIND_NOTES = {
    "integrated_preset": "Rendered from a saved preset: what loading that file sounds like.",
    "offline_mix": ("Files summed outside Falcon: a direction to try, not a preset. It must be migrated into a "
                    "preset and verified with falcon_audition_migration before it counts as delivered."),
}


class AuditionError(ValueError):
    pass


def plain(value):
    """JSON-safe copy: numpy scalars/arrays become Python values."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    return value


def new_id() -> str:
    return time.strftime("aud-%Y%m%d-%H%M%S-", time.localtime()) + secrets.token_hex(3)


def check_id(audition_id: str) -> str:
    if not re.match(ID_PATTERN, audition_id or ""):
        raise AuditionError(f"Unknown audition id {audition_id!r}")
    return audition_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sidecar_timeline(audio_path: Path) -> Path | None:
    for suffix in (".events.json", ".mid"):
        candidate = audio_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def controllers_in_timeline(path: Path) -> dict | None:
    """Readable controller summary of a compiled falcon-mcp-events-1 file (initial values and moving CCs)."""
    if path.suffix.lower() != ".json":
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != "falcon-mcp-events-1":
        return None
    initial, changes = {}, {}
    for time_s, status, d1, d2 in payload.get("events", []):
        kind, channel = status & 0xF0, (status & 15) + 1
        if kind == 0xB0:
            key, value = f"ch{channel}:cc{d1}", d2
        elif kind == 0xE0:
            key, value = f"ch{channel}:pitch_bend", d1 + (d2 << 7)
        elif kind == 0xD0:
            key, value = f"ch{channel}:channel_pressure", d1
        else:
            continue
        if time_s <= 0.001:
            initial[key] = value
        else:
            changes[key] = changes.get(key, 0) + 1
    return {"initial": dict(sorted(initial.items())), "moving": dict(sorted(changes.items()))}


def timeline_binding(audio_path: Path, timeline_path: Path | None) -> dict | None:
    """The event timeline a render came from: explicit path, else the render's .events.json/.mid sidecar."""
    source = "explicit"
    if timeline_path is None:
        timeline_path, source = _sidecar_timeline(audio_path), "sidecar"
    if timeline_path is None:
        return None
    if not timeline_path.is_file():
        raise AuditionError(f"Timeline file not found: {timeline_path}")
    return {"path": str(timeline_path), "sha256": sha256_file(timeline_path), "source": source,
            "controllers": controllers_in_timeline(timeline_path)}


def file_versions(paths: list[str]) -> list[dict]:
    out = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise AuditionError(f"File for version binding not found: {path}")
        stat = path.stat()
        out.append({"path": str(path.resolve()), "sha256": sha256_file(path), "bytes": stat.st_size,
                    "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime))})
    return out


def _normalize_controllers(controllers: dict | None) -> dict:
    return {str(k): float(v) for k, v in sorted((controllers or {}).items())}


def mix_layers(layers: list[tuple[np.ndarray, int, float]]) -> tuple[np.ndarray, int]:
    """Sum (audio, rate, gain_db) layers, padded to the longest. All rates must agree (no resampling)."""
    rates = {rate for _, rate, _ in layers}
    if len(rates) != 1:
        raise AuditionError(f"Layer sample rates differ {sorted(rates)}; render all layers at one rate")
    channels = max(audio.shape[1] for audio, _, _ in layers)
    length = max(len(audio) for audio, _, _ in layers)
    out = np.zeros((length, channels), dtype=np.float64)
    for audio, _, gain_db in layers:
        data = audio.astype(np.float64)
        if data.shape[1] != channels:
            data = np.repeat(data.mean(axis=1, keepdims=True), channels, axis=1)
        out[:len(data)] += data * 10 ** (gain_db / 20)
    return out.astype(np.float32), rates.pop()


def level(audio: np.ndarray, rate: int) -> dict:
    measured = parity.measure(audio, rate)
    lufs = measured["integrated_lufs"]
    return {"integrated_lufs": None if lufs is None else round(lufs, 2),
            "rms_dbfs": round(measured["rms_dbfs"], 2), "peak_dbfs": round(measured["peak_dbfs"], 2),
            "silent": bool(measured["silent"]), "seconds": round(len(audio) / rate, 3), "sample_rate": rate,
            "channels": int(audio.shape[1])}


def loudness_plan(levels: dict[str, dict], target_lufs: float | None) -> dict:
    """Per-label gain to a common loudness. Default target: the quietest candidate (no boost, no clipping).

    Uses integrated LUFS when every candidate has one, else RMS. With an explicit target the whole session is
    lowered (still matched) when a boosted candidate would peak above PEAK_CEILING_DBFS.
    """
    use_lufs = all(v["integrated_lufs"] is not None for v in levels.values())
    measure_key = "integrated_lufs" if use_lufs else "rms_dbfs"
    values = {label: v[measure_key] for label, v in levels.items()}
    target = min(values.values()) if target_lufs is None or not use_lufs else target_lufs
    gains = {label: target - value for label, value in values.items()}
    overshoot = max(levels[label]["peak_dbfs"] + gain for label, gain in gains.items()) - PEAK_CEILING_DBFS
    lowered = 0.0
    if overshoot > 0:
        lowered = overshoot
        gains = {label: gain - overshoot for label, gain in gains.items()}
        target -= overshoot
    return {"measure": measure_key, "target": round(target, 2), "lowered_for_peaks_db": round(lowered, 2),
            "gains_db": {label: round(gain, 2) for label, gain in gains.items()},
            "requested_target_lufs": target_lufs,
            "note": None if use_lufs else "Integrated LUFS unavailable for some candidate (too short or no "
                                          "pyloudnorm); matched on RMS instead"}


def binding_issues(candidates: list[dict], duration_tolerance: float = DURATION_TOLERANCE_SECONDS) -> list[dict]:
    """Errors make the session not comparable; warnings are shown with the listening sheet."""
    issues = []

    def add(severity, code, message, labels=None):
        issues.append({"severity": severity, "code": code, "message": message, "labels": labels or []})

    rates = {c["label"]: c["level"]["sample_rate"] for c in candidates}
    if len(set(rates.values())) > 1:
        add("error", "sample_rate_mismatch", f"Sample rates differ {rates}; render every candidate at one rate "
            "(nothing is resampled)", list(rates))
    silent = [c["label"] for c in candidates if c["level"]["silent"]]
    if silent:
        add("error", "silent", "Silent candidate(s); check the render before listening", silent)
    timelines = {c["label"]: (c["timeline"] or {}).get("sha256") for c in candidates}
    known = {v for v in timelines.values() if v}
    if len(known) > 1:
        add("error", "midi_mismatch", "Candidates were rendered from different event timelines; the same MIDI "
            "is required", [k for k, v in timelines.items() if v])
    missing = [k for k, v in timelines.items() if not v]
    if missing:
        add("error" if known else "warning", "midi_unbound",
            "No event timeline found for these candidates (no .events.json/.mid next to the render and no "
            "timeline_path); the same MIDI cannot be proven", missing)
    controllers = {c["label"]: json.dumps(c["controllers"], sort_keys=True) for c in candidates}
    if len(set(controllers.values())) > 1:
        add("error", "controller_mismatch", "Declared controller values differ between candidates",
            list(controllers))
    for c in candidates:
        for layer in c.get("layers") or []:
            sha = (layer.get("timeline") or {}).get("sha256")
            if sha and c["timeline"] and sha != c["timeline"]["sha256"]:
                add("error", "layer_midi_mismatch", f"Layer {layer['path']} of {c['label']} was rendered from a "
                    "different timeline than its other layers", [c["label"]])
    durations = {c["label"]: c["level"]["seconds"] for c in candidates}
    if max(durations.values()) - min(durations.values()) > duration_tolerance:
        add("warning", "duration_mismatch", f"Durations differ by more than {duration_tolerance} s {durations}; "
            "tails or render lengths differ", list(durations))
    unversioned = [c["label"] for c in candidates if c["kind"] == "integrated_preset" and not c["files"]]
    if unversioned:
        add("warning", "file_version_unbound", "integrated_preset candidates without preset_paths: the file "
            "version that produced them is not recorded", unversioned)
    return issues


def compare_binding(previous_target: dict, binding: dict) -> list[str]:
    """Differences between the previous round's target binding and this session's binding."""
    old = previous_target.get("binding", {})
    changes = []
    if old.get("timeline_sha256") and binding.get("timeline_sha256") != old.get("timeline_sha256"):
        changes.append("event timeline (MIDI) changed")
    if old.get("controllers") != binding.get("controllers"):
        changes.append("declared controller values changed")
    if old.get("sample_rate") != binding.get("sample_rate"):
        changes.append("sample rate changed")
    old_level, new_level = old.get("loudness_target"), binding.get("loudness_target")
    if (old_level is not None and new_level is not None and old.get("loudness_measure") ==
            binding.get("loudness_measure") and abs(old_level - new_level) > 0.5):
        changes.append(f"loudness target moved {old_level} -> {new_level}")
    return changes


def assign_labels(count: int, blind: bool, seed: int | None) -> tuple[list[str], int | None]:
    labels = list(string.ascii_uppercase[:count])
    if not blind:
        return labels, None
    seed = seed if seed is not None else secrets.randbelow(2**31)
    random.Random(seed).shuffle(labels)
    return labels, seed


def write_audio(path: Path, audio: np.ndarray, rate: int, gain_db: float = 0.0) -> None:
    sf.write(path, (audio.astype(np.float64) * 10 ** (gain_db / 20)).astype(np.float32), rate, "FLOAT")


def write_sequence(path: Path, ordered: list[tuple[np.ndarray, float]], rate: int) -> None:
    """A, gap, B, gap, C ... in label order, already loudness matched, for a quick back-to-back listen."""
    channels = max(audio.shape[1] for audio, _ in ordered)
    gap = np.zeros((int(GAP_SECONDS * rate), channels), dtype=np.float64)
    parts = []
    for audio, gain_db in ordered:
        data = audio.astype(np.float64) * 10 ** (gain_db / 20)
        if data.shape[1] != channels:
            data = np.repeat(data.mean(axis=1, keepdims=True), channels, axis=1)
        parts += [data, gap]
    sf.write(path, np.concatenate(parts[:-1]).astype(np.float32), rate, "FLOAT")


def public_view(session: dict) -> dict:
    """The session as shown to a listener: blind sessions hide names/kinds until a choice is recorded."""
    hidden = session.get("blind") and not session.get("choice")
    rows = []
    for c in sorted(session["candidates"], key=lambda c: c["label"]):
        row = {"label": c["label"], "audition_path": c["audition_path"], "gain_db": c["gain_db"],
               "level": c["level"]}
        if not hidden:
            row.update({"name": c["name"], "kind": c["kind"], "source_audio": c["source_audio"],
                        "files": c["files"], "revision": c.get("revision"), "notes": c.get("notes")})
            if c.get("layers"):
                row["layers"] = [{"path": l["path"], "gain_db": l["gain_db"]} for l in c["layers"]]
        rows.append(row)
    out = {k: session.get(k) for k in ("audition_id", "round", "previous_audition_id", "created", "status",
                                       "binding", "loudness", "issues", "sequence_path", "question")}
    out["candidates"] = rows
    out["blind"] = bool(session.get("blind"))
    if hidden:
        out["blind_note"] = "Names and kinds are hidden until falcon_audition_choose records the choice."
    for key in ("choice", "target_path", "migrations", "binding_changes"):
        if session.get(key):
            out[key] = session[key]
    return out
