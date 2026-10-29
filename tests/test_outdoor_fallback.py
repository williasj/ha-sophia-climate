# -*- coding: utf-8 -*-
"""SOPHIA Climate must keep working when outdoor or indoor sensors die.

Drives the real SophiaClimateCoordinator._run_climate_check under Home
Assistant with a fake LLM client and history manager.
    python -m pytest -q tests/test_outdoor_fallback.py
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


ZONES = [
    {"id": "downstairs", "name": "Downstairs", "enabled": True, "priority": 5,
     "thermostat": "climate.downstairs", "temp_sensor": None,
     "energy_priority": "comfort", "aggressive_optimization": False,
     "special_instructions": "",
     "seasonal_temps": {s: {"target": 72, "sleep": 70}
                        for s in ("winter", "spring", "summer", "fall")}},
    {"id": "upstairs", "name": "Upstairs", "enabled": True, "priority": 5,
     "thermostat": "climate.upstairs", "temp_sensor": "sensor.upstairs_temp",
     "energy_priority": "comfort", "aggressive_optimization": False,
     "special_instructions": "",
     "seasonal_temps": {s: {"target": 72, "sleep": 71}
                        for s in ("winter", "spring", "summer", "fall")}},
]

ENTRY_DATA = {
    "zones": ZONES,
    "check_interval": 30,
    "energy_priority": "comfort",
    "outdoor_temp_sensor": "sensor.home_temperature",
    "outdoor_humidity_sensor": "sensor.home_relative_humidity",
    "passive_humidity_limit": 60.0,
    "seasonal_temps": {s: {"target": 72, "sleep": 70}
                       for s in ("winter", "spring", "summer", "fall")},
}


class FakeLLM:
    def __init__(self):
        self.prompts = []

    async def generate(self, prompt, module_id=None, context=None, **_kw):
        self.prompts.append(prompt)
        return {"response": '{"decision": "NO_CHANGE", "reasoning": "fine"}'}


def _history(full_history=None):
    hm = AsyncMock()
    hm.get_full_history.return_value = full_history or []
    hm.get_statistics.return_value = {"total_decisions": 0}
    return hm


def _thermostats(hass, upstairs_sensor="73.5"):
    hass.states.async_set("climate.downstairs", "cool", {
        "current_temperature": 74, "temperature": 72, "hvac_action": "idle"})
    hass.states.async_set("climate.upstairs", "cool", {
        "current_temperature": 75, "temperature": 72, "hvac_action": "idle"})
    hass.states.async_set("sensor.upstairs_temp", upstairs_sensor)


def _coordinator(hass, llm, history, data=None):
    from custom_components.sophia_climate import SophiaClimateCoordinator
    entry = MockConfigEntry(domain="sophia_climate", data=data or ENTRY_DATA, version=2)
    entry.add_to_hass(hass)
    return SophiaClimateCoordinator(hass, entry, llm, history)


async def test_outdoor_sensor_ok(hass: HomeAssistant):
    _thermostats(hass)
    hass.states.async_set("sensor.home_temperature", "68.0")
    hass.states.async_set("sensor.home_relative_humidity", "55")
    llm = FakeLLM()
    coord = _coordinator(hass, llm, _history())
    data = await coord._run_climate_check()
    assert data["outdoor_temp"] == 68.0
    assert data["outdoor_source"] == "sensor.home_temperature"
    assert data["outdoor_humidity"] == 55.0
    assert len(llm.prompts) == 2
    assert "Outdoor: 68.0F" in llm.prompts[0]


async def test_unavailable_sensor_falls_back_to_weather_entity(hass: HomeAssistant):
    """The 2026-10-25 outage: Weather.com throttled, sensors unavailable."""
    _thermostats(hass)
    hass.states.async_set("sensor.home_temperature", "unavailable")
    hass.states.async_set("sensor.home_relative_humidity", "unavailable")
    hass.states.async_set("weather.home", "unavailable")
    hass.states.async_set("weather.forecast_home", "cloudy", {
        "temperature": 61.2, "humidity": 80, "temperature_unit": "\u00b0F"})
    llm = FakeLLM()
    coord = _coordinator(hass, llm, _history())
    data = await coord._run_climate_check()
    assert data["outdoor_temp"] == 61.2
    assert data["outdoor_source"] == "weather.forecast_home"
    assert data["outdoor_humidity"] == 80.0
    assert len(llm.prompts) == 2


async def test_celsius_weather_entity_is_converted(hass: HomeAssistant):
    _thermostats(hass)
    hass.states.async_set("sensor.home_temperature", "unknown")
    hass.states.async_set("weather.met", "sunny", {
        "temperature": 20.0, "temperature_unit": "\u00b0C"})
    coord = _coordinator(hass, FakeLLM(), _history())
    data = await coord._run_climate_check()
    assert data["outdoor_temp"] == 68.0


async def test_preferred_weather_entity_wins(hass: HomeAssistant):
    _thermostats(hass)
    hass.states.async_set("sensor.home_temperature", "unavailable")
    hass.states.async_set("weather.aaa", "sunny", {"temperature": 50})
    hass.states.async_set("weather.zzz", "sunny", {"temperature": 60})
    data_in = dict(ENTRY_DATA, outdoor_weather_entity="weather.zzz")
    coord = _coordinator(hass, FakeLLM(), _history(), data_in)
    data = await coord._run_climate_check()
    assert data["outdoor_source"] == "weather.zzz"


async def test_recent_history_reading_used_when_no_weather(hass: HomeAssistant):
    _thermostats(hass)
    hass.states.async_set("sensor.home_temperature", "unavailable")
    recent = (datetime.now() - timedelta(minutes=40)).isoformat()
    llm = FakeLLM()
    coord = _coordinator(hass, llm, _history([{"timestamp": recent, "outdoor_temp": 66}]))
    data = await coord._run_climate_check()
    assert data["outdoor_temp"] == 66.0
    assert data["outdoor_source"].startswith("last known reading")
    assert len(llm.prompts) == 2


async def test_no_outdoor_data_leaves_thermostats_alone(hass: HomeAssistant):
    _thermostats(hass)
    hass.states.async_set("sensor.home_temperature", "unavailable")
    old = (datetime.now() - timedelta(hours=9)).isoformat()
    llm = FakeLLM()
    coord = _coordinator(hass, llm, _history([{"timestamp": old, "outdoor_temp": 66}]))
    data = await coord._run_climate_check()
    assert data["outdoor_temp"] is None
    assert data["outdoor_source"] is None
    assert llm.prompts == []
    assert {d["decision"] for d in data["decisions"].values()} == {"NO_CHANGE"}
    assert set(data["decisions"]) == {"downstairs", "upstairs"}


async def test_missing_outdoor_entity_no_longer_invents_50f(hass: HomeAssistant):
    _thermostats(hass)
    llm = FakeLLM()
    coord = _coordinator(hass, llm, _history())
    data = await coord._run_climate_check()
    assert data["outdoor_temp"] is None
    assert llm.prompts == []


async def test_unavailable_indoor_sensor_uses_thermostat_reading(hass: HomeAssistant):
    _thermostats(hass, upstairs_sensor="unavailable")
    hass.states.async_set("sensor.home_temperature", "70")
    coord = _coordinator(hass, FakeLLM(), _history())
    data = await coord._run_climate_check()
    assert data["zones"]["upstairs"]["indoor_temp"] == 75.0


async def test_unavailable_thermostat_skips_only_that_zone(hass: HomeAssistant):
    _thermostats(hass)
    hass.states.async_set("climate.downstairs", "unavailable")
    hass.states.async_set("sensor.home_temperature", "70")
    llm = FakeLLM()
    coord = _coordinator(hass, llm, _history())
    data = await coord._run_climate_check()
    assert set(data["zones"]) == {"upstairs"}
    assert len(llm.prompts) == 1


# ----------------------------------------------------------------------
# Fan handling with a thermostat that refuses fan commands while off
# (Nest SDM: "Fan.SetTimer command not allowed in current thermostat mode")
# ----------------------------------------------------------------------

def _nest_like(hass, calls, fan_mode="off"):
    from homeassistant.exceptions import HomeAssistantError

    hass.states.async_set("switch.sophia_climate_enabled", "on")
    hass.states.async_set("climate.downstairs", "cool", {
        "current_temperature": 74, "temperature": 72, "hvac_action": "cooling",
        "fan_mode": fan_mode, "fan_modes": ["on", "off"]})

    async def set_hvac_mode(call):
        calls.append(("hvac", call.data["hvac_mode"]))
        st = hass.states.get("climate.downstairs")
        hass.states.async_set("climate.downstairs", call.data["hvac_mode"], st.attributes)

    async def set_fan_mode(call):
        calls.append(("fan", call.data["fan_mode"]))
        if hass.states.get("climate.downstairs").state == "off":
            raise HomeAssistantError(
                "FAILED_PRECONDITION: Fan.SetTimer command not allowed in current thermostat mode")

    async def set_temperature(call):
        calls.append(("temp", call.data.get("hvac_mode"), call.data.get("temperature")))
        st = hass.states.get("climate.downstairs")
        hass.states.async_set("climate.downstairs", call.data.get("hvac_mode", st.state), st.attributes)

    hass.services.async_register("climate", "set_hvac_mode", set_hvac_mode)
    hass.services.async_register("climate", "set_fan_mode", set_fan_mode)
    hass.services.async_register("climate", "set_temperature", set_temperature)


ZONE = {"thermostat_entity": "climate.downstairs", "indoor_temp": 74.0,
        "sophia_target": 72, "target_temp": 72, "hvac_mode": "cool"}


async def test_off_with_fan_already_off_sends_no_fan_command(hass: HomeAssistant, caplog):
    calls = []
    _nest_like(hass, calls, fan_mode="off")
    coord = _coordinator(hass, FakeLLM(), _history())
    await coord._execute_decision("downstairs", dict(ZONE),
                                  {"decision": "OFF", "reasoning": "sleep"})
    assert calls == [("hvac", "off")]
    assert hass.states.get("climate.downstairs").state == "off"
    assert "Error turning off HVAC" not in caplog.text


async def test_off_when_thermostat_refuses_fan_is_not_an_error(hass: HomeAssistant, caplog):
    calls = []
    _nest_like(hass, calls, fan_mode="on")
    coord = _coordinator(hass, FakeLLM(), _history())
    await coord._execute_decision("downstairs", dict(ZONE),
                                  {"decision": "OFF", "reasoning": "sleep"})
    assert calls == [("hvac", "off"), ("fan", "off")]
    assert hass.states.get("climate.downstairs").state == "off"
    assert "Error turning off HVAC" not in caplog.text
    assert "refused fan mode off" in caplog.text


async def test_off_fan_on_keeps_hvac_off_when_fan_refused(hass: HomeAssistant, caplog):
    calls = []
    _nest_like(hass, calls, fan_mode="off")
    coord = _coordinator(hass, FakeLLM(), _history())
    await coord._execute_decision("downstairs", dict(ZONE),
                                  {"decision": "OFF_FAN_ON", "reasoning": "circulate"})
    assert calls == [("hvac", "off"), ("fan", "on")]
    assert hass.states.get("climate.downstairs").state == "off"
    assert "Error setting circulation mode" not in caplog.text


async def test_cool_setpoint_still_applies(hass: HomeAssistant, caplog):
    calls = []
    _nest_like(hass, calls, fan_mode="on")
    coord = _coordinator(hass, FakeLLM(), _history())
    await coord._execute_decision("downstairs", dict(ZONE),
                                  {"decision": "COOL_71", "reasoning": "warm"})
    temps = [c for c in calls if c[0] == "temp"]
    assert temps and temps[0][1] == "cool" and float(temps[0][2]) == 71.0, calls
    assert ("fan", "off") in calls
    assert "Error executing decision" not in caplog.text
