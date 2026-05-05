"""Workflow tests for Carrier config and options flows."""

from __future__ import annotations

from aiohttp import ClientError
from carrier_api import AuthError, BaseError
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_carrier.const import (
    CONF_INFINITE_HOLDS,
    DOMAIN,
    ERROR_AUTH,
    ERROR_CANNOT_CONNECT,
    ERROR_UNKNOWN,
)

from .conftest import PASSWORD, USERNAME, FakeCarrierApiConnection


@pytest.mark.asyncio
async def test_user_flow_creates_entry_after_successful_validation(
    hass: HomeAssistant,
    patch_carrier_api: FakeCarrierApiConnection,
) -> None:
    """Validate credentials and create a config entry from the user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    assert patch_carrier_api.cleanup_calls == 1


@pytest.mark.asyncio
async def test_user_flow_aborts_duplicate_account(
    hass: HomeAssistant,
    patch_carrier_api: FakeCarrierApiConnection,
) -> None:
    """Abort a user flow when the Carrier username is already configured."""
    config_entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, data={})
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (AuthError("unauthorized"), ERROR_AUTH),
        (ClientError("temporary"), ERROR_CANNOT_CONNECT),
        (BaseError("unexpected"), ERROR_UNKNOWN),
    ],
)
async def test_user_flow_maps_validation_errors(
    hass: HomeAssistant,
    patch_carrier_api: FakeCarrierApiConnection,
    error: BaseException,
    expected_error: str,
) -> None:
    """Map Carrier credential validation failures to flow error keys."""
    patch_carrier_api.load_data_error = error

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


@pytest.mark.asyncio
async def test_reauth_flow_updates_password(
    hass: HomeAssistant,
    patch_carrier_api: FakeCarrierApiConnection,
) -> None:
    """Validate a new password and update the existing config entry."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old"},
    )
    config_entry.add_to_hass(hass)

    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": config_entry.entry_id},
        data=config_entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"],
        user_input={CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == PASSWORD
    assert patch_carrier_api.password == PASSWORD


@pytest.mark.asyncio
async def test_options_flow_updates_infinite_hold_option(hass: HomeAssistant) -> None:
    """Create options data from the Carrier options flow."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        options={CONF_INFINITE_HOLDS: True},
    )
    config_entry.add_to_hass(hass)

    form = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        user_input={CONF_INFINITE_HOLDS: False},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_INFINITE_HOLDS: False}
