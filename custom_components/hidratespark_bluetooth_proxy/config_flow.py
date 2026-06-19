"""Config flow for the HidrateSpark integration.

Discovery sources:
  * Bluetooth — when HA's bluetooth stack (or an ESPHome proxy) sees a bottle
    advertising the HydroSync reference service or a `h2o*` local name, the
    user is offered a one-click "Configure" action.
  * Manual — the user picks from any HidrateSpark candidate currently
    advertising in range, or types a MAC address directly.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_MODEL,
    CONF_NAME_PREFIX,
    CONF_SIZE_ML,
    DEFAULT_MODEL,
    DEFAULT_NAME_PREFIX,
    DEFAULT_SIZE_ML,
    DOMAIN,
    MODELS,
    SERVICE_REF,
)

_LOGGER = logging.getLogger(__name__)

# {model_key: human label} for the setup dropdown.
_MODEL_CHOICES = {key: spec["label"] for key, spec in MODELS.items()}


def _resolve_size_ml(user_input: dict[str, Any]) -> int:
    """Bottle size for the chosen model; the size field only applies to 'other'."""
    model = user_input.get(CONF_MODEL, DEFAULT_MODEL)
    spec = MODELS.get(model)
    if model != "other" and spec:
        return int(spec["size_ml"])
    return int(user_input.get(CONF_SIZE_ML, DEFAULT_SIZE_ML))


def _setup_fields(default_model: str = DEFAULT_MODEL, default_size: int = DEFAULT_SIZE_ML) -> dict:
    """Model + size fields shared by the setup and options forms. The model sets
    the calibration scale (and the size, except for 'Other' where size is used)."""
    return {
        vol.Required(CONF_MODEL, default=default_model): vol.In(_MODEL_CHOICES),
        vol.Required(CONF_SIZE_ML, default=default_size): vol.All(
            cv.positive_int, vol.Range(min=100, max=2000)
        ),
    }


def _looks_like_bottle(info: BluetoothServiceInfoBleak) -> bool:
    if SERVICE_REF.lower() in {u.lower() for u in info.service_uuids or []}:
        return True
    if info.name and info.name.lower().startswith(DEFAULT_NAME_PREFIX):
        return True
    return False


class HidrateSparkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HidrateSpark."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    # ----------------------------------------------------------- bluetooth flow

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a bottle discovered via the bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _looks_like_bottle(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name
                or self._discovery_info.address,
                data={
                    CONF_ADDRESS: self._discovery_info.address,
                    CONF_NAME_PREFIX: DEFAULT_NAME_PREFIX,
                },
                options={
                    CONF_MODEL: user_input.get(CONF_MODEL, DEFAULT_MODEL),
                    CONF_SIZE_ML: _resolve_size_ml(user_input),
                },
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address,
            },
            data_schema=vol.Schema(_setup_fields()),
        )

    # ---------------------------------------------------------------- user flow

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a manually-initiated flow."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            title = self._discovered.get(address)
            return self.async_create_entry(
                title=(title.name if title and title.name else address),
                data={
                    CONF_ADDRESS: address,
                    CONF_NAME_PREFIX: DEFAULT_NAME_PREFIX,
                },
                options={
                    CONF_MODEL: user_input.get(CONF_MODEL, DEFAULT_MODEL),
                    CONF_SIZE_ML: _resolve_size_ml(user_input),
                },
            )

        # Build a picker from currently-advertising candidates.
        current_addresses = {
            entry.unique_id for entry in self._async_current_entries()
        }
        for info in async_discovered_service_info(self.hass):
            if info.address in current_addresses:
                continue
            if _looks_like_bottle(info):
                self._discovered[info.address] = info

        if self._discovered:
            choices = {
                addr: f"{(info.name or addr)} ({addr})"
                for addr, info in self._discovered.items()
            }
            address_field: dict = {vol.Required(CONF_ADDRESS): vol.In(choices)}
        else:
            address_field = {vol.Required(CONF_ADDRESS): str}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({**address_field, **_setup_fields()}),
        )

    # ----------------------------------------------------------------- options

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HidrateSparkOptionsFlow(config_entry)


class HidrateSparkOptionsFlow(OptionsFlow):
    """Allow the bottle model and size to be changed after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_MODEL: user_input.get(CONF_MODEL, DEFAULT_MODEL),
                    CONF_SIZE_ML: _resolve_size_ml(user_input),
                },
            )
        opts = self.config_entry.options
        current_model = opts.get(CONF_MODEL, DEFAULT_MODEL)
        current_size = opts.get(
            CONF_SIZE_ML, self.config_entry.data.get(CONF_SIZE_ML, DEFAULT_SIZE_ML)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_setup_fields(current_model, current_size)),
        )
