"""Text-first analysis of performance renders: re-strikes, legato joins, pedal, chords, voices and controllers.

All checks compare parts of the same render with each other (a clean first hit, a control note, a dry
section), which cancels most of the instrument's own character. Thresholds are heuristics for steering
edits, not perceptual verdicts.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal

from . import listen
from .perform import note_windows, pedal_windows

FLOOR_DB = -100.0
SILENT_DBFS = -80.0
CLICK_EXCESS_DB = 6.0
RELEASE_CLICK_DB = 12.0
PEDAL_EFFECT_DB = 6.0
CUT_DROP_DB = 10.0
STEAL_DROP_DB = 12.0
RAMP = " .:-=+*#%@"


def _round(value, digits=1):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


class Signal:
    """Shared envelopes for one render."""

    def __init__(self, audio: np.ndarray, rate: int):
        self.audio = audio.astype(np.float64)
        if self.audio.ndim == 1:
            self.audio = self.audio[:, None]
        self.rate = rate
        self.duration = len(self.audio) / rate
        self.mono = listen.analysis_mono(self.audio)
        self.t, self.env = listen.fine_envelope(self.audio, rate, window_ms=5.0, hop_ms=1.0)
        self.env = np.maximum(self.env, FLOOR_DB)
        cutoff = min(6000.0, rate * 0.4)
        high = signal.sosfilt(signal.butter(4, cutoff, "highpass", fs=rate, output="sos"), self.audio, axis=0)
        self.ht, self.henv = listen.fine_envelope(high, rate, window_ms=1.0, hop_ms=0.5)
        self.henv = np.maximum(self.henv, FLOOR_DB - 20)
        self.peak_db = listen.db(float(np.max(np.abs(self.audio)))) if self.audio.size else FLOOR_DB

    def _span(self, times, values, t0, t1, how):
        mask = (times >= t0) & (times <= t1)
        if not mask.any():
            return None
        chunk = values[mask]
        if how == "max":
            return float(chunk.max())
        if how == "min":
            return float(chunk.min())
        return float(10 * np.log10(np.mean(10 ** (chunk / 10))))  # power mean

    def level(self, t0, t1, how="mean"):
        return self._span(self.t, self.env, t0, t1, how)

    def hf(self, t0, t1, how="max"):
        return self._span(self.ht, self.henv, t0, t1, how)

    def onset_burst(self, t):
        """HF peak in the first 8 ms after t (dBFS)."""
        return self.hf(t - 0.001, t + 0.008)

    def transient(self, t):
        """HF burst at t above both the HF just before and the settled HF just after (dB).

        A click stands out from both neighbours; an attack that simply adds steady brightness does not.
        """
        burst = self.onset_burst(t)
        before = self.hf(t - 0.03, t - 0.005, "mean")
        after = self.hf(t + 0.02, t + 0.05, "mean")
        if burst is None:
            return None
        return burst - max(before if before is not None else FLOOR_DB, after if after is not None else FLOOR_DB)

    def rise(self, t, window=0.05):
        before = self.level(t - 0.03, t - 0.003, "mean")
        after = self.level(t, t + window, "max")
        return None if before is None or after is None else after - before

    def centroid(self, t0, t1):
        a, b = int(max(0, t0) * self.rate), int(min(self.duration, t1) * self.rate)
        frame = self.mono[a:b]
        if len(frame) < 256 or float(np.mean(frame**2)) < 1e-12:
            return None
        power = np.abs(np.fft.rfft(frame * np.hanning(len(frame)))) ** 2
        freqs = np.fft.rfftfreq(len(frame), 1 / self.rate)
        return float((power * freqs).sum() / max(power.sum(), 1e-30))

    def f0(self, t0, t1):
        a, b = int(max(0, t0) * self.rate), int(min(self.duration, t1) * self.rate)
        _, f0, aperiodicity, level = listen.yin_track(self.mono[a:b], self.rate, hop_seconds=0.01)
        good = (aperiodicity < 0.3) & (level > -70)
        return float(np.median(f0[good])) if good.any() else None

    def band_share(self, t0, t1, low, high):
        a, b = int(max(0, t0) * self.rate), int(min(self.duration, t1) * self.rate)
        frame = self.mono[a:b]
        if len(frame) < 512:
            return None
        power = np.abs(np.fft.rfft(frame * np.hanning(len(frame)))) ** 2
        freqs = np.fft.rfftfreq(len(frame), 1 / self.rate)
        total = float(power[freqs > 20].sum())
        return None if total < 1e-20 else 100 * float(power[(freqs >= low) & (freqs < high)].sum()) / total

    def partial_track(self, f0, t0, t1, hop=0.02):
        """dB magnitude at f0 (+/-3 %) over time, from 8192-point frames."""
        size = 8192 if self.rate <= 48000 else 16384
        freqs = np.fft.rfftfreq(size, 1 / self.rate)
        band = (freqs > f0 * 0.97) & (freqs < f0 * 1.03)
        window = np.hanning(size)
        times, values = [], []
        t = t0
        while t <= t1:
            a = int(t * self.rate - size / 2)
            if a >= 0 and a + size <= len(self.mono):
                spectrum = np.abs(np.fft.rfft(self.mono[a:a + size] * window))
                values.append(20 * math.log10(max(float(spectrum[band].max()) / (size / 4), 1e-12)))
                times.append(t)
            t += hop
        return np.array(times), np.array(values)

    def spectrum_peaks(self, t0, t1, freqs_hz):
        """Prominence (dB) of a peak at each frequency over its local floor."""
        a, b = int(max(0, t0) * self.rate), int(min(self.duration, t1) * self.rate)
        frame = self.mono[a:b]
        size = max(len(frame), 1)
        power = 20 * np.log10(np.abs(np.fft.rfft(frame * np.hanning(size))) + 1e-12)
        freqs = np.fft.rfftfreq(size, 1 / self.rate)
        out, bin_hz = [], self.rate / size
        for f in freqs_hz:
            # A semitone neighbour sits 5.9 % away, so the floor ring stays inside +/-4.5 %.
            half = max(0.01 * f, 1.5 * bin_hz)
            inner, outer = max(0.025 * f, half + bin_hz), max(0.045 * f, half + 3 * bin_hz)
            distance = np.abs(freqs - f)
            peak = power[distance <= half]
            floor = power[(distance >= inner) & (distance <= outer)]
            out.append(float(peak.max() - np.median(floor)) if len(peak) and len(floor) else None)
        return out


def check(name, status, detail, **evidence):
    return {"check": name, "status": status, "detail": detail,
            **{k: (_round(v) if isinstance(v, float) else v) for k, v in evidence.items()}}


def _section(sections, name):
    return next(s for s in sections if s["name"] == name)


def _notes_in(events, name):
    return [n for n in note_windows(events) if events[n["index"]].get("section") == name]


# --------------------------------------------------------------------------- phrase analyzers

def analyze_retrigger(sig: Signal, events, sections):
    rows, checks, spacing = [], [], {}
    hits = note_windows(events)
    reference_burst = sig.transient(hits[0]["on"])
    reference_peak = sig.level(hits[0]["on"], hits[0]["on"] + 0.05, "max")
    for section in sections:
        notes = _notes_in(events, section["name"])
        spacing[section["name"]] = notes[1]["on"] - notes[0]["on"] if len(notes) > 1 else 1.0
        first_peak = None
        for k, n in enumerate(notes):
            window = min(0.05, max(0.01, (notes[k + 1]["on"] - n["on"]) * 0.8)) if k + 1 < len(notes) else 0.05
            peak = sig.level(n["on"], n["on"] + window, "max")
            first_peak = peak if k == 0 else first_peak
            before = sig.level(n["on"] - 0.015, n["on"] - 0.008, "mean")
            dip = sig.level(n["on"] - 0.004, n["on"] + 0.001, "min")
            burst = sig.transient(n["on"])
            rows.append({"section": section["name"], "hit": k + 1, "time": _round(n["on"], 3),
                         "peak_dbfs": _round(peak), "rise_db": _round(sig.rise(n["on"], window)),
                         "level_vs_first_db": _round(peak - first_peak if peak is not None else None),
                         "click_vs_clean_db": _round(burst - reference_burst
                                                        if burst is not None and reference_burst is not None else None),
                         "pre_strike_drop_db": _round(before - dip if k and before is not None and dip is not None
                                                      else None)})
    silent = [r for r in rows if r["peak_dbfs"] is None or r["peak_dbfs"] < SILENT_DBFS]
    # Below ~120 ms a long release legitimately blurs repeats; those rates are reported, not judged.
    missed = [r for r in rows if r["hit"] > 1 and spacing[r["section"]] >= 0.12 and
              (r["rise_db"] is None or r["rise_db"] < 1.5)]
    by_rate = {}
    for r in rows:
        if r["hit"] > 1 and r["rise_db"] is not None:
            by_rate.setdefault(r["section"], []).append(r["rise_db"])
    quiet = [r for r in rows if r["level_vs_first_db"] is not None and r["level_vs_first_db"] < -6]
    clicks = [r for r in rows if r["hit"] > 1 and (r["click_vs_clean_db"] or -99) > CLICK_EXCESS_DB]
    choke = [r for r in rows if (r["pre_strike_drop_db"] or 0) > CUT_DROP_DB]
    checks.append(check("repeats_sound", "fail" if silent else "ok",
                        f"{len(silent)} of {len(rows)} hits silent" if silent else "every hit produced sound",
                        hits=[f"{r['section']}#{r['hit']}" for r in silent][:12]))
    checks.append(check("repeats_reattack", "warn" if missed else "ok",
                        "no audible re-attack (<1.5 dB rise) on: " + ", ".join(
                            f"{r['section']}#{r['hit']}" for r in missed[:8]) if missed else
                        "each repeat at >=120 ms spacing re-attacks (>=1.5 dB rise)",
                        mean_rise_db_by_group={k: _round(float(np.mean(v))) for k, v in by_rate.items()}))
    checks.append(check("repeat_level", "warn" if quiet else "ok",
                        "repeats more than 6 dB below the first hit of their group: " + ", ".join(
                            f"{r['section']}#{r['hit']} ({r['level_vs_first_db']} dB)" for r in quiet[:8])
                        if quiet else "repeat levels within 6 dB of each group's first hit "
                                      "(round-robin/voice stealing would show here)"))
    checks.append(check("retrigger_clicks", "warn" if clicks else "ok",
                        "transient HF burst >6 dB above the clean first strike's at: " + ", ".join(
                            f"{r['section']}#{r['hit']} (+{r['click_vs_clean_db']} dB)" for r in clicks[:8])
                        if clicks else "no re-strike burst above the clean first strike + 6 dB",
                        reference_peak_dbfs=reference_peak))
    checks.append(check("tail_cut_before_restrike", "warn" if choke else "ok",
                        "level drops >10 dB in the 5 ms before a re-strike (voice choke/steal): " + ", ".join(
                            f"{r['section']}#{r['hit']} ({r['pre_strike_drop_db']} dB)" for r in choke[:8])
                        if choke else "no abrupt drop right before re-strikes"))
    return checks, rows


def analyze_legato(sig: Signal, events, sections):
    rows, summary = [], {}
    for section in sections:
        notes = _notes_in(events, section["name"])
        burst0 = sig.transient(notes[0]["on"])
        rises, offsets = [], []
        for k, n in enumerate(notes):
            target = listen.midi_to_hz(n["note"])
            early = sig.f0(n["on"] + 0.005, n["on"] + 0.05)
            mid = sig.f0(n["on"] + 0.1, n["off"] - 0.02) if n["off"] - n["on"] > 0.15 else None
            row = {"section": section["name"], "note": listen.note_name(n["note"]), "time": _round(n["on"], 3),
                   "rise_db": _round(sig.rise(n["on"])),
                   "join_dip_db": _round((sig.level(n["on"] - 0.08, n["on"] - 0.03) or FLOOR_DB) -
                                         (sig.level(n["on"] - 0.02, n["on"] + 0.03, "min") or FLOOR_DB)) if k else None,
                   "click_vs_first_db": _round((sig.transient(n["on"]) or FLOOR_DB) - (burst0 or FLOOR_DB)),
                   "early_cents": _round(1200 * math.log2(early / target), 0) if early else None,
                   "cents": _round(1200 * math.log2(mid / target), 0) if mid else None}
            rows.append(row)
            if k:
                rises.append(row["rise_db"] or 0)
                if row["early_cents"] is not None:
                    offsets.append(abs(row["early_cents"]))
        summary[section["name"]] = {"mean_rise_db": float(np.mean(rises)) if rises else None,
                                    "mean_early_pitch_offset_cents": float(np.mean(offsets)) if offsets else None}
    legato, detached = (summary.get("legato_overlap_40ms", {}), summary.get("detached_gap_100ms", {}))
    joined = [r for r in rows if r["section"].startswith("legato")]
    dips = [r for r in joined if (r["join_dip_db"] or 0) > 6]
    clicks = [r for r in rows if (r["click_vs_first_db"] or -99) > CLICK_EXCESS_DB]
    detuned = [r for r in rows if r["cents"] is not None and abs(r["cents"]) > 20]
    checks = [check("legato_joins", "warn" if dips else "ok",
                    "overlapped notes dip >6 dB at the join: " + ", ".join(f"{r['note']}@{r['time']}" for r in dips)
                    if dips else "no level gap at overlapped joins"),
              check("legato_mode", "info",
                    ("overlapped notes re-attack about as strongly as detached ones; no legato/mono behaviour "
                     "detected" if (legato.get("mean_rise_db") or 0) > 0.7 * (detached.get("mean_rise_db") or 0)
                     else "overlapped notes re-attack noticeably less than detached ones (legato-style join)"),
                    legato_mean_rise_db=legato.get("mean_rise_db"), detached_mean_rise_db=detached.get("mean_rise_db"),
                    legato_early_pitch_offset_cents=legato.get("mean_early_pitch_offset_cents"),
                    detached_early_pitch_offset_cents=detached.get("mean_early_pitch_offset_cents")),
              check("transition_clicks", "warn" if clicks else "ok",
                    "transient HF burst >6 dB above the first note's at: " + ", ".join(
                        f"{r['note']}@{r['time']}" for r in clicks[:8]) if clicks
                    else "no join has a sharper HF transient than the first onset + 6 dB"),
              check("scale_pitch", "warn" if detuned else "ok",
                    "notes more than 20 cents off: " + ", ".join(f"{r['note']} ({r['cents']:+.0f})" for r in detuned[:8])
                    if detuned else "scale notes within 20 cents where pitch was measurable")]
    return checks, rows


def analyze_sustain_pedal(sig: Signal, events, sections):
    hold, dry, held = (_section(sections, n) for n in ("pedal_hold", "no_pedal", "pedal_up_while_held"))
    after = (0.3, 0.6)
    ped = sig.level(hold["key_up"] + after[0], hold["key_up"] + after[1])
    nop = sig.level(dry["key_up"] + after[0], dry["key_up"] + after[1])
    gain = (ped or FLOOR_DB) - (nop or FLOOR_DB)
    before_up = sig.level(hold["pedal_up"] - 0.1, hold["pedal_up"] - 0.02)
    after_up = sig.level(hold["pedal_up"] + 0.35, hold["pedal_up"] + 0.5)
    release_drop = (before_up or FLOOR_DB) - (after_up or FLOOR_DB)
    dry_before = sig.level(hold["start"] + (hold["pedal_up"] - hold["start"]) - 0.1 + (dry["start"] - hold["start"]),
                           dry["start"] + (hold["pedal_up"] - hold["start"]) - 0.02)
    held_before = sig.level(held["pedal_up"] - 0.3, held["pedal_up"] - 0.05)
    held_after = sig.level(held["pedal_up"] + 0.2, held["pedal_up"] + 0.5)
    held_drop = (held_before or FLOOR_DB) - (held_after or FLOOR_DB)
    pedal_click = (sig.hf(hold["pedal_up"] - 0.001, hold["pedal_up"] + 0.01) or FLOOR_DB) - \
                  (sig.hf(hold["pedal_up"] - 0.08, hold["pedal_up"] - 0.02, "mean") or FLOOR_DB)
    rows = [{"measure": "level 0.3-0.6 s after key-up", "pedal_dbfs": _round(ped), "no_pedal_dbfs": _round(nop)},
            {"measure": "level before/after pedal-up (keys already up)", "before_dbfs": _round(before_up),
             "after_dbfs": _round(after_up), "no_pedal_same_time_dbfs": _round(dry_before)},
            {"measure": "held keys before/after pedal-up", "before_dbfs": _round(held_before),
             "after_dbfs": _round(held_after)}]
    checks = []
    if (ped or FLOOR_DB) < SILENT_DBFS:
        checks.append(check("pedal_sustains", "fail", "sound stops at key-up even with CC64 down",
                            pedal_dbfs=ped, no_pedal_dbfs=nop))
    else:
        checks.append(check("pedal_sustains", "ok" if gain >= PEDAL_EFFECT_DB else "warn",
                            f"pedal keeps the chord {gain:.1f} dB louder 0.3-0.6 s after key-up than without"
                            if gain >= PEDAL_EFFECT_DB else
                            f"pedal adds only {gain:.1f} dB after key-up (CC64 ignored, or the release is already long)",
                            pedal_dbfs=ped, no_pedal_dbfs=nop))
    if (before_up or FLOOR_DB) > SILENT_DBFS:
        checks.append(check("pedal_up_releases", "ok" if release_drop >= CUT_DROP_DB else "warn",
                            f"level falls {release_drop:.1f} dB within 0.5 s of pedal-up" if release_drop >= CUT_DROP_DB
                            else f"only {release_drop:.1f} dB drop after pedal-up (long release, reverb tail or pedal "
                                 "latched)", drop_db=release_drop))
    checks.append(check("held_keys_survive_pedal_up", "fail" if held_drop > CUT_DROP_DB else "ok",
                        f"pedal-up cut keys that were still held ({held_drop:.1f} dB drop)" if held_drop > CUT_DROP_DB
                        else f"held keys keep sounding after pedal-up ({held_drop:.1f} dB change)", drop_db=held_drop))
    checks.append(check("pedal_up_click", "warn" if pedal_click > RELEASE_CLICK_DB else "ok",
                        f"HF burst {pedal_click:.1f} dB at pedal-up" if pedal_click > RELEASE_CLICK_DB
                        else "no HF burst at pedal-up", excess_db=pedal_click))
    return checks, rows


def analyze_pedal_wash(sig: Signal, events, sections):
    rows, stats = [], {}
    for section in sections:
        notes = _notes_in(events, section["name"])
        contrasts = [sig.rise(n["on"], 0.04) for n in notes]
        contrasts = [c for c in contrasts if c is not None]
        first = sig.level(notes[0]["on"], notes[0]["on"] + 0.1)
        last_on = notes[-1]["on"]
        late = sig.level(last_on - 0.2, last_on)
        stats[section["name"]] = {
            "mean_onset_contrast_db": float(np.mean(contrasts)) if contrasts else None,
            "min_onset_contrast_db": float(np.min(contrasts)) if contrasts else None,
            "build_up_db": (late or FLOOR_DB) - (first or FLOOR_DB),
            "low_share_percent": sig.band_share(last_on - 0.5, last_on, 20, 250),
            "low_mid_share_percent": sig.band_share(last_on - 0.5, last_on, 250, 800),
            "centroid_hz": sig.centroid(last_on - 0.5, last_on)}
        rows.append({"section": section["name"], **{k: _round(v) for k, v in stats[section["name"]].items()}})
    ped, dry = stats["pedal"], stats["dry"]
    contrast_loss = (dry["mean_onset_contrast_db"] or 0) - (ped["mean_onset_contrast_db"] or 0)
    mud = ((ped["low_share_percent"] or 0) + (ped["low_mid_share_percent"] or 0)) - \
          ((dry["low_share_percent"] or 0) + (dry["low_mid_share_percent"] or 0))
    checks = [check("onsets_under_pedal", "warn" if (ped["mean_onset_contrast_db"] or 0) < 3 else "ok",
                    f"notes under pedal rise {ped['mean_onset_contrast_db'] or 0:.1f} dB above the wash on average "
                    f"(dry {dry['mean_onset_contrast_db'] or 0:.1f} dB; loss {contrast_loss:.1f} dB)"),
              check("pedal_build_up", "warn" if ped["build_up_db"] > 12 else "info",
                    f"level grows {ped['build_up_db']:.1f} dB across the pedalled scale (dry {dry['build_up_db']:.1f} dB)"),
              check("low_mid_build_up", "info",
                    f"below-800 Hz share {mud:+.1f} percentage points under pedal vs dry at the end of the scale "
                    "(some build-up is what a pedal does; compare instruments or versions with it)")]
    return checks, rows


def analyze_dense_chords(sig: Signal, events, sections):
    rows, smaller = [], {}
    single = None
    for section in sections:
        notes = section["notes"]
        peak = sig.level(section["start"], section["start"] + 0.9, "max")
        body = sig.level(section["start"] + 0.05, section["start"] + 0.5)
        freqs = [listen.midi_to_hz(n) for n in notes]
        checkable = [i for i, f in enumerate(freqs) if 30 < f < sig.rate * 0.4 and not any(
            abs(f / (g * k) - 1) < 0.02 for g in freqs if g < f for k in range(1, 9))]
        prominence = sig.spectrum_peaks(section["start"] + 0.05, section["start"] + 0.85, freqs)
        missing = [listen.note_name(notes[i]) for i in checkable if (prominence[i] or 0) < 6]
        size = len(notes)
        single = body if size == 1 else single
        rows.append({"section": section["name"], "notes": size, "peak_dbfs": _round(peak),
                     "body_dbfs": _round(body),
                     "growth_vs_single_db": _round(body - single if body is not None and single is not None else None),
                     "uncorrelated_growth_db": _round(10 * math.log10(size)),
                     "checked_fundamentals": len(checkable), "missing_fundamentals": missing,
                     "quieter_than_smaller_chord": bool(body is not None and section["name"] != "cluster_12" and any(
                         size > s and body < level - 1 for s, level in smaller.items()))})
        if body is not None:
            smaller[size] = max(body, smaller.get(size, FLOOR_DB))
    clipped = [r for r in rows if (r["peak_dbfs"] or FLOOR_DB) > -0.1]
    drops = [r for r in rows if r["quieter_than_smaller_chord"]]
    missing = [r for r in rows if r["missing_fundamentals"]]
    checks = [check("chord_clipping", "fail" if clipped else "ok",
                    "peaks reach 0 dBFS in: " + ", ".join(r["section"] for r in clipped) if clipped
                    else f"loudest chord peak {max((r['peak_dbfs'] or FLOOR_DB) for r in rows):.1f} dBFS"),
              check("chord_level_growth", "warn" if drops else "ok",
                    "adding notes made the chord quieter (voice limit, stealing or a limiter): " + ", ".join(
                        r["section"] for r in drops) if drops else "level grows or holds as chords get denser"),
              check("chord_notes_present", "warn" if missing else "ok",
                    "; ".join(f"{r['section']}: no peak at {', '.join(r['missing_fundamentals'])}" for r in missing)
                    if missing else "every checkable fundamental shows a spectral peak",
                    note="fundamentals that coincide with a lower note's harmonic are not checkable")]
    return checks, rows


def analyze_polyphony(sig: Signal, events, sections):
    alone, stacked = _section(sections, "probe_alone"), _section(sections, "probe_with_stack")
    f0 = listen.midi_to_hz(alone["probe"])
    length = alone["end"] - alone["start"]
    ta, va = sig.partial_track(f0, alone["start"] + 0.05, alone["end"])
    _tb, vb = sig.partial_track(f0, stacked["start"] + 0.05, stacked["end"])
    count = min(len(va), len(vb))
    diff = vb[:count] - va[:count]
    rel = ta[:count] - alone["start"]
    stolen_at, alive_until = None, None
    audible = np.where(va[:count] > max(va[:count].max() - 40, -90))[0] if count else []
    if len(audible):
        alive_until = float(rel[audible[-1]])
    steps = max(1, int(0.1 / 0.02))
    for i in range(count - steps):
        if np.all(diff[i:i + steps] < -STEAL_DROP_DB) and va[i] > va[:count].max() - 40:
            stolen_at = float(rel[i])
            break
    added = None
    if stolen_at is not None:
        added = int(max(0, math.floor((stolen_at - (stacked["stack_start"] - stacked["start"])) / stacked["step"]) + 1))
        added = min(added, len(stacked["stack"]))
    rows = [{"time_after_probe": _round(float(rel[i]), 2), "probe_alone_db": _round(float(va[i])),
             "probe_with_stack_db": _round(float(vb[i])), "difference_db": _round(float(diff[i]))}
            for i in range(0, count, max(1, count // 16))]
    tested_keys = len(stacked["stack"]) + 1
    if stolen_at is not None:
        result = check("voice_stealing", "warn",
                       f"probe partial fell >{STEAL_DROP_DB:.0f} dB below the solo probe {stolen_at:.2f} s after onset, "
                       f"when about key #{added + 1} was pressed: roughly {added} keys sound at once before stealing "
                       "(+/-1 key; one key can use several voices when layers stack)", keys_before_steal=added)
    else:
        result = check("voice_stealing", "ok" if alive_until and alive_until > length * 0.8 else "info",
                       f"probe survived all {tested_keys} held keys" if alive_until and alive_until > length * 0.8 else
                       f"probe decayed naturally by {alive_until or 0:.2f} s; stealing beyond that was not testable",
                       held_keys_tested=tested_keys)
    return [result], rows


def analyze_cross_channel(sig: Signal, events, sections):
    rows = []
    for section in sections:
        rows.append({"section": section["name"], "channel": section.get("channel"),
                     "peak_dbfs": _round(sig.level(section["start"], section["start"] + 0.6, "max")),
                     "body_dbfs": _round(sig.level(section["start"] + 0.02, section["start"] + 0.5))})
    loud = max((r["peak_dbfs"] or FLOOR_DB) for r in rows)
    silent = [r for r in rows if r["channel"] and (r["peak_dbfs"] or FLOOR_DB) < max(SILENT_DBFS, loud - 40)]
    sounding = [r["channel"] for r in rows if r["channel"] and r not in silent]
    return [check("channels_sounding", "info" if silent else "ok",
                  f"channels {sounding} sound; {[r['channel'] for r in silent]} are silent (no Part listens there)"
                  if silent else f"all tested channels sound: {sounding}",
                  sounding=sounding, silent=[r["channel"] for r in silent])], rows


def analyze_expression(sig: Signal, events, sections):
    control = _section(sections, "control")
    length = control["end"] - control["start"]
    root = _notes_in(events, "control")[0]["note"]
    rows, checks = [], []
    offsets = np.arange(0.15, length - 0.05, 0.15)
    for section in sections[1:]:
        series = []
        for off in offsets:
            a0, b0 = control["start"] + off - 0.05, control["start"] + off + 0.05
            a1, b1 = section["start"] + off - 0.05, section["start"] + off + 0.05
            la, lb = sig.level(a0, b0), sig.level(a1, b1)
            ca, cb = sig.centroid(a0, b0), sig.centroid(a1, b1)
            series.append({"t": _round(float(off), 2), "level_db": _round((lb or FLOOR_DB) - (la or FLOOR_DB)),
                           "centroid_ratio": _round(cb / ca, 2) if ca and cb else None})
        level_span = max(abs(s["level_db"] or 0) for s in series)
        bright_span = max(abs((s["centroid_ratio"] or 1) - 1) for s in series)
        row = {"section": section["name"], "max_level_change_db": _round(level_span),
               "max_brightness_change_percent": _round(100 * bright_span), "series": series}
        name = section["name"]
        if name == "pitch_bend":
            ref = sig.f0(control["start"] + 0.95, control["start"] + 1.25) or listen.midi_to_hz(root)
            up = sig.f0(section["start"] + 0.95, section["start"] + 1.25)
            down = sig.f0(section["start"] + 1.95, section["start"] + 2.25)
            up_st = 12 * math.log2(up / ref) if up else None
            down_st = 12 * math.log2(down / ref) if down else None
            row.update(bend_up_semitones=_round(up_st), bend_down_semitones=_round(down_st))
            if up_st is None and down_st is None:
                checks.append(check("pitch_bend_range", "unknown", "pitch not measurable (noisy or polyphonic tone)"))
            else:
                moved = max(abs(up_st or 0), abs(down_st or 0)) > 0.15
                checks.append(check("pitch_bend_range", "ok" if moved else "warn",
                                    f"full bend moves +{up_st or 0:.2f} / {down_st or 0:.2f} semitones" if moved
                                    else "pitch bend has no measurable effect",
                                    up_semitones=up_st, down_semitones=down_st))
        else:
            responds = level_span > 1.5 or bright_span > 0.1
            label = {"mod_wheel_cc1": "mod wheel (CC1)", "expression_cc11": "expression (CC11)",
                     "channel_pressure": "channel pressure"}[name]
            status = "ok" if responds else ("warn" if name == "expression_cc11" else "info")
            checks.append(check(f"{name}_response", status,
                                f"{label} changes level by up to {level_span:.1f} dB and brightness by "
                                f"{100 * bright_span:.0f} %" if responds else f"{label} has no measurable level/"
                                "brightness effect (may still modulate vibrato or other targets)",
                                level_db=level_span, brightness_percent=100 * bright_span))
        rows.append(row)
    return checks, rows


ANALYZERS = {"retrigger": analyze_retrigger, "legato": analyze_legato, "sustain_pedal": analyze_sustain_pedal,
             "pedal_wash": analyze_pedal_wash, "dense_chords": analyze_dense_chords, "polyphony": analyze_polyphony,
             "cross_channel": analyze_cross_channel, "expression": analyze_expression}


# --------------------------------------------------------------------------- generic timeline analysis

def analyze_timeline(sig: Signal, events) -> tuple[list[dict], list[dict]]:
    """Per-note onset/level/burst table for any timeline, plus release clicks at note-offs."""
    notes = note_windows(events)
    if not notes:
        return [], []
    burst0 = sig.transient(notes[0]["on"])
    rows = []
    for n in notes[:64]:
        peak = sig.level(n["on"], n["on"] + 0.05, "max")
        off_burst = (sig.hf(n["off"] - 0.001, n["off"] + 0.008) or FLOOR_DB) - \
                    max(sig.hf(n["off"] - 0.05, n["off"] - 0.01, "mean") or FLOOR_DB,
                        sig.hf(n["off"] + 0.02, n["off"] + 0.05, "mean") or FLOOR_DB)
        new_onset_near = any(abs(m["on"] - n["off"]) < 0.015 for m in notes)
        rows.append({"note": listen.note_name(n["note"]), "channel": n["channel"], "velocity": n["velocity"],
                     "on": _round(n["on"], 3), "off": _round(n["off"], 3), "peak_dbfs": _round(peak),
                     "rise_db": _round(sig.rise(n["on"])),
                     "click_vs_first_db": _round((sig.transient(n["on"]) or FLOOR_DB) - (burst0 or FLOOR_DB)),
                     "release_burst_db": None if new_onset_near else _round(off_burst)})
    silent = [r for r in rows if (r["peak_dbfs"] or FLOOR_DB) < SILENT_DBFS]
    no_onset = [r for r in rows if r not in silent and (r["rise_db"] is None or r["rise_db"] < 2.0)]
    clicks = [r for r in rows if (r["click_vs_first_db"] or -99) > CLICK_EXCESS_DB and r is not rows[0]]
    release = [r for r in rows if (r["release_burst_db"] or -99) > RELEASE_CLICK_DB]
    checks = [check("notes_sound", "fail" if silent else "ok",
                    f"{len(silent)} notes silent: " + ", ".join(f"{r['note']}@{r['on']}" for r in silent[:8])
                    if silent else f"all {len(rows)} analysed notes produced sound"),
              check("notes_onset", "warn" if no_onset else "ok",
                    "no audible onset (<2 dB rise) for: " + ", ".join(f"{r['note']}@{r['on']}" for r in no_onset[:8])
                    + " (unrouted channel, voice limit, or masked by louder notes)" if no_onset
                    else "every note adds an audible onset"),
              check("onset_clicks", "warn" if clicks else "ok",
                    "transient HF burst >6 dB above the first note's at: " + ", ".join(
                        f"{r['note']}@{r['on']}" for r in clicks[:8]) if clicks
                    else "no onset has a sharper HF transient than the first + 6 dB"),
              check("release_clicks", "warn" if release else "ok",
                    "HF burst >12 dB at note-off: " + ", ".join(f"{r['note']}@{r['off']}" for r in release[:8])
                    if release else "no HF burst at note-offs")]
    return checks, rows


# --------------------------------------------------------------------------- ASCII timeline

def ascii_timeline(sig: Signal, events, seconds: float, sections=None, columns: int = 72) -> str:
    step = seconds / columns
    top = sig.peak_db
    env_row, note_row, pedal_row, ctl_row = [], [" "] * columns, [" "] * columns, [" "] * columns
    for c in range(columns):
        value = sig.level(c * step, (c + 1) * step, "max")
        frac = 0 if value is None else max(0.0, min(1.0, (value - (top - 60)) / 60))
        env_row.append(RAMP[round(frac * (len(RAMP) - 1))])
    for n in note_windows(events):
        c = min(columns - 1, int(n["on"] / step))
        count = 1 if note_row[c] == " " else (10 if note_row[c] == "+" else int(note_row[c]) + 1)
        note_row[c] = "+" if count >= 10 else str(count)
    for span in pedal_windows(events, seconds):
        for c in range(int(span["down"] / step), min(columns, int((span["up"] or seconds) / step) + 1)):
            pedal_row[c] = "_"
    for e in events:
        if e["type"] in ("cc", "pitch_bend", "channel_pressure", "poly_pressure"):
            for c in range(int(e["time"] / step), min(columns, int((e.get("end") or e["time"]) / step) + 1)):
                ctl_row[c] = {"pitch_bend": "b", "channel_pressure": "p", "poly_pressure": "p"}.get(e["type"], "c")
    lines = [f"level |{''.join(env_row)}|", f"notes |{''.join(note_row)}|"]
    if any(ch != " " for ch in pedal_row):
        lines.append(f"pedal |{''.join(pedal_row)}|")
    if any(ch != " " for ch in ctl_row):
        lines.append(f"ctrl  |{''.join(ctl_row)}|")
    if sections:
        marks, legend = [" "] * columns, []
        for i, s in enumerate(sections[:26]):
            letter = chr(65 + i)
            marks[min(columns - 1, int(s["start"] / step))] = letter
            legend.append(f"{letter}={s['name']}")
        lines.append(f"sect  |{''.join(marks)}|")
        lines.append("       " + "  ".join(legend))
    lines.append(f"       0s{' ' * (columns - 8)}{seconds:.1f}s; level ramp '{RAMP}' = -60..0 dB re peak; "
                 "digits = note-ons per column; _ = sustain down; c/b/p = CC/bend/pressure")
    return "\n".join(lines)


def analyze(audio: np.ndarray, rate: int, events: list[dict], seconds: float,
            phrase_name: str | None = None, sections: list[dict] | None = None) -> dict:
    sig = Signal(audio, rate)
    if sig.peak_db < -100:
        return {"checks": [check("render_audible", "fail", "render is silent")], "table": [],
                "timeline": ascii_timeline(sig, events, seconds, sections)}
    if phrase_name in ANALYZERS:
        checks, table = ANALYZERS[phrase_name](sig, events, sections)
    else:
        checks, table = analyze_timeline(sig, events)
    peak = check("render_peak", "fail" if sig.peak_db > -0.1 else "ok", f"render peak {sig.peak_db:.1f} dBFS",
                 peak_dbfs=sig.peak_db)
    status = "fail" if any(c["status"] == "fail" for c in checks + [peak]) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "ok")
    return {"status": status, "checks": [peak] + checks, "table": table,
            "timeline": ascii_timeline(sig, events, seconds, sections)}
