# -*- coding: utf-8 -*-
"""Config flow for SOPHIA Climate"""
from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import DOMAIN


class SophiaClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SOPHIA Climate"""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step."""

        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            # Check SOPHIA Core is installed
            if "sophia_core" not in self.hass.data:
                errors["base"] = "sophia_core_not_found"
            else:
                zone_name = user_input.get("zone_name", "Main Zone").strip()
                thermostat = user_input.get("thermostat_entity", "").strip()
                outdoor_sensor = user_input.get(
                    "outdoor_temp_sensor", "sensor.outdoor_temperature"
                ).strip()

                if not thermostat:
                    errors["thermostat_entity"] = "thermostat_required"
                else:
                    zone_id = zone_name.lower().replace(" ", "_")
                    title = f"SOPHIA Climate ({zone_name})"

                    data = {
                        "check_interval": 30,
                        "outdoor_temp_sensor": outdoor_sensor,
                        "outdoor_humidity_sensor": "",
                        "passive_humidity_limit": 60,
                        "zones": [
                            {
                                "id": zone_id,
                                "name": zone_name,
                                "thermostat": thermostat,
                                "temp_sensor": None,
                                "priority": 5,
                                "enabled": True,
                            }
                        ],
                        "seasonal_temps": {
                            "winter": {"target": 72, "sleep": 68},
                            "spring": {"target": 72, "sleep": 68},
                            "summer": {"target": 70, "sleep": 66},
                            "fall": {"target": 72, "sleep": 68},
                        },
                        "special_instructions": "",
                        "energy_priority": "balanced",
                        "aggressive_optimization": False,
                    }

                    return self.async_create_entry(title=title, data=data)

        data_schema = vol.Schema(
            {
                vol.Required("zone_name", default="Main Zone"): str,
                vol.Required("thermostat_entity"): selector.selector(
                    {"entity": {"domain": "climate"}}
                ),
                vol.Required(
                    "outdoor_temp_sensor", default="sensor.outdoor_temperature"
                ): selector.selector(
                    {"entity": {"domain": "sensor", "device_class": "temperature"}}
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": (
                    "Enter your zone name (e.g. 'Downstairs'), the entity ID of your "
                    "thermostat (e.g. 'climate.my_thermostat'), and your outdoor "
                    "temperature sensor. Additional zones and settings can be configured "
                    "in the integration options after setup."
                )
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler"""
        return SophiaClimateOptionsFlowHandler(config_entry)


class SophiaClimateOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for SOPHIA Climate"""

    def __init__(self, config_entry):
        """Initialize options flow"""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options - show menu of what to configure"""

        if user_input is not None:
            action = user_input.get("action")

            if action == "check_interval":
                return await self.async_step_check_interval()
            elif action == "seasonal_temps":
                return await self.async_step_seasonal_temps()
            elif action == "energy_priority":
                return await self.async_step_energy_priority()
            elif action == "special_instructions":
                return await self.async_step_special_instructions()
            elif action == "sensors":
                return await self.async_step_sensors()
            else:
                return self.async_create_entry(title="", data={})

        data_schema = vol.Schema(
            {
                vol.Optional("action"): vol.In(
                    {
                        "check_interval": "Update Check Interval",
                        "seasonal_temps": "Update Seasonal Temperatures",
                        "energy_priority": "Update Energy Priority",
                        "special_instructions": "Update Special Instructions",
                        "sensors": "Update Sensor Configuration",
                        "none": "Cancel (No Changes)",
                    }
                )
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            description_placeholders={"info": "Choose which settings you'd like to update."},
        )

    async def async_step_check_interval(self, user_input=None):
        """Update check interval"""

        if user_input is not None:
            new_data = dict(self._config_entry.data)
            new_data["check_interval"] = user_input["check_interval"]
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        current_interval = self._config_entry.data.get("check_interval", 30)

        data_schema = vol.Schema(
            {
                vol.Required("check_interval", default=current_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=120)
                )
            }
        )

        return self.async_show_form(
            step_id="check_interval",
            data_schema=data_schema,
            description_placeholders={
                "info": (
                    "How often (in minutes) should SOPHIA check and optimize your climate?\n\n"
                    "Recommended: 30 minutes\n"
                    "Range: 5-120 minutes"
                )
            },
        )

    async def async_step_seasonal_temps(self, user_input=None):
        """Update seasonal temperatures"""

        if user_input is not None:
            new_data = dict(self._config_entry.data)
            new_data["seasonal_temps"] = {
                "winter": {
                    "target": user_input["winter_target"],
                    "sleep": user_input["winter_sleep"],
                },
                "spring": {
                    "target": user_input["spring_target"],
                    "sleep": user_input.get(
                        "spring_sleep", user_input["spring_target"] - 4
                    ),
                },
                "summer": {
                    "target": user_input["summer_target"],
                    "sleep": user_input["summer_sleep"],
                },
                "fall": {
                    "target": user_input["fall_target"],
                    "sleep": user_input.get(
                        "fall_sleep", user_input["fall_target"] - 4
                    ),
                },
            }
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        current_temps = self._config_entry.data.get("seasonal_temps", {})

        data_schema = vol.Schema(
            {
                vol.Required(
                    "winter_target",
                    default=current_temps.get("winter", {}).get("target", 72),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=85)),
                vol.Required(
                    "winter_sleep",
                    default=current_temps.get("winter", {}).get("sleep", 68),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=85)),
                vol.Required(
                    "spring_target",
                    default=current_temps.get("spring", {}).get("target", 72),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=85)),
                vol.Optional("spring_sleep"): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=85)
                ),
                vol.Required(
                    "summer_target",
                    default=current_temps.get("summer", {}).get("target", 70),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=85)),
                vol.Required(
                    "summer_sleep",
                    default=current_temps.get("summer", {}).get("sleep", 66),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=85)),
                vol.Required(
                    "fall_target",
                    default=current_temps.get("fall", {}).get("target", 72),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=85)),
                vol.Optional("fall_sleep"): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=85)
                ),
            }
        )

        return self.async_show_form(
            step_id="seasonal_temps",
            data_schema=data_schema,
            description_placeholders={
                "info": (
                    "Set your ideal temperatures for each season.\n\n"
                    "Target: Comfortable temp when awake\n"
                    "Sleep: Temperature for nighttime (10 PM - 6 AM)\n\n"
                    "Leave spring/fall sleep temps empty to use target - 4 degrees F"
                )
            },
        )

    async def async_step_energy_priority(self, user_input=None):
        """Update energy priority"""

        if user_input is not None:
            new_data = dict(self._config_entry.data)
            new_data["energy_priority"] = user_input["energy_priority"]
            new_data["aggressive_optimization"] = user_input.get(
                "aggressive_optimization", False
            )
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        current_priority = self._config_entry.data.get("energy_priority", "balanced")
        current_aggressive = self._config_entry.data.get("aggressive_optimization", False)

        data_schema = vol.Schema(
            {
                vol.Required("energy_priority", default=current_priority): vol.In(
                    {
                        "comfort": "Comfort First - Prioritize comfort over savings",
                        "balanced": "Balanced - Balance comfort and efficiency (recommended)",
                        "savings": "Savings First - Prioritize energy savings",
                    }
                ),
                vol.Optional("aggressive_optimization", default=current_aggressive): bool,
            }
        )

        return self.async_show_form(
            step_id="energy_priority",
            data_schema=data_schema,
            description_placeholders={
                "info": (
                    "Energy Priority: How should SOPHIA balance comfort vs. savings?\n\n"
                    "Aggressive Optimization: Allow more frequent HVAC adjustments for better efficiency"
                )
            },
        )

    async def async_step_special_instructions(self, user_input=None):
        """Update special instructions"""

        if user_input is not None:
            new_data = dict(self._config_entry.data)
            new_data["special_instructions"] = user_input.get("special_instructions", "")
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        current_instructions = self._config_entry.data.get("special_instructions", "")

        data_schema = vol.Schema(
            {vol.Optional("special_instructions", default=current_instructions): str}
        )

        return self.async_show_form(
            step_id="special_instructions",
            data_schema=data_schema,
            description_placeholders={
                "info": (
                    "Provide custom instructions for the AI.\n\n"
                    "Examples:\n"
                    "- Keep bedroom extra cool\n"
                    "- Avoid running HVAC 2-7 PM (peak rates)\n"
                    "- Prefer heating over cooling\n\n"
                    "Leave blank for default behavior."
                )
            },
        )

    async def async_step_sensors(self, user_input=None):
        """Update outdoor sensor configuration"""

        if user_input is not None:
            new_data = dict(self._config_entry.data)
            new_data["outdoor_temp_sensor"] = user_input.get(
                "outdoor_temp_sensor", "sensor.outdoor_temperature"
            ).strip()
            new_data["outdoor_humidity_sensor"] = user_input.get(
                "outdoor_humidity_sensor", ""
            ).strip()
            new_data["passive_humidity_limit"] = user_input.get(
                "passive_humidity_limit", 60
            )
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        current_temp_sensor = self._config_entry.data.get(
            "outdoor_temp_sensor", "sensor.outdoor_temperature"
        )
        current_humidity_sensor = self._config_entry.data.get(
            "outdoor_humidity_sensor", ""
        )
        current_humidity_limit = self._config_entry.data.get("passive_humidity_limit", 60)

        data_schema = vol.Schema(
            {
                vol.Required(
                    "outdoor_temp_sensor", default=current_temp_sensor
                ): selector.selector(
                    {"entity": {"domain": "sensor", "device_class": "temperature"}}
                ),
                vol.Optional(
                    "outdoor_humidity_sensor", default=current_humidity_sensor
                ): selector.selector(
                    {"entity": {"domain": "sensor", "device_class": "humidity"}}
                ),
                vol.Required(
                    "passive_humidity_limit", default=current_humidity_limit
                ): selector.selector(
                    {"number": {"min": 30, "max": 90, "step": 1, "unit_of_measurement": "%"}}
                ),
            }
        )

        return self.async_show_form(
            step_id="sensors",
            data_schema=data_schema,
            description_placeholders={
                "info": (
                    "Outdoor sensor configuration.\n\n"
                    "outdoor_temp_sensor: Entity ID for outdoor temperature.\n"
                    "outdoor_humidity_sensor: Entity ID for outdoor humidity "
                    "(leave blank to disable).\n"
                    "passive_humidity_limit: Max outdoor RH% before passive cooling "
                    "is suppressed (default 60).\n\n"
                    "When outdoor humidity exceeds the limit, SOPHIA will use AC "
                    "instead of recommending open windows."
                )
            },
        )