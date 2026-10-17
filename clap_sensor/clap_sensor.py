from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import numpy as np
import yaml

from detector import ClapDetector, ClapEvent, WindowsInputGuard
from ha_client import HomeAssistantDispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("clap_sensor")


DEFAULT_OPTIONS: Dict[str, Any] = {
    "audio_backend": "auto",
    "input_device": "default",
    "sample_rate": 16000,
    "block_duration_ms": 16,
    "min_amplitude": 0.05,
    "snr_threshold_db": 12.0,
    "max_clap_duration_ms": 150,
    "min_high_freq_ratio": 0.30,
    "double_clap_min_gap_ms": 140,
    "double_clap_max_gap_ms": 650,
    "settle_window_ms": 480,
    "cooldown_ms": 1500,
    "enable_triple_clap": True,
    "target_entity_id": "",
    "target_service": "homeassistant.toggle",
    "triple_clap_entity_id": "",
    "triple_clap_service": "homeassistant.toggle",
    "webhook_url": "",
    "ignore_during_pc_input_ms": 400,
    "log_calibration": True,
}


def load_options(config_path: Optional[str] = None) -> Dict[str, Any]:
    options = dict(DEFAULT_OPTIONS)

    # 1. Home Assistant Add-on Optionen (/data/options.json)
    ha_options_file = Path("/data/options.json")
    if ha_options_file.is_file():
        try:
            with ha_options_file.open("r", encoding="utf-8") as f:
                ha_opts = json.load(f)
            if isinstance(ha_opts, dict):
                options.update(ha_opts)
            logger.info("Konfiguration aus /data/options.json (Home Assistant Add-on) geladen.")
            return options
        except Exception as exc:
            logger.warning("Konnte /data/options.json nicht lesen: %s", exc)

    # 2. Lokale config.yaml
    cfg_file = Path(config_path) if config_path else Path(__file__).parent / "config.yaml"
    if cfg_file.is_file():
        try:
            with cfg_file.open("r", encoding="utf-8") as f:
                raw_yaml = yaml.safe_load(f) or {}
            if isinstance(raw_yaml, dict):
                if "options" in raw_yaml and isinstance(raw_yaml["options"], dict):
                    options.update(raw_yaml["options"])
                else:
                    options.update(raw_yaml)
            logger.info("Konfiguration aus %s geladen.", cfg_file)
        except Exception as exc:
            logger.warning("Konnte %s nicht lesen: %s", cfg_file, exc)

    return options


def select_audio_backend(preferred: str) -> str:
    preferred = (preferred or "auto").lower().strip()
    if preferred in ("parec", "arecord", "sounddevice"):
        return preferred

    # Im HAOS Add-on (oder Linux mit PulseAudio) ist 'parec' der direkteste Weg
    if shutil.which("parec"):
        return "parec"
    if shutil.which("arecord"):
        return "arecord"
    return "sounddevice"


def stream_subprocess_pcm(
    cmd: list[str],
    block_samples: int,
    stop_event: threading.Event,
) -> Iterator[np.ndarray]:
    """
    Startet `parec` oder `arecord` und liest 16-Bit-PCM-Blöcke mit einem
    integrierten Stream-Watchdog (erkennt eingefrorene Prozesse oder Null-Streams
    nach USB-Reset und wirft RuntimeError für sauberen Auto-Reconnect).
    """
    bytes_per_block = block_samples * 2  # int16 = 2 Bytes pro Sample
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=bytes_per_block * 4,
    )

    q: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=64)

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            while not stop_event.is_set():
                chunk = proc.stdout.read(bytes_per_block)
                if not chunk:
                    q.put(None)
                    break
                q.put(chunk)
        except Exception:
            q.put(None)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    zero_blocks_in_a_row = 0
    max_zero_blocks = 500  # ca. 8 Sekunden komplett tote Null-Bytes -> USB/Audio-Reset nötig

    try:
        while not stop_event.is_set():
            try:
                chunk = q.get(timeout=3.5)
            except queue.Empty:
                raise RuntimeError(
                    f"Audio-Stream Timeout (3.5s keine Daten von {cmd[0]}) - führe Reconnect durch."
                )

            if chunk is None or len(chunk) < bytes_per_block:
                stderr_out = ""
                if proc.stderr:
                    try:
                        stderr_out = proc.stderr.read().decode("utf-8", errors="ignore").strip()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"Audio-Prozess {cmd[0]} beendet ({stderr_out or 'EOF'}) - führe Reconnect durch."
                )

            samples = np.frombuffer(chunk, dtype=np.int16)
            if np.max(np.abs(samples)) == 0:
                zero_blocks_in_a_row += 1
                if zero_blocks_in_a_row >= max_zero_blocks:
                    raise RuntimeError(
                        "Watchdog: Audio-Stream liefert seit >8s ausschließlich Null-Bytes (USB-Mikrofon getrennt/stumm). Starte Stream neu..."
                    )
            else:
                zero_blocks_in_a_row = 0

            yield samples
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def build_parec_cmd(sample_rate: int, block_duration_ms: int, input_device: str) -> list[str]:
    cmd = [
        "parec",
        f"--rate={sample_rate}",
        "--format=s16le",
        "--channels=1",
        f"--latency-msec={max(10, block_duration_ms)}",
    ]
    if input_device and input_device.lower() not in ("default", "null", "none", ""):
        cmd.append(f"--device={input_device}")
    return cmd


def build_arecord_cmd(sample_rate: int, block_samples: int, input_device: str) -> list[str]:
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
        str(block_samples),
    ]
    if input_device and input_device.lower() not in ("default", "null", "none", ""):
        cmd.extend(["-D", input_device])
    return cmd


def stream_sounddevice_pcm(
    sample_rate: int,
    block_samples: int,
    input_device: str,
    stop_event: threading.Event,
) -> Iterator[np.ndarray]:
    import sounddevice as sd

    dev: Any = None
    if input_device and input_device.lower() not in ("default", "null", "none", ""):
        dev = int(input_device) if input_device.isdigit() else input_device

    q: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)

    def _callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        try:
            q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    with sd.InputStream(
        samplerate=sample_rate,
        blocksize=block_samples,
        device=dev,
        channels=1,
        dtype="float32",
        callback=_callback,
    ):
        while not stop_event.is_set():
            try:
                block = q.get(timeout=3.5)
                yield block
            except queue.Empty:
                raise RuntimeError("sounddevice Stream-Timeout (3.5s) - führe Reconnect durch.")


def list_audio_devices() -> None:
    print("=== Verfügbare Audio-Eingabegeräte ===")
    if shutil.which("pactl"):
        print("\n[PulseAudio Sources (pactl)]:")
        subprocess.run(["pactl", "list", "short", "sources"], check=False)
    if shutil.which("arecord"):
        print("\n[ALSA Capture Devices (arecord -l)]:")
        subprocess.run(["arecord", "-l"], check=False)
    try:
        import sounddevice as sd

        print("\n[PortAudio / sounddevice Devices]:")
        print(sd.query_devices())
    except Exception:
        pass


def run_sensor(options: Dict[str, Any], force_calibrate: bool = False) -> None:
    sample_rate = int(options.get("sample_rate", 16000))
    block_duration_ms = int(options.get("block_duration_ms", 16))
    block_samples = max(64, int(sample_rate * (block_duration_ms / 1000.0)))
    input_device = str(options.get("input_device", "default") or "default").strip()
    backend = select_audio_backend(str(options.get("audio_backend", "auto")))
    log_calibration = force_calibrate or bool(options.get("log_calibration", True))

    input_guard = WindowsInputGuard(
        ignore_window_ms=int(options.get("ignore_during_pc_input_ms", 400) or 0)
    )
    detector = ClapDetector(
        sample_rate=sample_rate,
        min_amplitude=float(options.get("min_amplitude", 0.05)),
        snr_threshold_db=float(options.get("snr_threshold_db", 12.0)),
        max_clap_duration_ms=float(options.get("max_clap_duration_ms", 150.0)),
        min_high_freq_ratio=float(options.get("min_high_freq_ratio", 0.30)),
        double_clap_min_gap_ms=float(options.get("double_clap_min_gap_ms", 140.0)),
        double_clap_max_gap_ms=float(options.get("double_clap_max_gap_ms", 650.0)),
        settle_window_ms=float(options.get("settle_window_ms", 480.0)),
        cooldown_ms=float(options.get("cooldown_ms", 1500.0)),
        enable_triple_clap=bool(options.get("enable_triple_clap", True)),
        input_guard=input_guard,
    )
    dispatcher = HomeAssistantDispatcher(options)

    stop_event = threading.Event()

    def _handle_sig(*_args: Any) -> None:
        logger.info("Beende Klatsch-Sensor...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    logger.info(
        "Klatsch-Sensor bereit | Backend=%s | Device=%s | Rate=%d Hz | MinAmp=%.3f | MinSNR=%.1f dB | 3xClap=%s",
        backend,
        input_device,
        sample_rate,
        detector.min_amplitude,
        detector.snr_threshold_db,
        detector.enable_triple_clap,
    )
    if dispatcher.is_supervisor_available:
        logger.info(
            "Home Assistant Supervisor-API verbunden (target_entity_id='%s', triple_clap_entity_id='%s').",
            dispatcher.target_entity_id or "-",
            dispatcher.triple_clap_entity_id or "-",
        )
    elif dispatcher.webhook_url:
        logger.info("Webhook-Modus aktiv: %s", dispatcher.webhook_url)

    while not stop_event.is_set():
        try:
            if backend == "parec":
                cmd = build_parec_cmd(sample_rate, block_duration_ms, input_device)
                stream = stream_subprocess_pcm(cmd, block_samples, stop_event)
            elif backend == "arecord":
                cmd = build_arecord_cmd(sample_rate, block_samples, input_device)
                stream = stream_subprocess_pcm(cmd, block_samples, stop_event)
            else:
                stream = stream_sounddevice_pcm(
                    sample_rate, block_samples, input_device, stop_event
                )

            for block in stream:
                event = detector.process_block(block)
                diag = detector.last_diagnostics

                if log_calibration and diag is not None:
                    if diag.single_clap_registered:
                        logger.info(
                            "[Kalibrierung] Einzel-Klatsch #%d erkannt! (Peak=%.3f, SNR=+%.1f dB, HF-Ratio=%.2f, NoiseFloor=%.4f%s)",
                            diag.clap_count_in_buffer,
                            diag.hp_peak,
                            diag.snr_db,
                            diag.high_freq_ratio,
                            diag.noise_floor,
                            " [CLIPPED]" if diag.is_clipped else "",
                        )
                    elif diag.rejected_reason:
                        logger.info(
                            "[Kalibrierung] Geräusch ignoriert: %s (Peak=%.3f, SNR=+%.1f dB, HF-Ratio=%.2f)",
                            diag.rejected_reason,
                            diag.hp_peak,
                            diag.snr_db,
                            diag.high_freq_ratio,
                        )

                if event is not None:
                    logger.info(
                        ">>> %s (%dx Klatschen) ERKANNT! (Peak=%.3f, SNR=+%.1f dB, HF-Ratio=%.2f) <<<",
                        event.pattern.upper(),
                        event.claps,
                        event.peak_amplitude,
                        event.snr_db,
                        event.high_freq_ratio,
                    )
                    dispatcher.dispatch(event)

        except Exception as exc:
            if stop_event.is_set():
                break
            logger.warning("Audio-Stream unterbrochen: %s. Versuche Neustart in 3 Sekunden...", exc)
            time.sleep(3.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Klatsch-Sensor (Double & Triple Clap) für Home Assistant / Proxmox USB-Mikrofone"
    )
    parser.add_argument("--config", type=str, default=None, help="Pfad zur config.yaml")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Aktiviert ausführliche Live-Kalibrierungs-Logs für jedes erkannte/verworfene Geräusch",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Listet alle verfügbaren Audio-Eingabegeräte auf und beendet das Programm",
    )
    parser.add_argument(
        "--test-action",
        choices=["double", "triple"],
        default=None,
        help="Sendet sofort ein Test-Event (double_clap oder triple_clap) an Home Assistant",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    options = load_options(args.config)

    if args.test_action:
        dispatcher = HomeAssistantDispatcher(options)
        claps = 2 if args.test_action == "double" else 3
        pattern = "double_clap" if claps == 2 else "triple_clap"
        test_event = ClapEvent(
            pattern=pattern,
            claps=claps,
            timestamp_ms=0.0,
            peak_amplitude=0.42,
            snr_db=22.5,
            high_freq_ratio=0.68,
            noise_floor=0.002,
        )
        logger.info("Sende Test-Trigger (%s)...", pattern)
        dispatcher.dispatch(test_event, async_exec=False)
        return

    run_sensor(options, force_calibrate=args.calibrate)


if __name__ == "__main__":
    main()
