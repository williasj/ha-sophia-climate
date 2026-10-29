# -*- coding: utf-8 -*-
"""Config flow for SOPHIA Climate"""
from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    DEFAULT_RAG_ENABLED,
    DEFAULT_RAG_RETENTION_DAYS,
    DEFAULT_RAG_MEMORY_ENTRIES,
    MIN_RAG_RETENTION_DAYS,
    MAX_RAG_RETENTION_DAYS,
    MIN_RAG_MEMORY_ENTRIES,
    MAX_RAG_MEMORY_ENTRIES,
    DEFAULT_ZONE_PRIORITY,
    MIN_ZONE_PRIORITY,
    MAX_ZONE_PRIORITY,
)



SEASONS = ("winter", "spring", "summer", "fall")
SEASON_DEFAULTS = {
    "winter": {"target": 72, "sleep": 68},
    "spring": {"target": 72, "sleep": 68},
    "summer": {"target": 70, "sleep": 66},
    "fall": {"target": 72, "sleep": 68},
}
TEMP_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=60, max=85))


def _saved_temp(saved: dict, season: str, kind: str) -> int:
    """Stored target/sleep for a season, or the default when missing."""
    value = (saved or {}).get(season, {}).get(kind)
    if value is None:
        value = SEASON_DEFAULTS[season][kind]
    try:
        return int(value)
    except (TypeError, ValueError):
        return SEASON_DEFAULTS[season][kind]


def _seasonal_fields(saved: dict) -> dict:
    """All eight temps as required fields, pre-filled with the saved values.

    Every field is required and shows its stored value, so saving a form
    never silently replaces a temperature the user did not touch.
    """
    fields = {}
    for season in SEASONS:
        for kind in ("target", "sleep"):
            fields[vol.Required(
                f"{season}_{kind}", default=_saved_temp(saved, season, kind)
            )] = TEMP_VALIDATOR
    return fields


def _seasonal_from_input(user_input: dict) -> dict:
    return {
        season: {
            "target": int(user_input[f"{season}_target"]),
            "sleep": int(user_input[f"{season}_sleep"]),
        }
        for season in SEASONS
    }


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
        self._selected_zone_id = None

    async def async_step_init(self, user_input=None):
        """Manage the options - show menu of what to configure"""

        if user_input is not None:
            action = user_input.get("action")

            if action == "check_interval":
                return await self.async_step_check_interval()
            elif action == "edit_zone":
                return await self.async_step_edit_zone()
            elif action == "energy_priority":
                return await self.async_step_energy_priority()
            elif action == "special_instructions":
                return await self.async_step_special_instructions()
            elif action == "sensors":
                return await self.async_step_sensors()
            elif action == "rag_settings":
                return await self.async_step_rag_settings()
            elif action == "add_zone":
                return await self.async_step_add_zone()
            elif action == "remove_zone":
                return await self.async_step_remove_zone()
            else:
                return self.async_create_entry(title="", data={})

        data_schema = vol.Schema(
            {
                vol.Optional("action"): vol.In(
                    {
                        "check_interval": "Update Check Interval",
                        "edit_zone": "Edit Zone Comfort & Settings",
                        "sensors": "Update Sensor Configuration",
                        "rag_settings": "Update RAG Decision History Settings",
                        "add_zone": "Add a Zone",
                        "remove_zone": "Remove a Zone",
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
            new_data["seasonal_temps"] = _seasonal_from_input(user_input)
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        current_temps = self._config_entry.data.get("seasonal_temps", {})

        data_schema = vol.Schema(
            {
                **_seasonal_fields(current_temps),
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
                    "All eight temperatures are required and show their saved values."
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

    async def async_step_rag_settings(self, user_input=None):
        """Update RAG decision-history settings (behavior and retention)"""

        if user_input is not None:
            new_data = dict(self._config_entry.data)
            new_data["rag_decision_enabled"] = bool(
                user_input.get("rag_decision_enabled", DEFAULT_RAG_ENABLED)
            )
            new_data["rag_decision_retention_days"] = int(
                user_input.get(
                    "rag_decision_retention_days", DEFAULT_RAG_RETENTION_DAYS
                )
            )
            new_data["rag_decision_memory_entries"] = int(
                user_input.get(
                    "rag_decision_memory_entries", DEFAULT_RAG_MEMORY_ENTRIES
                )
            )
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        current = self._config_entry.data
        current_enabled = current.get("rag_decision_enabled", DEFAULT_RAG_ENABLED)
        current_retention = current.get(
            "rag_decision_retention_days", DEFAULT_RAG_RETENTION_DAYS
        )
        current_memory = current.get(
            "rag_decision_memory_entries", DEFAULT_RAG_MEMORY_ENTRIES
        )

        data_schema = vol.Schema(
            {
                vol.Required(
                    "rag_decision_enabled", default=current_enabled
                ): selector.BooleanSelector(),
                vol.Required(
                    "rag_decision_retention_days", default=current_retention
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_RAG_RETENTION_DAYS, max=MAX_RAG_RETENTION_DAYS),
                ),
                vol.Required(
                    "rag_decision_memory_entries", default=current_memory
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_RAG_MEMORY_ENTRIES, max=MAX_RAG_MEMORY_ENTRIES),
                ),
            }
        )

        return self.async_show_form(
            step_id="rag_settings",
            data_schema=data_schema,
            description_placeholders={
                "info": (
                    "RAG decision-history settings. These only take effect when "
                    "SOPHIA Core has a Qdrant URL configured; otherwise climate "
                    "decisions are retained in memory and on disk only.\n\n"
                    "rag_decision_enabled: Mirror each climate decision into "
                    "the sophia_climate_decisions Qdrant collection for long-term "
                    "analysis.\n"
                    "rag_decision_retention_days: Maximum age of decisions kept in "
                    f"RAG ({MIN_RAG_RETENTION_DAYS}-{MAX_RAG_RETENTION_DAYS}, "
                    f"default {DEFAULT_RAG_RETENTION_DAYS}). A nightly purge "
                    "removes anything older.\n"
                    "rag_decision_memory_entries: Number of most recent decisions "
                    f"kept in memory for sensor attributes "
                    f"({MIN_RAG_MEMORY_ENTRIES}-{MAX_RAG_MEMORY_ENTRIES}, "
                    f"default {DEFAULT_RAG_MEMORY_ENTRIES})."
                )
            },
        )
    async def async_step_add_zone(self, user_input=None):
        """Add a new climate zone"""

        errors = {}

        existing_zones = list(self._config_entry.data.get("zones", []))
        existing_ids = {
            z.get("id") if isinstance(z, dict) else str(z).lower().replace(" ", "_")
            for z in existing_zones
        }

        if user_input is not None:
            zone_name = user_input.get("zone_name", "").strip()
            thermostat = user_input.get("thermostat", "").strip()
            temp_sensor = user_input.get("temp_sensor", "").strip() or None
            priority = int(user_input.get("priority", DEFAULT_ZONE_PRIORITY))

            if not zone_name:
                errors["zone_name"] = "zone_name_required"
            elif not thermostat:
                errors["thermostat"] = "thermostat_required"
            else:
                zone_id = zone_name.lower().replace(" ", "_")
                if zone_id in existing_ids:
                    errors["zone_name"] = "zone_exists"

            if not errors:
                new_zone = {
                    "id": zone_id,
                    "name": zone_name,
                    "thermostat": thermostat,
                    "temp_sensor": temp_sensor,
                    "priority": priority,
                    "enabled": True,
                    "seasonal_temps": {
                        "winter": {"target": 72, "sleep": 68},
                        "spring": {"target": 72, "sleep": 68},
                        "summer": {"target": 70, "sleep": 66},
                        "fall": {"target": 72, "sleep": 68},
                    },
                    "energy_priority": "balanced",
                    "aggressive_optimization": False,
                    "special_instructions": "",
                }
                new_data = dict(self._config_entry.data)
                new_data["zones"] = existing_zones + [new_zone]
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=new_data
                )
                return self.async_create_entry(title="", data={})

        data_schema = vol.Schema(
            {
                vol.Required("zone_name"): str,
                vol.Required("thermostat"): selector.selector(
                    {"entity": {"domain": "climate"}}
                ),
                vol.Optional("temp_sensor"): selector.selector(
                    {"entity": {"domain": "sensor", "device_class": "temperature"}}
                ),
                vol.Required(
                    "priority", default=DEFAULT_ZONE_PRIORITY
                ): selector.selector(
                    {
                        "number": {
                            "min": MIN_ZONE_PRIORITY,
                            "max": MAX_ZONE_PRIORITY,
                            "step": 1,
                            "mode": "slider",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="add_zone",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": (
                    "Add a new climate zone.\n\n"
                    "Zone Name: human-readable name (e.g. 'Upstairs'). The entity "
                    "IDs sensor.sophia_<id>_zone_status and switch.sophia_<id>_zone "
                    "are derived from a lowercased, underscored version of this name.\n"
                    "Thermostat: the climate entity SOPHIA will control for this zone.\n"
                    "Temp Sensor: optional dedicated indoor temperature sensor. Leave "
                    "blank to use the thermostat's own reading.\n"
                    f"Priority: {MIN_ZONE_PRIORITY}-{MAX_ZONE_PRIORITY} "
                    f"(default {DEFAULT_ZONE_PRIORITY}).\n\n"
                    "SOPHIA reloads automatically after saving; the new zone's "
                    "sensor and switch are created on reload."
                )
            },
        )

    async def async_step_remove_zone(self, user_input=None):
        """Remove an existing climate zone"""

        errors = {}
        existing_zones = list(self._config_entry.data.get("zones", []))

        def _zid(z):
            return z.get("id") if isinstance(z, dict) else str(z).lower().replace(" ", "_")

        def _zname(z):
            return z.get("name", _zid(z)) if isinstance(z, dict) else str(z)

        choices = {_zid(z): _zname(z) for z in existing_zones}

        if user_input is not None:
            target = user_input.get("zone_id")
            if len(existing_zones) <= 1:
                errors["base"] = "cannot_remove_last"
            elif target not in choices:
                errors["zone_id"] = "zone_not_found"
            else:
                new_zones = [z for z in existing_zones if _zid(z) != target]
                new_data = dict(self._config_entry.data)
                new_data["zones"] = new_zones
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=new_data
                )
                return self.async_create_entry(title="", data={})

        if len(existing_zones) <= 1:
            return self.async_abort(reason="cannot_remove_last")

        data_schema = vol.Schema({vol.Required("zone_id"): vol.In(choices)})

        return self.async_show_form(
            step_id="remove_zone",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": (
                    "Select a zone to remove. Its sensor and switch entities are "
                    "deleted on the automatic reload that follows. The decision "
                    "history stored in RAG/on disk is not purged."
                )
            },
        )

    async def async_step_edit_zone(self, user_input=None):
        """Pick which zone's comfort settings to edit"""

        zones = list(self._config_entry.data.get("zones", []))
        choices = {}
        for z in zones:
            if isinstance(z, dict):
                zid = z.get("id")
                choices[zid] = z.get("name", zid)

        if not choices:
            return self.async_abort(reason="no_zones")

        # Single zone: skip the picker
        if len(choices) == 1 and user_input is None:
            self._selected_zone_id = next(iter(choices))
            return await self.async_step_zone_settings()

        if user_input is not None:
            self._selected_zone_id = user_input["zone_id"]
            return await self.async_step_zone_settings()

        data_schema = vol.Schema({vol.Required("zone_id"): vol.In(choices)})

        return self.async_show_form(
            step_id="edit_zone",
            data_schema=data_schema,
            description_placeholders={
                "info": "Choose which zone's comfort temperatures and settings to edit."
            },
        )

    async def async_step_zone_settings(self, user_input=None):
        """Edit one zone's own comfort temps, energy priority, and instructions"""

        zones = list(self._config_entry.data.get("zones", []))
        idx = None
        for i, z in enumerate(zones):
            if isinstance(z, dict) and z.get("id") == self._selected_zone_id:
                idx = i
                break

        if idx is None:
            return self.async_abort(reason="zone_not_found")

        zone = dict(zones[idx])
        zone_name = zone.get("name", self._selected_zone_id)
        st = zone.get("seasonal_temps", {})

        if user_input is not None:
            zone["seasonal_temps"] = _seasonal_from_input(user_input)
            zone["energy_priority"] = user_input["energy_priority"]
            zone["aggressive_optimization"] = user_input.get(
                "aggressive_optimization", False
            )
            zone["special_instructions"] = user_input.get("special_instructions", "")
            if "priority" in user_input:
                zone["priority"] = int(user_input["priority"])

            new_zones = list(zones)
            new_zones[idx] = zone
            new_data = dict(self._config_entry.data)
            new_data["zones"] = new_zones
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        data_schema = vol.Schema(
            {
                **_seasonal_fields(st),
                vol.Required(
                    "energy_priority",
                    default=zone.get("energy_priority", "balanced"),
                ): vol.In(
                    {
                        "comfort": "Comfort First - Prioritize comfort over savings",
                        "balanced": "Balanced - Balance comfort and efficiency (recommended)",
                        "savings": "Savings First - Prioritize energy savings",
                    }
                ),
                vol.Optional(
                    "aggressive_optimization",
                    default=zone.get("aggressive_optimization", False),
                ): bool,
                vol.Required(
                    "priority",
                    default=zone.get("priority", DEFAULT_ZONE_PRIORITY),
                ): selector.selector(
                    {
                        "number": {
                            "min": MIN_ZONE_PRIORITY,
                            "max": MAX_ZONE_PRIORITY,
                            "step": 1,
                            "mode": "slider",
                        }
                    }
                ),
                vol.Optional(
                    "special_instructions",
                    default=zone.get("special_instructions", ""),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="zone_settings",
            data_schema=data_schema,
            description_placeholders={
                "info": (
                    f"Comfort settings for the '{zone_name}' zone only. These are "
                    "independent per zone - changing them here does not affect any "
                    "other zone.\n\n"
                    "Target: comfortable temp when awake. Sleep: nighttime temp "
                    "(10 PM - 6 AM). All eight temperatures are required.\n"
                    "Energy Priority and Special Instructions apply to this zone's "
                    "own SOPHIA decisions."
                )
            },
        )
