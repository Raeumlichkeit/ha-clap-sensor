"""Test buttons for simulating Double Clap and Triple Clap directly in the Home Assistant UI."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_CLAP_DETECTED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Erstellt Test-Buttons auf der Geräte-Seite für 2x und 3x Klatschen."""
    async_add_entities(
        [
            ClapTestButton(
                entry=entry,
                pattern="double_clap",
                claps=2,
                name="Test: Doppelklatschen auslösen",
                icon="mdi:gesture-double-tap",
            ),
            ClapTestButton(
                entry=entry,
                pattern="triple_clap",
                claps=3,
                name="Test: Dreifachklatschen auslösen",
                icon="mdi:gesture-tap-button",
            ),
        ]
    )


class ClapTestButton(ButtonEntity):
    """Simuliert ein Klatschen zum sofortigen Testen der verknüpften Lampe/Automation."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        pattern: str,
        claps: int,
        name: str,
        icon: str,
    ) -> None:
        self._entry = entry
        self._pattern = pattern
        self._claps = claps
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_test_{pattern}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Clap Sensor (USB-Mikrofon)",
            manufacturer="Clap Sensor DSP",
            model="Double & Triple Clap Detector",
            sw_version="1.1.0",
        )

    async def async_press(self) -> None:
        """Feuert das Klatsch-Event auf dem Home Assistant Event-Bus."""
        self.hass.bus.async_fire(
            EVENT_CLAP_DETECTED,
            {
                "pattern": self._pattern,
                "claps": self._claps,
                "peak_amplitude": 0.42,
                "snr_db": 20.0,
                "high_freq_ratio": 0.65,
                "source": "test_button",
            },
        )
