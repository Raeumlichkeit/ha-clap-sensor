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
    2. Hüllkurven-Prüfung (steiler Anstieg, kurzes Decay <= max_clap_duration_ms)
       trennt kurze Klatsch-Impulse von Sprache.
    3. Spektrale Band-Ratio (1000-4800 Hz vs. Tiefton 150-800 Hz) filtert
       dumpfe Tischstöße, Schritte und Türenschlagen heraus.
    4. Zustandsmaschine mit Settle-Window unterscheidet sauber zwischen
       Einzelklatsch (ignoriert), Doppelklatsch (2x), Dreifachklatsch (3x)
       und Applaus (>= 4x, ignoriert).
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        min_amplitude: float = 0.05,
        snr_threshold_db: float = 12.0,
        max_clap_duration_ms: float = 150.0,
        min_high_freq_ratio: float = 0.30,
        double_clap_min_gap_ms: float = 140.0,
        double_clap_max_gap_ms: float = 650.0,
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

        # Gleitender Rauschpegel im Hochpass-Band (> 800 Hz)
        self._noise_history: Deque[float] = deque(maxlen=60)
        self._noise_floor: float = 0.003

        # Aktueller Impuls-Zustand
        self._in_impulse: bool = False
        self._impulse_start_ms: float = 0.0
        self._impulse_last_active_ms: float = 0.0
        self._impulse_peak: float = 0.0
        self._impulse_snr_db: float = 0.0
        self._impulse_hf_ratio: float = 0.0
        self._impulse_invalidated: bool = False

        # Gesammelte gültige Klatsch-Zeitpunkte einer Sequenz
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
        """
        Verarbeitet einen 1D-Audio-Block (float32 im Bereich [-1.0, 1.0] oder int16).
        Gibt ein `ClapEvent` zurück, sobald ein Doppel- oder Dreifachklatschen
        abgeschlossen ist, sonst `None`.
        """
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

        # Prüfe auf Clipping am Rohsignal (billige Webcam-Mikrofone)
        raw_peak = float(np.max(np.abs(x)))
        is_clipped = raw_peak >= 0.98

        # FFT berechnen für:
        # 1. Hochpass-Filterung (> 800 Hz gegen Server-Lüfterrauschen)
        # 2. Spektrales Verhältnis (1000-4800 Hz Klatschband vs. 120-800 Hz Tiefton/Stöße)
        window = np.hanning(n).astype(np.float32)
        spectrum = np.fft.rfft(x * window)
        freqs = np.fft.rfftfreq(n, d=1.0 / self.sample_rate)
        power = np.abs(spectrum) ** 2

        # Hochpass im Frequenzbereich (> highpass_cutoff_hz) für saubere Transienten-Amplitude
        hp_spectrum = np.fft.rfft(x)
        hp_spectrum[freqs < self.highpass_cutoff_hz] = 0.0
        x_hp = np.fft.irfft(hp_spectrum, n=n)
        hp_peak = float(np.max(np.abs(x_hp)))
        hp_rms = float(np.sqrt(np.mean(x_hp * x_hp)) + 1e-9)

        # Frequenzband-Energien
        total_relevant_mask = (freqs >= 120.0) & (freqs <= 6500.0)
        clap_band_mask = (freqs >= 1000.0) & (freqs <= 4800.0)
        low_thud_mask = (freqs >= 120.0) & (freqs < 750.0)

        total_energy = float(np.sum(power[total_relevant_mask])) + 1e-12
        clap_energy = float(np.sum(power[clap_band_mask]))
        low_energy = float(np.sum(power[low_thud_mask]))

        # Bei starkem Clipping entstehen Oberschwingungen; trotzdem dominiert beim Klatschen >= 1 kHz
        high_freq_ratio = clap_energy / (clap_energy + low_energy + 1e-12)
        if total_energy < 1e-9:
            high_freq_ratio = 0.0

        # SNR relativ zum gleitenden Hochpass-Rauschpegel berechnen
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

        # Cooldown nach einer erfolgreichen Auslösung prüfen
        if (now_ms - self._last_trigger_ms) < self.cooldown_ms:
            self._update_noise_floor_if_quiet(hp_rms, hp_peak)
            self.last_diagnostics = diag
            return None

        # Schwellenwert für Impuls-Start und Impuls-Ende
        required_linear_snr = 10.0 ** (self.snr_threshold_db / 20.0)
        onset_threshold = max(self.min_amplitude, self._noise_floor * required_linear_snr)
        release_threshold = max(self.min_amplitude * 0.45, self._noise_floor * (required_linear_snr * 0.45))

        if not self._in_impulse:
            if hp_peak >= onset_threshold:
                # Falls der Nutzer am Windows-PC gerade tippt/klickt, Impuls sofort sperren
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
            else:
                # Ruhiger Block -> Rauschpegel langsam aktualisieren
                self._update_noise_floor_if_quiet(hp_rms, hp_peak)
        else:
            # Wir befinden uns mitten in einem akustischen Impuls
            if hp_peak >= release_threshold:
                self._impulse_last_active_ms = now_ms
                if hp_peak > self._impulse_peak:
                    self._impulse_peak = hp_peak
                    self._impulse_snr_db = max(self._impulse_snr_db, snr_db)
                    self._impulse_hf_ratio = high_freq_ratio

                current_duration = self._impulse_last_active_ms - self._impulse_start_ms
                if current_duration > self.max_clap_duration_ms:
                    # Zu lang für ein Klatschen (z. B. Sprache, Husten, Türquietschen)
                    self._impulse_invalidated = True
            else:
                # Impuls ist abgeklungen -> Auswerten!
                impulse_duration = max(
                    block_duration_ms,
                    self._impulse_last_active_ms - self._impulse_start_ms,
                )
                peak = self._impulse_peak
                imp_snr = self._impulse_snr_db
                imp_hf = self._impulse_hf_ratio
                invalidated = self._impulse_invalidated
                impulse_time_ms = self._impulse_start_ms + (impulse_duration * 0.5)

                # Impuls-Status zurücksetzen
                self._in_impulse = False
                self._impulse_peak = 0.0
                self._impulse_invalidated = False

                if invalidated or impulse_duration > self.max_clap_duration_ms:
                    diag.rejected_reason = f"duration_too_long ({impulse_duration:.0f}ms)"
                    # Dauerlärm während einer laufenden Sequenz bricht die Klatsch-Sequenz ab
                    self._reset_clap_sequence()
                elif imp_hf < self.min_high_freq_ratio:
                    diag.rejected_reason = f"low_freq_thud (hf_ratio={imp_hf:.2f})"
                else:
                    # Gültiger Einzel-Klatsch!
                    self._register_valid_clap(impulse_time_ms, peak, imp_snr, imp_hf, diag)

        # Prüfen, ob eine begonnene Klatsch-Sequenz ihr Settle-Window erreicht hat
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
            # Zu dicht dran (Raum-Echo / Reflexion unter 140 ms) -> ignorieren
            diag.rejected_reason = f"echo_too_close ({gap_ms:.0f}ms)"
            return

        if gap_ms <= self.double_clap_max_gap_ms:
            self._clap_times.append(clap_time_ms)
            self._clap_peaks.append(peak)
            self._clap_snrs.append(snr_db)
            self._clap_hf_ratios.append(hf_ratio)
            diag.single_clap_registered = True
        else:
            # Abstand war zu groß -> dieser Klatsch startet eine neue Sequenz als 1. Klatsch
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
            # Wenn nach dem 1. Klatsch mehr als double_clap_max_gap_ms vergangen ist -> verwerfen
            if time_since_last_clap > self.double_clap_max_gap_ms:
                self._reset_clap_sequence()
            return None

        # Ab 2 oder mehr Klatschern warten wir das Settle-Window ab, um sicherzugehen,
        # ob noch ein 3. Klatschen (triple_clap) oder Applaus (>= 4) folgt.
        if time_since_last_clap < self.settle_window_ms:
            return None

        # Settle-Window ist abgelaufen -> Auswertung der abgeschlossenen Sequenz!
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

        # >= 4 Klatscher hintereinander gelten als Applaus / Störgeräusch -> nicht auslösen
        return None

    def _reset_clap_sequence(self) -> None:
        self._clap_times.clear()
        self._clap_peaks.clear()
        self._clap_snrs.clear()
        self._clap_hf_ratios.clear()

    def _update_noise_floor_if_quiet(self, hp_rms: float, hp_peak: float) -> None:
        # Aktualisiere den Rauschpegel nur, wenn gerade kein starker Impuls anliegt
        if hp_peak < max(self.min_amplitude * 0.8, self._noise_floor * 3.0):
            self._noise_history.append(max(hp_rms, 0.0005))
            if len(self._noise_history) >= 5:
                sorted_vals = sorted(self._noise_history)
                median_val = sorted_vals[len(sorted_vals) // 2]
                # Sanfte Glättung
                self._noise_floor = 0.85 * self._noise_floor + 0.15 * max(median_val, 0.0008)
