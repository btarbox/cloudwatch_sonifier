#!/usr/bin/env python3
"""
cloudtrail_sonifier.py — "tail -f" for AWS CloudTrail, rendered as music.

Inspired by JFugue and log4jfugue, this program polls AWS CloudTrail
for new events and renders them as chords — one per second — so you
hear the *density* and *character* of your cloud activity at a glance.

Chord-per-second model (like log4jfugue):
  - Events are grouped into 1-second time buckets
  - All unique notes in a bucket are layered into a single chord
  - Repeated events within a bucket increase the chord's velocity
  - Thick chords = busy infrastructure; thin = quiet
  - Silence = nothing happening

Three audio backends (auto-detected):
  1. FluidSynth  — rich General MIDI via SoundFont (pip install pyfluidsynth)
  2. sounddevice — pure synth, zero config        (pip install sounddevice numpy)
  3. mido        — external MIDI port for DAWs    (pip install mido python-rtmidi)

Usage:
    pip install boto3 sounddevice numpy
    python cloudtrail_sonifier.py --test
    python cloudtrail_sonifier.py
    python cloudtrail_sonifier.py --chord-duration 0.8 --services s3 ec2 iam
"""

import argparse
import hashlib
import math
import os
import signal
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3

# ─────────────────────────────────────────────────────────────
# Service → General MIDI instrument mapping
# ─────────────────────────────────────────────────────────────
SERVICE_INSTRUMENTS = {
    "ec2": 0, "s3": 12, "iam": 56, "lambda": 81, "dynamodb": 14,
    "rds": 24, "ecs": 38, "eks": 62, "cloudformation": 46,
    "sqs": 112, "sns": 114, "kms": 71, "sts": 73, "logs": 8,
    "cloudwatch": 11, "elasticloadbalancing": 4, "autoscaling": 50,
    "route53": 68, "cloudfront": 88, "secretsmanager": 104,
}
DEFAULT_INSTRUMENT = 0

PERC_CHANNEL = 9
PERC_ERROR = 38
PERC_DENIED = 39
PERC_THROTTLE = 56
PERC_HEARTBEAT = 42

SERVICE_WAVEFORMS = {
    "ec2": "sine", "s3": "triangle", "iam": "square",
    "lambda": "sawtooth", "dynamodb": "sine", "rds": "triangle",
    "ecs": "sawtooth", "eks": "square", "sts": "sine",
    "kms": "triangle", "sqs": "sine", "sns": "triangle",
    "logs": "sine", "cloudwatch": "sine",
}

# ─────────────────────────────────────────────────────────────
# Action verb → pitch mapping
# ─────────────────────────────────────────────────────────────
ACTION_PREFIXES = {
    "get": 48, "describe": 50, "list": 52, "lookup": 53,
    "head": 55, "check": 57,
    "create": 60, "put": 62, "run": 64, "start": 65,
    "allocate": 67, "register": 69, "attach": 71,
    "update": 72, "modify": 74, "set": 76, "enable": 77,
    "tag": 79, "associate": 81,
    "delete": 84, "remove": 86, "terminate": 88, "stop": 89,
    "deregister": 91, "detach": 93, "revoke": 95,
    "assume": 58, "console": 66, "generate": 64,
}


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class NoteEvent:
    """A single parsed event ready for sonification."""
    note: int
    service: str
    event_name: str
    waveform: str
    pan: float          # -1.0 to 1.0
    is_error: bool
    error_code: str
    error_message: str
    read_only: bool


@dataclass
class ChordBucket:
    """All events grouped into one time-slice, rendered as a chord."""
    notes: list[NoteEvent] = field(default_factory=list)

    @property
    def unique_pitches(self) -> list[int]:
        seen = {}
        for n in self.notes:
            if n.note not in seen:
                seen[n.note] = n
        return list(seen.keys())

    @property
    def note_counts(self) -> Counter:
        return Counter(n.note for n in self.notes)

    @property
    def has_errors(self) -> bool:
        return any(n.is_error for n in self.notes)

    @property
    def error_notes(self) -> list[NoteEvent]:
        return [n for n in self.notes if n.is_error]

    @property
    def density(self) -> int:
        return len(self.notes)

    def amplitude_for_pitch(self, pitch: int) -> float:
        count = self.note_counts[pitch]
        return min(0.3 + (count - 1) * 0.07, 0.9)

    def avg_pan_for_pitch(self, pitch: int) -> float:
        pans = [n.pan for n in self.notes if n.note == pitch]
        return sum(pans) / len(pans) if pans else 0.0

    def waveform_for_pitch(self, pitch: int) -> str:
        for n in self.notes:
            if n.note == pitch:
                return n.waveform
        return "sine"

    def service_for_pitch(self, pitch: int) -> str:
        for n in self.notes:
            if n.note == pitch:
                return n.service
        return ""


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def normalize_service(event_source: str) -> str:
    return event_source.split(".")[0].lower() if event_source else "unknown"


def action_to_note(event_name: str) -> int:
    name_lower = event_name.lower()
    for prefix, note in ACTION_PREFIXES.items():
        if name_lower.startswith(prefix):
            return note
    return 48 + (hash(name_lower) % 48)


def midi_note_to_freq(note: int) -> float:
    return 440.0 * (2.0 ** ((note - 69) / 12.0))


def ip_to_pan(source_ip: str) -> float:
    if not source_ip or source_ip == "AWS Internal":
        return 0.0
    h = int(hashlib.md5(source_ip.encode()).hexdigest()[:4], 16)
    return (h % 128) / 63.5 - 1.0


def midi_note_name(note: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F",
             "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[note % 12]}{(note // 12) - 1}"


# ═════════════════════════════════════════════════════════════
# Audio Backends
# ═════════════════════════════════════════════════════════════

class SoundDeviceBackend:
    """
    Pure-Python synthesizer using sounddevice + numpy.
    Plays chords by summing waveforms together.
    """

    def __init__(self):
        import numpy as np
        import sounddevice as sd
        self.np = np
        self.sd = sd
        self.sample_rate = 44100
        print("♫  Backend: sounddevice (direct audio synthesis)")
        print(f"   Output device: {sd.query_devices(kind='output')['name']}")

    def _generate_tone(self, freq: float, duration: float, amplitude: float,
                       pan: float = 0.0, waveform: str = "sine",
                       attack: float = 0.01, release: float = 0.08) -> 'np.ndarray':
        np = self.np
        n_samples = int(self.sample_rate * duration)
        t = np.linspace(0, duration, n_samples, endpoint=False)

        if waveform == "sine":
            wave = np.sin(2 * np.pi * freq * t)
        elif waveform == "triangle":
            wave = 2 * np.abs(2 * (t * freq - np.floor(t * freq + 0.5))) - 1
        elif waveform == "square":
            wave = np.sign(np.sin(2 * np.pi * freq * t)) * 0.6
        elif waveform == "sawtooth":
            wave = 2 * (t * freq - np.floor(t * freq + 0.5)) * 0.7
        elif waveform == "noise":
            wave = np.random.uniform(-1, 1, n_samples)
        else:
            wave = np.sin(2 * np.pi * freq * t)

        envelope = np.ones(n_samples)
        att = min(int(self.sample_rate * attack), n_samples // 2)
        rel = min(int(self.sample_rate * release), n_samples // 2)
        if att > 0:
            envelope[:att] = np.linspace(0, 1, att)
        if rel > 0:
            envelope[-rel:] = np.linspace(1, 0, rel)

        wave = wave * envelope * amplitude
        left = np.sqrt(0.5 * (1.0 - pan))
        right = np.sqrt(0.5 * (1.0 + pan))
        return np.column_stack([wave * left, wave * right]).astype(np.float32)

    def play_chord(self, chord: ChordBucket, duration: float):
        """Mix all unique pitches into a single audio buffer and play."""
        np = self.np
        pitches = chord.unique_pitches

        if not pitches:
            return

        headroom = 1.0 / max(math.sqrt(len(pitches)), 1.0)

        layers = []
        for pitch in pitches:
            freq = midi_note_to_freq(pitch)
            amp = chord.amplitude_for_pitch(pitch) * headroom
            pan = chord.avg_pan_for_pitch(pitch)
            wf = chord.waveform_for_pitch(pitch)
            layers.append(self._generate_tone(freq, duration, amp, pan, wf))

        # Errors: dissonant intervals + noise to stand out, but not terrify
        if chord.has_errors:
            err_headroom = headroom * 1.2

            for err in chord.error_notes:
                # Minor second above — gentle clash
                freq_m2 = midi_note_to_freq(err.note + 1)
                layers.append(self._generate_tone(freq_m2, duration,
                                                  0.3 * err_headroom, err.pan,
                                                  "sine"))

                # Tritone above — a hint of menace
                freq_tri = midi_note_to_freq(err.note + 6)
                layers.append(self._generate_tone(freq_tri, duration * 0.4,
                                                  0.18 * err_headroom, err.pan,
                                                  "sawtooth"))

                # Short noise accent
                layers.append(self._generate_tone(150, duration * 0.25,
                                                  0.2 * err_headroom, err.pan,
                                                  "noise"))

            # Subtle bass undertone
            layers.append(self._generate_tone(55, duration * 0.5,
                                              0.25 * headroom, 0.0, "sine",
                                              attack=0.02, release=0.15))

        max_len = max(l.shape[0] for l in layers)
        mixed = np.zeros((max_len, 2), dtype=np.float32)
        for l in layers:
            mixed[:l.shape[0]] += l
        mixed = np.clip(mixed, -1.0, 1.0)

        self.sd.play(mixed, self.sample_rate)
        self.sd.wait()

    def play_heartbeat(self):
        audio = self._generate_tone(800, 0.03, 0.15, 0.0, "sine",
                                    attack=0.002, release=0.01)
        self.sd.play(audio, self.sample_rate)
        self.sd.wait()

    def play_test_scale(self):
        """Play a test scale, then a chord."""
        import numpy as np
        print("\n🎵  TEST MODE — playing C major scale, then a chord...")
        print("   If you hear this, audio is working!\n")
        wfs = ["sine", "triangle", "square", "sawtooth",
               "sine", "triangle", "square", "sine"]
        for i, note in enumerate([60, 62, 64, 65, 67, 69, 71, 72]):
            print(f"   ♩ {midi_note_name(note):4s}  ({wfs[i]})")
            audio = self._generate_tone(midi_note_to_freq(note), 0.3, 0.5,
                                        waveform=wfs[i])
            self.sd.play(audio, self.sample_rate)
            self.sd.wait()
            time.sleep(0.02)

        print("   ♬ C major chord (3 notes at once — like a busy second)")
        c = self._generate_tone(midi_note_to_freq(60), 0.8, 0.35, -0.5, "sine")
        e = self._generate_tone(midi_note_to_freq(64), 0.8, 0.35, 0.0, "triangle")
        g = self._generate_tone(midi_note_to_freq(67), 0.8, 0.35, 0.5, "sawtooth")
        mx = max(c.shape[0], e.shape[0], g.shape[0])
        mixed = np.zeros((mx, 2), dtype=np.float32)
        mixed[:c.shape[0]] += c
        mixed[:e.shape[0]] += e
        mixed[:g.shape[0]] += g
        mixed = np.clip(mixed, -1.0, 1.0)
        self.sd.play(mixed, self.sample_rate)
        self.sd.wait()

        print("\n   ♬ Dissonant error chord (what errors sound like)")
        c = self._generate_tone(midi_note_to_freq(60), 0.8, 0.4, 0, "sine")
        cs = self._generate_tone(midi_note_to_freq(61), 0.8, 0.4, 0, "sine")
        ns = self._generate_tone(200, 0.3, 0.3, 0, "noise")
        mx = max(c.shape[0], cs.shape[0], ns.shape[0])
        mixed = np.zeros((mx, 2), dtype=np.float32)
        mixed[:c.shape[0]] += c
        mixed[:cs.shape[0]] += cs
        mixed[:ns.shape[0]] += ns
        mixed = np.clip(mixed, -1.0, 1.0)
        self.sd.play(mixed, self.sample_rate)
        self.sd.wait()

        print("\n✅  Test complete. Run without --test to sonify CloudTrail.\n")

    def cleanup(self):
        pass


class FluidSynthBackend:
    """General MIDI synthesis via FluidSynth + SoundFont."""

    def __init__(self, soundfont_path: str):
        import fluidsynth
        self.fluidsynth = fluidsynth
        self.fs = fluidsynth.Synth(gain=0.6)
        driver = ("coreaudio" if sys.platform == "darwin"
                  else "alsa" if sys.platform == "linux" else "dsound")
        self.fs.start(driver=driver)
        self.sfid = self.fs.sfload(soundfont_path)
        self._assigned = {}
        self._ch_map = {}
        print(f"♫  Backend: FluidSynth — SoundFont: {soundfont_path}")

    def _channel_for(self, service: str) -> int:
        if service not in self._ch_map:
            ch = len(self._ch_map) % 16
            if ch == PERC_CHANNEL:
                ch = (ch + 1) % 16
            self._ch_map[service] = ch
            program = SERVICE_INSTRUMENTS.get(service, DEFAULT_INSTRUMENT)
            self.fs.program_select(ch, self.sfid, 0, program)
        return self._ch_map[service]

    def play_chord(self, chord: ChordBucket, duration: float):
        for pitch in chord.unique_pitches:
            svc = chord.service_for_pitch(pitch)
            ch = self._channel_for(svc)
            vel = int(chord.amplitude_for_pitch(pitch) * 127)
            pan = int((chord.avg_pan_for_pitch(pitch) + 1.0) * 63.5)
            self.fs.cc(ch, 10, pan)
            self.fs.noteon(ch, pitch, vel)

        for err in chord.error_notes:
            ch = self._channel_for(err.service)
            dissonant = min(err.note + 1, 127)
            self.fs.noteon(ch, dissonant, 85)
            tritone = min(err.note + 6, 127)
            self.fs.noteon(ch, tritone, 60)
            perc = PERC_THROTTLE if "Throttl" in err.error_code else (
                PERC_DENIED if "Denied" in err.error_code else PERC_ERROR)
            self.fs.noteon(PERC_CHANNEL, perc, 100)

        time.sleep(duration)

        for pitch in chord.unique_pitches:
            svc = chord.service_for_pitch(pitch)
            ch = self._channel_for(svc)
            self.fs.noteoff(ch, pitch)
        for err in chord.error_notes:
            ch = self._channel_for(err.service)
            self.fs.noteoff(ch, min(err.note + 1, 127))
            self.fs.noteoff(ch, min(err.note + 6, 127))
            self.fs.noteoff(PERC_CHANNEL, PERC_ERROR)
            self.fs.noteoff(PERC_CHANNEL, PERC_DENIED)
            self.fs.noteoff(PERC_CHANNEL, PERC_THROTTLE)

    def play_heartbeat(self):
        self.fs.noteon(PERC_CHANNEL, PERC_HEARTBEAT, 30)
        time.sleep(0.05)
        self.fs.noteoff(PERC_CHANNEL, PERC_HEARTBEAT)

    def play_test_scale(self):
        print("\n🎵  TEST MODE — playing C major scale...")
        self.fs.program_select(0, self.sfid, 0, 0)
        for note in [60, 62, 64, 65, 67, 69, 71, 72]:
            print(f"   ♩ {midi_note_name(note)}")
            self.fs.noteon(0, note, 80)
            time.sleep(0.35)
            self.fs.noteoff(0, note)
        print("   ♬ C major chord")
        for n in [60, 64, 67]:
            self.fs.noteon(0, n, 80)
        time.sleep(0.8)
        for n in [60, 64, 67]:
            self.fs.noteoff(0, n)
        print("\n✅  Test complete.\n")

    def cleanup(self):
        self.fs.delete()


class MidoBackend:
    """External MIDI port for DAW routing."""

    def __init__(self, virtual_port: Optional[str]):
        import mido as _mido
        self.mido = _mido
        self._ch_map = {}

        if virtual_port:
            self.port = _mido.open_output(virtual_port, virtual=True)
            print(f"♫  Backend: MIDI (virtual port: {virtual_port})")
            return
        available = _mido.get_output_names()
        if available:
            self.port = _mido.open_output(available[0])
            print(f"♫  Backend: MIDI (port: {available[0]})")
        else:
            self.port = _mido.open_output("CloudTrail Sonifier", virtual=True)
            print("♫  Backend: MIDI (virtual port: CloudTrail Sonifier)")

    def _channel_for(self, service: str) -> int:
        if service not in self._ch_map:
            ch = len(self._ch_map) % 16
            if ch == PERC_CHANNEL:
                ch = (ch + 1) % 16
            self._ch_map[service] = ch
            program = SERVICE_INSTRUMENTS.get(service, DEFAULT_INSTRUMENT)
            self.port.send(self.mido.Message("program_change",
                                             channel=ch, program=program))
        return self._ch_map[service]

    def play_chord(self, chord: ChordBucket, duration: float):
        for pitch in chord.unique_pitches:
            svc = chord.service_for_pitch(pitch)
            ch = self._channel_for(svc)
            vel = int(chord.amplitude_for_pitch(pitch) * 127)
            pan = int((chord.avg_pan_for_pitch(pitch) + 1.0) * 63.5)
            self.port.send(self.mido.Message("control_change",
                                             channel=ch, control=10, value=pan))
            self.port.send(self.mido.Message("note_on",
                                             channel=ch, note=pitch, velocity=vel))
        for err in chord.error_notes:
            ch = self._channel_for(err.service)
            dissonant = min(err.note + 1, 127)
            tritone = min(err.note + 6, 127)
            self.port.send(self.mido.Message("note_on",
                                             channel=ch, note=dissonant, velocity=85))
            self.port.send(self.mido.Message("note_on",
                                             channel=ch, note=tritone, velocity=60))
            perc = PERC_THROTTLE if "Throttl" in err.error_code else (
                PERC_DENIED if "Denied" in err.error_code else PERC_ERROR)
            self.port.send(self.mido.Message("note_on",
                                             channel=PERC_CHANNEL,
                                             note=perc, velocity=100))

        time.sleep(duration)

        for pitch in chord.unique_pitches:
            svc = chord.service_for_pitch(pitch)
            ch = self._channel_for(svc)
            self.port.send(self.mido.Message("note_off",
                                             channel=ch, note=pitch, velocity=0))
        for err in chord.error_notes:
            ch = self._channel_for(err.service)
            self.port.send(self.mido.Message("note_off",
                                             channel=ch,
                                             note=min(err.note + 1, 127), velocity=0))
            self.port.send(self.mido.Message("note_off",
                                             channel=ch,
                                             note=min(err.note + 6, 127), velocity=0))

    def play_heartbeat(self):
        self.port.send(self.mido.Message("note_on", channel=PERC_CHANNEL,
                                         note=PERC_HEARTBEAT, velocity=30))
        time.sleep(0.05)
        self.port.send(self.mido.Message("note_off", channel=PERC_CHANNEL,
                                         note=PERC_HEARTBEAT, velocity=0))

    def play_test_scale(self):
        print("\n🎵  TEST MODE — playing C major scale via MIDI...")
        self._channel_for("ec2")
        ch = self._ch_map["ec2"]
        for note in [60, 62, 64, 65, 67, 69, 71, 72]:
            print(f"   ♩ {midi_note_name(note)}")
            self.port.send(self.mido.Message("note_on",
                                             channel=ch, note=note, velocity=80))
            time.sleep(0.35)
            self.port.send(self.mido.Message("note_off",
                                             channel=ch, note=note, velocity=0))
        print("\n✅  Test complete.\n")

    def cleanup(self):
        self.port.close()


# ═════════════════════════════════════════════════════════════
# Main Sonifier
# ═════════════════════════════════════════════════════════════

class CloudTrailSonifier:
    """Polls CloudTrail and plays events as chords — one per second."""

    def __init__(self, region: str, interval: int, services: Optional[list],
                 virtual_port: Optional[str], dry_run: bool,
                 chord_duration: float, soundfont: Optional[str] = None,
                 backend_pref: Optional[str] = None):
        self.region = region
        self.interval = interval
        self.filter_services = {s.lower() for s in services} if services else None
        self.dry_run = dry_run
        self.chord_duration = chord_duration

        self.seen_ids: deque = deque(maxlen=5000)
        self.lookback_minutes = 20  # always look this far back; seen_ids deduplicates
        self.backoff = 0  # extra seconds to wait after throttling

        self.ct_client = boto3.client("cloudtrail", region_name=self.region)

        self.backend = None
        if not self.dry_run:
            self.backend = self._init_backend(virtual_port, soundfont, backend_pref)

    def _find_soundfont(self, sf_arg: Optional[str]) -> Optional[str]:
        if sf_arg and os.path.isfile(sf_arg):
            return sf_arg
        for p in [
            "/usr/share/sounds/sf2/FluidR3_GM.sf2",
            "/usr/share/sounds/sf2/default-GM.sf2",
            "/usr/share/soundfonts/FluidR3_GM.sf2",
            os.path.expanduser("~/FluidR3_GM.sf2"),
            os.path.expanduser("~/GeneralUser_GS.sf2"),
            "/usr/local/share/fluidsynth/FluidR3_GM.sf2",
            "/opt/homebrew/share/fluidsynth/FluidR3_GM.sf2",
        ]:
            if os.path.isfile(p):
                return p
        return None

    def _init_backend(self, virtual_port, soundfont, backend_pref):
        errors = []
        if backend_pref in (None, "fluidsynth"):
            try:
                sf = self._find_soundfont(soundfont)
                if sf:
                    return FluidSynthBackend(sf)
                errors.append("fluidsynth: no .sf2 SoundFont found")
            except ImportError:
                errors.append("fluidsynth: pip install pyfluidsynth")
            except Exception as e:
                errors.append(f"fluidsynth: {e}")
            if backend_pref == "fluidsynth":
                print(f"⚠  {errors[-1]}"); sys.exit(1)

        if backend_pref in (None, "sounddevice"):
            try:
                return SoundDeviceBackend()
            except ImportError:
                errors.append("sounddevice: pip install sounddevice numpy")
            except Exception as e:
                errors.append(f"sounddevice: {e}")
            if backend_pref == "sounddevice":
                print(f"⚠  {errors[-1]}"); sys.exit(1)

        if backend_pref in (None, "midi"):
            try:
                return MidoBackend(virtual_port)
            except ImportError:
                errors.append("mido: pip install mido python-rtmidi")
            except Exception as e:
                errors.append(f"mido: {e}")
            if backend_pref == "midi":
                print(f"⚠  {errors[-1]}"); sys.exit(1)

        print("⚠  No audio backend! Tried:")
        for e in errors:
            print(f"     • {e}")
        print("\n   Easiest fix:  pip install sounddevice numpy")
        sys.exit(1)

    # ── CloudTrail polling ────────────────────────────────────

    def poll_events(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=self.lookback_minutes)
        events = []
        try:
            # Single API call with a rolling lookback window.
            # seen_ids handles deduplication so we never replay events.
            response = self.ct_client.lookup_events(
                StartTime=start,
                EndTime=now,
                MaxResults=50,
            )
            for event in response.get("Events", []):
                eid = event.get("EventId")
                if eid and eid not in self.seen_ids:
                    self.seen_ids.append(eid)
                    events.append(event)
            # Success — reset backoff
            self.backoff = 0
        except self.ct_client.exceptions.ThrottlingException:
            self.backoff = min(max(self.backoff * 2, 30), 300)
            print(f"  ⚠  Throttled by CloudTrail — backing off, "
                  f"next poll in {self.interval + self.backoff}s")
        except Exception as e:
            print(f"  ⚠  CloudTrail poll error: {e}")
        return events

    # ── Event parsing ─────────────────────────────────────────

    def parse_event(self, event: dict, record: Optional[dict] = None) -> Optional[NoteEvent]:
        import json
        if record is None:
            raw = event.get("CloudTrailEvent", "{}")
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                return None

        service = normalize_service(record.get("eventSource", ""))
        event_name = record.get("eventName", "Unknown")
        error_code = record.get("errorCode")
        error_message = record.get("errorMessage", "")
        source_ip = record.get("sourceIPAddress", "")
        read_only = record.get("readOnly", False)

        if self.filter_services and service not in self.filter_services:
            return None

        return NoteEvent(
            note=action_to_note(event_name),
            service=service,
            event_name=event_name,
            waveform=SERVICE_WAVEFORMS.get(service, "sine"),
            pan=ip_to_pan(source_ip),
            is_error=bool(error_code),
            error_code=error_code or "",
            error_message=error_message,
            read_only=read_only,
        )

    # ── Bucketing: group events into 1-second slices ──────────

    def bucket_events(self, events: list[dict]) -> list[ChordBucket]:
        import json

        timed: list[tuple[int, NoteEvent]] = []
        for event in events:
            raw = event.get("CloudTrailEvent", "{}")
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts_str = record.get("eventTime", "")
            try:
                ts = int(datetime.fromisoformat(
                    ts_str.replace("Z", "+00:00")).timestamp())
            except (ValueError, TypeError):
                ts = 0
            note_event = self.parse_event(event, record)
            if note_event:
                timed.append((ts, note_event))

        if not timed:
            return []

        timed.sort(key=lambda x: x[0])
        buckets: list[ChordBucket] = []
        current_ts = timed[0][0]
        current_bucket = ChordBucket()

        for ts, note_event in timed:
            if ts != current_ts:
                buckets.append(current_bucket)
                current_bucket = ChordBucket()
                current_ts = ts
            current_bucket.notes.append(note_event)
        buckets.append(current_bucket)

        return buckets

    # ── Display ───────────────────────────────────────────────

    def print_chord(self, bucket: ChordBucket, index: int, total: int):
        pitches = bucket.unique_pitches
        counts = bucket.note_counts
        pitch_strs = []
        for p in pitches:
            nm = midi_note_name(p)
            c = counts[p]
            svc = bucket.service_for_pitch(p)
            if c > 1:
                pitch_strs.append(f"{nm}×{c}({svc})")
            else:
                pitch_strs.append(f"{nm}({svc})")
        chord_label = " + ".join(pitch_strs) if pitch_strs else "—"
        err = " ✖ ERRORS" if bucket.has_errors else ""
        density_bar = "█" * min(bucket.density, 30)
        print(f"  chord {index:3d}/{total:<3d} │ {bucket.density:3d} events │ "
              f"{chord_label}{err}")
        print(f"              │ density  │ {density_bar}")
        if bucket.has_errors:
            for n in bucket.error_notes:
                msg = n.error_message if n.error_message else "(no message)"
                print(f"              │ ✖ ERROR  │ {n.service}.{n.event_name}: "
                      f"{n.error_code} — {msg}")

    # ── Main loop ─────────────────────────────────────────────

    def run(self):
        print("=" * 78)
        print("  ♫  CloudTrail Sonifier  — chord-per-second mode")
        print("=" * 78)
        print(f"  Region:         {self.region}")
        print(f"  Poll interval:  {self.interval}s")
        print(f"  Chord duration: {self.chord_duration}s")
        svcs = ', '.join(self.filter_services) if self.filter_services else 'all'
        print(f"  Services:       {svcs}")
        mode = 'DRY RUN' if self.dry_run else 'LIVE'
        print(f"  Mode:           {mode}")
        print("─" * 78)

        while True:
            events = self.poll_events()
            if events:
                buckets = self.bucket_events(events)
                n = len(buckets)
                print(f"\n  ▶ {len(events)} events → {n} chord(s)")

                # Stretch chords to fill the entire poll interval
                # so we get a continuous stream with no silence gaps.
                # Busier intervals → shorter, more rapid chords
                # Quieter intervals → long, sustained chords
                poll_window = self.interval + self.backoff
                dur_per_chord = max(poll_window / n, self.chord_duration) if n > 0 else self.chord_duration

                for i, bucket in enumerate(buckets):
                    dur = dur_per_chord * 1.25 if bucket.has_errors else dur_per_chord
                    self.print_chord(bucket, i + 1, n)
                    if self.backend:
                        self.backend.play_chord(bucket, dur)
            else:
                if self.backend:
                    self.backend.play_heartbeat()
                print(f"  ··· (no new events — heartbeat) ♩")
                time.sleep(self.interval + self.backoff)


def main():
    parser = argparse.ArgumentParser(
        description="CloudTrail Sonifier — chord-per-second mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --test                                 # verify audio
  %(prog)s                                        # auto-detect backend
  %(prog)s --chord-duration 0.5                   # snappier chords
  %(prog)s --chord-duration 1.5                   # lush sustained chords
  %(prog)s --backend sounddevice                  # force direct audio
  %(prog)s --services s3 iam lambda --interval 10
  %(prog)s --dry-run                              # no audio
        """,
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--interval", type=int, default=60,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--services", nargs="*")
    parser.add_argument("--virtual-port", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chord-duration", type=float, default=1.0,
                        help="How long each chord rings (seconds, default: 1.0)")
    parser.add_argument("--soundfont", default=None)
    parser.add_argument("--backend",
                        choices=["fluidsynth", "sounddevice", "midi", "auto"],
                        default="auto")
    parser.add_argument("--test", action="store_true",
                        help="Play test sounds to verify audio, then exit")

    args = parser.parse_args()

    backend_pref = None if args.backend == "auto" else args.backend

    sonifier = CloudTrailSonifier(
        region=args.region,
        interval=args.interval,
        services=args.services,
        virtual_port=args.virtual_port,
        dry_run=args.dry_run,
        chord_duration=args.chord_duration,
        soundfont=args.soundfont,
        backend_pref=backend_pref,
    )

    def handle_signal(sig, frame):
        print("\n♫  Fin. 🎵")
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.test:
        if sonifier.backend:
            sonifier.backend.play_test_scale()
        else:
            print("⚠  No audio backend. Nothing to test.")
        return

    sonifier.run()


if __name__ == "__main__":
    main()
