"""Initialize and manage the Home Assistant Carrier integration lifecycle."""

import asyncio
import logging

from carrier_api import ApiConnectionGraphql
from gql.transport.exceptions import TransportServerError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .carrier_data_update_coordinator import CarrierDataUpdateCoordinator
from .const import (
    DOMAIN,
    PLATFORMS,
    TO_REDACT,
    UNAUTHORIZED_RETRY_THRESHOLD,
    WEBSOCKET_RETRY_INITIAL_DELAY_SECONDS,
    WEBSOCKET_RETRY_MAX_DELAY_SECONDS,
)
from .util import WEBSOCKET_RECOVERABLE_EXCEPTIONS, async_redact_data, is_unauthorized_error

type ConfigEntryCarrier = ConfigEntry[CarrierDataUpdateCoordinator]

_LOGGER: logging.Logger = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SETUP_UNAUTHORIZED_COUNTS = "setup_unauthorized_counts"


def _setup_unauthorized_key(config_entry: ConfigEntryCarrier) -> str:
    """Return the stable key used for setup unauthorized retry tracking.

    Args:
        config_entry: Config entry currently being set up.

    Returns:
        str: Entry id when available, otherwise the configured username.
    """
    return config_entry.entry_id or str(config_entry.data[CONF_USERNAME])


def _reset_setup_unauthorized_count(
    hass: HomeAssistant,
    config_entry: ConfigEntryCarrier,
) -> None:
    """Clear setup unauthorized retry tracking for a config entry.

    Args:
        hass: Home Assistant instance holding integration runtime data.
        config_entry: Config entry whose setup tracking should be cleared.
    """
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return
    setup_counts = domain_data.get(SETUP_UNAUTHORIZED_COUNTS)
    if not isinstance(setup_counts, dict):
        return
    setup_counts.pop(_setup_unauthorized_key(config_entry), None)


def _handle_setup_unauthorized(
    hass: HomeAssistant,
    config_entry: ConfigEntryCarrier,
    error: BaseException,
) -> None:
    """Record setup unauthorized failures and raise the HA-facing exception.

    Args:
        hass: Home Assistant instance holding integration runtime data.
        config_entry: Config entry currently being set up.
        error: Unauthorized failure raised during setup or first refresh.

    Raises:
        ConfigEntryNotReady: Raised while unauthorized failures are still below
            the retry threshold and may represent a transient Carrier outage.
        ConfigEntryAuthFailed: Raised once repeated unauthorized failures should
            start Home Assistant reauthentication.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    setup_counts = domain_data.setdefault(SETUP_UNAUTHORIZED_COUNTS, {})
    setup_key = _setup_unauthorized_key(config_entry)
    setup_count = int(setup_counts.get(setup_key, 0)) + 1

    if setup_count < UNAUTHORIZED_RETRY_THRESHOLD:
        setup_counts[setup_key] = setup_count
        _LOGGER.info(
            "Carrier API returned unauthorized during setup attempt %s; retrying setup.",
            setup_count,
        )
        raise ConfigEntryNotReady(
            "Carrier API temporarily rejected setup; retrying soon."
        ) from error

    setup_counts.pop(setup_key, None)
    _LOGGER.error(
        "Carrier API returned unauthorized during setup %s consecutive times; "
        "starting reauthentication.",
        setup_count,
    )
    raise ConfigEntryAuthFailed("Carrier API repeatedly rejected setup credentials.") from error


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
    username = config_entry.data[CONF_USERNAME]
    password = config_entry.data[CONF_PASSWORD]

    try:
        api_connection = ApiConnectionGraphql(username=username, password=password)
        coordinator = CarrierDataUpdateCoordinator(
            hass=hass,
            api_connection=api_connection,
            config_entry=config_entry,
        )
        await coordinator.async_config_entry_first_refresh()
        config_entry.runtime_data = coordinator

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
                    await asyncio.sleep(retry_delay_seconds)
                    retry_delay_seconds = WEBSOCKET_RETRY_INITIAL_DELAY_SECONDS
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
            _handle_setup_unauthorized(hass, config_entry, error)
        _LOGGER.exception("Carrier transport error during setup")
        raise ConfigEntryNotReady(error) from error
    except ConfigEntryAuthFailed as error:
        if is_unauthorized_error(error):
            _handle_setup_unauthorized(hass, config_entry, error)
        _LOGGER.exception("Carrier unauthorized during setup")
        raise
    except ConfigEntryNotReady as error:
        if is_unauthorized_error(error):
            _LOGGER.exception("Carrier unauthorized during setup")
            _handle_setup_unauthorized(hass, config_entry, error)
        if isinstance(error.__cause__, TransportServerError):
            transport_error = error.__cause__
            _LOGGER.exception("Carrier transport error during setup")
            raise ConfigEntryNotReady(transport_error) from transport_error
        raise
    except WEBSOCKET_RECOVERABLE_EXCEPTIONS as error:
        if is_unauthorized_error(error):
            _handle_setup_unauthorized(hass, config_entry, error)
        _LOGGER.exception("Carrier recoverable error during setup")
        raise ConfigEntryNotReady(error) from error

    _reset_setup_unauthorized_count(hass, config_entry)

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
    websocket_task = config_entry.runtime_data.websocket_task

    if websocket_task is not None:
        websocket_task.cancel()
        await _async_await_websocket_task(websocket_task)
        config_entry.runtime_data.websocket_task = None

    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
