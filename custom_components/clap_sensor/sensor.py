"""Diagnostic sensors for Clap Sensor (Microphone Status, Last Pattern & SNR dB)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_CLAP_EVENT, SIGNAL_STATUS_UPDATE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Erstellt die Diagnose-Sensoren für Mikrofon-Status, letztes Muster und SNR-Wert."""
    async_add_entities(
        [
            ClapMicrophoneStatusSensor(hass, entry),
            ClapLastPatternSensor(entry),
            ClapLastSnrSensor(entry),
        ]
    )


class ClapMicrophoneStatusSensor(SensorEntity):
    """Zeigt live an, ob das USB-Mikrofon gefunden wurde und was es zuletzt gehört hat."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Mikrofon-Status"
    _attr_icon = "mdi:microphone"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mic_status"
        worker = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        self._attr_native_value = worker.current_status if worker else "Initialisiere..."
        self._attr_extra_state_attributes: dict[str, Any] = (
            dict(worker.status_attributes) if worker else {}
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Clap Sensor (USB-Mikrofon)",
            manufacturer="Clap Sensor DSP",
            model="Double & Triple Clap Detector",
            sw_version="1.1.0",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATUS_UPDATE}_{self._entry.entry_id}",
                self._async_on_status,
            )
        )

    @callback
    def _async_on_status(self, status: str, attributes: dict[str, Any]) -> None:
        self._attr_native_value = status
        self._attr_extra_state_attributes = attributes
        self.async_write_ha_state()


class ClapLastPatternSensor(SensorEntity):
    """Zeigt das zuletzt erkannte Klatsch-Muster ('double_clap' / 'triple_clap')."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Letztes Klatsch-Muster"
    _attr_icon = "mdi:waveform"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_pattern"
        self._attr_native_value = "none"
        self._attr_extra_state_attributes: dict[str, Any] = {}

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Clap Sensor (USB-Mikrofon)",
            manufacturer="Clap Sensor DSP",
            model="Double & Triple Clap Detector",
            sw_version="1.1.0",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_CLAP_EVENT}_{self._entry.entry_id}",
                self._async_on_clap,
            )
        )

    @callback
    def _async_on_clap(self, data: dict[str, Any]) -> None:
        self._attr_native_value = str(data.get("pattern", "double_clap"))
        self._attr_extra_state_attributes = dict(data)
        self.async_write_ha_state()


class ClapLastSnrSensor(SensorEntity):
    """Zeigt den Signal-Rausch-Abstand (SNR in dB) des letzten Klatschens."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Letzter Klatsch-SNR"
    _attr_native_unit_of_measurement = "dB"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:microphone-settings"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_snr"
        self._attr_native_value = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Clap Sensor (USB-Mikrofon)",
            manufacturer="Clap Sensor DSP",
            model="Double & Triple Clap Detector",
            sw_version="1.1.0",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_CLAP_EVENT}_{self._entry.entry_id}",
                self._async_on_clap,
            )
        )

    @callback
    def _async_on_clap(self, data: dict[str, Any]) -> None:
        snr = data.get("snr_db")
        if snr is not None:
            self._attr_native_value = float(snr)
            self.async_write_ha_state()
