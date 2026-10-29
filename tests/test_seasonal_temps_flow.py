# -*- coding: utf-8 -*-
"""Seasonal temperatures must survive saving the climate options forms.

Spring and fall sleep temps used to be optional fields with no default: the
form showed them empty and saving replaced them with target - 4 every time.
    python -m pytest -q tests/test_seasonal_temps_flow.py
"""
import copy

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


ZONE_TEMPS = {
    "winter": {"target": 72, "sleep": 69},
    "spring": {"target": 73, "sleep": 70},
    "summer": {"target": 74, "sleep": 71},
    "fall": {"target": 72, "sleep": 70},
}

DATA = {
    "zones": [
        {"id": "downstairs", "name": "Downstairs", "enabled": True, "priority": 5,
         "thermostat": "climate.downstairs", "temp_sensor": None,
         "energy_priority": "comfort", "aggressive_optimization": False,
         "special_instructions": "", "seasonal_temps": copy.deepcopy(ZONE_TEMPS)},
        {"id": "upstairs", "name": "Upstairs", "enabled": True, "priority": 5,
         "thermostat": "climate.upstairs", "temp_sensor": None,
         "energy_priority": "comfort", "aggressive_optimization": False,
         "special_instructions": "",
         "seasonal_temps": {s: {"target": 72, "sleep": 71} for s in ZONE_TEMPS}},
    ],
    "check_interval": 30,
    "outdoor_temp_sensor": "sensor.outdoor_temperature",
    "seasonal_temps": copy.deepcopy(ZONE_TEMPS),
}

TEMP_KEYS = [f"{s}_{k}" for s in ("winter", "spring", "summer", "fall")
             for k in ("target", "sleep")]


def _defaults(result):
    """What the HA UI would submit if the user changed nothing."""
    out = {}
    for key in result["data_schema"].schema:
        default = key.default() if callable(getattr(key, "default", None)) else None
        if default is not None and not isinstance(default, type(None)):
            out[str(key)] = default
    return out


def _entry(hass):
    entry = MockConfigEntry(domain="sophia_climate", data=copy.deepcopy(DATA), version=2)
    entry.add_to_hass(hass)
    return entry


async def _open_zone_form(hass, entry, zone_id="downstairs"):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "edit_zone"})
    if result.get("step_id") == "edit_zone":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"zone_id": zone_id})
    assert result["step_id"] == "zone_settings", result
    return result


async def test_zone_form_shows_every_saved_temperature(hass: HomeAssistant):
    entry = _entry(hass)
    result = await _open_zone_form(hass, entry)
    keys = {str(k): k for k in result["data_schema"].schema}
    for name in TEMP_KEYS:
        season, kind = name.split("_")
        marker = keys[name]
        assert type(marker).__name__ == "Required", f"{name} must be required"
        assert marker.default() == ZONE_TEMPS[season][kind], name


async def test_saving_unrelated_zone_setting_keeps_spring_and_fall(hass: HomeAssistant):
    entry = _entry(hass)
    result = await _open_zone_form(hass, entry)
    submit = _defaults(result)
    submit["energy_priority"] = "savings"
    result = await hass.config_entries.options.async_configure(result["flow_id"], submit)
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    zone = entry.data["zones"][0]
    assert zone["seasonal_temps"] == ZONE_TEMPS
    assert zone["energy_priority"] == "savings"
    # the other zone is untouched
    assert entry.data["zones"][1]["seasonal_temps"]["fall"] == {"target": 72, "sleep": 71}


async def test_changed_fall_temps_are_stored(hass: HomeAssistant):
    entry = _entry(hass)
    result = await _open_zone_form(hass, entry)
    submit = _defaults(result)
    submit.update({"fall_target": 71, "fall_sleep": 67, "spring_sleep": 66})
    await hass.config_entries.options.async_configure(result["flow_id"], submit)
    st = entry.data["zones"][0]["seasonal_temps"]
    assert st["fall"] == {"target": 71, "sleep": 67}
    assert st["spring"] == {"target": 73, "sleep": 66}
    # reopening shows the new values
    result = await _open_zone_form(hass, entry)
    keys = {str(k): k for k in result["data_schema"].schema}
    assert keys["fall_sleep"].default() == 67
    assert keys["spring_sleep"].default() == 66


async def test_omitted_sleep_temp_keeps_saved_value(hass: HomeAssistant):
    """A submission without fall_sleep falls back to the saved value, not target - 4."""
    entry = _entry(hass)
    result = await _open_zone_form(hass, entry)
    submit = _defaults(result)
    submit.pop("fall_sleep")
    submit.pop("spring_sleep")
    submit["fall_target"] = 75
    result = await hass.config_entries.options.async_configure(result["flow_id"], submit)
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    st = entry.data["zones"][0]["seasonal_temps"]
    assert st["fall"] == {"target": 75, "sleep": 70}
    assert st["spring"]["sleep"] == 70


async def test_global_seasonal_step_keeps_values(hass: HomeAssistant):
    entry = _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    from custom_components.sophia_climate.config_flow import _seasonal_fields
    fields = {str(k): k for k in _seasonal_fields(entry.data["seasonal_temps"])}
    assert fields["spring_sleep"].default() == 70
    assert fields["fall_sleep"].default() == 70
    hass.config_entries.options.async_abort(flow["flow_id"])
