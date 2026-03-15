# -*- coding: utf-8 -*-
"""Sensors for SOPHIA Climate"""
import logging
from typing import Any
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
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
    """Set up SOPHIA Climate sensors"""
    
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    entities = [
        SophiaClimateStatusSensor(coordinator, entry),
        SophiaClimateDecisionHistorySensor(coordinator, entry, hass),
        SophiaClimateSummarySensor(coordinator, entry),
        SophiaEfficiencyScoreSensor(coordinator, entry, hass),
        SophiaImpactScoreSensor(coordinator, entry),
        SophiaTemperatureDeltaSensor(coordinator, entry),
    ]
    
    # Add zone status sensors
    for zone in coordinator.zones:
        entities.append(SophiaZoneStatusSensor(coordinator, entry, zone))
    
    async_add_entities(entities)


class SophiaClimateStatusSensor(CoordinatorEntity, SensorEntity):
    """Sensor for SOPHIA Climate overall status"""
    
    def __init__(self, coordinator, entry):
        """Initialize sensor"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_name = "SOPHIA Climate Status"
        self._attr_unique_id = f"{DOMAIN}_status"
        self._attr_icon = "mdi:thermostat-auto"
    
    @property
    def state(self) -> str:
        """Return the state"""
        if not self.coordinator.data:
            return "initializing"
        
        decisions = self.coordinator.data.get("decisions", {})
        
        # Count active decisions
        active_count = sum(
            1 for d in decisions.values()
            if d.get("decision") != "NO_CHANGE"
        )
        
        if active_count > 0:
            return "active"
        else:
            return "idle"
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes"""
        if not self.coordinator.data:
            return {}
        
        return {
            "zones": len(self.coordinator.zones) if hasattr(self.coordinator, 'zones') else 0,
            "zone_list": list(self.coordinator.zones) if hasattr(self.coordinator, 'zones') else [],
            "outdoor_temp": self.coordinator.data.get("outdoor_temp"),
            "last_check": self.coordinator.data.get("timestamp"),
            "check_interval": self._entry.data.get("check_interval"),
            "season": self.coordinator.data.get("season"),
            "is_sleep_time": self.coordinator.data.get("is_sleep_time"),
            "energy_priority": self._entry.data.get("energy_priority", "balanced"),
        }


class SophiaClimateSummarySensor(CoordinatorEntity, SensorEntity):
    """Summary sensor for SOPHIA Climate"""
    
    def __init__(self, coordinator, entry):
        """Initialize sensor"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_name = "SOPHIA Climate Summary"
        self._attr_unique_id = f"{DOMAIN}_summary"
        self._attr_icon = "mdi:home-thermometer"
    
    @property
    def state(self) -> str:
        """Return the state"""
        if not self.coordinator.data:
            return "unknown"
        
        zones_data = self.coordinator.data.get("zones", {})
        if not zones_data:
            return "no data"
        
        # Get first zone for summary
        first_zone = next(iter(zones_data.values()), {})
        indoor_temp = first_zone.get("indoor_temp", 0)
        target_temp = first_zone.get("target_temp", 0)
        
        if indoor_temp and target_temp:
            diff = indoor_temp - target_temp
            direction = "above" if diff > 0 else "below"
            return f"{indoor_temp}F ({abs(diff):.1f} {direction})"
        
        return f"{indoor_temp}F"
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes"""
        if not self.coordinator.data:
            return {}
        
        zones_data = self.coordinator.data.get("zones", {})
        first_zone = next(iter(zones_data.values()), {})
        
        return {
            "indoor_temp": first_zone.get("indoor_temp"),
            "outdoor_temp": self.coordinator.data.get("outdoor_temp"),
            "hvac_action": first_zone.get("hvac_action"),
            "season": self.coordinator.data.get("season"),
        }


class SophiaEfficiencyScoreSensor(CoordinatorEntity, SensorEntity):
    """Efficiency score sensor - evaluates SOPHIA climate control performance
    
    HIGH score = Good performance
    - High stability (lots of NO_CHANGE decisions)
    - Low temperature delta (staying close to target)
    - Appropriate actions when needed
    """
    
    def __init__(self, coordinator, entry, hass):
        """Initialize sensor"""
        super().__init__(coordinator)
        self._entry = entry
        self._hass = hass
        self._attr_name = "SOPHIA Efficiency Score"
        self._attr_unique_id = f"{DOMAIN}_efficiency_score"
        self._attr_icon = "mdi:gauge"
        self._attr_native_unit_of_measurement = "%"
        
        # Get history manager from coordinator
        self._history_manager = coordinator.history_manager
    
    @property
    def state(self) -> int:
        """Return the efficiency score (0-100)"""
        if not self.coordinator.data:
            return 0
        
        # Get cached statistics (this was the bug - wasn't properly awaiting)
        # Use a workaround: store stats in coordinator data during update
        stats = getattr(self.coordinator, '_cached_stats', {})
        
        total_decisions = stats.get("total_decisions", 0)
        if total_decisions < 5:
            return 0  # Not enough data yet
        
        stability_pct = stats.get("stability_percentage", 0)
        
        # Get temperature performance
        zones_data = self.coordinator.data.get("zones", {})
        if not zones_data:
            return 0
        
        # Calculate average temperature delta across zones
        temp_deltas = []
        for zone_data in zones_data.values():
            indoor = zone_data.get("indoor_temp", 0)
            target = zone_data.get("sophia_target", 0)  # Use SOPHIA target not thermostat target
            if indoor and target:
                temp_deltas.append(abs(indoor - target))
        
        avg_delta = sum(temp_deltas) / len(temp_deltas) if temp_deltas else 0
        
        # Scoring algorithm:
        # 1. Stability (60% weight) - HIGH stability = GOOD
        #    - 90%+ NO_CHANGE = excellent (60 points)
        #    - 80-90% = good (50 points)
        #    - 70-80% = fair (40 points)
        #    - <70% = needs improvement (<40 points)
        
        if stability_pct >= 90:
            stability_score = 60
        elif stability_pct >= 80:
            stability_score = 50
        elif stability_pct >= 70:
            stability_score = 40
        else:
            # Linearly scale below 70%
            stability_score = int((stability_pct / 70) * 40)
        
        # 2. Temperature accuracy (40% weight) - LOW delta = GOOD
        #    - Within 0.5F = perfect (40 points)
        #    - Within 1F = excellent (35 points)
        #    - Within 2F = good (30 points)
        #    - Within 3F = fair (20 points)
        #    - >3F = needs improvement (<20 points)
        
        if avg_delta <= 0.5:
            temp_score = 40
        elif avg_delta <= 1.0:
            temp_score = 35
        elif avg_delta <= 2.0:
            temp_score = 30
        elif avg_delta <= 3.0:
            temp_score = 20
        else:
            # Linearly scale above 3F
            temp_score = max(0, int(20 - ((avg_delta - 3) * 5)))
        
        total_score = stability_score + temp_score
        
        return min(100, max(0, total_score))
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed scoring breakdown"""
        if not self.coordinator.data:
            return {}
        
        history_sensor = self._hass.states.get(f"sensor.{DOMAIN}_decision_history")
        stability_pct = history_sensor.attributes.get("stability_percentage", 0) if history_sensor else 0
        
        zones_data = self.coordinator.data.get("zones", {})
        temp_deltas = []
        for zone_data in zones_data.values():
            indoor = zone_data.get("indoor_temp", 0)
            target = zone_data.get("target_temp", 0)
            if indoor and target:
                temp_deltas.append(abs(indoor - target))
        
        avg_delta = sum(temp_deltas) / len(temp_deltas) if temp_deltas else 0
        
        # Determine rating
        score = self.state
        if score >= 90:
            rating = "Excellent"
        elif score >= 80:
            rating = "Very Good"
        elif score >= 70:
            rating = "Good"
        elif score >= 60:
            rating = "Fair"
        else:
            rating = "Needs Improvement"
        
        return {
            "rating": rating,
            "stability_percentage": round(stability_pct, 1),
            "avg_temp_delta": round(avg_delta, 2),
            "explanation": self._get_explanation(score, stability_pct, avg_delta),
        }
    
    def _get_explanation(self, score: int, stability: float, delta: float) -> str:
        """Generate human-readable explanation"""
        parts = []
        
        # Stability feedback
        if stability >= 90:
            parts.append("Maintaining temperature excellently")
        elif stability >= 80:
            parts.append("Good temperature stability")
        elif stability >= 70:
            parts.append("Acceptable stability")
        else:
            parts.append("Frequent adjustments needed")
        
        # Temperature accuracy feedback
        if delta <= 0.5:
            parts.append("perfect accuracy")
        elif delta <= 1.0:
            parts.append("excellent accuracy")
        elif delta <= 2.0:
            parts.append("good accuracy")
        elif delta <= 3.0:
            parts.append("fair accuracy")
        else:
            parts.append(f"delta {delta:.1f}F from target")
        
        return ", ".join(parts) + "."


class SophiaImpactScoreSensor(CoordinatorEntity, SensorEntity):
    """Performance impact rating - qualitative assessment"""
    
    def __init__(self, coordinator, entry):
        """Initialize sensor"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_name = "SOPHIA Impact Score"
        self._attr_unique_id = f"{DOMAIN}_impact_score"
        self._attr_icon = "mdi:star"
    
    @property
    def state(self) -> str:
        """Return qualitative performance rating"""
        # This should be based on efficiency score
        efficiency_sensor = self.hass.states.get(f"sensor.{DOMAIN}_efficiency_score")
        if not efficiency_sensor:
            return "Unknown"
        
        try:
            score = int(efficiency_sensor.state)
            
            if score >= 90:
                return "Excellent"
            elif score >= 80:
                return "Very Good"
            elif score >= 70:
                return "Good"
            elif score >= 60:
                return "Fair"
            else:
                return "Needs Improvement"
        except (ValueError, TypeError):
            return "Unknown"
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes"""
        efficiency_sensor = self.hass.states.get(f"sensor.{DOMAIN}_efficiency_score")
        if not efficiency_sensor:
            return {}
        
        return {
            "efficiency_score": efficiency_sensor.state,
            "description": efficiency_sensor.attributes.get("explanation", ""),
        }


class SophiaTemperatureDeltaSensor(CoordinatorEntity, SensorEntity):
    """Temperature delta from target sensor"""
    
    def __init__(self, coordinator, entry):
        """Initialize sensor"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_name = "Temperature Delta"
        self._attr_unique_id = f"{DOMAIN}_temperature_delta"
        self._attr_icon = "mdi:thermometer-lines"
        self._attr_native_unit_of_measurement = "F"
    
    @property
    def state(self) -> float:
        """Return average temperature delta"""
        if not self.coordinator.data:
            return 0.0
        
        zones_data = self.coordinator.data.get("zones", {})
        if not zones_data:
            return 0.0
        
        temp_deltas = []
        for zone_data in zones_data.values():
            indoor = zone_data.get("indoor_temp", 0)
            target = zone_data.get("sophia_target", 0)  # Use SOPHIA target, not thermostat setpoint
            if indoor and target:
                temp_deltas.append(abs(indoor - target))
        
        return round(sum(temp_deltas) / len(temp_deltas), 2) if temp_deltas else 0.0
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return zone-specific deltas"""
        if not self.coordinator.data:
            return {}
        
        zones_data = self.coordinator.data.get("zones", {})
        zone_deltas = {}
        
        for zone_id, zone_data in zones_data.items():
            indoor = zone_data.get("indoor_temp", 0)
            target = zone_data.get("sophia_target", 0)  # Use SOPHIA target
            if indoor and target:
                zone_deltas[zone_id] = round(abs(indoor - target), 2)
        
        return {
            "zone_deltas": zone_deltas,
            "performance": "Excellent" if self.state <= 1.0 else "Good" if self.state <= 2.0 else "Fair"
        }


class SophiaClimateDecisionHistorySensor(CoordinatorEntity, SensorEntity):
    """Sensor tracking SOPHIA Climate decision history WITH FILE-BASED STORAGE"""
    
    def __init__(self, coordinator, entry, hass):
        """Initialize sensor"""
        super().__init__(coordinator)
        self._entry = entry
        self._hass = hass
        self._attr_name = "SOPHIA Climate Decision History"
        self._attr_unique_id = f"{DOMAIN}_decision_history"
        self._attr_icon = "mdi:history"
        
        # Get history manager from coordinator
        self._history_manager = coordinator.history_manager
    
    async def async_added_to_hass(self) -> None:
        """Entity added to hass"""
        await super().async_added_to_hass()
        # History manager is already initialized in coordinator
    
    @property
    def state(self) -> str:
        """Return the state"""
        latest = self._history_manager.get_memory_history()
        
        if not latest:
            return "No decisions yet"
        
        last = latest[0]
        decision = last.get("decision", "UNKNOWN")
        zone = last.get("zone", "unknown")
        return f"Last: {decision} ({zone})"
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return decision history - ONLY recent decisions to avoid 16KB limit"""
        
        # Get recent decisions from memory (max 20)
        recent_history = self._history_manager.get_memory_history()
        
        # Get statistics from coordinator's cache (fixes async issue)
        stats_data = getattr(self.coordinator, '_cached_stats', {
            "total_decisions": len(recent_history),
            "action_decisions": 0,
            "no_change_decisions": 0,
            "action_percentage": 0,
            "stability_percentage": 0,
        })
        
        # Get latest decision with full details
        latest = recent_history[0] if recent_history else None
        
        return {
            "recent_history": recent_history,  # Only last 20 decisions
            "total_decisions": stats_data.get("total_decisions", 0),
            "action_decisions": stats_data.get("action_decisions", 0),
            "no_change_decisions": stats_data.get("no_change_decisions", 0),
            "action_percentage": stats_data.get("action_percentage", 0),
            "stability_percentage": stats_data.get("stability_percentage", 0),
            "latest_decision": latest,
            "history_file": str(self._history_manager.history_file),
            "note": "Full history stored in JSON file to avoid database bloat"
        }
    
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator"""
        if not self.coordinator.data:
            return
        
        decisions = self.coordinator.data.get("decisions", {})
        timestamp = self.coordinator.data.get("timestamp")
        outdoor_temp = self.coordinator.data.get("outdoor_temp")
        season = self.coordinator.data.get("season")
        is_sleep_time = self.coordinator.data.get("is_sleep_time")
        zones_data = self.coordinator.data.get("zones", {})
        
        # Add ALL decisions to history (including NO_CHANGE) using history manager
        for zone_id, decision in decisions.items():
            zone_data = zones_data.get(zone_id, {})
            
            decision_entry = {
                "zone": zone_id,
                "decision": decision.get("decision"),
                "reasoning": decision.get("reasoning"),
                "timestamp": timestamp,
                "outdoor_temp": outdoor_temp,
                "season": season,
                "is_sleep_time": is_sleep_time,
                "indoor_temp": zone_data.get("indoor_temp"),
                "target_temp": zone_data.get("target_temp"),
                "hvac_mode": zone_data.get("hvac_mode"),
                "hvac_action": zone_data.get("hvac_action"),
                "temp_delta": round(
                    ((zone_data.get("indoor_temp") or 0) - (zone_data.get("target_temp") or 0)),
                    1
                ),
            }
            
            # Add to history manager (async, fire and forget)
            self._hass.async_create_task(
                self._history_manager.add_decision(decision_entry)
            )
        
        super()._handle_coordinator_update()


class SophiaZoneStatusSensor(CoordinatorEntity, SensorEntity):
    """Sensor for individual zone status"""
    
    def __init__(self, coordinator, entry, zone):
        """Initialize sensor"""
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
        
        self._attr_name = f"SOPHIA {self._zone_name} Zone Status"
        self._attr_unique_id = f"{DOMAIN}_{self._zone_id}_status"
        self._attr_icon = "mdi:home-thermometer"
    
    @property
    def state(self) -> str:
        """Return the state"""
        if not self.coordinator.data:
            return "unknown"
        
        zones = self.coordinator.data.get("zones", {})
        zone_data = zones.get(self._zone_id)
        
        if not zone_data:
            return "unavailable"
        
        return zone_data.get("hvac_action", "unknown")
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return zone attributes"""
        if not self.coordinator.data:
            return {}
        
        zones = self.coordinator.data.get("zones", {})
        zone_data = zones.get(self._zone_id, {})
        
        decisions = self.coordinator.data.get("decisions", {})
        decision = decisions.get(self._zone_id, {})
        
        return {
            "zone_id": self._zone_id,
            "zone_name": self._zone_name,
            "indoor_temp": zone_data.get("indoor_temp"),
            "target_temp": zone_data.get("target_temp"),
            "hvac_mode": zone_data.get("hvac_mode"),
            "thermostat": zone_data.get("thermostat_entity"),
            "temp_sensor": zone_data.get("temp_sensor"),
            "priority": zone_data.get("priority", 5),
            "last_decision": decision.get("decision"),
            "last_reasoning": decision.get("reasoning"),
            "enabled": True,
        }