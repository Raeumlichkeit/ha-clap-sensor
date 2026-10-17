"""The Clap Sensor (Double & Triple Clap) HACS integration."""
from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

import numpy as np

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_ENABLE_TRIPLE_CLAP,
    CONF_INPUT_DEVICE,
    CONF_MIN_AMPLITUDE,
    CONF_MIN_HIGH_FREQ_RATIO,
    CONF_SNR_THRESHOLD_DB,
    DEFAULT_ENABLE_TRIPLE_CLAP,
    DEFAULT_INPUT_DEVICE,
    DEFAULT_MIN_AMPLITUDE,
    DEFAULT_MIN_HIGH_FREQ_RATIO,
    DEFAULT_SNR_THRESHOLD_DB,
    DOMAIN,
    EVENT_CLAP_DETECTED,
    SIGNAL_CLAP_EVENT,
)
from .detector import ClapDetector, ClapEvent

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


def _build_capture_commands(input_device: str, sample_rate: int = 16000) -> list[list[str]]:
    """
    Liefert eine priorisierte Liste möglicher Audio-Capture-Befehle im HAOS-Container:
    1. parec (PulseAudio)
    2. ffmpeg via PulseAudio (in jedem offiziellen Home Assistant Core Container vorinstalliert!)
    3. arecord (ALSA)
    4. ffmpeg via ALSA
    """
    dev = (input_device or "default").strip()
    cmds: list[list[str]] = []

    if shutil.which("parec"):
        cmd = [
            "parec",
            f"--rate={sample_rate}",
            "--format=s16le",
            "--channels=1",
            "--latency-msec=16",
        ]
        if dev.lower() not in ("default", "auto", ""):
            cmd.append(f"--device={dev}")
        cmds.append(cmd)

    if shutil.which("ffmpeg"):
        pulse_dev = "default" if dev.lower() in ("default", "auto", "") else dev
        cmds.append(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "pulse",
                "-i",
                pulse_dev,
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-",
            ]
        )
        alsa_dev = "default" if dev.lower() in ("default", "auto", "") else dev
        cmds.append(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "alsa",
                "-i",
                alsa_dev,
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-",
            ]
        )

    if shutil.which("arecord"):
        cmd = [
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-c",
            "1",
            "-r",
            str(sample_rate),
            "--period-size",
            "256",
        ]
        if dev.lower() not in ("default", "auto", ""):
            cmd.extend(["-D", dev])
        cmds.append(cmd)

    return cmds


class ClapAudioWorker:
    """Hintergrund-Thread für das Auslesen des USB-Mikrofons direkt in der HACS-Integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        cfg = {**self.entry.data, **self.entry.options}
        input_device = str(cfg.get(CONF_INPUT_DEVICE, DEFAULT_INPUT_DEVICE)).strip()
        if input_device.lower() == "event_only":
            _LOGGER.info("Clap Sensor läuft im 'event_only'-Modus (hört auf Add-on/Webhook-Events).")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="clap_sensor_audio_worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run_loop(self) -> None:
        cfg = {**self.entry.data, **self.entry.options}
        sample_rate = 16000
        block_samples = 256  # 16 ms bei 16 kHz
        bytes_per_block = block_samples * 2
        input_device = str(cfg.get(CONF_INPUT_DEVICE, DEFAULT_INPUT_DEVICE)).strip()

        detector = ClapDetector(
            sample_rate=sample_rate,
            min_amplitude=float(cfg.get(CONF_MIN_AMPLITUDE, DEFAULT_MIN_AMPLITUDE)),
            snr_threshold_db=float(cfg.get(CONF_SNR_THRESHOLD_DB, DEFAULT_SNR_THRESHOLD_DB)),
            min_high_freq_ratio=float(cfg.get(CONF_MIN_HIGH_FREQ_RATIO, DEFAULT_MIN_HIGH_FREQ_RATIO)),
            enable_triple_clap=bool(cfg.get(CONF_ENABLE_TRIPLE_CLAP, DEFAULT_ENABLE_TRIPLE_CLAP)),
        )

        cmds = _build_capture_commands(input_device, sample_rate)
        if not cmds:
            _LOGGER.warning(
                "Kein Audio-Capture-Tool (parec/ffmpeg/arecord) gefunden. HACS-Integration wartet auf externe '%s'-Events.",
                EVENT_CLAP_DETECTED,
            )
            return

        cmd_idx = 0
        while not self._stop_event.is_set():
            cmd = cmds[cmd_idx % len(cmds)]
            proc: Optional[subprocess.Popen] = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=bytes_per_block * 4,
                )
                q: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=64)

                def _reader(p: subprocess.Popen) -> None:
                    try:
                        assert p.stdout is not None
                        while not self._stop_event.is_set():
                            chunk = p.stdout.read(bytes_per_block)
                            if not chunk:
                                q.put(None)
                                break
                            q.put(chunk)
                    except Exception:
                        q.put(None)

                threading.Thread(target=_reader, args=(proc,), daemon=True).start()
                _LOGGER.info("Clap Sensor Mikrofon-Stream aktiv (%s)", " ".join(cmd[:4]))

                zero_blocks = 0
                while not self._stop_event.is_set():
                    try:
                        chunk = q.get(timeout=4.0)
                    except queue.Empty:
                        raise RuntimeError("Audio-Stream Timeout (4s)")

                    if chunk is None or len(chunk) < bytes_per_block:
                        raise RuntimeError("Audio-Stream vorzeitig beendet")

                    samples = np.frombuffer(chunk, dtype=np.int16)
                    if np.max(np.abs(samples)) == 0:
                        zero_blocks += 1
                        if zero_blocks >= 500:
                            raise RuntimeError("Audio-Stream liefert nur Null-Bytes (Watchdog)")
                    else:
                        zero_blocks = 0

                    ev = detector.process_block(samples)
                    if ev is not None:
                        self._notify_homeassistant(ev)

            except Exception as exc:
                if self._stop_event.is_set():
                    break
                cmd_idx += 1
                _LOGGER.debug("Wechsle/Starte Audio-Stream neu (%s)...", exc)
                time.sleep(4.0)
            finally:
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

    def _notify_homeassistant(self, ev: ClapEvent) -> None:
        payload = {
            "pattern": ev.pattern,
            "claps": ev.claps,
            "peak_amplitude": round(ev.peak_amplitude, 4),
            "snr_db": round(ev.snr_db, 1),
            "high_freq_ratio": round(ev.high_freq_ratio, 2),
            "source": "hacs_integration",
        }
        self.hass.loop.call_soon_threadsafe(
            self.hass.bus.async_fire, EVENT_CLAP_DETECTED, payload
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Richtet den Clap Sensor über einen Config Entry ein."""
    hass.data.setdefault(DOMAIN, {})

    worker = ClapAudioWorker(hass, entry)
    worker.start()

    @callback
    def _handle_clap_event(event: Event) -> None:
        data: dict[str, Any] = dict(event.data or {})
        async_dispatcher_send(hass, f"{SIGNAL_CLAP_EVENT}_{entry.entry_id}", data)

    unsub_event = hass.bus.async_listen(EVENT_CLAP_DETECTED, _handle_clap_event)

    def _stop_worker(_event: Event) -> None:
        worker.stop()

    unsub_stop = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_worker)
    entry.async_on_unload(unsub_event)
    entry.async_on_unload(unsub_stop)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    hass.data[DOMAIN][entry.entry_id] = worker
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Entlädt den Clap Sensor Config Entry."""
    worker: Optional[ClapAudioWorker] = hass.data[DOMAIN].pop(entry.entry_id, None)
    if worker is not None:
        worker.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
