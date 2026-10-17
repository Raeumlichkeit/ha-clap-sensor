"""Config flow for the Clap Sensor HACS integration."""
from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

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
)


def _get_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_INPUT_DEVICE,
                default=defaults.get(CONF_INPUT_DEVICE, DEFAULT_INPUT_DEVICE),
            ): str,
            vol.Optional(
                CONF_MIN_AMPLITUDE,
                default=defaults.get(CONF_MIN_AMPLITUDE, DEFAULT_MIN_AMPLITUDE),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.005, max=0.95)),
            vol.Optional(
                CONF_SNR_THRESHOLD_DB,
                default=defaults.get(CONF_SNR_THRESHOLD_DB, DEFAULT_SNR_THRESHOLD_DB),
            ): vol.All(vol.Coerce(float), vol.Range(min=4.0, max=35.0)),
            vol.Optional(
                CONF_MIN_HIGH_FREQ_RATIO,
                default=defaults.get(
                    CONF_MIN_HIGH_FREQ_RATIO, DEFAULT_MIN_HIGH_FREQ_RATIO
                ),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.10, max=0.85)),
            vol.Optional(
                CONF_ENABLE_TRIPLE_CLAP,
                default=defaults.get(
                    CONF_ENABLE_TRIPLE_CLAP, DEFAULT_ENABLE_TRIPLE_CLAP
                ),
            ): bool,
        }
    )


class ClapSensorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Clap Sensor."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="Clap Sensor (Mikrofon)",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_get_schema({}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return ClapSensorOptionsFlowHandler(config_entry)


class ClapSensorOptionsFlowHandler(config_entries.OptionsFlow):
    """Erlaubt das nachträgliche Anpassen der Empfindlichkeit über 'Konfigurieren'."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_get_schema(current),
        )
