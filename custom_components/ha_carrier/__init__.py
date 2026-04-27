"""Initialize and manage the Home Assistant Carrier integration lifecycle."""

import asyncio
import logging

from carrier_api import ApiConnectionGraphql
from gql.transport.exceptions import TransportServerError
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .carrier_data_update_coordinator import CarrierDataUpdateCoordinator
from .const import (
    DOMAIN,
    PLATFORMS,
    TO_REDACT,
    WEBSOCKET_RETRY_INITIAL_DELAY_SECONDS,
    WEBSOCKET_RETRY_MAX_DELAY_SECONDS,
)
from .runtime import (
    ConfigEntryCarrier,
    get_runtime_data,
    handle_setup_unauthorized,
    reset_setup_unauthorized_count,
)
from .util import WEBSOCKET_RECOVERABLE_EXCEPTIONS, async_redact_data, is_unauthorized_error

_LOGGER: logging.Logger = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def _async_await_websocket_task(websocket_task: asyncio.Task[None]) -> None:
    """Await websocket task shutdown after cancellation.

    Args:
        websocket_task: Background websocket listener task to await.

    Returns:
        None: The task is fully drained before unload continues.
    """
    try:
        await websocket_task
    except asyncio.CancelledError:
        pass
    except WEBSOCKET_RECOVERABLE_EXCEPTIONS:
        _LOGGER.exception("websocket task raised during cancellation")


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntryCarrier) -> bool:
    """Set up one Carrier config entry and start platform forwarding.

    The setup creates a Carrier API connection, initializes the data
    coordinator, performs the first refresh, and starts a long-running
    websocket listener task for near-real-time updates.

    Args:
        hass: Home Assistant instance.
        config_entry: Configuration entry containing Carrier credentials.

    Returns:
        bool: True when setup succeeds.

    Raises:
        ConfigEntryNotReady: Raised when authentication or initial data loading
            fails and Home Assistant should retry setup later.
        ConfigEntryAuthFailed: Raised when repeated unauthorized responses
            indicate invalid credentials and Home Assistant should reauth.
    """
    _LOGGER.debug(
        "async setup entry: %s",
        async_redact_data(config_entry.as_dict(), TO_REDACT),
    )
    runtime_data = get_runtime_data(config_entry)
    username = config_entry.data[CONF_USERNAME]
    password = config_entry.data[CONF_PASSWORD]

    try:
        api_connection = ApiConnectionGraphql(username=username, password=password)
        coordinator = CarrierDataUpdateCoordinator(
            hass=hass,
            api_connection=api_connection,
            config_entry=config_entry,
        )
        runtime_data.coordinator = coordinator
        await coordinator.async_config_entry_first_refresh()

        async def ws_updates() -> None:
            """Keep websocket updates running for this config entry.

            The loop exits on cancellation and forces a coordinator refresh if
            websocket handling fails so entity state can recover gracefully.
            Retry delays back off after repeated transport failures and reset
            after a successful listener session.

            Returns:
                None: This coroutine runs until cancelled.
            """
            retry_delay_seconds = WEBSOCKET_RETRY_INITIAL_DELAY_SECONDS
            while True:
                try:
                    _LOGGER.debug("websocket task listening")
                    await coordinator.api_connection.api_websocket.listener()
                    _LOGGER.debug("websocket task ending")
                    coordinator.data_flush = True
                    await coordinator.async_request_refresh()
                    retry_delay_seconds = WEBSOCKET_RETRY_INITIAL_DELAY_SECONDS
                    await asyncio.sleep(retry_delay_seconds)
                except asyncio.CancelledError:
                    _LOGGER.debug("websocket task cancelled")
                    raise
                except WEBSOCKET_RECOVERABLE_EXCEPTIONS:
                    _LOGGER.exception(
                        "websocket task exception; retrying in %s seconds", retry_delay_seconds
                    )
                    coordinator.data_flush = True
                    await coordinator.async_request_refresh()
                    await asyncio.sleep(retry_delay_seconds)
                    retry_delay_seconds = min(
                        retry_delay_seconds * 2, WEBSOCKET_RETRY_MAX_DELAY_SECONDS
                    )

        websocket_task = hass.async_create_background_task(
            ws_updates(),
            f"{DOMAIN}_ws_{config_entry.entry_id}",
        )
        coordinator.websocket_task = websocket_task

        def cancel_websocket_task() -> None:
            """Request websocket listener shutdown during entry unload.

            ConfigEntry.async_on_unload expects a synchronous callback.
            Only cancel the task here; the coordinator keeps the task reference
            and async_unload_entry awaits it so websocket cleanup actually
            finishes before teardown completes.
            """
            websocket_task.cancel()

        config_entry.async_on_unload(cancel_websocket_task)
    except TransportServerError as error:
        if is_unauthorized_error(error):
            handle_setup_unauthorized(config_entry, error)
        _LOGGER.exception("Carrier transport error during setup")
        raise ConfigEntryNotReady(error) from error
    except ConfigEntryAuthFailed as error:
        handle_setup_unauthorized(config_entry, error)
    except ConfigEntryNotReady as error:
        if is_unauthorized_error(error):
            _LOGGER.exception("Carrier unauthorized during setup")
            handle_setup_unauthorized(config_entry, error)
        if isinstance(error.__cause__, TransportServerError):
            transport_error = error.__cause__
            _LOGGER.exception("Carrier transport error during setup")
            raise ConfigEntryNotReady(transport_error) from transport_error
        raise
    except WEBSOCKET_RECOVERABLE_EXCEPTIONS as error:
        if is_unauthorized_error(error):
            handle_setup_unauthorized(config_entry, error)
        _LOGGER.exception("Carrier recoverable error during setup")
        raise ConfigEntryNotReady(error) from error

    reset_setup_unauthorized_count(config_entry)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    config_entry.async_on_unload(config_entry.add_update_listener(async_update_options))

    return True


async def async_update_options(hass: HomeAssistant, config_entry: ConfigEntryCarrier) -> None:
    """Reload the integration when options are changed.

    Args:
        hass: Home Assistant instance.
        config_entry: Updated configuration entry.

    Returns:
        None: This coroutine schedules and awaits the entry reload.
    """
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntryCarrier) -> bool:
    """Unload one Carrier config entry and all forwarded platforms.

    Args:
        hass: Home Assistant instance.
        config_entry: Configuration entry being unloaded.

    Returns:
        bool: True when all platforms were unloaded cleanly.
    """
    _LOGGER.debug("unload entry")
    coordinator = config_entry.runtime_data.coordinator
    websocket_task = coordinator.websocket_task

    if websocket_task is not None:
        websocket_task.cancel()
        await _async_await_websocket_task(websocket_task)
        coordinator.websocket_task = None

    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
