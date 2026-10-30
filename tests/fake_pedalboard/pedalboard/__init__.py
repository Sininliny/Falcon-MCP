"""Stand-in for pedalboard in rack worker subprocesses (tests only; put this folder on PYTHONPATH).

Behaviour follows the plugin file name: Crash* exits during load, Hang* never finishes loading, Shell*
holds two plugins, Fx* is an effect, Slow* is silent for its first three renders (background loading),
Bright* has stronger upper partials, Deaf* ignores notes in the first 0.5 s after a reset, Sticky* hangs
while unloading, Patient* keeps its window open until the close event. State files are JSON
({"gain": ..., "instrument": ...}); show_editor sets gain 0.25; program change n sets gain 1/(n+1);
drop_file (FALCON_RACK_TEST_DROP) embeds the file's text in the state and sets gain 0.5.
"""

import json
import os
import time
from pathlib import Path

import numpy as np


class _Plugin:
    def __init__(self, path, plugin_name=None):
        base = Path(path).stem
        if base.startswith("Crash"):
            os._exit(3)
        if base.startswith("Hang"):
            time.sleep(3600)
        self.base = base
        self.name = plugin_name or base
        self.descriptive_name = self.name
        self.manufacturer_name = "FakeCo"
        self.category = "Fx" if base.startswith("Fx") else "Instrument|Synth"
        self.version = "1.0.0"
        self.identifier = f"VST3-{self.name}"
        self.is_instrument = not base.startswith("Fx")
        self.is_effect = not self.is_instrument
        self.reported_latency_samples = 0
        self.parameters = {"gain": None}
        self.gain = 1.0
        self.instrument = None
        self.calls = 0
        self.reset()

    @property
    def raw_state(self):
        return json.dumps({"gain": self.gain, "instrument": self.instrument}).encode()

    @raw_state.setter
    def raw_state(self, data):
        state = json.loads(data)
        self.gain, self.instrument = float(state["gain"]), state.get("instrument")

    def drop_file(self, path):
        # Like Kontakt loading a dropped .nki: the sound changes and the state embeds the instrument.
        self.instrument, self.gain = Path(path).read_text(encoding="utf-8"), 0.5

    preset_data = raw_state

    def load_preset(self, path):
        self.raw_state = Path(path).read_bytes()

    def show_editor(self, close_event=None):
        self.gain = 0.25
        if self.base.startswith("Patient"):
            close_event.wait()

    def __del__(self):
        if getattr(self, "base", "").startswith("Sticky"):
            time.sleep(3600)

    def reset(self):
        self.voices, self.position = [], 0

    def __call__(self, messages, duration, sample_rate, num_channels=2, buffer_size=8192, reset=True):
        if reset:
            self.reset()
        self.calls += 1
        n = round(duration * sample_rate)
        for data, at in sorted(messages, key=lambda m: m[1]):
            status, note, velocity = data[0] & 0xF0, data[1], data[2] if len(data) > 2 else 0
            if status == 0xB0:
                continue
            at = self.position + round(at * sample_rate)
            if status == 0xC0:
                self.gain = 1.0 / (note + 1)  # program change: data byte 1 is the program
                continue
            if status == 0x90 and velocity > 0:
                if self.base.startswith("Deaf") and at < 0.5 * sample_rate:
                    continue
                self.voices.append([note, velocity, at, None])
            elif status in (0x80, 0x90):
                for voice in self.voices:
                    if voice[0] == note and voice[3] is None:
                        voice[3] = at
                        break
        index = self.position + np.arange(n)
        out = np.zeros(n)
        upper = 0.6 if self.base.startswith("Bright") else 0.15
        for note, velocity, start, end in self.voices:
            t = (index - start) / sample_rate
            live = t >= 0
            if not live.any():
                continue
            f0 = 440.0 * 2 ** ((note - 69) / 12)
            if self.base.startswith("RateBug") and sample_rate == 48000:
                f0 *= 48000 / 44100
            amp = (velocity / 127) ** 1.5 * self.gain * np.exp(-1.5 * np.clip(t, 0, None))
            if end is not None:
                amp = amp * np.where(index >= end, np.exp(-np.clip(index - end, 0, None) / sample_rate / 0.08), 1.0)
            tone = np.sin(2 * np.pi * f0 * t) + upper * np.sin(4 * np.pi * f0 * t) + 0.5 * upper * np.sin(6 * np.pi * f0 * t)
            out += np.where(live, amp * tone * 0.3, 0.0)
        self.voices = [v for v in self.voices if v[3] is None or (self.position + n - v[3]) < sample_rate]
        self.position += n
        if self.base.startswith("Slow") and self.calls <= 3:
            out[:] = 0
        return np.stack([out] * num_channels).astype(np.float32)


class VST3Plugin(_Plugin):
    @staticmethod
    def get_plugin_names_for_file(path):
        base = Path(path).stem
        if base.startswith("Shell"):
            return ["Shell A", "Shell B"]
        if base.startswith("Crash"):
            os._exit(3)
        return [base]


def load_plugin(path, plugin_name=None):
    return VST3Plugin(path, plugin_name)
