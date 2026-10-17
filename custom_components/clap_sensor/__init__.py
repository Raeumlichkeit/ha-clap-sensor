"""The Clap Sensor (Double & Triple Clap) HACS integration."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

import numpy as np
import requests

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
    CONF_TARGET_ENTITY_ID,
    CONF_TRIPLE_ENTITY_ID,
    DEFAULT_ENABLE_TRIPLE_CLAP,
    DEFAULT_INPUT_DEVICE,
    DEFAULT_MIN_AMPLITUDE,
    DEFAULT_MIN_HIGH_FREQ_RATIO,
    DEFAULT_SNR_THRESHOLD_DB,
    DOMAIN,
    EVENT_CLAP_DETECTED,
    SIGNAL_CLAP_EVENT,
    SIGNAL_STATUS_UPDATE,
)
from .detector import ClapDetector, ClapEvent

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
]


def configure_haos_supervisor_audio() -> tuple[Optional[str], str]:
    """
    Fragt den Home Assistant Supervisor Audio-Dienst (http://supervisor/audio/info) ab,
    sucht das USB-Webcam-Mikrofon, schaltet es automatisch stummfrei (unmute),
    setzt die Eingangs-Lautstärke auf 100 % und aktiviert es als Standard-Eingang!
    Gibt (pulse_source_name, diagnose_text) zurück.
    """
    token = (
        os.environ.get("SUPERVISOR_TOKEN")
        or os.environ.get("HASSIO_TOKEN")
        or ""
    ).strip()
    if not token:
        return None, "Kein SUPERVISOR_TOKEN"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        # Optional: Audio-Hardware im Supervisor neu einlesen lassen, falls Webcam per Hotplug kam
        requests.post("http://supervisor/audio/reload", headers=headers, timeout=3.0)
    except Exception:
        pass

    try:
        resp = requests.get("http://supervisor/audio/info", headers=headers, timeout=4.0)
        if not resp.ok:
            return None, f"Supervisor Audio HTTP {resp.status_code}"
        payload = resp.json()
        audio_data = payload.get("data", {}).get("audio", {})
        inputs: list[dict[str, Any]] = audio_data.get("input", []) or []
    except Exception as exc:
        return None, f"Supervisor Audio Fehler: {exc}"

    if not inputs:
        return None, "Keine Mikrofone in Supervisor Audio gefunden"

    # Bevorzuge USB-/Webcam-Mikrofon, sonst erstes echtes Eingabegerät
    chosen: dict[str, Any] = inputs[0]
    for inp in inputs:
        name_desc = f"{inp.get('name', '')} {inp.get('description', '')}".lower()
        if "usb" in name_desc or "webcam" in name_desc or "c270" in name_desc:
            chosen = inp
            break

    src_name = str(chosen.get("name", "")).strip()
    src_idx = chosen.get("index")
    src_desc = str(chosen.get("description", src_name)).strip()

    # 1. Als Standard-Eingabegerät in HAOS setzen
    if src_name:
        try:
            requests.post(
                "http://supervisor/audio/default/input",
                headers=headers,
                json={"name": src_name},
                timeout=3.0,
            )
        except Exception:
            pass

    # 2. Stummschaltung aufheben (Unmute) & Pegel auf 100 % (1.0) setzen
    if src_idx is not None:
        try:
            requests.post(
                "http://supervisor/audio/mute/input",
                headers=headers,
                json={"index": src_idx, "active": False},
                timeout=3.0,
            )
            requests.post(
                "http://supervisor/audio/volume/input",
                headers=headers,
                json={"index": src_idx, "volume": 1.0},
                timeout=3.0,
            )
        except Exception:
            pass

    all_names = ", ".join(
        f"{i.get('description') or i.get('name')}" for i in inputs
    )
    return src_name, f"Aktiviert: {src_desc} (Alle: {all_names})"


def discover_alsa_usb_cards() -> tuple[list[str], str]:
    """Liest /proc/asound/cards aus, um angeschlossene USB-Mikrofone zu finden."""
    cards_file = Path("/proc/asound/cards")
    if not cards_file.is_file():
        return [], "Kein /proc/asound/cards"

    try:
        content = cards_file.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception as exc:
        return [], f"Fehler: {exc}"

    if not content or "no soundcards" in content.lower():
        return [], "Keine Soundkarte in /proc/asound/cards"

    usb_devices: list[str] = []
    other_devices: list[str] = []
    summary_lines: list[str] = []

    for line in content.splitlines():
        m = re.match(r"^\s*(\d+)\s+\[([^\]]+)\]:\s*(.+)$", line)
        if m:
            card_num, _card_id, card_desc = m.group(1), m.group(2).strip(), m.group(3).strip()
            summary_lines.append(f"Card {card_num}: {card_desc}")
            # dsnoop erlaubt geteilten Zugriff, falls PulseAudio die Karte offen hält
            if "usb" in card_desc.lower() or "webcam" in card_desc.lower() or "c270" in card_desc.lower():
                usb_devices.extend([f"plughw:{card_num},0", f"dsnoop:{card_num},0", f"hw:{card_num},0"])
            else:
                other_devices.append(f"plughw:{card_num},0")

    ordered = usb_devices + other_devices
    return ordered, "; ".join(summary_lines) if summary_lines else content[:120]


def _build_capture_commands(
    input_device: str,
    pulse_source: Optional[str],
    sample_rate: int = 16000,
) -> tuple[list[tuple[str, list[str], dict[str, str]]], str]:
    """
    Erzeugt eine priorisierte Liste von (Label, Kommando, Env-Dict)-Tupeln.
    """
    dev = (input_device or "auto").strip()
    alsa_devs, cards_summary = discover_alsa_usb_cards()
    cmds: list[tuple[str, list[str], dict[str, str]]] = []

    base_env = dict(os.environ)
    if pulse_source:
        base_env["PULSE_SOURCE"] = pulse_source

    explicit_dev = dev.lower() not in ("default", "auto", "")

    if explicit_dev and shutil.which("ffmpeg"):
        cmds.append(
            (
                f"ALSA ({dev})",
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-f",
                    "alsa",
                    "-i",
                    dev,
                    "-ac",
                    "1",
                    "-ar",
                    str(sample_rate),
                    "-f",
                    "s16le",
                    "-",
                ],
                base_env,
            )
        )

    if shutil.which("ffmpeg"):
        # 1. In HAOS leitet ALSA 'default' über /etc/asound.conf direkt an PulseAudio weiter.
        # Mit gesetztem PULSE_SOURCE greift 'default' exakt die USB-Webcam ab!
        cmds.append(
            (
                f"HAOS Audio ({pulse_source or 'default'})",
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-f",
                    "alsa",
                    "-i",
                    "default",
                    "-ac",
                    "1",
                    "-ar",
                    str(sample_rate),
                    "-f",
                    "s16le",
                    "-",
                ],
                base_env,
            )
        )

        # 2. Direkter ALSA-Zugriff auf die USB-Soundkarte (falls PulseAudio sie nicht exklusiv sperrt)
        for alsa_dev in alsa_devs:
            cmds.append(
                (
                    f"USB-ALSA ({alsa_dev})",
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
                    ],
                    base_env,
                )
            )

    if shutil.which("parec"):
        parec_cmd = [
            "parec",
            f"--rate={sample_rate}",
            "--format=s16le",
            "--channels=1",
            "--latency-msec=16",
        ]
        if pulse_source:
            parec_cmd.append(f"--device={pulse_source}")
        cmds.append(("parec (PulseAudio)", parec_cmd, base_env))

    return cmds, cards_summary


class ClapAudioWorker:
    """Hintergrund-Thread für das Auslesen des USB-Mikrofons direkt in der HACS-Integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.current_status: str = "Initialisiere..."
        self.status_attributes: dict[str, Any] = {}

    def start(self) -> None:
        cfg = {**self.entry.data, **self.entry.options}
        input_device = str(cfg.get(CONF_INPUT_DEVICE, DEFAULT_INPUT_DEVICE)).strip()
        if input_device.lower() == "event_only":
            self._publish_status(
                "Wartet auf Add-on Events (event_only)",
                {"mode": "event_only"},
            )
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

    def _publish_status(self, status: str, extra: Optional[dict[str, Any]] = None) -> None:
        self.current_status = status
        if extra:
            self.status_attributes.update(extra)
        self.hass.loop.call_soon_threadsafe(
            async_dispatcher_send,
            self.hass,
            f"{SIGNAL_STATUS_UPDATE}_{self.entry.entry_id}",
            self.current_status,
            dict(self.status_attributes),
        )

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

        cmd_idx = 0
        while not self._stop_event.is_set():
            # Vor jedem Startversuch das USB-Mikrofon in HAOS Supervisor Audio aktivieren & entstummen
            pulse_source, supervisor_info = configure_haos_supervisor_audio()
            cmds, cards_summary = _build_capture_commands(
                input_device, pulse_source, sample_rate
            )
            self.status_attributes["alsa_soundcards"] = cards_summary
            self.status_attributes["supervisor_audio"] = supervisor_info

            if not cmds:
                self._publish_status(
                    "Kein Audio-Tool gefunden",
                    {"alsa_soundcards": cards_summary},
                )
                return

            label, cmd, env = cmds[cmd_idx % len(cmds)]
            proc: Optional[subprocess.Popen] = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=env,
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

                zero_blocks = 0
                blocks_read = 0
                max_peak_window = 0.0

                while not self._stop_event.is_set():
                    try:
                        chunk = q.get(timeout=4.0)
                    except queue.Empty:
                        raise RuntimeError(f"Timeout bei {label}")

                    if chunk is None or len(chunk) < bytes_per_block:
                        raise RuntimeError(f"{label} konnte Gerät nicht öffnen")

                    samples = np.frombuffer(chunk, dtype=np.int16)
                    raw_max = int(np.max(np.abs(samples)))
                    norm_peak = raw_max / 32768.0
                    if norm_peak > max_peak_window:
                        max_peak_window = norm_peak

                    if raw_max == 0:
                        zero_blocks += 1
                        # Falls 15 Sekunden lang exakt digitale Nullen kommen, einmalig Supervisor Audio neu setzen,
                        # aber den laufenden Stream NICHT abbrechen, sondern offen halten!
                        if zero_blocks == 900:
                            configure_haos_supervisor_audio()
                            self._publish_status(
                                f"Aktiv ({label}) - Signalpegel ist 0 (Mikrofon stumm?)",
                                {
                                    "live_peak": 0.0,
                                    "supervisor_audio": supervisor_info,
                                    "alsa_soundcards": cards_summary,
                                },
                            )
                    else:
                        zero_blocks = 0

                    blocks_read += 1
                    if blocks_read == 20:
                        _LOGGER.info("Clap Sensor Mikrofon-Stream stabil über: %s", label)
                        self._publish_status(
                            f"Bereit ({label})",
                            {
                                "active_backend": label,
                                "live_peak": round(max_peak_window, 4),
                                "noise_floor": round(detector._noise_floor, 5),
                            },
                        )
                    elif blocks_read % 200 == 0 and raw_max > 0:
                        # Alle ~3,2 Sekunden Live-Pegel im Sensor aktualisieren
                        self._publish_status(
                            f"Bereit ({label})",
                            {
                                "active_backend": label,
                                "live_peak": round(max_peak_window, 4),
                                "noise_floor": round(detector._noise_floor, 5),
                            },
                        )
                        max_peak_window = 0.0

                    ev = detector.process_block(samples)
                    diag = detector.last_diagnostics

                    if diag is not None:
                        if diag.single_clap_registered:
                            self._publish_status(
                                f"Klatsch #{diag.clap_count_in_buffer} erkannt! ({label})",
                                {
                                    "last_heard_peak": round(diag.hp_peak, 4),
                                    "last_heard_snr_db": round(diag.snr_db, 1),
                                    "last_heard_hf_ratio": round(diag.high_freq_ratio, 2),
                                    "noise_floor": round(diag.noise_floor, 5),
                                },
                            )
                        elif diag.rejected_reason:
                            self._publish_status(
                                f"Bereit ({label})",
                                {
                                    "last_rejected_reason": diag.rejected_reason,
                                    "last_heard_peak": round(diag.hp_peak, 4),
                                    "last_heard_snr_db": round(diag.snr_db, 1),
                                    "last_heard_hf_ratio": round(diag.high_freq_ratio, 2),
                                    "noise_floor": round(diag.noise_floor, 5),
                                },
                            )

                    if ev is not None:
                        self._notify_homeassistant(ev)

            except Exception as exc:
                if self._stop_event.is_set():
                    break
                cmd_idx += 1
                self._publish_status(
                    f"Verbinde Mikrofon... ({cards_summary[:50]})",
                    {"last_error": str(exc)},
                )
                time.sleep(2.0)
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


async def async_trigger_configured_entity(
    hass: HomeAssistant, entry: ConfigEntry, data: dict[str, Any]
) -> None:
    """Schaltet direkt die in den Integrations-Optionen gewählte Entität um."""
    cfg = {**entry.data, **entry.options}
    pattern = data.get("pattern")
    claps = data.get("claps")

    target_entity = ""
    if pattern == "double_clap" or claps == 2:
        target_entity = str(cfg.get(CONF_TARGET_ENTITY_ID, "") or "").strip()
    elif pattern == "triple_clap" or claps == 3:
        target_entity = str(cfg.get(CONF_TRIPLE_ENTITY_ID, "") or "").strip()

    if not target_entity:
        return

    _LOGGER.info("Clap Sensor schaltet konfigurierte Entität um: %s", target_entity)
    await hass.services.async_call(
        "homeassistant",
        "toggle",
        {"entity_id": target_entity},
        blocking=False,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Richtet den Clap Sensor über einen Config Entry ein."""
    hass.data.setdefault(DOMAIN, {})

    worker = ClapAudioWorker(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = worker

    @callback
    def _handle_clap_event(event: Event) -> None:
        data: dict[str, Any] = dict(event.data or {})
        async_dispatcher_send(hass, f"{SIGNAL_CLAP_EVENT}_{entry.entry_id}", data)
        hass.async_create_task(async_trigger_configured_entity(hass, entry, data))

    unsub_event = hass.bus.async_listen(EVENT_CLAP_DETECTED, _handle_clap_event)

    def _stop_worker(_event: Event) -> None:
        worker.stop()

    unsub_stop = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_worker)
    entry.async_on_unload(unsub_event)
    entry.async_on_unload(unsub_stop)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    worker.start()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Entlädt den Clap Sensor Config Entry."""
    worker: Optional[ClapAudioWorker] = hass.data[DOMAIN].pop(entry.entry_id, None)
    if worker is not None:
        worker.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
