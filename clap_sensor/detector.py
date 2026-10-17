from __future__ import annotations

import ctypes
import math
import sys
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional


class WindowsInputGuard:
    """
    Erkennt unter Windows, ob in den letzten X Millisekunden eine Taste gedrückt
    oder die Maus bewegt/geklickt wurde. Auf Linux/HAOS ist dieser Check ein No-Op.
    """

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwTime", ctypes.c_uint),
        ]

    def __init__(self, ignore_window_ms: int = 400) -> None:
        self.ignore_window_ms = max(0, int(ignore_window_ms))
        self._is_windows = sys.platform == "win32"

    def is_user_typing_or_clicking(self) -> bool:
        if not self._is_windows or self.ignore_window_ms <= 0:
            return False
        try:
            lii = self._LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(self._LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                tick_count = ctypes.windll.kernel32.GetTickCount()
                elapsed_ms = (tick_count - lii.dwTime) & 0xFFFFFFFF
                return elapsed_ms < self.ignore_window_ms
        except Exception:
            return False
        return False


@dataclass
class ClapEvent:
    pattern: str          # "double_clap" oder "triple_clap"
    claps: int            # 2 oder 3
    timestamp_ms: float
    peak_amplitude: float
    snr_db: float
    high_freq_ratio: float
    noise_floor: float


@dataclass
class BlockDiagnostics:
    timestamp_ms: float
    hp_peak: float
    noise_floor: float
    snr_db: float
    high_freq_ratio: float
    is_clipped: bool
    single_clap_registered: bool = False
    rejected_reason: Optional[str] = None
    clap_count_in_buffer: int = 0


class ClapDetector:
    """
    Mehrstufiger Echtzeit-Klatschdetektor (DSP + FFT + Zustandsmaschine):
    1. Hochpass-Filterung (> 800 Hz) vor der Rauschpegel-Messung blendet
       Proxmox-Serverlüfter und tieffrequentes Brummen aus.
    2. Saubere Hysterese (Release < 65 % der Startschwelle + Pflicht-Ruhepause)
       verhindert, dass ein einzelner lauter Klatsch durch Raum-Nachhall doppelt gezählt wird.
    3. Spektrale Band-Ratio (1000-4800 Hz vs. Tiefton 120-750 Hz) filtert
       dumpfe Tischstöße, Schritte und Türenschlagen heraus.
    4. Zustandsmaschine mit Settle-Window unterscheidet sauber zwischen
       Einzelklatsch (ignoriert), Doppelklatsch (2x), Dreifachklatsch (3x)
       und Applaus (>= 4x, ignoriert).
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        min_amplitude: float = 0.18,
        snr_threshold_db: float = 18.0,
        max_clap_duration_ms: float = 160.0,
        min_high_freq_ratio: float = 0.65,
        double_clap_min_gap_ms: float = 170.0,
        double_clap_max_gap_ms: float = 700.0,
        settle_window_ms: float = 480.0,
        cooldown_ms: float = 1500.0,
        enable_triple_clap: bool = True,
        highpass_cutoff_hz: float = 800.0,
        input_guard: Optional[WindowsInputGuard] = None,
    ) -> None:
        import numpy as np

        self._np = np
        self.sample_rate = int(sample_rate)
        self.min_amplitude = float(min_amplitude)
        self.snr_threshold_db = float(snr_threshold_db)
        self.max_clap_duration_ms = float(max_clap_duration_ms)
        self.min_high_freq_ratio = float(min_high_freq_ratio)
        self.double_clap_min_gap_ms = float(double_clap_min_gap_ms)
        self.double_clap_max_gap_ms = float(double_clap_max_gap_ms)
        self.settle_window_ms = float(settle_window_ms)
        self.cooldown_ms = float(cooldown_ms)
        self.enable_triple_clap = bool(enable_triple_clap)
        self.highpass_cutoff_hz = float(highpass_cutoff_hz)
        self.input_guard = input_guard

        self._noise_history: Deque[float] = deque(maxlen=60)
        self._noise_floor: float = 0.003

        self._in_impulse: bool = False
        self._impulse_start_ms: float = 0.0
        self._impulse_last_active_ms: float = 0.0
        self._impulse_peak: float = 0.0
        self._impulse_snr_db: float = 0.0
        self._impulse_hf_ratio: float = 0.0
        self._impulse_invalidated: bool = False
        self._quiet_blocks_since_impulse: int = 10

        self._clap_times: List[float] = []
        self._clap_peaks: List[float] = []
        self._clap_snrs: List[float] = []
        self._clap_hf_ratios: List[float] = []

        self._last_trigger_ms: float = -1e9
        self._elapsed_ms: float = 0.0
        self.last_diagnostics: Optional[BlockDiagnostics] = None

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    def process_block(
        self,
        samples,
        timestamp_ms: Optional[float] = None,
    ) -> Optional[ClapEvent]:
        np = self._np
        arr = np.asarray(samples)
        if arr.ndim > 1:
            arr = arr[:, 0]

        if arr.dtype == np.int16:
            x = arr.astype(np.float32) / 32768.0
        else:
            x = arr.astype(np.float32)

        n = len(x)
        if n == 0:
            return None

        block_duration_ms = (n / self.sample_rate) * 1000.0
        if timestamp_ms is None:
            self._elapsed_ms += block_duration_ms
            now_ms = self._elapsed_ms
        else:
            now_ms = float(timestamp_ms)
            self._elapsed_ms = now_ms

        raw_peak = float(np.max(np.abs(x)))
        is_clipped = raw_peak >= 0.98

        window = np.hanning(n).astype(np.float32)
        spectrum = np.fft.rfft(x * window)
        freqs = np.fft.rfftfreq(n, d=1.0 / self.sample_rate)
        power = np.abs(spectrum) ** 2

        hp_spectrum = np.fft.rfft(x)
        hp_spectrum[freqs < self.highpass_cutoff_hz] = 0.0
        x_hp = np.fft.irfft(hp_spectrum, n=n)
        hp_peak = float(np.max(np.abs(x_hp)))
        hp_rms = float(np.sqrt(np.mean(x_hp * x_hp)) + 1e-9)

        total_relevant_mask = (freqs >= 120.0) & (freqs <= 6500.0)
        clap_band_mask = (freqs >= 1000.0) & (freqs <= 4800.0)
        low_thud_mask = (freqs >= 120.0) & (freqs < 750.0)

        total_energy = float(np.sum(power[total_relevant_mask])) + 1e-12
        clap_energy = float(np.sum(power[clap_band_mask]))
        low_energy = float(np.sum(power[low_thud_mask]))

        high_freq_ratio = clap_energy / (clap_energy + low_energy + 1e-12)
        if total_energy < 1e-9:
            high_freq_ratio = 0.0

        snr_ratio = hp_peak / max(self._noise_floor, 1e-6)
        snr_db = 20.0 * math.log10(max(snr_ratio, 1e-6))

        diag = BlockDiagnostics(
            timestamp_ms=now_ms,
            hp_peak=hp_peak,
            noise_floor=self._noise_floor,
            snr_db=snr_db,
            high_freq_ratio=high_freq_ratio,
            is_clipped=is_clipped,
            clap_count_in_buffer=len(self._clap_times),
        )

        if (now_ms - self._last_trigger_ms) < self.cooldown_ms:
            self._update_noise_floor_if_quiet(hp_rms, hp_peak)
            self.last_diagnostics = diag
            return None

        required_linear_snr = 10.0 ** (self.snr_threshold_db / 20.0)
        onset_threshold = max(self.min_amplitude, self._noise_floor * required_linear_snr)
        # WICHTIG: Release-Schwelle liegt strikt UNTER der Onset-Schwelle (55 %),
        # damit das Ausklingen desselben Klatschers niemals sofort einen 2. Impuls startet!
        release_threshold = max(
            self.min_amplitude * 0.50,
            self._noise_floor * (required_linear_snr * 0.50),
        )

        if not self._in_impulse:
            # Mindestens 3 ruhige Blöcke (~48 ms echte Stille unter der Release-Schwelle)
            # müssen nach einem Impuls vergangen sein, bevor ein neuer Klatsch-Impuls starten darf!
            if hp_peak >= onset_threshold and self._quiet_blocks_since_impulse >= 3:
                if self.input_guard and self.input_guard.is_user_typing_or_clicking():
                    diag.rejected_reason = "pc_input_guard_active"
                    self.last_diagnostics = diag
                    return None

                self._in_impulse = True
                self._impulse_start_ms = now_ms - block_duration_ms
                self._impulse_last_active_ms = now_ms
                self._impulse_peak = hp_peak
                self._impulse_snr_db = snr_db
                self._impulse_hf_ratio = high_freq_ratio
                self._impulse_invalidated = False
                self._quiet_blocks_since_impulse = 0
            else:
                if hp_peak < release_threshold:
                    self._quiet_blocks_since_impulse += 1
                if not self._clap_times:
                    self._update_noise_floor_if_quiet(hp_rms, hp_peak)
        else:
            if hp_peak >= release_threshold:
                self._impulse_last_active_ms = now_ms
                if hp_peak > self._impulse_peak:
                    self._impulse_peak = hp_peak
                    self._impulse_snr_db = max(self._impulse_snr_db, snr_db)
                    self._impulse_hf_ratio = high_freq_ratio

                current_duration = self._impulse_last_active_ms - self._impulse_start_ms
                if current_duration > self.max_clap_duration_ms:
                    self._impulse_invalidated = True
            else:
                # Impuls ist echt unter die Release-Schwelle abgeklungen!
                self._quiet_blocks_since_impulse = 1
                impulse_duration = max(
                    block_duration_ms,
                    self._impulse_last_active_ms - self._impulse_start_ms,
                )
                peak = self._impulse_peak
                imp_snr = self._impulse_snr_db
                imp_hf = self._impulse_hf_ratio
                invalidated = self._impulse_invalidated
                # Nutze den Startzeitpunkt des Impulses (Anstiegsflanke) für präzise Abstandsmessung
                impulse_time_ms = self._impulse_start_ms

                self._in_impulse = False
                self._impulse_peak = 0.0
                self._impulse_invalidated = False

                # Trage die wahren Spitzenwerte des Impulses in diag ein (für die Log-Ausgabe)
                diag.hp_peak = peak
                diag.snr_db = imp_snr
                diag.high_freq_ratio = imp_hf

                if invalidated or impulse_duration > self.max_clap_duration_ms:
                    diag.rejected_reason = f"duration_too_long ({impulse_duration:.0f}ms)"
                    self._reset_clap_sequence()
                elif imp_hf < self.min_high_freq_ratio:
                    diag.rejected_reason = f"low_freq_thud (hf_ratio={imp_hf:.2f})"
                else:
                    self._register_valid_clap(impulse_time_ms, peak, imp_snr, imp_hf, diag)

        event = self._check_sequence_completion(now_ms)
        diag.clap_count_in_buffer = len(self._clap_times)
        self.last_diagnostics = diag
        return event

    def _register_valid_clap(
        self,
        clap_time_ms: float,
        peak: float,
        snr_db: float,
        hf_ratio: float,
        diag: BlockDiagnostics,
    ) -> None:
        if not self._clap_times:
            self._clap_times.append(clap_time_ms)
            self._clap_peaks.append(peak)
            self._clap_snrs.append(snr_db)
            self._clap_hf_ratios.append(hf_ratio)
            diag.single_clap_registered = True
            return

        gap_ms = clap_time_ms - self._clap_times[-1]
        if gap_ms < self.double_clap_min_gap_ms:
            diag.rejected_reason = f"echo_too_close ({gap_ms:.0f}ms)"
            return

        if gap_ms <= self.double_clap_max_gap_ms:
            self._clap_times.append(clap_time_ms)
            self._clap_peaks.append(peak)
            self._clap_snrs.append(snr_db)
            self._clap_hf_ratios.append(hf_ratio)
            diag.single_clap_registered = True
        else:
            self._reset_clap_sequence()
            self._clap_times.append(clap_time_ms)
            self._clap_peaks.append(peak)
            self._clap_snrs.append(snr_db)
            self._clap_hf_ratios.append(hf_ratio)
            diag.single_clap_registered = True

    def _check_sequence_completion(self, now_ms: float) -> Optional[ClapEvent]:
        if not self._clap_times or self._in_impulse:
            return None

        time_since_last_clap = now_ms - self._clap_times[-1]
        count = len(self._clap_times)

        if count == 1:
            if time_since_last_clap > self.double_clap_max_gap_ms:
                self._reset_clap_sequence()
            return None

        if time_since_last_clap < self.settle_window_ms:
            return None

        avg_peak = max(self._clap_peaks)
        avg_snr = sum(self._clap_snrs) / len(self._clap_snrs)
        avg_hf = sum(self._clap_hf_ratios) / len(self._clap_hf_ratios)
        self._reset_clap_sequence()

        if count == 2:
            self._last_trigger_ms = now_ms
            return ClapEvent(
                pattern="double_clap",
                claps=2,
                timestamp_ms=now_ms,
                peak_amplitude=avg_peak,
                snr_db=avg_snr,
                high_freq_ratio=avg_hf,
                noise_floor=self._noise_floor,
            )

        if count == 3 and self.enable_triple_clap:
            self._last_trigger_ms = now_ms
            return ClapEvent(
                pattern="triple_clap",
                claps=3,
                timestamp_ms=now_ms,
                peak_amplitude=avg_peak,
                snr_db=avg_snr,
                high_freq_ratio=avg_hf,
                noise_floor=self._noise_floor,
            )

        return None

    def _reset_clap_sequence(self) -> None:
        self._clap_times.clear()
        self._clap_peaks.clear()
        self._clap_snrs.clear()
        self._clap_hf_ratios.clear()

    def _update_noise_floor_if_quiet(self, hp_rms: float, hp_peak: float) -> None:
        if hp_peak < max(self.min_amplitude * 0.8, self._noise_floor * 3.0):
            self._noise_history.append(max(hp_rms, 0.0005))
            if len(self._noise_history) >= 5:
                sorted_vals = sorted(self._noise_history)
                median_val = sorted_vals[len(sorted_vals) // 2]
                self._noise_floor = 0.85 * self._noise_floor + 0.15 * max(median_val, 0.0008)
