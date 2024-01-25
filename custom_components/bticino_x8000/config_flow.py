"""Config Flow."""
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.webhook import async_generate_id as generate_id
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .api import BticinoX8000Api
from .auth import exchange_code_for_tokens
from .const import (
    AUTH_URL_ENDPOINT,
    CLIENT_ID,
    CLIENT_SECRET,
    DEFAULT_AUTH_BASE_URL,
    DEFAULT_REDIRECT_URI,
    DOMAIN,
    SUBSCRIPTION_KEY,
)

_LOGGER = logging.getLogger(__name__)


class BticinoX8000ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type:ignore
    """Bticino ConfigFlow."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """User configuration."""
        try:
            external_url = self.hass.config.external_url
        except:
            _LOGGER.warning("No external url available, using default")
            external_url = "My HA external url ex: https://pippo.duckdns.com:8123 (specify the port if is not standard 443)"

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            "client_id",
                            description="Client ID",
                            default=CLIENT_ID,
                        ): str,
                        vol.Required(
                            "client_secret",
                            description="Client Secret",
                            default=CLIENT_SECRET,
                        ): str,
                        vol.Required(
                            "subscription_key",
                            description="Subscription Key",
                            default=SUBSCRIPTION_KEY,
                        ): str,
                        vol.Required(
                            "external_url",
                            description="HA external_url",
                            default=external_url,
                        ): str,
                    }
                ),
            )

        self.data = user_input
        authorization_url = self.get_authorization_url(user_input)
        message = (
            f"Click the link below to authorize Bticino X8000. After authorization, paste the browser URL here.\n\n"
            f"{authorization_url}"
        )
        return self.async_show_form(
            step_id="get_authorize_code",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "browser_url",
                        description="Paste here the browser URL",
                        default="Paste here the browser URL",
                    ): str,
                }
            ),
            errors={"base": message},
        )

    def get_authorization_url(self, user_input: dict[str, Any]) -> str:
        """Compose the auth url."""
        state = secrets.token_hex(16)
        return (
            f"{DEFAULT_AUTH_BASE_URL}{AUTH_URL_ENDPOINT}?"
            + f"client_id={user_input['client_id']}"
            + "&response_type=code"
            + f"&state={state}"
            + f"&redirect_uri={DEFAULT_REDIRECT_URI}"
        )

    async def async_step_get_authorize_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Get authorization code."""
        if user_input is not None:
            try:
                parsed_url = urlparse(user_input["browser_url"])
                _LOGGER.debug("Parsed URL: %s", parsed_url)
                query_params = parse_qs(parsed_url.query)
                _LOGGER.debug("Query Parameters: %s", query_params)
                code = query_params.get("code", [""])[0]
                state = query_params.get("state", [""])[0]

                if not code or not state:
                    raise ValueError(
                        "Unable to identify the Authorize Code or State. Please make sure to provide a valid URL."
                    )

                self.data["code"] = code

                (
                    access_token,
                    refresh_token,
                    access_token_expires_on,
                ) = await exchange_code_for_tokens(
                    self.data["client_id"],
                    self.data["client_secret"],
                    DEFAULT_REDIRECT_URI,
                    code,
                )

                self.data["access_token"] = access_token
                self.data["refresh_token"] = refresh_token
                self.data["access_token_expires_on"] = access_token_expires_on

                self.bticino_api = BticinoX8000Api(self.data)

                if not await self.bticino_api.check_api_endpoint_health():
                    return self.async_abort(reason="Auth Failed!")

                # Fetch and display the list of thermostats
                plants_data = await self.bticino_api.get_plants()
                if plants_data["status_code"] == 200:
                    thermostat_options = {}
                    plant_ids = list(set(plant["id"] for plant in plants_data["data"]))
                    for plant_id in plant_ids:
                        topologies = await self.bticino_api.get_topology(plant_id)
                        for thermo in topologies["data"]:
                            webhook_id = generate_id()
                            thermostat_options.update(
                                {
                                    plant_id: {
                                        "id": thermo["id"],
                                        "name": thermo["name"],
                                        "webhook_id": webhook_id,
                                        "programs": await self.get_programs_from_api(
                                            plant_id, thermo["id"]
                                        ),
                                    }
                                }
                            )
                    self._thermostat_options = thermostat_options

                return self.async_show_form(
                    step_id="select_thermostats",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                "selected_thermostats",
                                description="Select Thermostats",
                                default=[
                                    thermostat_options[thermo]["name"]
                                    for thermo in thermostat_options
                                ],
                            ): cv.multi_select(
                                [
                                    thermostat_options[thermo]["name"]
                                    for thermo in thermostat_options
                                ]
                            ),
                        }
                    ),
                )

            except ValueError as error:
                _LOGGER.error(error)
                return await self.async_step_get_authorize_code()
        return await self.async_step_user(self.data)

    async def add_c2c_subscription(self, plantId: str, webhook_id: str) -> str | None:
        """Subscribe C2C."""
        webhook_path = "/api/webhook/"
        webhook_endpoint = self.data["external_url"] + webhook_path + webhook_id
        response = await self.bticino_api.set_subscribe_C2C_notifications(
            plantId, {"EndPointUrl": webhook_endpoint}
        )
        if response["status_code"] == 201:
            _LOGGER.debug("Webhook subscription registrata con successo!")
            subscriptionId: str = response["text"]["subscriptionId"]
            return subscriptionId
        else:
            return None

    async def get_programs_from_api(
        self, plant_id: str, topology_id: str
    ) -> list[dict[str, Any]]:
        """Retreive the program list."""
        programs = await self.bticino_api.get_chronothermostat_programlist(
            plant_id, topology_id
        )
        filtered_programs = [
            program for program in programs["data"] if program["number"] != 0
        ]

        return filtered_programs

    async def async_step_select_thermostats(
        self, user_input: dict[str, Any]
    ) -> FlowResult:
        """User can select one o more thermostat to add."""
        selected_thermostats = [
            {
                thermo_id: {
                    **thermo_data,
                    "subscription_id": await self.add_c2c_subscription(
                        thermo_id, thermo_data["webhook_id"]
                    ),
                }
            }
            for thermo_id, thermo_data in self._thermostat_options.items()
            if thermo_data["name"] in user_input["selected_thermostats"]
        ]
        return self.async_create_entry(
            title="Bticino X8000",
            data={
                "client_id": self.data["client_id"],
                "client_secret": self.data["client_secret"],
                "subscription_key": self.data["subscription_key"],
                "external_url": self.data["external_url"],
                "access_token": self.data["access_token"],
                "refresh_token": self.data["refresh_token"],
                "access_token_expires_on": self.data["access_token_expires_on"],
                "selected_thermostats": selected_thermostats,
            },
        )
