"""Static sample-source audit of .uvip/.uvim presets: mapping layout plus DSP on the referenced sample files.

Reads Keygroup key/velocity ranges and SamplePlayer SamplePath/BaseNote from preset XML, then measures each
unique sample file once (peak, noise floor, tail level, channel layout, fingerprint). No Falcon instance runs.
"""

from __future__ import annotations

import hashlib
import math
import os
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path, PurePosixPath

import numpy as np
import soundfile as sf

MAX_SAMPLE_SECONDS = 180
WINDOW_SECONDS = 0.01
FINGERPRINT_BINS = 384
FINGERPRINT_MAX_HZ = 6000.0
NEAR_DUPLICATE = 0.985
WAVE_RATE = 11025
WAVE_MATCH = 0.9
SOUNDBANK_SUFFIXES = (".ufs",)
_CACHE: dict[tuple, dict] = {}
_CACHE_LIMIT = 4096


def _on(node: ET.Element) -> bool:
    return node.get("Bypass", "0") in ("0", "false")


def _float(node: ET.Element, name: str, default: float) -> float:
    try:
        return float(node.get(name, default))
    except ValueError:
        return default


def _int(node: ET.Element, name: str, default: int) -> int:
    return round(_float(node, name, default))


def _db(value: float) -> float:
    return 20 * math.log10(max(value, 1e-12))


def _r(value, digits=1):
    return None if value is None else round(float(value), digits)


# ---------------------------------------------------------------- mapping


def programs(root: ET.Element, label: str) -> list[dict]:
    """Every Program in a .uvip (one) or .uvim (one per Part), with its active sample zones."""
    out = []
    nodes = [root] if root.tag == "Program" else list(root.iter("Program"))
    for index, program in enumerate(nodes, 1):
        name = program.get("DisplayName") or program.get("Name") or label
        zones, mapping_oscillators, bypassed = [], 0, 0
        for layer in program.iter("Layer"):
            layer_gain = _float(layer, "Gain", 1.0)
            layer_on = _on(layer) and layer.get("Mute", "0") in ("0", "false")
            layer_name = layer.get("DisplayName") or layer.get("Name") or "Layer"
            for group in layer.iter("Keygroup"):
                for oscillator in group.iter():
                    if oscillator.tag == "SampleMappingOscillator":
                        mapping_oscillators += 1
                    if oscillator.tag != "SamplePlayer":
                        continue
                    if not (layer_on and _on(group) and _on(oscillator)):
                        bypassed += 1
                        continue
                    raw = oscillator.get("SamplePath", "")
                    if not raw:
                        continue
                    gain = layer_gain * _float(group, "Gain", 1.0) * _float(oscillator, "Gain", 1.0)
                    zones.append({
                        "layer": layer_name,
                        "keygroup": group.get("DisplayName") or group.get("Name") or "",
                        "release_trigger": group.get("TriggerMode", "0") not in ("0", ""),
                        "low_key": _int(group, "LowKey", 0), "high_key": _int(group, "HighKey", 127),
                        "low_vel": _int(group, "LowVelocity", 1), "high_vel": _int(group, "HighVelocity", 127),
                        "root": _int(oscillator, "BaseNote", 60)
                        + _int(oscillator, "CoarseTune", 0),
                        "tracking": _float(oscillator, "NoteTracking", 1.0),
                        "gain_db": _db(gain) if gain > 0 else -120.0,
                        "path": raw,
                    })
        out.append({"index": index, "name": name, "zones": zones,
                    "mapping_oscillators": mapping_oscillators, "bypassed_zones": bypassed})
    return out


def _ranges(keys: list[int]) -> str:
    if not keys:
        return ""
    parts, start, prev = [], keys[0], keys[0]
    for key in keys[1:] + [None]:
        if key is not None and key == prev + 1:
            prev = key
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if key is not None:
            start = prev = key
    return ",".join(parts)


def velocity_ranks(zones: list[dict]) -> list[int]:
    """1-based velocity layer of each zone among the velocity ranges that overlap its key range.

    Ranges are ordered by their midpoint, so crossfaded (overlapping) ranges still count as separate layers.
    """
    ranks = []
    for zone in zones:
        middle = zone["low_vel"] + zone["high_vel"]
        below = {(z["low_vel"], z["high_vel"]) for z in zones
                 if z["low_key"] <= zone["high_key"] and z["high_key"] >= zone["low_key"]
                 and z["low_vel"] + z["high_vel"] < middle}
        ranks.append(len(below) + 1)
    return ranks


def mapping_summary(zones: list[dict]) -> dict:
    tracked = [z for z in zones if abs(z["tracking"]) > 0.01]
    if not zones:
        return {"zones": 0}
    keys = range(min(z["low_key"] for z in zones), max(z["high_key"] for z in zones) + 1)
    layers_at, gaps = [], []
    for key in keys:
        here = [z for z in zones if z["low_key"] <= key <= z["high_key"]]
        layers_at.append(len({(z["low_vel"], z["high_vel"]) for z in here}))
        covered = np.zeros(128, bool)
        for z in here:
            covered[z["low_vel"]:z["high_vel"] + 1] = True
        if not covered[1:].all():
            gaps.append(key)
    split_sets = Counter()
    for key in keys:
        here = sorted({(z["low_vel"], z["high_vel"]) for z in zones if z["low_key"] <= key <= z["high_key"]})
        if here:
            split_sets[tuple(f"{a}-{b}" for a, b in here)] += 1
    by_layer = defaultdict(set)
    for zone in tracked:
        by_layer[(zone["low_vel"], zone["high_vel"])].add(zone["root"])
    intervals, roots_per_layer = [], []
    for roots in by_layer.values():
        ordered = sorted(roots)
        roots_per_layer.append(len(ordered))
        intervals += [b - a for a, b in pairwise(ordered) if b > a]
    stretch = [max(abs(z["high_key"] - z["root"]), abs(z["low_key"] - z["root"])) for z in tracked]
    paths = Counter(z["path"] for z in zones)
    out = {
        "zones": len(zones),
        "key_range": [keys.start, keys.stop - 1],
        "velocity_layers": {"min": min(layers_at), "median": statistics.median(layers_at), "max": max(layers_at),
                            "most_common_split": list(split_sets.most_common(1)[0][0]) if split_sets else []},
        "sampling_interval_semitones": ({"median": statistics.median(intervals), "min": min(intervals),
                                         "max": max(intervals), "histogram": dict(sorted(Counter(intervals).items()))}
                                        if intervals else None),
        "roots_per_velocity_layer": ({"min": min(roots_per_layer), "max": max(roots_per_layer)}
                                     if roots_per_layer else None),
        "max_stretch_semitones": max(stretch) if stretch else None,
        "untracked_zones": len(zones) - len(tracked),
        "unique_samples": len(paths),
        "samples_reused_in_program": sum(1 for count in paths.values() if count > 1),
    }
    if gaps:
        out["velocity_gaps_at_keys"] = _ranges(gaps)
    return out


# ---------------------------------------------------------------- sample files


def resolve(raw: str, preset: Path, roots: list[Path], hint: dict) -> Path | None:
    """Find a sample: as written, relative to the preset, or by path tail under the preset's folders/roots."""
    text = raw.replace("\\", "/")
    direct = Path(text)
    if direct.is_absolute() and direct.is_file():
        return direct
    if not direct.is_absolute() and (preset.parent / text).is_file():
        return preset.parent / text
    parts = [p for p in PurePosixPath(text).parts if p not in ("/",) and not p.endswith(":")]
    bases = list(roots) + [preset.parent, *list(preset.parents)[:4]]
    if hint.get("base") is not None:
        candidate = hint["base"].joinpath(*parts[-hint["tail"]:])
        if candidate.is_file():
            return candidate
    for base in bases:
        for tail in range(min(len(parts), 6), 0, -1):
            candidate = base.joinpath(*parts[-tail:])
            if candidate.is_file():
                hint.update(base=base, tail=tail)
                return candidate
    return None


def _windows_db(mono_power: np.ndarray, size: int) -> np.ndarray:
    count = len(mono_power) // size
    if count == 0:
        return np.array([10 * math.log10(max(float(mono_power.mean()), 1e-24))])
    blocks = mono_power[:count * size].reshape(count, size).mean(axis=1)
    return 10 * np.log10(np.maximum(blocks, 1e-24))


def _fingerprint(mono: np.ndarray, rate: int) -> np.ndarray:
    """Log-magnitude spectrum of the first second on a fixed frequency grid (pitch-sensitive)."""
    head = mono[: rate]
    size = 8192
    if len(head) < size:
        head = np.pad(head, (0, size - len(head)))
    frames = np.lib.stride_tricks.sliding_window_view(head, size)[:: size // 2]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(size), axis=-1)).mean(axis=0)
    freqs = np.fft.rfftfreq(size, 1 / rate)
    grid = np.linspace(40, min(FINGERPRINT_MAX_HZ, rate / 2 - 1), FINGERPRINT_BINS)
    shape = np.log(np.interp(grid, freqs, spectrum) + 1e-9)
    shape -= shape.mean()
    norm = np.linalg.norm(shape)
    return (shape / norm).astype(np.float32) if norm > 0 else shape.astype(np.float32)


def _envelope_print(power: np.ndarray, rate: int) -> np.ndarray:
    """Level in 100 ms steps over the first 4 s, relative to its maximum, floored at -80 dB."""
    env = _windows_db(power[: rate * 4], max(1, rate // 10))
    env = np.maximum(env - env.max(), -80)
    return np.pad(env, (0, max(0, 40 - len(env))), constant_values=-80)[:40].astype(np.float32)


def _wave_print(mono: np.ndarray, rate: int) -> np.ndarray:
    """First 0.75 s from the onset, resampled to a common rate and normalized, for waveform matching."""
    start = int(np.argmax(np.abs(mono) > 0.05 * max(float(np.max(np.abs(mono))), 1e-9)))
    head = mono[start: start + int(rate * 0.75)].astype(np.float64)
    grid = np.arange(int(len(head) * WAVE_RATE / rate)) * rate / WAVE_RATE
    wave = np.interp(grid, np.arange(len(head)), head) if len(head) > 1 else np.zeros(1)
    wave -= wave.mean()
    norm = np.linalg.norm(wave)
    return (wave / norm).astype(np.float32) if norm > 0 else wave.astype(np.float32)


def same_waveform(a: np.ndarray, b: np.ndarray, max_lag_seconds: float = 0.05) -> bool:
    """Normalized cross-correlation peak within a small lag: copies, re-gained or re-trimmed takes match;
    a different take or a different velocity layer of the same note does not."""
    size = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    lag = int(max_lag_seconds * WAVE_RATE)
    window = np.concatenate([corr[: lag + 1], corr[-lag:]])
    return bool(np.max(np.abs(window)) >= WAVE_MATCH)


def same_envelope(a: np.ndarray, b: np.ndarray, tolerance_db: float = 2.0) -> bool:
    audible = (a > -60) | (b > -60)
    return bool(audible.any() and np.mean(np.abs(a[audible] - b[audible])) <= tolerance_db)


def analyze_file(path: Path, truncation_db: float = -50.0) -> dict:
    """Measure one sample file. Cached by path, size and modification time."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns, truncation_db)
    if key in _CACHE:
        return _CACHE[key]
    info = sf.info(str(path))
    if info.frames == 0 or info.samplerate <= 0:
        raise ValueError("empty file")
    if info.frames > MAX_SAMPLE_SECONDS * info.samplerate:
        raise ValueError(f"longer than {MAX_SAMPLE_SECONDS} s")
    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if not np.isfinite(audio).all():
        raise ValueError("contains NaN or infinity")
    channels = audio.shape[1]
    peak = float(np.max(np.abs(audio)))
    power = (audio.astype(np.float64) ** 2).mean(axis=1)
    env = _windows_db(power, max(1, int(rate * WINDOW_SECONDS)))
    peak_window = float(env.max())
    audible = env[env > -140]
    floor = float(np.percentile(audible, 5)) if len(audible) else -140.0
    tail = float(env[-3:].mean()) if len(env) >= 3 else float(env[-1])
    tail_rel = tail - peak_window
    onset_index = int(np.argmax(env > peak_window - 40))
    lead = env[: max(0, onset_index - 1)]
    lead = lead[lead > -140]
    # A fade applied to a cut: the last 100 ms falls much faster than the 0.5 s before it.
    fade = False
    if len(env) >= 70:
        last = env[-10:]
        before = env[-60:-10]
        slope_last = (last[0] - last[-1]) / (len(last) * WINDOW_SECONDS)
        slope_before = (before[0] - before[-1]) / (len(before) * WINDOW_SECONDS)
        fade = bool(last[0] - last[-1] > 15 and slope_last > 3 * max(slope_before, 1.0)
                    and last[0] - peak_window > -60)
    # Ending loud is a cut unless the file already sits on a high noise floor it decayed into.
    decayed_into_noise = tail - floor <= 6 and floor - peak_window <= truncation_db
    truncated = bool((tail_rel > truncation_db and not decayed_into_noise) or fade)
    onset = onset_index * WINDOW_SECONDS
    # The quietest windows are room noise only if the file decayed (or has a silent lead-in to measure).
    if len(lead) >= 3:
        noise = float(np.median(lead))
    elif not truncated:
        noise = floor
    else:
        noise = None
    layout = "mono"
    stereo = None
    if channels >= 2:
        left, right = audio[:, 0].astype(np.float64), audio[:, 1].astype(np.float64)
        if np.max(np.abs(left - right)) <= 1e-6 * max(peak, 1e-9):
            layout = "dual_mono"
        else:
            mid, side = (left + right) / 2, (left - right) / 2
            side_db = 10 * math.log10(max(float(np.mean(side ** 2)), 1e-24) / max(float(np.mean(mid ** 2)), 1e-24))
            corr = float(np.corrcoef(left, right)[0, 1]) if left.std() > 0 and right.std() > 0 else 1.0
            layout = "narrow_stereo" if side_db < -30 else "stereo"
            stereo = {"side_to_mid_db": round(side_db, 1), "lr_correlation": round(corr, 3)}
    mono = audio.mean(axis=1)
    pcm = np.round(audio * 32767).astype(np.int16)
    result = {
        "seconds": round(len(audio) / rate, 2), "sample_rate": rate, "channels": channels, "layout": layout,
        "peak_dbfs": round(_db(peak), 2), "noise_floor_dbfs": _r(noise),
        "tail_rel_peak_db": round(tail_rel, 1),
        "faded_cut": fade, "truncated": truncated, "onset_ms": round(onset * 1000),
        "ends_nonzero": bool(np.max(np.abs(audio[-1])) > 0.01 * max(peak, 1e-9)),
        "content_sha1": hashlib.sha1(pcm.tobytes()).hexdigest()[:16],
        "fingerprint": _fingerprint(mono, rate),
        "envelope": _envelope_print(power, rate),
        "wave": _wave_print(mono, rate),
    }
    if stereo:
        result["stereo"] = stereo
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = result
    return result


def analyze_files(paths: list[Path], truncation_db: float, workers: int | None = None) -> dict[Path, dict]:
    def one(path):
        try:
            return path, analyze_file(path, truncation_db)
        except (OSError, ValueError, RuntimeError, sf.LibsndfileError) as exc:
            return path, {"error": str(exc)[:200]}

    workers = workers or min(8, (os.cpu_count() or 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, paths))


# ---------------------------------------------------------------- report


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def _short(path: str) -> str:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return "/".join(parts[-2:])


def layer_levels(zones: list[dict], files: dict[str, dict]) -> list[dict]:
    ranks = velocity_ranks(zones)
    groups = defaultdict(list)
    for zone, rank in zip(zones, ranks):
        groups[rank].append(zone)
    out = []
    for rank in sorted(groups):
        members = groups[rank]
        peaks = [files[z["path"]]["peak_dbfs"] for z in members if "peak_dbfs" in files.get(z["path"], {})]
        effective = [files[z["path"]]["peak_dbfs"] + z["gain_db"] for z in members
                     if "peak_dbfs" in files.get(z["path"], {})]
        velocities = Counter(f"{z['low_vel']}-{z['high_vel']}" for z in members)
        out.append({"layer": rank, "velocity": velocities.most_common(1)[0][0], "zones": len(members),
                    "median_peak_dbfs": _median(peaks),
                    "peak_spread_db": round(max(peaks) - min(peaks), 1) if peaks else None,
                    "median_effective_db": _median(effective)})
    return out


def normalization_verdict(levels: list[dict]) -> dict:
    measured = [level for level in levels if level["median_peak_dbfs"] is not None]
    if len(measured) < 2:
        return {"verdict": "single_layer" if measured else "not_measured"}
    raw = [level["median_peak_dbfs"] for level in measured]
    effective = [level["median_effective_db"] for level in measured]
    raw_span = raw[-1] - raw[0]
    effective_span = effective[-1] - effective[0]
    out = {"raw_soft_to_loud_db": round(raw_span, 1), "effective_soft_to_loud_db": round(effective_span, 1)}
    if max(raw) - min(raw) < 3:
        normalized = min(raw) > -3
        if effective_span >= 6:
            out["verdict"] = "per_layer_normalized_compensated" if normalized else "level_matched_compensated"
        else:
            out["verdict"] = "per_layer_normalized" if normalized else "level_matched"
    elif any(b < a - 1 for a, b in pairwise(effective)):
        out["verdict"] = "non_monotonic"
    else:
        out["verdict"] = "natural_dynamics"
    return out


def audit(sources: list[Path], sample_roots: list[Path] | None = None, analyze_audio: bool = True,
          truncation_db: float = -50.0, max_files: int = 2000, sample_rows: int = 0,
          parse=None, detail: str = "summary") -> dict:
    """Audit one or more presets. `parse` turns file bytes into a UVI4 root (injected for validation)."""
    roots = [Path(r) for r in sample_roots or []]
    progs = []
    for source in sources:
        root = parse(source.read_bytes())
        for program in programs(root, source.stem):
            program["source"] = source
            progs.append(program)
    if len(progs) > 1:
        names = Counter(p["name"] for p in progs)
        for program in progs:
            if names[program["name"]] > 1:
                program["name"] = f"{program['name']} ({program['source'].name}#{program['index']})"
    resolved: dict[str, Path | None] = {}
    soundbank = Counter()
    for program in progs:
        hint: dict = {}
        for zone in program["zones"]:
            raw = zone["path"]
            if raw in resolved:
                continue
            if any(suffix in raw.lower() for suffix in SOUNDBANK_SUFFIXES):
                resolved[raw] = None
                soundbank[program["name"]] += 1
                continue
            resolved[raw] = resolve(raw, program["source"], roots, hint)
    unique = sorted({p for p in resolved.values() if p is not None}, key=str)
    skipped = max(0, len(unique) - max_files)
    unique = unique[:max_files]
    measured = analyze_files(unique, truncation_db) if analyze_audio and unique else {}
    files = {raw: measured.get(path, {}) if path else {} for raw, path in resolved.items()}

    reports = []
    all_findings = []
    for program in progs:
        zones = program["zones"]
        raws = sorted({z["path"] for z in zones})
        missing = [r for r in raws if resolved.get(r) is None and not any(s in r.lower() for s in SOUNDBANK_SUFFIXES)]
        report = {"program": program["name"], "source": program["source"].name, "keygroup_zones": len(zones)}
        if program["mapping_oscillators"]:
            report["multisample_oscillators_not_audited"] = program["mapping_oscillators"]
        if program["bypassed_zones"]:
            report["bypassed_zones_skipped"] = program["bypassed_zones"]
        report["files"] = {"referenced": len(raws), "resolved": len(raws) - len(missing) - soundbank[program["name"]],
                           "missing": len(missing), "in_soundbank": soundbank[program["name"]]}
        if missing:
            report["files"]["missing_examples"] = [_short(r) for r in missing[:3]]
        by_layer = defaultdict(list)
        for zone in zones:
            by_layer[zone["layer"]].append(zone)
        report["layers"] = []
        for name, members in by_layer.items():
            layer = _layer_report(name, members, files, analyze_audio, sample_rows)
            all_findings += [f"{program['name']} / {name}: {f}" for f in layer["findings"]]
            report["layers"].append(layer if detail == "full" else _compact_layer(layer))
        reports.append(report)

    result = {"programs": reports}
    if analyze_audio and len(progs) > 1:
        result["shared_recordings"] = shared_recordings(progs, files, resolved)
        shares = [f"{pair['a']} and {pair['b']} share {pair['shared_fraction']:.0%} of their recordings "
                  f"({pair['same_file']} same files, {pair['same_audio']} identical audio, "
                  f"{pair['near_duplicate']} near-identical)"
                  for pair in result["shared_recordings"]["pairs"] if pair["shared_fraction"] >= 0.25]
        all_findings = shares + all_findings
    all_findings.sort(key=_priority)
    if skipped:
        result["files_not_measured_over_limit"] = skipped
    if len(all_findings) > 60:
        result["findings_not_shown"] = len(all_findings) - 60
    result["findings"] = all_findings[:60]
    result["limits"] = {
        "truncation_db": truncation_db,
        "noise_floor": ("median of a silent lead-in, else the quietest 5% of 10 ms windows of a file that decayed; "
                        "null (not_measurable) when a file is cut before it decays"),
        "layers": ("mapping, levels and findings are per Falcon Layer (one source stack each); release-trigger "
                   "layers get no mapping or level findings"),
        "truncated": ("file ends more than |truncation_db| below its peak window is NOT truncated; ends louder "
                      "than that and 10 dB above the floor, or with a steep end fade, is flagged"),
        "near_duplicate": (f"cosine >= {NEAR_DUPLICATE} of first-second log spectra, level envelope within 2 dB, "
                           f"duration within 5% and waveform correlation >= {WAVE_MATCH} within 50 ms (same take, "
                           "re-gained or re-trimmed); identical PCM always counts"),
        "not_checked": ["loop points and release samples", "SampleStart offsets", "MultiSample oscillators",
                        "UFS soundbank contents", "how it sounds"],
    }
    return result


def _layer_report(name: str, zones: list[dict], files: dict, analyze_audio: bool, sample_rows: int) -> dict:
    """One Falcon Layer: a separate source stack with its own key/velocity map."""
    raws = sorted({z["path"] for z in zones})
    release = sum(1 for z in zones if z["release_trigger"])
    report = {"layer": name, "mapping": mapping_summary(zones)}
    if release:
        report["release_trigger_zones"] = release
    findings = _mapping_findings(report["mapping"]) if release < len(zones) else []
    if analyze_audio:
        stats = [files[r] for r in raws if "peak_dbfs" in files.get(r, {})]
        errors = [r for r in raws if "error" in files.get(r, {})]
        report["audio"] = _audio_summary(stats, raws, files, errors)
        if release < len(zones):
            levels = layer_levels(zones, files)
            report["velocity_layer_levels"] = levels
            report["normalization"] = normalization_verdict(levels)
            findings += _audio_findings(report["audio"], report["normalization"])
        if sample_rows:
            report["samples"] = _sample_rows(zones, files, sample_rows)
    report["findings"] = findings
    return report


PRIORITY = ("share", "loud", "normalized", "quieter", "decay", "sparse", "stretched", "gaps", "noise floor")


def _priority(finding: str) -> int:
    return next((i for i, word in enumerate(PRIORITY) if word in finding), len(PRIORITY))


def _compact_layer(layer: dict) -> dict:
    """One flat row per Falcon Layer; detail="full" keeps the nested report."""
    mapping, audio = layer["mapping"], layer.get("audio") or {}
    out = {"layer": layer["layer"], "zones": mapping.get("zones"), "keys": mapping.get("key_range")}
    if layer.get("release_trigger_zones"):
        out["release_trigger_zones"] = layer["release_trigger_zones"]
    velocity = mapping.get("velocity_layers")
    if velocity:
        out["velocity_layers"] = velocity["median"] if velocity["min"] == velocity["max"] else \
            [velocity["min"], velocity["max"]]
        out["velocity_split"] = velocity["most_common_split"]
    interval = mapping.get("sampling_interval_semitones")
    if interval:
        out["interval_semitones"] = {"median": interval["median"], "max": interval["max"]}
    for key in ("max_stretch_semitones", "velocity_gaps_at_keys"):
        if mapping.get(key):
            out[key] = mapping[key]
    if audio.get("measured"):
        out["files"] = audio["measured"]
        out["layouts"] = audio["layouts"]
        out["truncated"] = audio["truncated"]
        out["noise_floor_dbfs"] = audio["noise_floor_dbfs"]["median"]
        out["peak_dbfs_median"] = audio["peak_dbfs"]["median"]
        if audio.get("truncated_examples"):
            out["truncated_example"] = audio["truncated_examples"][0]
    if audio.get("errors"):
        out["errors"] = audio.get("error_examples")
    normalization = layer.get("normalization")
    if normalization and normalization.get("verdict") not in (None, "single_layer", "not_measured"):
        out["normalization"] = normalization
    if layer.get("samples"):
        out["samples"] = layer["samples"]
    return out


def _mapping_findings(mapping: dict) -> list[str]:
    out = []
    interval = mapping.get("sampling_interval_semitones")
    if interval and interval["median"] >= 3:
        out.append(f"sparse sampling: a root every {interval['median']:g} semitones (max gap {interval['max']})")
    if (mapping.get("max_stretch_semitones") or 0) > 6:
        out.append(f"zones stretched up to {mapping['max_stretch_semitones']} semitones from their root")
    layers = mapping.get("velocity_layers")
    if layers and layers["max"] <= 1 and mapping.get("zones"):
        out.append("single velocity layer: timbre does not change with velocity unless modulated")
    if mapping.get("velocity_gaps_at_keys"):
        out.append(f"velocity gaps (some velocities play nothing) at keys {mapping['velocity_gaps_at_keys']}")
    return out


def _audio_summary(stats: list[dict], raws: list[str], files: dict, errors: list[str]) -> dict:
    if not stats:
        return {"measured": 0, "errors": len(errors)}
    layouts = Counter(s["layout"] for s in stats)
    truncated = [r for r in raws if files.get(r, {}).get("truncated")]
    out = {
        "measured": len(stats),
        "layouts": dict(layouts),
        "sample_rates": dict(Counter(s["sample_rate"] for s in stats)),
        "seconds": {"min": min(s["seconds"] for s in stats), "median": _median([s["seconds"] for s in stats]),
                    "max": max(s["seconds"] for s in stats)},
        "peak_dbfs": {"min": min(s["peak_dbfs"] for s in stats), "median": _median([s["peak_dbfs"] for s in stats]),
                      "max": max(s["peak_dbfs"] for s in stats)},
        "noise_floor_dbfs": _noise_summary(stats),
        "truncated": len(truncated),
        "faded_cuts": sum(1 for s in stats if s["faded_cut"]),
        "late_onset_over_50ms": sum(1 for s in stats if s["onset_ms"] > 50),
    }
    if truncated:
        worst = sorted(truncated, key=lambda r: -files[r]["tail_rel_peak_db"])[:3]
        out["truncated_examples"] = [{"file": _short(r), "seconds": files[r]["seconds"],
                                      "tail_rel_peak_db": files[r]["tail_rel_peak_db"]} for r in worst]
    widths = [s["stereo"]["side_to_mid_db"] for s in stats if "stereo" in s]
    if widths:
        out["side_to_mid_db_median"] = _median(widths)
    if errors:
        out["errors"] = len(errors)
        out["error_examples"] = [f"{_short(r)}: {files[r]['error']}" for r in errors[:3]]
    return out


def _noise_summary(stats: list[dict]) -> dict:
    floors = [s["noise_floor_dbfs"] for s in stats if s["noise_floor_dbfs"] is not None]
    out = {"median": _median(floors), "max": max(floors) if floors else None, "measured_in": len(floors)}
    if len(floors) < len(stats):
        out["not_measurable"] = len(stats) - len(floors)
    return out


def _audio_findings(audio: dict, normalization: dict) -> list[str]:
    out = []
    measured = audio.get("measured") or 0
    if not measured:
        return out
    if audio["truncated"]:
        out.append(f"{audio['truncated']}/{measured} samples end before their decay does "
                   f"({audio['faded_cuts']} with an end fade)")
    layouts = audio["layouts"]
    if layouts.get("dual_mono"):
        out.append(f"{layouts['dual_mono']} stereo files are dual mono (identical channels)")
    if layouts.get("mono") == measured:
        out.append("all samples are mono")
    elif layouts.get("mono"):
        out.append(f"mixed layouts: {layouts}")
    if audio["noise_floor_dbfs"]["median"] is not None and audio["noise_floor_dbfs"]["median"] > -70:
        out.append(f"high noise floor: median {audio['noise_floor_dbfs']['median']} dBFS")
    verdict = normalization.get("verdict")
    if verdict == "per_layer_normalized":
        out.append("velocity layers are each normalized to full scale and not compensated by gain: soft layers "
                   f"play about as loud as hard ones (effective span {normalization['effective_soft_to_loud_db']} dB)")
    elif verdict == "level_matched":
        out.append("velocity layers peak at the same level on disk and gains do not separate them: soft layers "
                   f"play about as loud as hard ones (effective span {normalization['effective_soft_to_loud_db']} dB)")
    elif verdict in ("per_layer_normalized_compensated", "level_matched_compensated"):
        out.append("velocity layers peak at about the same level on disk; keygroup or layer gains restore "
                   f"{normalization['effective_soft_to_loud_db']} dB soft-to-loud")
    elif verdict == "non_monotonic":
        out.append("a louder velocity layer plays quieter than the one below it")
    return out


def _sample_rows(zones: list[dict], files: dict, limit: int) -> list[dict]:
    seen, rows = set(), []
    for zone in zones:
        stats = files.get(zone["path"], {})
        if zone["path"] in seen or "peak_dbfs" not in stats:
            continue
        seen.add(zone["path"])
        rows.append({"file": _short(zone["path"]), "root": zone["root"], "vel": f"{zone['low_vel']}-{zone['high_vel']}",
                     "keys": f"{zone['low_key']}-{zone['high_key']}", "s": stats["seconds"], "ch": stats["layout"],
                     "peak": stats["peak_dbfs"], "floor": stats["noise_floor_dbfs"],
                     "tail": stats["tail_rel_peak_db"], "trunc": stats["truncated"]})
    rows.sort(key=lambda row: (not row["trunc"], -row["tail"]))
    return rows[:limit]


def shared_recordings(progs: list[dict], files: dict, resolved: dict) -> dict:
    """Pairs of programs that use the same file, identical audio, or near-identical audio."""
    owners = []  # (program index, raw path, stats)
    for index, program in enumerate(progs):
        for raw in sorted({z["path"] for z in program["zones"]}):
            stats = files.get(raw, {})
            if "fingerprint" in stats:
                owners.append((index, raw, stats))
    if not owners:
        return {"pairs": [], "compared_files": 0}
    prints = np.stack([o[2]["fingerprint"] for o in owners])
    similarity = prints @ prints.T
    program_of = np.array([o[0] for o in owners])
    durations = np.array([o[2]["seconds"] for o in owners])
    shared = defaultdict(lambda: {"same_file": set(), "same_audio": set(), "near": set(), "examples": []})
    files_of = defaultdict(set)
    for pi, raw, _ in owners:
        files_of[pi].add(resolved.get(raw))
    by_hash = defaultdict(list)
    for j, owner in enumerate(owners):
        by_hash[owner[2]["content_sha1"]].append(j)
    for i, (pi, raw_i, stats_i) in enumerate(owners):
        others = np.where((program_of > pi) & (similarity[i] >= NEAR_DUPLICATE)
                          & (np.abs(durations - durations[i]) <= 0.05 * max(durations[i], 0.1) + 0.05))[0]
        near = {j for j in others.tolist() if same_envelope(stats_i["envelope"], owners[j][2]["envelope"])
                and same_waveform(stats_i["wave"], owners[j][2]["wave"])}
        same_hash = {j for j in by_hash[stats_i["content_sha1"]] if owners[j][0] > pi}
        for j in near | same_hash:
            pj, raw_j, stats_j = owners[j]
            bucket = shared[(pi, pj)]
            if j in near and j not in same_hash and (resolved.get(raw_i) in files_of[pj]
                                                     or resolved.get(raw_j) in files_of[pi]):
                continue  # both files are in both programs: a look-alike inside one sample set, not a share
            if resolved.get(raw_i) == resolved.get(raw_j):
                bucket["same_file"].add(raw_i)
            elif stats_i["content_sha1"] == stats_j["content_sha1"]:
                bucket["same_audio"].add(raw_i)
            else:
                bucket["near"].add(raw_i)
                if len(bucket["examples"]) < 2:
                    bucket["examples"].append(f"{_short(raw_i)} ~ {_short(raw_j)} ({similarity[i, j]:.3f})")
    pairs = []
    sizes = Counter(program_of.tolist())
    for (pi, pj), bucket in shared.items():
        count = len(bucket["same_file"] | bucket["same_audio"] | bucket["near"])
        smaller = max(1, min(sizes[pi], sizes[pj]))
        pairs.append({"a": progs[pi]["name"], "b": progs[pj]["name"], "same_file": len(bucket["same_file"]),
                      "same_audio": len(bucket["same_audio"]), "near_duplicate": len(bucket["near"]),
                      "shared_fraction": round(count / smaller, 2), "near_examples": bucket["examples"]})
    pairs.sort(key=lambda p: -p["shared_fraction"])
    return {"pairs": pairs[:30], "compared_files": len(owners)}
