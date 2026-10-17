"""Binary Sensor entities for Double Clap and Triple Clap."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_CLAP_EVENT

PULSE_DURATION = timedelta(seconds=2)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Erstellt die Binary-Sensoren für 2x Klatschen und 3x Klatschen."""
    async_add_entities(
        [
            ClapBinarySensor(
                entry=entry,
                pattern="double_clap",
                name="Doppelklatschen (2x)",
                icon="mdi:hand-clap",
            ),
            ClapBinarySensor(
                entry=entry,
                pattern="triple_clap",
                name="Dreifachklatschen (3x)",
                icon="mdi:hand-clap",
            ),
        ]
    )


class ClapBinarySensor(BinarySensorEntity):
    """Schaltet bei erkanntem Klatsch-Muster für 2 Sekunden auf 'on'."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.SOUND

    def __init__(
        self,
        entry: ConfigEntry,
        pattern: str,
        name: str,
        icon: str,
    ) -> None:
        self._entry = entry
        self._pattern = pattern
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{pattern}"
        self._attr_is_on = False
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._reset_unsub: Optional[Callable[[], None]] = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Clap Sensor (USB-Mikrofon)",
            manufacturer="Clap Sensor DSP",
            model="Double & Triple Clap Detector",
            sw_version="1.0.0",
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
        if data.get("pattern") != self._pattern:
            return

        self._attr_is_on = True
        self._attr_extra_state_attributes = {
            "claps": data.get("claps"),
            "snr_db": data.get("snr_db"),
            "peak_amplitude": data.get("peak_amplitude"),
            "high_freq_ratio": data.get("high_freq_ratio"),
        }
        self.async_write_ha_state()

        if self._reset_unsub is not None:
            self._reset_unsub()

        @callback
        def _reset_state(_now: Any) -> None:
            self._attr_is_on = False
            self._reset_unsub = None
            self.async_write_ha_state()

        self._reset_unsub = async_call_later(
            self.hass, PULSE_DURATION.total_seconds(), _reset_state
        )
