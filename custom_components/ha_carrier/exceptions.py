"""Custom exceptions owned by the Carrier integration."""


class CarrierUnauthorizedError(Exception):
    """Raised when unauthorized Carrier responses stop looking transient."""
