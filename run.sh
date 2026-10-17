#!/usr/bin/env sh
set -e

export PYTHONUNBUFFERED=1

echo "============================================================"
echo " Starte Home Assistant Clap Sensor Add-on (v1.0.0)"
echo "============================================================"

if command -v pactl >/dev/null 2>&1; then
    echo "[Audio-Info] Verfügbare PulseAudio Eingabequellen (Sources):"
    pactl list short sources 2>/dev/null || true
fi

if command -v arecord >/dev/null 2>&1; then
    echo "[Audio-Info] Verfügbare ALSA Capture-Geräte:"
    arecord -l 2>/dev/null || true
fi

echo "============================================================"
exec python3 /app/clap_sensor.py
