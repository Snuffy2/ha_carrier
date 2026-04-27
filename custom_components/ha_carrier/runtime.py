"""Runtime state helpers for the Carrier config entry lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .carrier_data_update_coordinator import CarrierDataUpdateCoordinator
from .const import UNAUTHORIZED_RETRY_THRESHOLD

_LOGGER: logging.Logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CarrierConfigEntryRuntimeData:
    """Store per-entry runtime state for the Carrier integration."""

    setup_unauthorized_count: int = 0
    _coordinator: CarrierDataUpdateCoordinator | None = None

    @property
    def coordinator(self) -> CarrierDataUpdateCoordinator:
        """Return the initialized coordinator for this config entry.

        Returns:
            CarrierDataUpdateCoordinator: Active coordinator instance.

        Raises:
            RuntimeError: Raised when the coordinator is accessed before setup finishes.
        """
        if self._coordinator is None:
            raise RuntimeError("Carrier coordinator accessed before initialization.")
        return self._coordinator

    @coordinator.setter
    def coordinator(self, coordinator: CarrierDataUpdateCoordinator) -> None:
        """Store the active coordinator for this config entry.

        Args:
            coordinator: Newly initialized coordinator instance.

        Returns:
            None: Runtime data now references the coordinator.
        """
        self._coordinator = coordinator


type ConfigEntryCarrier = ConfigEntry[CarrierConfigEntryRuntimeData]


def get_runtime_data(config_entry: ConfigEntryCarrier) -> CarrierConfigEntryRuntimeData:
    """Return the per-entry runtime state container.

    Args:
        config_entry: Config entry currently being managed.

    Returns:
        CarrierConfigEntryRuntimeData: Existing or newly initialized runtime state.
    """
    runtime_data = getattr(config_entry, "runtime_data", None)
    if isinstance(runtime_data, CarrierConfigEntryRuntimeData):
        return runtime_data

    runtime_data = CarrierConfigEntryRuntimeData()
    config_entry.runtime_data = runtime_data
    return runtime_data


def reset_setup_unauthorized_count(config_entry: ConfigEntryCarrier) -> None:
    """Clear setup unauthorized retry tracking for a config entry.

    Args:
        config_entry: Config entry whose setup tracking should be cleared.

    Returns:
        None: Unauthorized setup retry tracking is reset to zero.
    """
    get_runtime_data(config_entry).setup_unauthorized_count = 0


def handle_setup_unauthorized(
    config_entry: ConfigEntryCarrier,
    error: BaseException,
) -> None:
    """Record setup unauthorized failures and raise the HA-facing exception.

    Args:
        config_entry: Config entry currently being set up.
        error: Unauthorized failure raised during setup or first refresh.

    Raises:
        ConfigEntryNotReady: Raised while unauthorized failures are still below
            the retry threshold and may represent a transient Carrier outage.
        ConfigEntryAuthFailed: Raised once repeated unauthorized failures should
            start Home Assistant reauthentication.
    """
    runtime_data = get_runtime_data(config_entry)
    runtime_data.setup_unauthorized_count += 1
    setup_count = runtime_data.setup_unauthorized_count

    if setup_count < UNAUTHORIZED_RETRY_THRESHOLD:
        _LOGGER.info(
            "Carrier API returned unauthorized during setup attempt %s; retrying setup.",
            setup_count,
        )
        raise ConfigEntryNotReady(
            "Carrier API temporarily rejected setup; retrying soon."
        ) from error

    runtime_data.setup_unauthorized_count = 0
    _LOGGER.error(
        "Carrier API returned unauthorized during setup %s consecutive times; "
        "starting reauthentication.",
        setup_count,
    )
    raise ConfigEntryAuthFailed("Carrier API repeatedly rejected setup credentials.") from error
