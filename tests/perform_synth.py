"""Tiny MIDI-driven additive synth standing in for Falcon in performance tests."""

from __future__ import annotations

import numpy as np


class Synth:
    def __init__(self, rate=48000, polyphony=64, pedal=True, bend_range=2.0, retrigger_click=False,
                 release=0.15, decay=None, channels=(1,), cc1_brightness=True, cc11_volume=True,
                 pedal_cuts_held=False, steal_click=False):
        self.rate, self.polyphony, self.pedal, self.bend_range = rate, polyphony, pedal, bend_range
        self.retrigger_click, self.release, self.decay = retrigger_click, release, decay
        self.channels, self.cc1_brightness, self.cc11_volume = set(channels), cc1_brightness, cc11_volume
        self.pedal_cuts_held = pedal_cuts_held

    def render(self, compiled, seconds):
        rate = self.rate
        frames = int(seconds * rate)
        out = np.zeros(frames)
        voices = []  # dict(note, start, off, f0)
        bend = {}  # channel -> list of (time, semis)
        cc = {}
        pedal_spans, pedal_down = [], {}
        for time, status, d1, d2 in compiled:
            kind, ch = status & 0xF0, (status & 0x0F) + 1
            if ch not in self.channels:
                continue
            if kind == 0xE0:
                bend.setdefault(ch, []).append((time, ((d2 << 7 | d1) - 8192) / 8192 * self.bend_range))
            elif kind == 0xB0:
                cc.setdefault((ch, d1), []).append((time, d2))
                if d1 == 64 and self.pedal:
                    if d2 >= 64 and ch not in pedal_down:
                        pedal_down[ch] = time
                    elif d2 < 64 and ch in pedal_down:
                        pedal_spans.append((ch, pedal_down.pop(ch), time))
        for ch, t in pedal_down.items():
            pedal_spans.append((ch, t, seconds))
        active = []
        for time, status, d1, d2 in compiled:
            kind, ch = status & 0xF0, (status & 0x0F) + 1
            if ch not in self.channels:
                continue
            if kind == 0x90:
                held = [v for v in active if v["end"] > time]
                if len(held) >= self.polyphony:
                    oldest = min(held, key=lambda v: v["start"])
                    oldest["end"] = time
                    oldest["cut"] = True
                v = {"note": d1, "ch": ch, "start": time, "off": seconds, "end": seconds + 10, "vel": d2,
                     "cut": False}
                for other in held:
                    if other["note"] == d1 and other["ch"] == ch and self.retrigger_click:
                        other["end"], other["cut"] = time, True
                active.append(v)
                voices.append(v)
            elif kind == 0x80:
                for v in active:
                    if v["note"] == d1 and v["ch"] == ch and v["off"] >= seconds and v["start"] < time:
                        off = time
                        for c, down, up in pedal_spans:
                            if c == ch and down <= off < up:
                                off = up
                        v["off"] = off
                        if not v["cut"]:
                            v["end"] = off + self.release * 6
                        break
        t = np.arange(frames) / rate

        def curve(points, default):
            if not points:
                return np.full(frames, float(default))
            times = np.array([p[0] for p in points])
            values = np.array([p[1] for p in points], dtype=float)
            index = np.searchsorted(times, t, side="right") - 1
            return np.where(index >= 0, values[np.maximum(index, 0)], default)

        for v in voices:
            a, b = int(v["start"] * rate), min(frames, int(min(v["end"], seconds) * rate))
            if b <= a:
                continue
            tt = t[a:b] - v["start"]
            amp = (v["vel"] / 127) * 0.08 * np.minimum(1, tt / 0.004)
            if self.decay:
                amp = amp * np.exp(-tt / self.decay)
            rel = np.clip((t[a:b] - v["off"]) / self.release, 0, None)
            amp = amp * np.exp(-rel * 4.6) * (t[a:b] >= v["off"]) + amp * (t[a:b] < v["off"])
            semis = curve(bend.get(v["ch"], []), 0.0)[a:b]
            bright = curve(cc.get((v["ch"], 1), []), 0)[a:b] / 127 if self.cc1_brightness else 0
            vol = curve(cc.get((v["ch"], 11), []), 127)[a:b] / 127 if self.cc11_volume else 1
            f0 = 440 * 2 ** ((v["note"] - 69 + semis) / 12)
            phase = 2 * np.pi * np.cumsum(f0) / rate
            wave = np.zeros(b - a)
            for k in range(1, 9):
                mask = f0 * k < rate / 2
                wave += mask * np.sin(k * phase) * (1 / k) * (1 if k <= 2 else 0.3 + 0.7 * bright)
            if v["cut"]:
                wave[-1:] += 0.0  # Hard cut at end: the discontinuity itself is the click.
            out[a:b] += amp * wave * vol
        return np.stack([out, out], axis=1)
