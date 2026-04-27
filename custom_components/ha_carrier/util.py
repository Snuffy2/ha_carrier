"""Utility helpers shared across Carrier integration modules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from typing import Any, overload

from aiohttp import ClientError
from carrier_api import AuthError, BaseError
from gql.transport.exceptions import (
    TransportConnectionFailed,
    TransportError,
    TransportProtocolError,
    TransportQueryError,
    TransportServerError,
)
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed

from .exceptions import CarrierUnauthorizedError

_LOGGER: logging.Logger = logging.getLogger(__name__)
REDACTED = "**REDACTED**"

RECOVERABLE_REFRESH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AuthError,
    BaseError,
    ClientError,
    TimeoutError,
    OSError,
    TransportConnectionFailed,
    TransportError,
    TransportProtocolError,
    TransportQueryError,
    TransportServerError,
)
TRANSIENT_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ClientError,
    TimeoutError,
    OSError,
    TransportConnectionFailed,
    TransportProtocolError,
    TransportServerError,
)
WEBSOCKET_RECOVERABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    CarrierUnauthorizedError,
    AuthError,
    BaseError,
    ClientError,
    TimeoutError,
    OSError,
    TransportConnectionFailed,
    TransportError,
    TransportProtocolError,
    TransportQueryError,
    TransportServerError,
)


def _is_unauthorized_status(error: BaseException) -> bool:
    """Return whether an exception directly carries a 401 status.

    Args:
        error: Exception to inspect.

    Returns:
        bool: True when the error exposes an HTTP unauthorized status.
    """
    status_code = getattr(error, "code", None) or getattr(error, "status", None)
    return status_code in (401, "401")


def _iter_exception_causes(error: BaseException) -> Iterable[BaseException]:
    """Yield chained exceptions attached to an exception.

    Args:
        error: Exception whose direct cause and context should be inspected.

    Yields:
        BaseException: Cause and context exceptions present on the error.
    """
    if error.__cause__ is not None:
        yield error.__cause__
    if error.__context__ is not None:
        yield error.__context__


def is_unauthorized_error(error: BaseException, _seen: set[int] | None = None) -> bool:
    """Return whether an exception chain represents an auth failure.

    Args:
        error: Exception to inspect.
        _seen: Internal set used to avoid revisiting exception cycles.

    Returns:
        bool: True when this error or a chained error represents unauthorized
        Carrier or Home Assistant authentication failure.
    """
    seen = set() if _seen is None else _seen
    error_id = id(error)
    if error_id in seen:
        return False
    seen.add(error_id)

    if isinstance(error, AuthError | CarrierUnauthorizedError | ConfigEntryAuthFailed):
        return True
    if _is_unauthorized_status(error):
        return True
    return any(
        is_unauthorized_error(chained_error, seen)
        for chained_error in _iter_exception_causes(error)
    )


def is_transient_transport_error(error: BaseException, _seen: set[int] | None = None) -> bool:
    """Return whether an exception chain represents retryable transport failure.

    Unauthorized errors are intentionally excluded from this classification so
    callers can apply their own auth escalation policy.

    Args:
        error: Exception to inspect.
        _seen: Internal set used to avoid revisiting exception cycles.

    Returns:
        bool: True when this error or a chained error represents a transient
        Carrier transport failure.
    """
    if is_unauthorized_error(error):
        return False

    seen = set() if _seen is None else _seen
    error_id = id(error)
    if error_id in seen:
        return False
    seen.add(error_id)

    if isinstance(error, TRANSIENT_TRANSPORT_EXCEPTIONS):
        return True
    return any(
        is_transient_transport_error(chained_error, seen)
        for chained_error in _iter_exception_causes(error)
    )


def is_retryable_write_error(error: BaseException) -> bool:
    """Return whether a write failure should be retried.

    Args:
        error: Exception raised by a Carrier write request.

    Returns:
        bool: True when the write failed due to an auth rejection or transient
        transport problem suitable for a bounded retry.
    """
    return is_unauthorized_error(error) or is_transient_transport_error(error)


@overload
def async_redact_data(data: list[Any], to_redact: Iterable[Any]) -> list[Any]: ...


@overload
def async_redact_data(data: Mapping[Any, Any], to_redact: Iterable[Any]) -> dict[Any, Any]: ...


@overload
def async_redact_data[T](data: T, to_redact: Iterable[Any]) -> T: ...


@callback
def async_redact_data(data: Any, to_redact: Iterable[Any]) -> Any:
    """Recursively redact selected keys from mapping and list structures.

    Args:
        data: Original value that may contain nested mappings or lists.
        to_redact: Iterable of keys that should be replaced with a redaction marker.

    Returns:
        Any: Copy of the original data with sensitive values redacted.
    """
    if not isinstance(data, Mapping | list):
        return data

    if isinstance(data, list):
        return [async_redact_data(val, to_redact) for val in data]

    redacted = {**data}

    for key, value in redacted.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if key in to_redact:
            redacted[key] = REDACTED
        elif isinstance(value, Mapping):
            redacted[key] = async_redact_data(value, to_redact)
        elif isinstance(value, list):
            redacted[key] = [async_redact_data(item, to_redact) for item in value]

    return redacted
