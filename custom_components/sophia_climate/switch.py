# -*- coding: utf-8 -*-
"""Switches for SOPHIA Climate"""
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SOPHIA Climate switches"""
    
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    entities = [
        SophiaClimateEnabledSwitch(coordinator, entry),
    ]
    
    # Add zone enable/disable switches
    for zone in coordinator.zones:
        entities.append(SophiaZoneSwitch(coordinator, entry, zone))
    
    async_add_entities(entities)


class SophiaClimateEnabledSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable SOPHIA Climate control"""
    
    def __init__(self, coordinator, entry):
        """Initialize switch"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_name = "SOPHIA Climate Enabled"
        self._attr_unique_id = f"{DOMAIN}_enabled"
        self._attr_icon = "mdi:robot"
        self._is_on = True
    
    @property
    def is_on(self) -> bool:
        """Return true if switch is on"""
        return self._is_on
    
    async def async_turn_on(self, **kwargs):
        """Turn the switch on"""
        self._is_on = True
        self.async_write_ha_state()
        _LOGGER.info("SOPHIA Climate control enabled")
        
        # Resume coordinator updates
        await self.coordinator.async_request_refresh()
    
    async def async_turn_off(self, **kwargs):
        """Turn the switch off"""
        self._is_on = False
        self.async_write_ha_state()
        _LOGGER.info("SOPHIA Climate control disabled")


class SophiaZoneSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable individual zones"""
    
    def __init__(self, coordinator, entry, zone):
        """Initialize switch"""
        super().__init__(coordinator)
        self._entry = entry
        
        # Handle both string and dict zone formats
        if isinstance(zone, dict):
            self._zone_id = zone.get("id")
            self._zone_name = zone.get("name", self._zone_id.title())
        else:
            # String format (v1)
            self._zone_id = zone
            self._zone_name = zone.title()
        
        self._attr_name = f"SOPHIA {self._zone_name} Zone"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_enabled"
        self._attr_icon = "mdi:home-thermometer-outline"
        self._is_on = True
    
    @property
    def is_on(self) -> bool:
        """Return true if zone is enabled"""
        return self._is_on
    
    async def async_turn_on(self, **kwargs):
        """Enable zone"""
        self._is_on = True
        self.async_write_ha_state()
        _LOGGER.info(f"Enabled {self._zone_id} zone")
    
    async def async_turn_off(self, **kwargs):
        """Disable zone"""
        self._is_on = False
        self.async_write_ha_state()
        _LOGGER.info(f"Disabled {self._zone_id} zone")