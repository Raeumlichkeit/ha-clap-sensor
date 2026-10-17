from __future__ import annotations

import unittest
from typing import List

import numpy as np

from detector import ClapDetector, ClapEvent


def synthesize_stream(
    duration_ms: int,
    sample_rate: int = 16000,
    fan_hum_amplitude: float = 0.04,
    hiss_amplitude: float = 0.002,
    seed: int = 42,
) -> np.ndarray:
    """
    Erzeugt einen realistischen Hintergrund-Stream mit starkem tieffrequentem
    Proxmox-Lüfterbrummen (180 Hz + 340 Hz) und leichtem Grundrauschen.
    """
    rng = np.random.default_rng(seed)
    total_samples = int(sample_rate * (duration_ms / 1000.0))
    t = np.arange(total_samples, dtype=np.float32) / sample_rate

    fan_hum = (
        fan_hum_amplitude * np.sin(2.0 * np.pi * 180.0 * t)
        + (fan_hum_amplitude * 0.6) * np.sin(2.0 * np.pi * 340.0 * t)
    )
    hiss = rng.normal(0.0, hiss_amplitude, size=total_samples).astype(np.float32)
    return (fan_hum + hiss).astype(np.float32)


def inject_clap(
    stream: np.ndarray,
    at_ms: float,
    duration_ms: float = 32.0,
    amplitude: float = 0.45,
    sample_rate: int = 16000,
    clip: bool = False,
) -> None:
    """
    Fügt einen realistischen Handklatsch-Impuls (steiler Attack, exponentielles Decay,
    breitbandiges Spektrum zwischen 1.2 kHz und 3.8 kHz) in den Audio-Stream ein.
    """
    start_idx = int(sample_rate * (at_ms / 1000.0))
    length = int(sample_rate * (duration_ms / 1000.0))
    if start_idx + length >= len(stream):
        return

    t = np.arange(length, dtype=np.float32) / sample_rate
    envelope = np.exp(-t / (duration_ms / 1000.0 * 0.35)).astype(np.float32)
    carrier = (
        0.45 * np.sin(2.0 * np.pi * 1550.0 * t)
        + 0.35 * np.sin(2.0 * np.pi * 2450.0 * t)
        + 0.20 * np.sin(2.0 * np.pi * 3600.0 * t)
    ).astype(np.float32)

    burst = amplitude * envelope * carrier
    stream[start_idx : start_idx + length] += burst
    if clip:
        np.clip(stream, -1.0, 1.0, out=stream)


def inject_low_thud(
    stream: np.ndarray,
    at_ms: float,
    duration_ms: float = 40.0,
    amplitude: float = 0.60,
    sample_rate: int = 16000,
) -> None:
    """Fügt einen dumpfen Tischstoß (220 Hz) ein."""
    start_idx = int(sample_rate * (at_ms / 1000.0))
    length = int(sample_rate * (duration_ms / 1000.0))
    t = np.arange(length, dtype=np.float32) / sample_rate
    envelope = np.exp(-t / 0.015).astype(np.float32)
    thud = amplitude * envelope * np.sin(2.0 * np.pi * 220.0 * t).astype(np.float32)
    stream[start_idx : start_idx + length] += thud


def run_detector_on_stream(
    detector: ClapDetector,
    stream: np.ndarray,
    sample_rate: int = 16000,
    block_duration_ms: int = 16,
) -> List[ClapEvent]:
    block_size = int(sample_rate * (block_duration_ms / 1000.0))
    events: List[ClapEvent] = []
    for i in range(0, len(stream) - block_size + 1, block_size):
        block = stream[i : i + block_size]
        ev = detector.process_block(block)
        if ev is not None:
            events.append(ev)
    return events


class TestClapDetector(unittest.TestCase):
    def test_double_clap_over_server_fan_noise(self) -> None:
        detector = ClapDetector(sample_rate=16000)
        stream = synthesize_stream(duration_ms=2600, fan_hum_amplitude=0.05)
        inject_clap(stream, at_ms=700.0)
        inject_clap(stream, at_ms=1020.0)

        events = run_detector_on_stream(detector, stream)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].pattern, "double_clap")
        self.assertEqual(events[0].claps, 2)

    def test_triple_clap_does_not_trigger_double_clap_first(self) -> None:
        detector = ClapDetector(sample_rate=16000, enable_triple_clap=True)
        stream = synthesize_stream(duration_ms=3000)
        inject_clap(stream, at_ms=600.0)
        inject_clap(stream, at_ms=900.0)
        inject_clap(stream, at_ms=1200.0)

        events = run_detector_on_stream(detector, stream)
        self.assertEqual(len(events), 1, "3x Klatschen darf nur genau ein Event (triple_clap) auslösen!")
        self.assertEqual(events[0].pattern, "triple_clap")
        self.assertEqual(events[0].claps, 3)

    def test_single_clap_is_ignored(self) -> None:
        detector = ClapDetector(sample_rate=16000)
        stream = synthesize_stream(duration_ms=2200)
        inject_clap(stream, at_ms=700.0)

        events = run_detector_on_stream(detector, stream)
        self.assertEqual(len(events), 0)

    def test_continuous_applause_is_rejected(self) -> None:
        detector = ClapDetector(sample_rate=16000)
        stream = synthesize_stream(duration_ms=3500)
        for offset in (600.0, 880.0, 1160.0, 1440.0, 1720.0):
            inject_clap(stream, at_ms=offset)

        events = run_detector_on_stream(detector, stream)
        self.assertEqual(len(events), 0, "Applaus (>= 4 Klatscher) muss ignoriert werden!")

    def test_low_frequency_desk_thuds_are_rejected(self) -> None:
        detector = ClapDetector(sample_rate=16000)
        stream = synthesize_stream(duration_ms=2500)
        inject_low_thud(stream, at_ms=700.0)
        inject_low_thud(stream, at_ms=1020.0)

        events = run_detector_on_stream(detector, stream)
        self.assertEqual(len(events), 0, "Dumpfe Tischstöße dürfen keinen Doppelklatsch auslösen!")

    def test_long_speech_bursts_are_rejected(self) -> None:
        detector = ClapDetector(sample_rate=16000)
        stream = synthesize_stream(duration_ms=2800)
        # Zwei 300 ms lange Impulse (wie laute Sprachsilben)
        inject_clap(stream, at_ms=600.0, duration_ms=300.0, amplitude=0.5)
        inject_clap(stream, at_ms=1050.0, duration_ms=300.0, amplitude=0.5)

        events = run_detector_on_stream(detector, stream)
        self.assertEqual(len(events), 0, "Lange Sprach-Impulse (> 150 ms) müssen verworfen werden!")

    def test_clipped_webcam_claps_still_detected(self) -> None:
        detector = ClapDetector(sample_rate=16000)
        stream = synthesize_stream(duration_ms=2600)
        inject_clap(stream, at_ms=700.0, amplitude=1.8, clip=True)
        inject_clap(stream, at_ms=1000.0, amplitude=1.8, clip=True)

        events = run_detector_on_stream(detector, stream)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].pattern, "double_clap")


if __name__ == "__main__":
    unittest.main()
