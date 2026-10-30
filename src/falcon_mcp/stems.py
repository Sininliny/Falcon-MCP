"""Layer/stem contribution analysis from repeated renders of one performance; deterministic DSP only.

A "target" (Layer, Keygroup, effect group, ...) is described by up to three renders of the SAME
performance: the full mix, the mix WITHOUT the target, and the target SOLO. Everything here is a signal
measurement with heuristic wording for steering edits; none of it is a perceptual or quality verdict.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from scipy import signal

from . import listen

ATTACK_SECONDS = 0.08
NFFT, HOP = 2048, 512
ACTIVE_FLOOR_DB = -60.0      # TF cells this far below the full mix's loudest cell count as silent
EXPOSED_MARGIN_DB = -3.0     # a target cell is "exposed" when it is at most 3 dB under the rest of the mix
DOMINANT_MARGIN_DB = 6.0     # a target cell "covers" the rest when it is 6 dB above it
INAUDIBLE_RE_FULL_DB = -40.0
MASKED_EXPOSURE = 0.2        # below this exposed-energy fraction an above-floor target is "masked"
DETERMINISTIC_NULL_DB = -40.0
MONO_DB = -60.0              # side-minus-mid floor; anything below reads as mono
# Whole-phrase features that stay meaningful for chords and phrases (envelope/pitch fields assume one note).
ROBUST_FEATURES = ("centroid_hz", "rolloff_85_hz", "flatness", "crest_db", "side_minus_mid_db", "stereo_correlation")
SPACE_FX_WORDS = ("Reverb", "Verb", "Delay", "Echo", "Convolver", "Diffus", "Bloom")


def _db_ratio(a: float, b: float) -> float | None:
    if a <= 1e-24 and b <= 1e-24:
        return None
    return round(10 * math.log10(max(a, 1e-24) / max(b, 1e-24)), 2)


def _stereo(audio: np.ndarray) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    return np.repeat(data, 2, axis=1) if data.shape[1] == 1 else data[:, :2]


def align(*renders: np.ndarray) -> list[np.ndarray]:
    """Trim to the shortest render; all renders must share one rate and performance."""
    data = [_stereo(r) for r in renders if r is not None]
    frames = min(len(d) for d in data)
    return [d[:frames] for d in data]


def _k_weighting(rate: int):
    """BS.1770 K-weighting (high shelf +4 dB at 1.5 kHz, RLB high-pass at 38 Hz) as two biquads."""
    def shelf(gain_db, q, fc):
        a, w0 = 10 ** (gain_db / 40), 2 * math.pi * fc / rate
        alpha, cos = math.sin(w0) / (2 * q), math.cos(w0)
        b = [a * ((a + 1) + (a - 1) * cos + 2 * math.sqrt(a) * alpha), -2 * a * ((a - 1) + (a + 1) * cos),
             a * ((a + 1) + (a - 1) * cos - 2 * math.sqrt(a) * alpha)]
        den = [(a + 1) - (a - 1) * cos + 2 * math.sqrt(a) * alpha, 2 * ((a - 1) - (a + 1) * cos),
               (a + 1) - (a - 1) * cos - 2 * math.sqrt(a) * alpha]
        return b, den

    def highpass(q, fc):
        w0 = 2 * math.pi * fc / rate
        alpha, cos = math.sin(w0) / (2 * q), math.cos(w0)
        return [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2], [1 + alpha, -2 * cos, 1 - alpha]

    return [shelf(4.0, 1 / math.sqrt(2), 1500.0), highpass(0.5, 38.0)]


def loudness(audio: np.ndarray, rate: int) -> tuple[float | None, str]:
    """Ungated K-weighted loudness (LKFS over the whole render).

    Integrated LUFS gates out quiet blocks, so a sparse stem (hammer bursts) can read louder than the
    mix that contains it; for with/without comparisons of one fixed-length performance, no gating is used.
    """
    data = _stereo(audio)
    if float(np.max(np.abs(data))) < 1e-6:
        return None, "silent"
    weighted = data
    for b, a in _k_weighting(rate):
        weighted = signal.lfilter(b, a, weighted, axis=0)
    power = float(np.sum(np.mean(weighted**2, axis=0)))
    return -0.691 + 10 * math.log10(max(power, 1e-30)), "lkfs_ungated"


def matched_preview_pair(a, b, rate: int, directory: Path, tag: str) -> dict:
    """Separate listening copies; attenuate to equal ungated LKFS, then apply common sample-peak headroom."""
    import soundfile as sf

    if len(a) != len(b):
        raise ValueError("Matched A/B needs equal frame counts; do not silently trim different performances")
    la, method = loudness(a, rate)
    lb, _ = loudness(b, rate)
    if la is None or lb is None:
        return {"status": "untested", "reason": "One side is silent; loudness matching would be misleading"}
    target = min(la, lb)
    ga, gb = target - la, target - lb
    peak = max(float(np.abs(a).max()) * 10 ** (ga / 20), float(np.abs(b).max()) * 10 ** (gb / 20))
    headroom = min(0.0, -1.0 - 20 * math.log10(max(peak, 1e-30)))
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for side, data, gain in (("a", a, ga + headroom), ("b", b, gb + headroom)):
        path = directory / f"{tag}-{side}.wav"
        sf.write(path, data * 10 ** (gain / 20), rate, subtype="FLOAT")
        outputs[f"{side}_path"] = str(path)
        outputs[f"{side}_gain_db"] = round(gain, 3)
    return {"status": "matched", **outputs, "method": method, "matched_lkfs": round(target + headroom, 3),
            "original_a_lkfs": round(la, 3), "original_b_lkfs": round(lb, 3),
            "shared_headroom_db": round(headroom, 3), "sample_peak_ceiling_dbfs": -1.0,
            "limitations": "Same-duration ungated K-weighted match; true peak and perceptual equality are not verified. Originals unchanged."}


def transition_levels(audio, rate: int, windows: list[dict]) -> list[dict]:
    """Level around each MIDI join, not evidence of actual legato/envelope/sample selection."""
    def level(a, b):
        segment = audio[max(0, round(a * rate)):max(0, round(b * rate))]
        return None if not len(segment) else round(10 * math.log10(max(float(np.mean(segment ** 2)), 1e-24)), 2)

    return [{"note": w.get("note"), "on": w["on"], "before_rms_dbfs": level(w["on"] - .05, w["on"] - .005),
             "after_rms_dbfs": level(w["on"] + .005, w["on"] + .05)} for w in windows[:64]]


def _loudness_pair(a: np.ndarray, b: np.ndarray, rate: int) -> tuple[float | None, float | None, str]:
    la, _ = loudness(a, rate)
    lb, _ = loudness(b, rate)
    return la, lb, "lkfs_ungated"


def note_windows_from_notes(notes: list[dict]) -> list[dict]:
    """falcon_native_render note dicts -> [{note, velocity, channel, on, off}] sorted by onset."""
    out = [{"note": n["note"], "velocity": n.get("velocity", 80), "channel": n.get("channel", 1),
            "on": float(n.get("start_seconds", 0.1)),
            "off": float(n.get("start_seconds", 0.1)) + float(n.get("duration_seconds", 0.8))} for n in notes]
    return sorted(out, key=lambda w: w["on"])


def region_masks(times: np.ndarray, windows: list[dict], attack_seconds: float = ATTACK_SECONDS,
                 guard: float = NFFT / 2 / 48000) -> dict:
    """attack: just after any onset; held: a note is down; tail: no note down after the first onset.

    guard (about half an analysis frame) keeps frames that still overlap a note out of the tail.
    """
    attack = np.zeros(len(times), bool)
    held = np.zeros(len(times), bool)
    for w in windows:
        attack |= (times >= w["on"] - guard) & (times < w["on"] + attack_seconds)
        held |= (times >= w["on"] - guard) & (times < w["off"] + guard)
    first = min((w["on"] for w in windows), default=0.0)
    return {"attack": attack, "held": held & ~attack, "tail": (times >= first) & ~held & ~attack}


def _frames(audio: np.ndarray, rate: int):
    """Per-frame power (sum of channels) and per-frame side/mid powers on the STFT hop grid."""
    data = _stereo(audio)
    mid, side = (data[:, 0] + data[:, 1]) / 2, (data[:, 0] - data[:, 1]) / 2
    count = max(1, 1 + (len(data) - NFFT) // HOP)
    starts = np.arange(count) * HOP
    cumulative = [np.concatenate([[0.0], np.cumsum(x**2)]) for x in (data[:, 0], data[:, 1], mid, side)]
    ends = np.minimum(starts + NFFT, len(data))
    sums = [(c[ends] - c[starts]) for c in cumulative]
    times = (starts + (ends - starts) / 2) / rate
    return times, sums[0] + sums[1], sums[2], sums[3]


def tf_power(audio: np.ndarray, rate: int):
    """Channel-summed STFT power (freqs x frames) with the hop grid used by _frames."""
    data = _stereo(audio)
    freqs, times, spec = signal.stft(data.T, fs=rate, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None,
                                     padded=False, axis=-1)
    return freqs, times, (np.abs(spec) ** 2).sum(axis=0)


def region_levels(audio: np.ndarray, rate: int, windows: list[dict]) -> dict:
    times, power, _, _ = _frames(audio, rate)
    masks = region_masks(times, windows)
    return {name: float(power[mask].sum()) for name, mask in masks.items()}


def band_energies(audio: np.ndarray, rate: int) -> dict:
    freqs, _, power = tf_power(audio, rate)
    total = power.sum(axis=1)
    return {name: float(total[(freqs >= low) & (freqs < min(high, rate / 2))].sum())
            for name, low, high in listen.BANDS if low < rate / 2}


def stereo_summary(audio: np.ndarray, rate: int, windows: list[dict] | None = None) -> dict:
    """Width (side-minus-mid dB), correlation and balance; by region when windows are known."""
    data = _stereo(audio)
    report = listen.stereo_analysis(data, rate) or {}
    out = {"side_minus_mid_db": round(max(report.get("side_minus_mid_db", MONO_DB), MONO_DB), 1),
           "correlation": None if report.get("correlation") is None else round(report["correlation"], 3),
           "left_minus_right_db": round(report.get("left_minus_right_db", 0.0), 1),
           "by_band": report.get("side_minus_mid_db_by_band")}
    if windows:
        times, _, mid, side = _frames(data, rate)
        masks = region_masks(times, windows)
        out["by_region"] = {name: max(_db_ratio(float(side[m].sum()), float(mid[m].sum())) or MONO_DB, MONO_DB)
                            for name, m in masks.items() if m.any()}
    return out


def exposure(target: np.ndarray, rest: np.ndarray, full: np.ndarray, rate: int,
             windows: list[dict] | None = None, full_power=None) -> dict:
    """Glimpse-style audibility proxy: share of the target's energy in cells where the rest does not cover it.

    exposed_energy_fraction: target energy in TF cells where target >= rest - 3 dB, over all of the target's
    active energy. covers_rest_fraction: rest energy in cells where the target is 6 dB above it (the target
    is likely masking the rest there). Both per band and per region.
    """
    freqs, times, pt = tf_power(target, rate)
    _, _, pr = tf_power(rest, rate)
    _, _, pf = full_power if full_power is not None else tf_power(full, rate)
    frames = min(pt.shape[1], pr.shape[1], pf.shape[1])
    pt, pr, pf, times = pt[:, :frames], pr[:, :frames], pf[:, :frames], times[:frames]
    floor = float(pf.max()) * 10 ** (ACTIVE_FLOOR_DB / 10) if pf.size else 0.0
    active_t, active_r = pt > floor, pr > floor
    exposed = active_t & (pt >= pr * 10 ** (EXPOSED_MARGIN_DB / 10))
    covers = active_r & (pt >= pr * 10 ** (DOMINANT_MARGIN_DB / 10))

    def share(mask, base, weights):
        total = float(weights[base].sum())
        return None if total <= 0 else round(float(weights[mask & base].sum()) / total, 3)

    out = {"exposed_energy_fraction": share(exposed, active_t, pt),
           "covers_rest_fraction": share(covers, active_r, pr),
           "active_cell_fraction": round(float(active_t.mean()), 3) if active_t.size else 0.0,
           "by_band": {}}
    for name, low, high in listen.BANDS:
        rows = (freqs >= low) & (freqs < min(high, rate / 2))
        if not rows.any():
            continue
        band = np.zeros_like(active_t)
        band[rows] = True
        if float(pt[band & active_t].sum()) > 0:
            out["by_band"][name] = {"exposed": share(exposed, band & active_t, pt),
                                    "target_share_db": _db_ratio(float(pt[band].sum()), float(pf[band].sum()))}
    if windows:
        masks = region_masks(times, windows)
        out["by_region"] = {}
        for name, mask in masks.items():
            cols = np.zeros_like(active_t)
            cols[:, mask] = True
            out["by_region"][name] = share(exposed, cols & active_t, pt)
    return out


def repeatability(full: np.ndarray, repeat: np.ndarray, rate: int) -> dict:
    """Null test of two identical renders; sets the floor below which differences mean nothing."""
    full, repeat = align(full, repeat)
    null = _db_ratio(float(np.sum((full - repeat) ** 2)), float(np.sum(full**2)))
    deterministic = null is None or null <= DETERMINISTIC_NULL_DB
    out = {"null_residual_db": null, "deterministic": deterministic}
    if not deterministic:
        heard = listen.diff_reports(_report(repeat, rate), _report(full, rate))
        out["repeat_noticeable_steps"] = max((abs(r["noticeable_steps"]) for r in heard.get("all_features", [])),
                                             default=0.0)
        out["note"] = ("Renders differ run to run (random/round-robin/free-running modulation); residual metrics "
                       "are disabled and changes within repeat_noticeable_steps are treated as noise")
    return out


def _report(audio: np.ndarray, rate: int) -> dict:
    """Light listen report (no pitch/harmonics/clicks): stems compare only robust whole-phrase features."""
    return listen.listen(audio, rate, include_ascii=False, light=True)


def _fractions(energies: dict) -> dict:
    total = sum(energies.values())
    return {k: round(v / total, 3) for k, v in energies.items()} if total > 0 else {}


def _flatness(audio: np.ndarray, rate: int) -> float | None:
    data = _stereo(audio)
    if float(np.max(np.abs(data))) < 1e-6:
        return None
    return round(listen.spectral_bands(data, rate)["flatness"], 3)


def contribution(full: np.ndarray, without: np.ndarray | None, rate: int, windows: list[dict] | None = None,
                 solo: np.ndarray | None = None, repeat: dict | None = None, cache: dict | None = None) -> dict:
    """How one target changes the full mix: level, regions, bands, exposure, stereo and roles.

    cache (optional, shared across targets of one run) memoises analysis of the unchanged full mix."""
    windows = windows or []
    original_full = full
    aligned = iter(align(*[x for x in (full, without, solo) if x is not None]))
    full = next(aligned)
    without = next(aligned) if without is not None else None
    solo = next(aligned) if solo is not None else None

    def of_full(name, fn):
        """fn(full), computed once per run when the full mix is not trimmed by alignment."""
        if cache is None or len(full) != len(original_full):
            return fn(full)
        key = (name, id(original_full))
        if key not in cache:
            cache[key] = fn(full)
        return cache[key]
    repeat = repeat or {"deterministic": True}
    out: dict = {}
    full_regions = of_full("regions", lambda a: region_levels(a, rate, windows))
    out["full_energy_by_region"] = _fractions(full_regions)
    if solo is not None:
        lf, ls, method = _loudness_pair(full, solo, rate)
        out["solo"] = {"loudness": None if ls is None else round(ls, 2), "loudness_method": method,
                       "level_re_full_db": None if ls is None or lf is None else round(ls - lf, 2),
                       "flatness": _flatness(solo, rate), "stereo": stereo_summary(solo, rate, windows)}
        solo_regions = region_levels(solo, rate, windows)
        # Share of each region's energy that the target alone carries (dB re full in that region).
        out["solo"]["share_by_region_db"] = {k: _db_ratio(solo_regions[k], full_regions[k])
                                             for k in full_regions if full_regions[k] > 0}
        bands_s, bands_f = band_energies(solo, rate), of_full("bands", lambda a: band_energies(a, rate))
        out["solo"]["share_by_band_db"] = {k: _db_ratio(bands_s[k], bands_f[k]) for k in bands_f if bands_f[k] > 0}
        out["solo"]["energy_by_region"] = _fractions(solo_regions)
        out["solo"]["energy_by_band"] = _fractions(bands_s)
    if without is not None:
        lf, lw, method = _loudness_pair(full, without, rate)
        removal = None if lf is None or lw is None else round(lf - lw, 2)
        without_regions = region_levels(without, rate, windows)
        added = {k: max(full_regions[k] - without_regions[k], 0.0) for k in full_regions}
        out["removal"] = {
            "loudness_drop_db": removal, "loudness_method": method, "silent_without": lw is None,
            "added_by_region_db": {k: _db_ratio(full_regions[k], without_regions[k]) for k in full_regions
                                   if full_regions[k] > 0},
            "added_energy_by_region": _fractions(added),
        }
        if repeat.get("deterministic"):
            residual_level = loudness(full - without, rate)[0]
            out["removal"]["residual_level_re_full_db"] = (None if residual_level is None or lf is None
                                                           else round(residual_level - lf, 2))
        if lw is not None and lf is not None:
            gain = 10 ** ((lf - lw) / 20)
            matched = without * gain
            bands_m, bands_f = band_energies(matched, rate), of_full("bands", lambda a: band_energies(a, rate))
            out["matched"] = {
                "gain_applied_db": round(lf - lw, 2),
                "band_change_db": {k: _db_ratio(bands_f[k], bands_m[k]) for k in bands_f
                                   if bands_f[k] + bands_m[k] > 0},
                "stereo_width_change_db": round(of_full("stereo", lambda a: stereo_summary(a, rate))["side_minus_mid_db"]
                                                - stereo_summary(matched, rate)["side_minus_mid_db"], 1),
            }
            diff = listen.diff_reports(_report(matched, rate), of_full("report", lambda a: _report(a, rate)))
            if diff.get("available"):
                floor = repeat.get("repeat_noticeable_steps", 0.0)
                rows = [r for r in diff["significant_changes"] if abs(r["noticeable_steps"]) > max(floor, 1.0)
                        and (r["feature"] in ROBUST_FEATURES or r["feature"].startswith("band_"))]
                out["matched"].update(
                    summary_with_target=[p for p in diff["summary"] if p.split(": ")[-1].split(" ")[0] in
                                         ROBUST_FEATURES][:6],
                    max_noticeable_steps=round(max((abs(r["noticeable_steps"]) for r in rows), default=0.0), 2),
                    changes=[{k: r[k] for k in ("feature", "before", "after", "noticeable_steps")} for r in rows[:10]],
                    direction="before = without target (loudness matched), after = full mix")
        else:
            out["matched"] = {"note": "The mix is silent without this target; it carries the whole sound"}
        if repeat.get("deterministic") and solo is not None:
            residual = full - without
            out["summing"] = {
                "residual_vs_solo_db": _db_ratio(float(np.sum((residual - solo) ** 2)), float(np.sum(solo**2))),
                "definition": "(full - without) vs solo; near -inf means the target simply adds, above -20 dB "
                              "means interaction (shared compressor/saturation, voice stealing, polyphony limits)"}
        rest = without
        target = solo if solo is not None else (full - without if repeat.get("deterministic") else None)
        if target is not None:
            out["exposure"] = exposure(target, rest, full, rate, windows,
                                       full_power=of_full("tf_power", lambda a: tf_power(a, rate)))
    out["roles"], out["status"], out["evidence"] = classify(out, repeat)
    return out


def classify(item: dict, repeat: dict | None = None) -> tuple[list[str], str, list[str]]:
    """Role words (body/transient/space/stereo_width/noise/brightness/low_end/level_only/masks_others) + status."""
    roles, evidence = [], []
    solo, removal = item.get("solo") or {}, item.get("removal") or {}
    if solo and solo.get("loudness") is None:
        # No roles for a silent solo: its zero side/mid and missing correlation are not "wide".
        return [], "silent", [("solo render is silent: check mapping, mute, routing, velocity range or "
                               "whether the phrase triggers it (release/pedal-up samples)")]
    matched, expo = item.get("matched") or {}, item.get("exposure") or {}
    share = solo.get("share_by_region_db") or {}
    added = removal.get("added_by_region_db") or {}
    # own: how the target's energy (solo, else what it adds) spreads over attack/held/tail windows.
    # emphasis: that spread relative to the full mix's spread; >= 2 means it leans on that region.
    own = solo.get("energy_by_region") or removal.get("added_energy_by_region") or {}
    mix_spread = item.get("full_energy_by_region") or {}
    emphasis = {k: own[k] / mix_spread[k] for k in own if mix_spread.get(k)}
    if emphasis.get("attack", 0) >= 2 and own.get("attack", 0) >= 0.25:
        roles.append("transient")
        evidence.append(f"transient: {own['attack']:.0%} of its energy is in attack windows "
                        f"({emphasis['attack']:.1f}x the mix's share)")
    if emphasis.get("tail", 0) >= 2 and own.get("tail", 0) >= 0.05:
        roles.append("space")
        evidence.append(f"space: {own['tail']:.0%} of its energy is in tails after release "
                        f"({emphasis['tail']:.1f}x the mix's share)")
    held_share, held_added = share.get("held"), added.get("held")
    if own.get("held", 0) >= 0.3 and ((held_share is not None and held_share >= -6) or
                                       (not share and held_added is not None and held_added >= 3)):
        roles.append("body")
        evidence.append(f"body: {own['held']:.0%} of its energy is under held notes, "
                        + (f"{held_share:+.1f} dB of the mix there" if held_share is not None
                           else f"adding {held_added:+.1f} dB there"))
    width = (solo.get("stereo") or {}).get("side_minus_mid_db")
    if solo and width is not None and width > -6:
        roles.append("stereo_width")
        evidence.append(f"stereo_width: target is wide on its own (side-mid {width:+.1f} dB)")
    widened = matched.get("stereo_width_change_db")
    if widened is not None and widened >= 6 and "stereo_width" not in roles:
        roles.append("stereo_width")
        evidence.append(f"stereo_width: the mix is {widened:.1f} dB wider (side-mid) with it, loudness matched")
    flat = solo.get("flatness")
    if flat is not None and flat >= 0.25:
        roles.append("noise")
        evidence.append(f"noise: spectral flatness {flat:.2f} (>=0.25 is noise-like)")
    bands = solo.get("energy_by_band") or {}
    if bands:
        high = sum(bands.get(b, 0) for b in ("presence", "brilliance", "air"))
        low = sum(bands.get(b, 0) for b in ("sub", "bass"))
        if high >= 0.4:
            roles.append("brightness")
            evidence.append(f"brightness: {high:.0%} of its energy is above 4 kHz")
        if low >= 0.6:
            roles.append("low_end")
            evidence.append(f"low_end: {low:.0%} of its energy is below 250 Hz")
    level = solo.get("level_re_full_db", removal.get("residual_level_re_full_db"))
    drop = removal.get("loudness_drop_db")
    steps = matched.get("max_noticeable_steps")
    exposed = expo.get("exposed_energy_fraction")
    covers = expo.get("covers_rest_fraction")
    if removal.get("silent_without"):
        return roles or ["body"], "carries_everything", evidence + ["the mix is silent without this target"]
    if level is not None and level < INAUDIBLE_RE_FULL_DB:
        return roles, "inaudible", evidence + [f"solo is {level:+.1f} dB re full mix"]
    quiet_change = drop is not None and abs(drop) < 0.5 and (steps is None or steps < 1)
    if quiet_change and (exposed is None or exposed < 0.05):
        status = "inaudible"
        evidence.append(f"removing it changes loudness {drop:+.2f} dB and no feature by >=1 step after matching")
    elif exposed is not None and exposed < MASKED_EXPOSURE:
        # Any target with level above the inaudible floor but little exposed energy is masked, however
        # quiet; a lower exposure must never read as "more audible" than a higher one.
        status = "masked"
        evidence.append(f"only {exposed:.0%} of its energy is exposed above the rest of the mix"
                        + (f" (solo {level:+.1f} dB re full)" if level is not None else ""))
    elif drop is not None and drop >= 1 and steps is not None and steps < 1:
        status = "audible"
        roles.append("level_only")
        evidence.append(f"removing it drops loudness {drop:.1f} dB but changes no timbre feature by >=1 step "
                        "after loudness matching (doubling/level)")
    else:
        status = "audible"
    if covers is not None and covers >= 0.5:
        roles.append("masks_others")
        evidence.append(f"it sits 6 dB above the rest in cells holding {covers:.0%} of the rest's energy")
    if status == "audible" and steps is not None and steps >= 1:
        evidence.append(f"after loudness matching the largest feature change is {steps:.1f} steps")
    if repeat and not repeat.get("deterministic"):
        evidence.append("renders are not repeatable; treat small differences as noise")
    return list(dict.fromkeys(roles)), status, evidence


def stereo_origin(full: np.ndarray, rate: int, dry: np.ndarray | None = None, no_space: np.ndarray | None = None,
                  solos: dict[str, np.ndarray] | None = None, windows: list[dict] | None = None) -> dict:
    """Where the width comes from: the instrument (dry), space effects/sends, or other inserts."""
    renders = {"full": full, "dry": dry, "no_space": no_space}
    widths = {k: stereo_summary(v, rate, windows) for k, v in renders.items() if v is not None}
    out = {"renders": widths}
    full_w = widths["full"]["side_minus_mid_db"]
    reference = widths.get("dry") or widths.get("no_space")
    if reference is not None:
        base = reference["side_minus_mid_db"]
        label = "dry" if "dry" in widths else "no_space"
        out["width_added_by_effects_db"] = round(full_w - base, 1)
        if full_w < -25:
            verdict = "near mono overall (side-mid below -25 dB)"
        elif base < -25:
            verdict = f"the stereo image comes from effects: {label} render is near mono ({base:+.1f} dB)"
        elif full_w - base >= 3:
            verdict = f"mixed: the instrument is stereo ({base:+.1f} dB) and effects widen it by {full_w - base:.1f} dB"
        else:
            verdict = f"the instrument itself is stereo ({base:+.1f} dB); effects add {full_w - base:+.1f} dB"
        out["verdict"] = verdict
        if "dry" in widths and "no_space" in widths:
            out["width_from_space_fx_db"] = round(full_w - widths["no_space"]["side_minus_mid_db"], 1)
            out["width_from_other_inserts_db"] = round(widths["no_space"]["side_minus_mid_db"] - base, 1)
    if solos:
        out["targets"] = {name: {k: v for k, v in stereo_summary(audio, rate).items() if k != "by_band"}
                          for name, audio in solos.items()}
    out["definition"] = ("side_minus_mid_db: side/mid power; below -25 dB is effectively mono, around 0 dB very wide. "
                         "left_minus_right_db: positive leans left")
    return out


def ascii_table(rows: list[dict]) -> str:
    """One line per target: solo level, loudness drop when removed, region shares, exposure, status."""
    head = f"{'target':<22}{'solo':>7}{'remove':>8}{'atk':>6}{'held':>6}{'tail':>6}{'expo':>6}  status/roles"
    lines = [head, "-" * len(head)]

    def num(value, width, digits=1):
        # Always one separating space; values below -99.9 dB (near silence) print as "<-99".
        if value is None:
            text = "n/a"
        elif value < -99.9:
            text = "<-99"
        else:
            text = f"{value:.{digits}f}"
        return " " + text.rjust(width - 1)

    for row in rows:
        c = row["contribution"]
        solo, removal, expo = c.get("solo") or {}, c.get("removal") or {}, c.get("exposure") or {}
        profile = solo.get("share_by_region_db") or removal.get("added_by_region_db") or {}
        drop = removal.get("loudness_drop_db")
        bar = "" if expo.get("exposed_energy_fraction") is None else "#" * round(10 * expo["exposed_energy_fraction"])
        lines.append(f"{row['name'][:21]:<22}{num(solo.get('level_re_full_db'), 7)}"
                     f"{'all'.rjust(8) if removal.get('silent_without') else num(drop, 8)}"
                     f"{num(profile.get('attack'), 6)}{num(profile.get('held'), 6)}{num(profile.get('tail'), 6)}"
                     f"{num(expo.get('exposed_energy_fraction'), 6, 2)}  {c['status']}"
                     f"{(' ' + ','.join(c['roles'])) if c['roles'] else ''} {bar}")
    lines.append("solo: dB re full; remove: loudness drop when muted; atk/held/tail: dB share of that region; "
                 "expo: exposed energy fraction")
    return "\n".join(lines)


def is_space_fx(node_type: str) -> bool:
    return any(word in node_type for word in SPACE_FX_WORDS)
