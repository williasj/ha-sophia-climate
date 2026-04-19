# -*- coding: utf-8 -*-
"""
SOPHIA Climate - Multi-zone HVAC control with AI decision making
Registers with SOPHIA Core for LLM access and capability discovery
"""
import logging
from datetime import timedelta, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    DEFAULT_RAG_ENABLED,
    DEFAULT_RAG_RETENTION_DAYS,
    DEFAULT_RAG_MEMORY_ENTRIES,
    RAG_COLLECTION_DECISIONS,
    RAG_PURGE_INTERVAL_HOURS,
)
from .history_manager import ClimateHistoryManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch"]

# Service names this module provides
SERVICES = [
    "run_climate_check",
    "set_zone_target",
    "enable_zone",
    "disable_zone",
    "cleanup_history",
    "export_history"
]


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry to new format"""
    _LOGGER.info("Checking if SOPHIA Climate config needs migration...")
    
    if config_entry.version == 1:
        # This is a v1 config - migrate to v2
        _LOGGER.info("Migrating SOPHIA Climate from version 1 to version 2")
        
        new_data = {**config_entry.data}
        
        # Convert old string zones to new zone objects if needed
        if "zones" in new_data and isinstance(new_data["zones"], list):
            if new_data["zones"] and isinstance(new_data["zones"][0], str):
                # Old format - convert
                old_zones = new_data["zones"]
                new_zones = []
                
                for zone_name in old_zones:
                    zone_id = zone_name.lower().replace(" ", "_")
                    new_zones.append({
                        "id": zone_id,
                        "name": zone_name.capitalize(),
                        "thermostat": new_data.get(f"{zone_id}_thermostat"),
                        "temp_sensor": None,
                        "priority": 5,
                        "enabled": True
                    })
                
                new_data["zones"] = new_zones
                
                # Remove old thermostat keys
                for zone_name in old_zones:
                    zone_id = zone_name.lower().replace(" ", "_")
                    new_data.pop(f"{zone_id}_thermostat", None)
        
        # Add seasonal temps if missing
        if "seasonal_temps" not in new_data:
            new_data["seasonal_temps"] = {
                "winter": {"target": 72, "sleep": 68},
                "spring": {"target": 72, "sleep": 68},
                "summer": {"target": 70, "sleep": 66},
                "fall": {"target": 72, "sleep": 68}
            }
        
        # Add new fields if missing
        if "special_instructions" not in new_data:
            new_data["special_instructions"] = ""
        
        if "energy_priority" not in new_data:
            new_data["energy_priority"] = "balanced"
        
        if "aggressive_optimization" not in new_data:
            new_data["aggressive_optimization"] = False
        
        # Update version
        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new_data)
        
        _LOGGER.info("Migration to version 2 successful")
        return True
    
    return True


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up SOPHIA Climate from configuration.yaml (legacy)"""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SOPHIA Climate from a config entry"""
    
    _LOGGER.info("Setting up SOPHIA Climate v2.0...")
    
    # Wait for SOPHIA Core to be ready
    if "sophia_core" not in hass.data:
        _LOGGER.error("SOPHIA Core not found! Climate module requires Core.")
        return False
    
    # Get references to Core services
    core_data = hass.data["sophia_core"]
    registry = core_data["registry"]
    llm_client = core_data["llm_client"]
    
    # Ask sophia_core whether a RAG backend is available. Scoped integrations
    # never probe Qdrant or TEI config directly - they use core's public API.
    try:
        core_has_rag = bool(llm_client.has_rag_backend())
    except AttributeError:
        # Older sophia_core without has_rag_backend helper - assume no RAG.
        core_has_rag = False

    # Read user-configured RAG decision-history settings (options flow)
    rag_enabled_cfg = entry.data.get("rag_decision_enabled", DEFAULT_RAG_ENABLED)
    rag_retention_days = entry.data.get(
        "rag_decision_retention_days", DEFAULT_RAG_RETENTION_DAYS
    )
    rag_memory_entries = entry.data.get(
        "rag_decision_memory_entries", DEFAULT_RAG_MEMORY_ENTRIES
    )
    rag_active = bool(core_has_rag and rag_enabled_cfg)

    # Create history manager for file-based decision storage (and optional RAG mirror)
    history_manager = ClimateHistoryManager(
        hass=hass,
        config_dir=hass.config.config_dir,
        max_memory_entries=rag_memory_entries,
        max_file_entries=500,
        llm_client=llm_client if rag_active else None,
        rag_enabled=rag_active,
        rag_collection=RAG_COLLECTION_DECISIONS,
        rag_retention_days=rag_retention_days,
    )
    await history_manager.initialize()

    # Schedule nightly RAG purge when RAG mirroring is active
    if rag_active:
        async def _purge_rag(_now):
            removed = await history_manager.purge_rag_older_than()
            if removed:
                _LOGGER.info(
                    "SOPHIA Climate: purged %d RAG decisions older than %d days",
                    removed, rag_retention_days,
                )

        unsub_purge = async_track_time_interval(
            hass, _purge_rag, timedelta(hours=RAG_PURGE_INTERVAL_HOURS)
        )
        entry.async_on_unload(unsub_purge)
    
    # Store module data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": None,  # Will be created below
        "llm_client": llm_client,
        "config": entry.data,
        "history_manager": history_manager
    }
    
    # Create coordinator for climate control
    coordinator = SophiaClimateCoordinator(hass, entry, llm_client, history_manager)
    await coordinator.async_config_entry_first_refresh()
    
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator
    
    # Helper function to build zone entities for dashboard
    def _build_zone_entities(zones):
        """Build zone entity list for dashboard"""
        entities = []
        for zone in zones:
            if isinstance(zone, dict):
                zone_id = zone.get("id")
            else:
                zone_id = zone.lower().replace(" ", "_")
            
            entities.append(f"sensor.sophia_{zone_id}_zone_status")
            entities.append(f"switch.sophia_{zone_id}_zone")
        
        return entities
    
    # Resolve first zone info for dashboard card generation
    _zones_for_dash = entry.data.get("zones", [])
    _first_zone = _zones_for_dash[0] if _zones_for_dash else {}
    _first_zone_id = _first_zone.get("id", "main") if isinstance(_first_zone, dict) else "main"
    _first_zone_thermostat = _first_zone.get("thermostat", "") if isinstance(_first_zone, dict) else ""
    _outdoor_sensor = entry.data.get("outdoor_temp_sensor", "sensor.outdoor_temperature")
    _first_zone_status = f"sensor.sophia_{_first_zone_id}_zone_status"

    # Build capabilities dict with ENHANCED dashboard config
    capabilities = {
        "name": "SOPHIA Climate Control",
        "version": "2.0.0",
        "services": SERVICES,
        "sensors": [
            "sensor.sophia_climate_status",
            "sensor.sophia_climate_decision_history",
            "sensor.sophia_climate_summary",
            "sensor.sophia_efficiency_score",
            "sensor.sophia_impact_score",
            "sensor.temperature_delta",
        ],
        "controls": [
            "switch.sophia_climate_enabled",
        ],
        "requires_llm": True,
        "metadata": {
            "zones": entry.data.get("zones", []),
            "check_interval": entry.data.get("check_interval", 30),
            "description": "AI-powered multi-zone HVAC control with seasonal optimization"
        },
        
        # ENHANCED Dashboard Configuration - Beautiful Visuals!
        "dashboard_config": {
            "title": "Climate",
            "path": "climate",
            "badges": [],
            "cards": [
                # Header
                {
                    "type": "markdown",
                    "content": (
                        "# SOPHIA Climate Control\n"
                        "## AI-Powered HVAC Optimization\n\n"
                        "**Status:** {{ states('sensor.sophia_climate_status') | title }}  \n"
                        "**Indoor:** {{ state_attr('sensor.sophia_climate_summary', 'indoor_temp') | default('--') }}F  \n"
                        "**Outdoor:** {{ state_attr('sensor.sophia_climate_summary', 'outdoor_temp') | default('--') }}F  \n"
                        "**Season:** {{ state_attr('sensor.sophia_climate_status', 'season') | default('Unknown') | title }}"
                    )
                },
                
                # Current Climate Status
                {
                    "type": "entities",
                    "title": "Current Climate Status",
                    "entities": [
                        _first_zone_thermostat,
                        {"entity": _outdoor_sensor, "name": "Outdoor Temperature"},
                    ]
                },
                
                # Temperature History Graph
                {
                    "type": "history-graph",
                    "title": "Temperature Last 24 Hours",
                    "entities": [
                        {"entity": _first_zone_thermostat, "name": "Indoor"},
                        {"entity": _outdoor_sensor, "name": "Outdoor"}
                    ],
                    "hours_to_show": 24
                },
                
                # Efficiency Score Gauge
                {
                    "type": "gauge",
                    "entity": "sensor.sophia_efficiency_score",
                    "name": "SOPHIA Efficiency Score",
                    "min": 0,
                    "max": 100,
                    "unit": "%",
                    "segments": [
                        {"from": 0, "to": 50, "color": "red"},
                        {"from": 50, "to": 75, "color": "orange"},
                        {"from": 75, "to": 95, "color": "yellow"},
                        {"from": 95, "to": 100, "color": "green"}
                    ]
                },
                
                # SOPHIA Performance
                {
                    "type": "entities",
                    "title": "SOPHIA Performance",
                    "entities": [
                        {"entity": "sensor.sophia_efficiency_score", "name": "Current Efficiency Score"},
                        {"entity": "sensor.sophia_impact_score", "name": "Performance Rating"},
                        {"entity": "sensor.temperature_delta", "name": "Temperature Stability"}
                    ]
                },
                
                # Note: Energy tracking cards require user-configured energy monitoring sensors.
                # Add your own energy entities here after setup.
                
                # Climate Zones
                {
                    "type": "entities",
                    "title": "Climate Zones",
                    "show_header_toggle": False,
                    "entities": _build_zone_entities(entry.data.get("zones", []))
                },
                
                # Decision Statistics
                {
                    "type": "entities",
                    "title": "SOPHIA Decision Statistics",
                    "state_color": False,
                    "entities": [
                        {"entity": "sensor.sophia_climate_decision_history", "name": "Latest Decision"},
                        {
                            "type": "attribute",
                            "entity": "sensor.sophia_climate_decision_history",
                            "attribute": "total_decisions",
                            "name": "Total Decisions Made",
                            "icon": "mdi:counter"
                        }
                    ]
                },
                
                # Latest AI Decision Details (first configured zone)
                {
                    "type": "markdown",
                    "title": "Latest AI Decision",
                    "content": (
                        f"**Decision:** `{{{{ state_attr('{_first_zone_status}', 'last_decision') | default('None') }}}}`\n\n"
                        f"**Reasoning:**  \n"
                        f"{{{{ state_attr('{_first_zone_status}', 'last_reasoning') | default('Waiting for first check...') }}}}\n\n"
                        f"---\n\n"
                        f"**Indoor:** {{{{ state_attr('{_first_zone_status}', 'indoor_temp') | default('--') }}}}F  \n"
                        f"**Target:** {{{{ state_attr('{_first_zone_status}', 'target_temp') | default('--') }}}}F  \n"
                        f"**Mode:** {{{{ state_attr('{_first_zone_status}', 'hvac_mode') | default('Unknown') }}}}  \n"
                        f"**Action:** {{{{ states('{_first_zone_status}') }}}}"
                    )
                },
                
                # Recent Decisions History
                {
                    "type": "markdown",
                    "title": "Recent SOPHIA Decisions",
                    "content": (
                        "<div style=\"max-height: 500px; overflow-y: auto; padding: 4px;\">\n\n"
                        "{% set history = state_attr('sensor.sophia_climate_decision_history', 'recent_history') -%}\n"
                        "{%- if history -%}\n"
                        "{%- for entry in history[:10] -%}\n\n"
                        "<div style=\"padding: 12px; margin-bottom: 12px; border-left: 4px solid orange; background: rgba(255,255,255,0.05);\">\n\n"
                        "**{{ entry['decision'] }}** - {{ entry['timestamp'] }}\n\n"
                        "*{{ entry['reasoning'] }}*\n\n"
                        "Zone: **{{ entry['zone'] }}**\n\n"
                        "</div>\n\n"
                        "{%- endfor -%}\n"
                        "{%- else -%}\n\n"
                        "*No decisions recorded yet.*\n\n"
                        "{%- endif -%}\n\n"
                        "</div>"
                    )
                },
                
                # Manual Controls
                {
                    "type": "horizontal-stack",
                    "cards": [
                        {
                            "type": "button",
                            "name": "Run Climate Check",
                            "icon": "mdi:play-circle",
                            "tap_action": {
                                "action": "call-service",
                                "service": "sophia_climate.run_climate_check"
                            }
                        },
                        {
                            "type": "entity",
                            "entity": "switch.sophia_climate_enabled",
                            "name": "SOPHIA Control"
                        }
                    ]
                },
                
                # System Statistics
                {
                    "type": "markdown",
                    "content": (
                        "### System Statistics\n\n"
                        "**Zones:** {{ state_attr('sensor.sophia_climate_status', 'zones') | default(0) }}  \n"
                        "**Active Zones:** {{ (state_attr('sensor.sophia_climate_status', 'zone_list') or []) | join(', ') or 'None' }}  \n"
                        "**Check Interval:** {{ state_attr('sensor.sophia_climate_status', 'check_interval') | default(30) }} min  \n"
                        "**Energy Priority:** {{ state_attr('sensor.sophia_climate_status', 'energy_priority') | default('balanced') | title }}  \n"
                        "**Sleep Mode:** {{ 'Active' if state_attr('sensor.sophia_climate_status', 'is_sleep_time') else 'Inactive' }}  \n"
                        "**Total Decisions:** {{ state_attr('sensor.sophia_climate_decision_history', 'total_decisions') | default(0) }}\n\n"
                        "{% if state_attr('sensor.sophia_climate_status', 'last_check') %}"
                        "**Last Check:** {{ relative_time(strptime(state_attr('sensor.sophia_climate_status', 'last_check'), '%Y-%m-%dT%H:%M:%S.%f')) }}"
                        "{% else %}"
                        "**Last Check:** Never"
                        "{% endif %}"
                    )
                }
            ]
        }
    }
    
    # Add zone-specific sensors and controls to capabilities
    for zone in entry.data.get("zones", []):
        if isinstance(zone, dict):
            zone_id = zone.get("id")
        else:
            zone_id = zone.lower().replace(" ", "_")
        
        capabilities["sensors"].append(f"sensor.sophia_{zone_id}_zone_status")
        capabilities["controls"].append(f"switch.sophia_{zone_id}_zone")
    
    # Register with SOPHIA Core
    success = registry.register_module(DOMAIN, capabilities)
    
    if success:
        _LOGGER.info(f"Successfully registered with SOPHIA Core")
        _LOGGER.info(f"Zones configured: {[z.get('name') if isinstance(z, dict) else z for z in entry.data.get('zones', [])]}")
    else:
        _LOGGER.error("Failed to register with SOPHIA Core!")
        return False
    
    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    async def handle_run_climate_check(call):
        """Handle manual climate check service call"""
        _LOGGER.info("Manual climate check triggered")
        await coordinator.async_request_refresh()
    
    async def handle_set_zone_target(call):
        """Handle set zone target temperature"""
        zone_id = call.data.get("zone_id")
        temperature = call.data.get("temperature")
        
        _LOGGER.info(f"Setting {zone_id} target to {temperature}F")
        # Implementation here
    
    async def handle_enable_zone(call):
        """Enable a climate zone"""
        zone_id = call.data.get("zone_id")
        _LOGGER.info(f"Enabling zone: {zone_id}")
        # Implementation here
    
    async def handle_disable_zone(call):
        """Disable a climate zone"""
        zone_id = call.data.get("zone_id")
        _LOGGER.info(f"Disabling zone: {zone_id}")
        # Implementation here
    
    async def handle_cleanup_history(call: ServiceCall):
        """Cleanup old decision history"""
        days = call.data.get("days", 30)
        _LOGGER.info(f"Cleaning up decision history older than {days} days")
        
        removed = await history_manager.cleanup_old(days)
        _LOGGER.info(f"Removed {removed} old decision entries")
    
    async def handle_export_history(call: ServiceCall):
        """Export decision history to CSV"""
        filepath = call.data.get("filepath")
        _LOGGER.info(f"Exporting decision history to {filepath}")
        
        try:
            await history_manager.export_to_csv(filepath)
            _LOGGER.info(f"Successfully exported history to {filepath}")
        except Exception as e:
            _LOGGER.error(f"Failed to export history: {e}")
    
    # Register services
    hass.services.async_register(DOMAIN, "run_climate_check", handle_run_climate_check)
    hass.services.async_register(DOMAIN, "set_zone_target", handle_set_zone_target)
    hass.services.async_register(DOMAIN, "enable_zone", handle_enable_zone)
    hass.services.async_register(DOMAIN, "disable_zone", handle_disable_zone)
    hass.services.async_register(DOMAIN, "cleanup_history", handle_cleanup_history)
    hass.services.async_register(DOMAIN, "export_history", handle_export_history)
    
    # Register options update listener for auto-reload on config changes
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    _LOGGER.info("SOPHIA Climate v2.0 setup complete")
    
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update - automatically reload the integration when settings change."""
    _LOGGER.info("SOPHIA Climate options changed - automatically reloading integration...")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload SOPHIA Climate"""
    
    _LOGGER.info("Unloading SOPHIA Climate...")
    
    # Unregister from SOPHIA Core
    if "sophia_core" in hass.data:
        registry = hass.data["sophia_core"]["registry"]
        registry.unregister_module(DOMAIN)
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    
    return unload_ok


class SophiaClimateCoordinator(DataUpdateCoordinator):
    """Coordinator for SOPHIA Climate updates"""
    
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, llm_client, history_manager):
        """Initialize coordinator"""
        super().__init__(
            hass,
            _LOGGER,
            name="SOPHIA Climate",
            update_interval=timedelta(minutes=entry.data.get("check_interval", 30))
        )
        self.entry = entry
        self.llm_client = llm_client
        self.history_manager = history_manager
        
        # Parse zones
        zones = entry.data.get("zones", [])
        if zones and isinstance(zones[0], dict):
            # v2 format
            self.zones = [z.get("id") for z in zones]
        else:
            # v1 format
            self.zones = zones
        
    def _get_current_season(self) -> str:
        """Determine current season based on month"""
        month = datetime.now().month
        
        if month in [12, 1, 2]:
            return "winter"
        elif month in [3, 4, 5]:
            return "spring"
        elif month in [6, 7, 8]:
            return "summer"
        else:  # 9, 10, 11
            return "fall"
    
    def _is_sleep_time(self) -> bool:
        """Check if it's sleep time (10 PM - 6 AM)"""
        hour = datetime.now().hour
        return hour >= 22 or hour < 6
    
    def _get_target_temp(self, season: str, is_sleep: bool) -> float:
        """Get target temperature for season and time"""
        seasonal_temps = self.entry.data.get("seasonal_temps", {})
        season_temps = seasonal_temps.get(season, {"target": 72, "sleep": 68})
        
        if is_sleep:
            return season_temps.get("sleep", 68)
        else:
            return season_temps.get("target", 72)
        
    async def _async_update_data(self):
        """Fetch data from API endpoint"""
        
        _LOGGER.info("Running SOPHIA climate check...")
        
        # Determine season and time
        season = self._get_current_season()
        is_sleep = self._is_sleep_time()
        target_temp = self._get_target_temp(season, is_sleep)
        
        # Get current state for all zones
        zones_data = {}
        
        zones_list = self.entry.data.get("zones", [])
        
        for zone in zones_list:
            if isinstance(zone, dict):
                zone_id = zone.get("id")
                thermostat_entity = zone.get("thermostat")
                temp_sensor_entity = zone.get("temp_sensor")
            else:
                # v1 format
                zone_id = zone.lower().replace(" ", "_")
                thermostat_entity = self.entry.data.get(f"{zone_id}_thermostat")
                temp_sensor_entity = None
            
            if not thermostat_entity:
                _LOGGER.warning(f"No thermostat configured for zone: {zone_id}")
                continue
            
            state = self.hass.states.get(thermostat_entity)
            
            if not state:
                _LOGGER.warning(f"Thermostat not found: {thermostat_entity}")
                continue
            
            # Prefer temp sensor reading if available
            if temp_sensor_entity:
                temp_state = self.hass.states.get(temp_sensor_entity)
                indoor_temp = float(temp_state.state) if temp_state else state.attributes.get("current_temperature")
            else:
                indoor_temp = state.attributes.get("current_temperature")
            
            # Get thermostat's ACTUAL current setpoint (what it's set to)
            # This is what we should compare against for efficiency, NOT SOPHIA's config
            thermostat_setpoint = state.attributes.get("temperature")
            
            zones_data[zone_id] = {
                "indoor_temp": indoor_temp,
                "target_temp": thermostat_setpoint,  # Use ACTUAL setpoint, not config
                "sophia_target": target_temp,  # Store SOPHIA's config separately for AI
                "hvac_mode": state.state,
                "hvac_action": state.attributes.get("hvac_action"),
                "thermostat_entity": thermostat_entity
            }
        
        # Get outdoor temperature
        outdoor_sensor = self.entry.data.get("outdoor_temp_sensor", "sensor.outdoor_temperature")
        outdoor_state = self.hass.states.get(outdoor_sensor)
        outdoor_temp = float(outdoor_state.state) if outdoor_state else 50.0

        # Get outdoor humidity (None = unavailable, treated as unknown)
        outdoor_humidity = None
        humidity_sensor = self.entry.data.get("outdoor_humidity_sensor", "")
        if humidity_sensor:
            humidity_state = self.hass.states.get(humidity_sensor)
            if humidity_state and humidity_state.state not in ("unavailable", "unknown", ""):
                try:
                    outdoor_humidity = float(humidity_state.state)
                except ValueError:
                    pass

        # Make AI decision for each zone
        decisions = {}

        for zone_id, zone_data in zones_data.items():
            prompt = self._build_climate_prompt(zone_id, zone_data, outdoor_temp, outdoor_humidity, season, is_sleep)
            
            llm_response = await self.llm_client.generate(
                prompt=prompt,
                module_id=DOMAIN,
                context={"zone": zone_id}
            )
            
            if llm_response:
                decision = self._parse_llm_response(llm_response["response"])
                decision = self._validate_decision(decision, zone_data)
                decisions[zone_id] = decision
                
                # Log decision to history
                await self.history_manager.add_decision({
                    "zone": zone_id,
                    "decision": decision.get("decision"),
                    "reasoning": decision.get("reasoning"),
                    "indoor_temp": zone_data.get("indoor_temp"),
                    "target_temp": zone_data.get("sophia_target"),
                    "outdoor_temp": outdoor_temp,
                    "season": season,
                    "is_sleep_time": is_sleep,
                    "hvac_mode": zone_data.get("hvac_mode"),
                    "hvac_action": zone_data.get("hvac_action"),
                    "temp_delta": round(
                        (zone_data.get("indoor_temp", 0) - zone_data.get("sophia_target", 0)), 
                        1
                    ),
                })
                
                # Execute the AI's decision
                await self._execute_decision(zone_id, zone_data, decision)
            else:
                decisions[zone_id] = {"decision": "NO_CHANGE", "reasoning": "LLM unavailable"}
        
        # Cache statistics for efficiency sensor (fixes async access issue)
        try:
            self._cached_stats = await self.history_manager.get_statistics()
        except Exception as e:
            _LOGGER.warning(f"Failed to cache stats: {e}")
            self._cached_stats = {}
        
        return {
            "zones": zones_data,
            "outdoor_temp": outdoor_temp,
            "season": season,
            "is_sleep_time": is_sleep,
            "decisions": decisions,
            "timestamp": datetime.now().isoformat()
        }
    
    def _build_climate_prompt(self, zone_id: str, zone_data: dict, outdoor_temp: float, outdoor_humidity, season: str, is_sleep: bool) -> str:
        """Build prompt for LLM climate decision"""

        energy_priority = self.entry.data.get("energy_priority", "balanced")
        special_instructions = self.entry.data.get("special_instructions", "")
        aggressive_optimization = self.entry.data.get("aggressive_optimization", False)
        passive_humidity_limit = self.entry.data.get("passive_humidity_limit", 60)
        
        # Define tolerance thresholds based on energy priority and aggressive mode
        if aggressive_optimization:
            tolerance_map = {
                "comfort": 0.5,
                "balanced": 1.0,
                "savings": 2.0
            }
        else:
            tolerance_map = {
                "comfort": 1.0,
                "balanced": 2.0,
                "savings": 3.0
            }
        tolerance = tolerance_map.get(energy_priority, 2.0)
        
        indoor_temp = zone_data['indoor_temp']
        sophia_target = zone_data['sophia_target']
        thermostat_setpoint = zone_data['target_temp']
        temp_delta = indoor_temp - sophia_target  # positive = too warm, negative = too cold
        current_hvac_mode = zone_data['hvac_mode']
        current_hvac_action = zone_data['hvac_action']
        
        # Determine season type for sleep logic
        heating_season = season in ["winter", "fall"]
        cooling_season = season in ["summer", "spring"]
        shoulder_season = 65 <= outdoor_temp <= 75
        
        # Passive cooling is possible when outdoor air can do the job for free.
        # If outdoor temp is below indoor AND below target, opening up/turning off
        # will naturally bring the house down - no AC needed.
        # HOWEVER: high outdoor humidity negates this - bringing in humid air raises
        # indoor moisture, which is bad for equipment and comfort.
        temp_allows_passive = (outdoor_temp < indoor_temp) and (outdoor_temp <= sophia_target + tolerance)
        humidity_blocks_passive = (
            outdoor_humidity is not None and outdoor_humidity > passive_humidity_limit
        )
        passive_cooling_available = temp_allows_passive and not humidity_blocks_passive

        # Build a human-readable note about passive cooling status
        if temp_allows_passive and humidity_blocks_passive:
            passive_note = (
                f"Outdoor temp ({outdoor_temp}F) is cool enough but outdoor humidity "
                f"({outdoor_humidity:.0f}%) exceeds the {passive_humidity_limit}% limit - "
                f"passive cooling suppressed to protect equipment."
            )
        elif temp_allows_passive:
            passive_note = f"Outdoor ({outdoor_temp}F) is cool and humidity is acceptable - passive cooling available."
        else:
            passive_note = f"Outdoor ({outdoor_temp}F) is too warm for passive cooling."
        
        # PRE-COMPUTE the situation so Mistral doesn't have to evaluate conditionals.
        # This is the single source of truth - the LLM just writes the explanation.
        if is_sleep and heating_season and indoor_temp > sophia_target + 1:
            situation = "SLEEP_DRIFT_DOWN"
            recommended = "OFF"
            situation_summary = (
                f"Sleep time in {season}. Indoor ({indoor_temp}F) is above sleep target ({sophia_target}F). "
                f"Turning OFF - temperature will drift down naturally. "
                f"Running AC in {season} to cool would waste energy."
            )
        elif is_sleep and heating_season and indoor_temp < sophia_target:
            situation = "SLEEP_TOO_COLD"
            recommended = f"HEAT_{int(sophia_target)}"
            situation_summary = (
                f"Sleep time in {season}. Indoor ({indoor_temp}F) has dropped below sleep target ({sophia_target}F). "
                f"Heating to prevent it getting too cold overnight."
            )
        elif temp_delta > tolerance:
            # Too warm - need to lower temperature
            situation = "TOO_WARM"
            if current_hvac_mode == "cool" and thermostat_setpoint == sophia_target:
                # Already cooling correctly, just wait
                recommended = "NO_CHANGE"
                situation_summary = (
                    f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F above target ({sophia_target}F). "
                    f"Thermostat is already cooling at {sophia_target}F - system is working, no change needed."
                )
            elif passive_cooling_available:
                # Outdoor conditions allow passive cooling, but SOPHIA cannot enforce it.
                # Cool actively and note passive cooling as an option in reasoning.
                recommended = f"COOL_{int(sophia_target)}"
                situation_summary = (
                    f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F above target ({sophia_target}F). "
                    f"{passive_note} Activating COOL mode to guarantee the target is reached. "
                    f"Note: passive cooling (open windows) is also an option if preferred."
                )
            else:
                # Outdoor is too warm or too humid to passively cool - AC required
                recommended = f"COOL_{int(sophia_target)}"
                situation_summary = (
                    f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F above target ({sophia_target}F). "
                    f"{passive_note} Switching to COOL mode."
                )
        elif temp_delta < -tolerance:
            # Too cold - HEAT is the only correct physical action
            situation = "TOO_COLD"
            if current_hvac_mode == "heat" and thermostat_setpoint == sophia_target:
                recommended = "NO_CHANGE"
                situation_summary = (
                    f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F below target ({sophia_target}F). "
                    f"Thermostat is already heating at {sophia_target}F - system is working, no change needed."
                )
            else:
                recommended = f"HEAT_{int(sophia_target)}"
                situation_summary = (
                    f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F below target ({sophia_target}F). "
                    f"Switching to HEAT mode to raise the temperature."
                )
        elif aggressive_optimization and shoulder_season:
            situation = "SHOULDER_SEASON_OPTIMIZE"
            recommended = "OFF_FAN_ON"
            situation_summary = (
                f"Indoor ({indoor_temp}F) is within {tolerance}F of target ({sophia_target}F). "
                f"Outdoor is {outdoor_temp}F (ideal range). Aggressive mode - using fan circulation instead of HVAC."
            )
        else:
            situation = "COMFORTABLE"
            recommended = "NO_CHANGE"
            situation_summary = (
                f"Indoor ({indoor_temp}F) is within {tolerance}F of target ({sophia_target}F). "
                f"No HVAC action needed."
            )
        
        humidity_display = f"{outdoor_humidity:.0f}%" if outdoor_humidity is not None else "unknown"
        prompt = f"""You are SOPHIA climate AI for the {zone_id} zone.

SITUATION (pre-analyzed by SOPHIA):
- Indoor: {indoor_temp}F | Target: {sophia_target}F | Delta: {temp_delta:+.1f}F
- Outdoor: {outdoor_temp}F | Outdoor Humidity: {humidity_display} | Season: {season} | {"SLEEP TIME" if is_sleep else "Awake"}
- HVAC Mode: {current_hvac_mode} | HVAC Action: {current_hvac_action}
- Thermostat Set To: {thermostat_setpoint}F | Tolerance: +/-{tolerance}F
- Passive cooling available: {passive_cooling_available} ({passive_note})
- Situation: {situation}

SOPHIA's ANALYSIS: {situation_summary}
{f"SPECIAL INSTRUCTIONS: {special_instructions}" if special_instructions else ""}

DECISION: {recommended}

Write a brief reasoning explanation for this decision using ONLY the facts listed above.
Do NOT invent exceptions, special modes, or rules not stated here.

Respond with JSON only:
{{"decision": "{recommended}", "reasoning": "brief explanation based only on the facts above"}}
"""
        return prompt
    
    def _parse_llm_response(self, response: str) -> dict:
        """Parse LLM JSON response"""
        import json
        
        try:
            # Extract JSON from response
            start = response.find('{')
            end = response.rfind('}') + 1
            
            if start != -1 and end > start:
                return json.loads(response[start:end])
            else:
                return {"decision": "NO_CHANGE", "reasoning": "Parse error"}
        except Exception as e:
            _LOGGER.error(f"Error parsing LLM response: {e}")
            return {"decision": "NO_CHANGE", "reasoning": f"Error: {e}"}
    
    def _validate_decision(self, decision: dict, zone_data: dict) -> dict:
        """
        Physics-based safety check. Runs after LLM response, before execution and logging.
        
        HEAT raises temperature. COOL lowers temperature. No exceptions.
        Catches and corrects cases where the LLM confuses HVAC mode with required action.
        """
        decision_str = decision.get("decision", "NO_CHANGE")
        
        # Only HEAT/COOL decisions can be physically wrong
        if decision_str in ("NO_CHANGE", "OFF", "OFF_FAN_ON"):
            return decision
        
        try:
            parts = decision_str.split("_")
            if len(parts) != 2:
                return decision
            
            action = parts[0].upper()
            indoor_temp = zone_data.get("indoor_temp")
            sophia_target = zone_data.get("sophia_target")
            
            if indoor_temp is None or sophia_target is None:
                return decision
            
            temp_delta = indoor_temp - sophia_target  # positive = too warm
            
            energy_priority = self.entry.data.get("energy_priority", "balanced")
            aggressive = self.entry.data.get("aggressive_optimization", False)
            tolerance_map = {"comfort": 0.5 if aggressive else 1.0,
                             "balanced": 1.0 if aggressive else 2.0,
                             "savings": 2.0 if aggressive else 3.0}
            tolerance = tolerance_map.get(energy_priority, 2.0)
            
            # HEAT when already too warm - physically impossible, override
            if action == "HEAT" and temp_delta > 0:
                original = decision_str
                if temp_delta > tolerance:
                    corrected = f"COOL_{int(sophia_target)}"
                    _LOGGER.warning(
                        f"[SOPHIA SAFETY OVERRIDE] {zone_id if (zone_id := zone_data.get('thermostat_entity', 'unknown')) else 'unknown'}: "
                        f"LLM requested {original} but indoor ({indoor_temp}F) is {temp_delta:+.1f}F "
                        f"ABOVE target ({sophia_target}F). HEAT would make it worse. Correcting to {corrected}."
                    )
                    return {
                        "decision": corrected,
                        "reasoning": (
                            f"[Safety override] LLM incorrectly requested {original}. "
                            f"Indoor {indoor_temp}F is {temp_delta:.1f}F above target {sophia_target}F - "
                            f"switched to COOL to actually lower the temperature."
                        )
                    }
                else:
                    # Within tolerance - just don't heat
                    _LOGGER.warning(
                        f"[SOPHIA SAFETY OVERRIDE] LLM requested HEAT but indoor ({indoor_temp}F) "
                        f"is at or above target ({sophia_target}F). Overriding to NO_CHANGE."
                    )
                    return {
                        "decision": "NO_CHANGE",
                        "reasoning": (
                            f"[Safety override] LLM requested HEAT but indoor ({indoor_temp}F) "
                            f"is already at/above target ({sophia_target}F). No action needed."
                        )
                    }
            
            # COOL when already too cold - physically wasteful, override
            if action == "COOL" and temp_delta < 0:
                original = decision_str
                if abs(temp_delta) > tolerance:
                    corrected = f"HEAT_{int(sophia_target)}"
                    _LOGGER.warning(
                        f"[SOPHIA SAFETY OVERRIDE] LLM requested {original} but indoor ({indoor_temp}F) "
                        f"is {temp_delta:+.1f}F BELOW target ({sophia_target}F). COOL would make it worse. "
                        f"Correcting to {corrected}."
                    )
                    return {
                        "decision": corrected,
                        "reasoning": (
                            f"[Safety override] LLM incorrectly requested {original}. "
                            f"Indoor {indoor_temp}F is {abs(temp_delta):.1f}F below target {sophia_target}F - "
                            f"switched to HEAT to actually raise the temperature."
                        )
                    }
                else:
                    _LOGGER.warning(
                        f"[SOPHIA SAFETY OVERRIDE] LLM requested COOL but indoor ({indoor_temp}F) "
                        f"is at or below target ({sophia_target}F). Overriding to NO_CHANGE."
                    )
                    return {
                        "decision": "NO_CHANGE",
                        "reasoning": (
                            f"[Safety override] LLM requested COOL but indoor ({indoor_temp}F) "
                            f"is already at/below target ({sophia_target}F). No action needed."
                        )
                    }
        
        except Exception as e:
            _LOGGER.error(f"Error in _validate_decision: {e}")
        
        return decision
    
    async def _execute_decision(self, zone_id: str, zone_data: dict, decision: dict):
        """Execute the AI's climate decision"""
        
        decision_str = decision.get("decision", "NO_CHANGE")
        reasoning = decision.get("reasoning", "")
        
        # Skip if no change needed
        if decision_str == "NO_CHANGE":
            _LOGGER.debug(f"No action needed for {zone_id}: {reasoning}")
            return
        
        thermostat_entity = zone_data.get("thermostat_entity")
        if not thermostat_entity:
            _LOGGER.error(f"No thermostat entity for zone {zone_id}")
            return
        
        # Check if SOPHIA control is enabled
        control_switch = f"switch.sophia_climate_enabled"
        if not self.hass.states.is_state(control_switch, "on"):
            _LOGGER.info(f"SOPHIA control disabled, skipping execution")
            return
        
        # Handle OFF mode (no temperature needed)
        if decision_str == "OFF":
            _LOGGER.info(
                f"SOPHIA executing decision for {zone_id}: "
                f"Turn OFF HVAC completely - {reasoning}"
            )
            
            try:
                # Turn off HVAC
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {
                        "entity_id": thermostat_entity,
                        "hvac_mode": "off",
                    },
                    blocking=True
                )
                
                # Turn off fan
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {
                        "entity_id": thermostat_entity,
                        "fan_mode": "off",
                    },
                    blocking=True
                )
                
                _LOGGER.info(f"Successfully turned off {thermostat_entity} (HVAC + fan)")
                return
                
            except Exception as e:
                _LOGGER.error(f"Error turning off HVAC for {zone_id}: {e}")
                import traceback
                _LOGGER.error(traceback.format_exc())
                return
        
        # Handle OFF_FAN_ON mode (HVAC off but fan circulates)
        if decision_str == "OFF_FAN_ON":
            _LOGGER.info(
                f"SOPHIA executing decision for {zone_id}: "
                f"Turn OFF HVAC but run fan for circulation - {reasoning}"
            )
            
            try:
                # Turn off HVAC
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {
                        "entity_id": thermostat_entity,
                        "hvac_mode": "off",
                    },
                    blocking=True
                )
                
                # Turn on fan for circulation
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {
                        "entity_id": thermostat_entity,
                        "fan_mode": "on",
                    },
                    blocking=True
                )
                
                _LOGGER.info(f"Successfully set {thermostat_entity} to circulation mode (HVAC off, fan on)")
                return
                
            except Exception as e:
                _LOGGER.error(f"Error setting circulation mode for {zone_id}: {e}")
                import traceback
                _LOGGER.error(traceback.format_exc())
                return
        
        # Parse decision: "HEAT_72" or "COOL_70"
        try:
            parts = decision_str.split("_")
            if len(parts) != 2:
                _LOGGER.error(f"Invalid decision format: {decision_str}")
                return
            
            action = parts[0].lower()  # "heat" or "cool"
            target_temp = int(parts[1])  # 72 or 70
            
            # Execute the decision
            _LOGGER.info(
                f"SOPHIA executing decision for {zone_id}: "
                f"{action.upper()} to {target_temp} degrees F - {reasoning}"
            )
            
            # Call climate service to set temperature and mode
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": thermostat_entity,
                    "temperature": target_temp,
                    "hvac_mode": action,  # "heat" or "cool"
                },
                blocking=True
            )
            
            # Set fan to auto (off) when heating/cooling
            await self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {
                    "entity_id": thermostat_entity,
                    "fan_mode": "off",  # Auto mode - runs with HVAC
                },
                blocking=True
            )
            
            _LOGGER.info(f"Successfully set {thermostat_entity} to {action} at {target_temp} degrees F with auto fan")
            
        except Exception as e:
            _LOGGER.error(f"Error executing decision for {zone_id}: {e}")
            import traceback
            _LOGGER.error(traceback.format_exc())