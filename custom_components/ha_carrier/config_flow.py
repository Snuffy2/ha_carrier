"""UI-driven setup and options flow for the Carrier integration."""

from collections.abc import Mapping
import logging
from typing import Any

from carrier_api import ApiConnectionGraphql
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .const import (
    CONF_INFINITE_HOLDS,
    CONFIG_FLOW_VERSION,
    DEFAULT_INFINITE_HOLDS,
    DOMAIN,
    ERROR_AUTH,
    ERROR_CANNOT_CONNECT,
    ERROR_UNKNOWN,
)
from .util import (
    RECOVERABLE_REFRESH_EXCEPTIONS,
    is_transient_transport_error,
    is_unauthorized_error,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def _async_validate_credentials(username: str, password: str) -> None:
    """Validate Carrier credentials by loading account data.

    Args:
        username: Carrier account username.
        password: Carrier account password.

    Raises:
        ConfigEntryAuthFailed: Raised when credentials are rejected.
        RECOVERABLE_REFRESH_EXCEPTIONS: Raised when Carrier cannot be reached or
            returns a retryable API/transport failure.
    """
    api_connection = ApiConnectionGraphql(username=username, password=password)
    await api_connection.load_data()


class OptionFlowHandler(config_entries.OptionsFlow):
    """Handle options updates for an existing Carrier config entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Build the options schema presented to the user.

        Args:
            config_entry: Existing config entry whose options are being edited.
        """
        self.schema = vol.Schema(
            {
                vol.Required(
                    CONF_INFINITE_HOLDS,
                    default=config_entry.options.get(CONF_INFINITE_HOLDS, DEFAULT_INFINITE_HOLDS),
                ): cv.boolean,
            }
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Render and process the initial options step.

        Args:
            user_input: Submitted option values when the form is posted.

        Returns:
            ConfigFlowResult: Form response or created options entry.
        """
        if user_input is not None:
            _LOGGER.debug("user input in option flow : %s", user_input)
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(step_id="init", data_schema=self.schema)


@config_entries.HANDLERS.register(DOMAIN)
class ConfigFlowHandler(config_entries.ConfigFlow):
    """Authenticate a Carrier account and create a config entry."""

    VERSION = CONFIG_FLOW_VERSION
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    data: dict[str, Any]
    _reauth_username: str

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionFlowHandler:
        """Return the options flow handler for this integration entry.

        Args:
            config_entry: Config entry requesting options management.

        Returns:
            OptionFlowHandler: Options flow implementation for this integration.
        """
        return OptionFlowHandler(config_entry)

    def __init__(self) -> None:
        """Initialize mutable state used while the flow runs."""
        self.data = {}
        self._reauth_username = ""

    def _credentials_schema(self, default_username: str | None = None) -> vol.Schema:
        """Return the username/password form schema.

        Args:
            default_username: Username to prefill when the form is shown.

        Returns:
            vol.Schema: Voluptuous schema for credential input.
        """
        username_field = (
            vol.Required(CONF_USERNAME, default=default_username)
            if default_username
            else vol.Required(CONF_USERNAME)
        )
        return vol.Schema(
            {
                username_field: str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

    async def _async_validate_user_input(self, user_input: Mapping[str, Any]) -> str | None:
        """Validate submitted Carrier credentials and return a form error.

        Args:
            user_input: Submitted form data containing username and password.

        Returns:
            str | None: Base form error key when validation fails, otherwise None.
        """
        username = str(user_input[CONF_USERNAME])
        password = str(user_input[CONF_PASSWORD])

        try:
            await _async_validate_credentials(username, password)
        except ConfigEntryAuthFailed as error:
            _LOGGER.debug("Carrier validation failed with auth exception: %s", error)
            return ERROR_AUTH
        except RECOVERABLE_REFRESH_EXCEPTIONS as error:
            if is_unauthorized_error(error):
                return ERROR_AUTH
            if is_transient_transport_error(error):
                return ERROR_CANNOT_CONNECT
            _LOGGER.debug("Carrier validation failed with API exception: %s", error)
            return ERROR_UNKNOWN
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle username/password input and validate Carrier credentials.

        Args:
            user_input: Submitted credentials for the Carrier account.

        Returns:
            ConfigFlowResult: Form response with errors or a created config entry.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            username = str(user_input[CONF_USERNAME])
            validation_error = await self._async_validate_user_input(user_input)
            if validation_error is None:
                self.data.update(user_input)
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=username, data=self.data)
            errors["base"] = validation_error

        return self.async_show_form(
            step_id="user",
            data_schema=self._credentials_schema(),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for an existing Carrier config entry.

        Args:
            entry_data: Existing config entry data supplied by Home Assistant.

        Returns:
            ConfigFlowResult: Reauthentication confirmation form.
        """
        self._reauth_username = str(entry_data.get(CONF_USERNAME, ""))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate replacement Carrier credentials for an existing entry.

        Args:
            user_input: Submitted replacement credentials.

        Returns:
            ConfigFlowResult: Form response with errors or reauth completion.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            username = str(user_input[CONF_USERNAME])
            password = str(user_input[CONF_PASSWORD])
            validation_error = await self._async_validate_user_input(user_input)
            if validation_error is None:
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )
            errors["base"] = validation_error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._credentials_schema(self._reauth_username),
            errors=errors,
        )
