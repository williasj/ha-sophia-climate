# -*- coding: utf-8 -*-
"""Climate must recover quickly when thermostats load after it does.

After a Home Assistant restart the first climate check can run before the
thermostat integration (Nest) has created its entities. That check used to
publish an empty result and the dashboard stayed blank until the next
regular check, 30 minutes later.
    python -m pytest -q tests/test_startup_retry.py
"""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


ZONES = [
    {"id": "downstairs", "name": "Downstairs", "thermostat": "climate.downstairs",
     "temp_sensor": None, "priority": 5,
     "seasonal_temps": {s: {"target": 72, "sleep": 70}
                        for s in ("winter", "spring", "summer", "fall")}},
    {"id": "upstairs", "name": "Upstairs", "thermostat": "climate.upstairs",
     "temp_sensor": None, "priority": 5,
     "seasonal_temps": {s: {"target": 72, "sleep": 71}
                        for s in ("winter", "spring", "summer", "fall")}},
]
DATA = {"zones": ZONES, "check_interval": 30,
        "outdoor_temp_sensor": "sensor.outdoor_temperature"}


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def generate(self, prompt, module_id=None, context=None, **_kw):
        self.calls += 1
        return {"response": '{"decision": "NO_CHANGE", "reasoning": "fine"}'}


def _coordinator(hass, llm):
    from custom_components.sophia_climate import SophiaClimateCoordinator
    entry = MockConfigEntry(domain="sophia_climate", data=DATA, version=2)
    entry.add_to_hass(hass)
    hm = AsyncMock()
    hm.get_full_history.return_value = []
    hm.get_statistics.return_value = {}
    return SophiaClimateCoordinator(hass, entry, llm, hm)


def _thermostat(hass, entity, temp):
    hass.states.async_set(entity, "cool", {
        "current_temperature": temp, "temperature": 72, "hvac_action": "idle"})


async def test_missing_thermostats_retry_and_fill_in(hass: HomeAssistant):
    hass.states.async_set("sensor.outdoor_temperature", "55")
    llm = FakeLLM()
    coord = _coordinator(hass, llm)

    await coord.async_refresh()
    assert coord.data["zones"] == {}
    assert set(coord.data["missing_thermostats"]) == {"climate.downstairs", "climate.upstairs"}
    assert llm.calls == 0
    assert coord._missing_retry_unsub is not None

    # Nest finishes loading
    _thermostat(hass, "climate.downstairs", 71)
    _thermostat(hass, "climate.upstairs", 72)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert set(coord.data["zones"]) == {"downstairs", "upstairs"}
    assert coord.data["zones"]["downstairs"]["indoor_temp"] == 71.0
    assert coord.data["missing_thermostats"] == []
    assert coord._missing_retry_unsub is None
    assert llm.calls == 2
    coord.cancel_missing_retry()


async def test_temporarily_missing_zone_keeps_last_readings(hass: HomeAssistant):
    hass.states.async_set("sensor.outdoor_temperature", "55")
    _thermostat(hass, "climate.downstairs", 71)
    _thermostat(hass, "climate.upstairs", 72)
    llm = FakeLLM()
    coord = _coordinator(hass, llm)
    await coord.async_refresh()
    assert llm.calls == 2

    hass.states.async_remove("climate.upstairs")
    await coord.async_refresh()
    up = coord.data["zones"]["upstairs"]
    assert up["indoor_temp"] == 72.0 and up["stale"] is True
    assert llm.calls == 3  # no decision for the missing zone
    coord.cancel_missing_retry()


async def test_retries_stop_after_limit(hass: HomeAssistant):
    from custom_components.sophia_climate import MISSING_THERMOSTAT_MAX_RETRIES
    hass.states.async_set("sensor.outdoor_temperature", "55")
    coord = _coordinator(hass, FakeLLM())
    await coord.async_refresh()
    for _ in range(MISSING_THERMOSTAT_MAX_RETRIES + 2):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
        await hass.async_block_till_done()
        coord.cancel_missing_retry() if coord._missing_retries > MISSING_THERMOSTAT_MAX_RETRIES else None
    assert coord._missing_retries >= MISSING_THERMOSTAT_MAX_RETRIES
    coord.cancel_missing_retry()
