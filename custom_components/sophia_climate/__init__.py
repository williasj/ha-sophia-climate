# -*- coding: utf-8 -*-
"""
SOPHIA Climate - Multi-zone HVAC control with AI decision making
Registers with SOPHIA Core for LLM access and capability discovery
"""
import copy
import logging
from collections import deque
from datetime import timedelta, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    DEFAULT_RAG_ENABLED,
    DEFAULT_RAG_RETENTION_DAYS,
    DEFAULT_RAG_MEMORY_ENTRIES,
    RAG_COLLECTION_DECISIONS,
    RAG_PURGE_INTERVAL_HOURS,
    SEASON_SUMMER,
    SEASON_WINTER,
    REGIME_COOLING,
    REGIME_HEATING,
    REGIME_NEUTRAL,
    REGIME_LOOKBACK_HOURS,
    REGIME_MAX_SAMPLES,
    REGIME_COOLING_RECENT_HIGH,
    REGIME_HEATING_RECENT_HIGH,
    REGIME_OUTDOOR_FLOOR,
)
from .history_manager import ClimateHistoryManager

# Entity states that carry no usable number.
UNAVAILABLE_STATES = ("unavailable", "unknown", "none", "")

# How old a remembered outdoor reading may be and still stand in for a dead
# outdoor sensor. Older than this, SOPHIA leaves the thermostats alone.
OUTDOOR_LAST_KNOWN_MAX_HOURS = 3

# A configured thermostat that is not loaded yet (typically the cloud
# integration still starting after a Home Assistant restart) triggers a quick
# retry instead of waiting for the next regular check.
MISSING_THERMOSTAT_RETRY_SECONDS = 60
MISSING_THERMOSTAT_MAX_RETRIES = 10

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


DEFAULT_SEASONAL_TEMPS = {
    "winter": {"target": 72, "sleep": 68},
    "spring": {"target": 72, "sleep": 68},
    "summer": {"target": 70, "sleep": 66},
    "fall": {"target": 72, "sleep": 68},
}


def _ensure_zone_settings(data: dict):
    """Seed per-zone comfort settings from legacy global values.

    Each zone owns its own seasonal_temps, energy_priority,
    aggressive_optimization, and special_instructions. Older entries kept
    these at the top level shared across all zones; this migrates them into
    each zone the first time, using the global value as the seed so behavior
    is unchanged until the user edits a specific zone.

    Returns (new_data, changed).
    """
    changed = False
    global_seasonal = data.get("seasonal_temps", DEFAULT_SEASONAL_TEMPS)
    global_priority = data.get("energy_priority", "balanced")
    global_aggressive = data.get("aggressive_optimization", False)
    global_special = data.get("special_instructions", "")

    new_zones = []
    for zone in data.get("zones", []):
        if not isinstance(zone, dict):
            new_zones.append(zone)
            continue
        z = dict(zone)
        if "seasonal_temps" not in z:
            z["seasonal_temps"] = copy.deepcopy(global_seasonal)
            changed = True
        if "energy_priority" not in z:
            z["energy_priority"] = global_priority
            changed = True
        if "aggressive_optimization" not in z:
            z["aggressive_optimization"] = global_aggressive
            changed = True
        if "special_instructions" not in z:
            z["special_instructions"] = global_special
            changed = True
        new_zones.append(z)

    if changed:
        data = dict(data)
        data["zones"] = new_zones
    return data, changed


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

    # Ensure each zone carries its own comfort settings (seeded from any
    # legacy global values). Persist once if anything was migrated.
    _migrated_data, _zone_changed = _ensure_zone_settings(entry.data)
    if _zone_changed:
        hass.config_entries.async_update_entry(entry, data=_migrated_data)
        _LOGGER.info("SOPHIA Climate: migrated global comfort settings into per-zone config")
    
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
    entry.async_on_unload(coordinator.cancel_missing_retry)
    
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

    def _zone_id_name(zone):
        """Return (zone_id, zone_name, thermostat) for a zone (dict or v1 string)."""
        if isinstance(zone, dict):
            zid = zone.get("id")
            zname = zone.get("name") or (zid.title() if zid else "Zone")
            thermostat = zone.get("thermostat", "")
        else:
            zid = zone.lower().replace(" ", "_")
            zname = zone.title()
            thermostat = ""
        return zid, zname, thermostat

    def _build_zone_graph_cards(zones, outdoor):
        """One temperature graph per zone so each zone is tracked separately."""
        cards = []
        for zone in zones:
            zid, zname, thermostat = _zone_id_name(zone)
            if not thermostat:
                continue
            cards.append({
                "type": "history-graph",
                "title": f"{zname} - Temperature (24h)",
                "entities": [
                    {"entity": thermostat, "name": zname},
                    {"entity": outdoor, "name": "Outdoor"},
                ],
                "hours_to_show": 24,
            })
        return cards

    def _build_zone_decision_cards(zones):
        """Per-zone card showing that zone's own SOPHIA decision and temps."""
        cards = []
        for zone in zones:
            zid, zname, _ = _zone_id_name(zone)
            status = f"sensor.sophia_{zid}_zone_status"
            cards.append({
                "type": "entities",
                "title": f"{zname} - SOPHIA Decision",
                "state_color": False,
                "entities": [
                    {"entity": status, "name": "HVAC Action"},
                    {"type": "attribute", "entity": status, "attribute": "indoor_temp", "name": "Indoor Temp"},
                    {"type": "attribute", "entity": status, "attribute": "target_temp", "name": "Thermostat Setpoint"},
                    {"type": "attribute", "entity": status, "attribute": "priority", "name": "Priority"},
                    {"type": "attribute", "entity": status, "attribute": "last_decision", "name": "Last Decision"},
                    {"type": "attribute", "entity": status, "attribute": "last_reasoning", "name": "Reasoning"},
                ],
            })
        return cards

    _zone_graph_cards = _build_zone_graph_cards(_zones_for_dash, _outdoor_sensor)
    _zone_decision_cards = _build_zone_decision_cards(_zones_for_dash)

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
                
                # Per-Zone Temperature History Graphs (each zone tracked separately)
                *_zone_graph_cards,
                
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

                # Per-Zone SOPHIA Decisions (each zone decided independently)
                *_zone_decision_cards,
                
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
        """Handle manual climate check service call.

        Bypasses the DataUpdateCoordinator debouncer to prevent the
        double-fire that occurs when async_request_refresh() triggers
        an immediate run and then re-queues after cooldown.
        """
        if coordinator._check_in_progress:
            _LOGGER.info("Climate check already in progress, skipping manual trigger")
            return
        _LOGGER.info("Manual climate check triggered")
        coordinator._check_in_progress = True
        try:
            data = await coordinator._run_climate_check()
            coordinator.async_set_updated_data(data)
        finally:
            coordinator._check_in_progress = False
    
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
        self._check_in_progress = False

        # Rolling outdoor temperature samples, used to decide whether we are
        # in heating or cooling weather. Seeded from the decision history on
        # the first check after a restart so a fresh boot is not blind.
        self._outdoor_samples = deque(maxlen=REGIME_MAX_SAMPLES)
        self._trend_seeded = False
        self._outdoor_source_reported = None
        self._cached_stats = {}
        self._missing_retry_unsub = None
        self._missing_retries = 0

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
        """Get target temperature for season and time (global fallback)"""
        seasonal_temps = self.entry.data.get("seasonal_temps", {})
        season_temps = seasonal_temps.get(season, {"target": 72, "sleep": 68})
        
        if is_sleep:
            return season_temps.get("sleep", 68)
        else:
            return season_temps.get("target", 72)

    def _get_zone_target_temp(self, zone, season: str, is_sleep: bool) -> float:
        """Get this zone's own target temperature for the season and time.

        Falls back to the legacy global seasonal_temps only if the zone has
        no seasonal_temps of its own (pre-migration entries).
        """
        seasonal = None
        if isinstance(zone, dict):
            seasonal = zone.get("seasonal_temps")
        if not seasonal:
            seasonal = self.entry.data.get("seasonal_temps", {})
        season_temps = seasonal.get(season, {"target": 72, "sleep": 68})
        if is_sleep:
            return season_temps.get("sleep", 68)
        return season_temps.get("target", 72)

    async def _seed_outdoor_trend(self) -> None:
        """Seed the outdoor trend from decision history after a restart.

        Without this, the first checks after a Home Assistant restart have no
        idea what the weather has been doing and would fall back to the
        calendar season, which is exactly the mistake we are trying to avoid
        in shoulder months.
        """
        if self._trend_seeded:
            return
        self._trend_seeded = True
        try:
            history = await self.history_manager.get_full_history(days=2)
        except Exception as err:
            _LOGGER.debug("Could not seed outdoor trend from history: %s", err)
            return

        seeded = 0
        for entry in reversed(history):
            outdoor = entry.get("outdoor_temp")
            stamp = entry.get("timestamp")
            if outdoor is None or not stamp:
                continue
            try:
                when = datetime.fromisoformat(stamp)
                self._outdoor_samples.append((when, float(outdoor)))
                seeded += 1
            except (ValueError, TypeError):
                continue

        if seeded:
            _LOGGER.info(
                "SOPHIA Climate: seeded outdoor trend with %d historical samples", seeded
            )

    def _record_outdoor(self, outdoor_temp: float) -> None:
        """Record an outdoor temperature sample for regime detection."""
        try:
            self._outdoor_samples.append((datetime.now(), float(outdoor_temp)))
        except (ValueError, TypeError):
            pass

    def _recent_outdoor_high(self, hours: int = REGIME_LOOKBACK_HOURS):
        """Highest outdoor temperature seen in the lookback window, or None."""
        cutoff = datetime.now() - timedelta(hours=hours)
        recent = [temp for when, temp in self._outdoor_samples if when >= cutoff]
        if not recent:
            return None
        return max(recent)

    # ------------------------------------------------------------------
    # Sensor reading helpers. None of these raise on unavailable/unknown
    # states: a dead sensor must never take the whole climate check down.
    # ------------------------------------------------------------------

    def _state_float(self, entity_id):
        """Return an entity's numeric state, or None when it has no number."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or str(state.state).lower() in UNAVAILABLE_STATES:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _weather_fallback(self):
        """First working weather entity as (temp_f, humidity, entity_id).

        An "outdoor_weather_entity" in the config entry is tried first, then
        every other weather entity in id order. Returns (None, None, None)
        when none has a temperature.
        """
        preferred = self.entry.data.get("outdoor_weather_entity")
        states = list(self.hass.states.async_all("weather"))
        states.sort(key=lambda st: (st.entity_id != preferred, st.entity_id))
        for state in states:
            if str(state.state).lower() in UNAVAILABLE_STATES:
                continue
            try:
                temp = float(state.attributes.get("temperature"))
            except (TypeError, ValueError):
                continue
            unit = str(state.attributes.get("temperature_unit") or "")
            if unit.upper().endswith("C"):
                temp = round(temp * 9.0 / 5.0 + 32.0, 1)
            humidity = state.attributes.get("humidity")
            try:
                humidity = float(humidity) if humidity is not None else None
            except (TypeError, ValueError):
                humidity = None
            return temp, humidity, state.entity_id
        return None, None, None

    def _last_known_outdoor(self):
        """Most recent recorded outdoor sample if it is fresh enough."""
        if not self._outdoor_samples:
            return None, None
        when, temp = self._outdoor_samples[-1]
        age = datetime.now() - when
        if age > timedelta(hours=OUTDOOR_LAST_KNOWN_MAX_HOURS):
            return None, None
        return temp, age

    def _resolve_outdoor(self):
        """Outdoor conditions as (temp_f, humidity, source, live).

        Order: configured sensors, then any working weather entity, then a
        recent remembered reading. temp_f is None when nothing is usable.
        live is False for a remembered reading (it is not re-recorded).
        """
        sensor = self.entry.data.get("outdoor_temp_sensor", "sensor.outdoor_temperature")
        humidity_sensor = self.entry.data.get("outdoor_humidity_sensor", "")
        temp = self._state_float(sensor)
        humidity = self._state_float(humidity_sensor)
        source = sensor
        live = True

        if temp is None or humidity is None:
            w_temp, w_humidity, w_entity = self._weather_fallback()
            if temp is None and w_temp is not None:
                temp, source = w_temp, w_entity
            if humidity is None and w_humidity is not None:
                humidity = w_humidity

        if temp is None:
            last, age = self._last_known_outdoor()
            if last is not None:
                temp = last
                live = False
                source = f"last known reading ({int(age.total_seconds() // 60)} min old)"
            else:
                source = None
        return temp, humidity, source, live

    @callback
    def cancel_missing_retry(self) -> None:
        """Cancel a pending missing-thermostat retry (used on unload)."""
        if self._missing_retry_unsub is not None:
            self._missing_retry_unsub()
            self._missing_retry_unsub = None

    def _schedule_missing_retry(self, missing: list) -> None:
        """Re-run the check soon when configured thermostats are not loaded."""
        if not missing:
            self._missing_retries = 0
            self.cancel_missing_retry()
            return
        if self._missing_retry_unsub is not None:
            return
        if self._missing_retries >= MISSING_THERMOSTAT_MAX_RETRIES:
            if self._missing_retries == MISSING_THERMOSTAT_MAX_RETRIES:
                _LOGGER.warning(
                    "SOPHIA Climate: thermostat(s) %s still not available after %d "
                    "retries; waiting for the regular check",
                    ", ".join(missing), MISSING_THERMOSTAT_MAX_RETRIES,
                )
                self._missing_retries += 1
            return
        self._missing_retries += 1

        @callback
        def _retry(_now) -> None:
            self._missing_retry_unsub = None
            self.hass.async_create_task(self.async_refresh())

        self._missing_retry_unsub = async_call_later(
            self.hass, MISSING_THERMOSTAT_RETRY_SECONDS, _retry
        )

    async def _set_fan_mode_best_effort(self, thermostat_entity: str, fan_mode: str) -> bool:
        """Set the fan mode without letting a refusal fail the HVAC change.

        Skips the call when the fan is already in that mode or the thermostat
        does not offer it. Some thermostats (Nest) refuse any fan command
        while HVAC is off; that is logged as a warning, not an error.
        """
        state = self.hass.states.get(thermostat_entity)
        attrs = state.attributes if state else {}
        if attrs.get("fan_mode") == fan_mode:
            return True
        fan_modes = attrs.get("fan_modes")
        if fan_modes is not None and fan_mode not in fan_modes:
            _LOGGER.debug(
                "%s does not offer fan mode %s (offers %s); skipping",
                thermostat_entity, fan_mode, fan_modes,
            )
            return False
        try:
            await self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {"entity_id": thermostat_entity, "fan_mode": fan_mode},
                blocking=True,
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "%s refused fan mode %s: %s", thermostat_entity, fan_mode, err
            )
            return False

    def _report_outdoor_source(self, source) -> None:
        """Log when the outdoor source changes, not on every check."""
        configured = self.entry.data.get("outdoor_temp_sensor", "sensor.outdoor_temperature")
        key = source or "none"
        if key == self._outdoor_source_reported:
            return
        self._outdoor_source_reported = key
        if source == configured:
            _LOGGER.info("SOPHIA Climate: outdoor temperature from %s", source)
        elif source:
            _LOGGER.warning(
                "SOPHIA Climate: outdoor sensor %s has no reading; using %s instead",
                configured, source,
            )
        else:
            _LOGGER.warning(
                "SOPHIA Climate: no outdoor temperature from %s, any weather entity, "
                "or a reading in the last %d hours; thermostats are left unchanged "
                "until one is available",
                configured, OUTDOOR_LAST_KNOWN_MAX_HOURS,
            )

    def _determine_regime(self, season: str, outdoor_temp: float) -> str:
        """Decide which direction of conditioning is physically sensible now.

        This exists because a target that steps UP (for example the 6 AM move
        from the sleep target to the daytime target) makes the house read as
        "too cold" even in the middle of August. Without a regime lock, the
        only correct-looking action is HEAT, and the furnace fires to close a
        gap that the sun would have closed for free.

        Calendar season alone is not enough - a September morning in the
        Southeast is still full cooling weather but classifies as fall. The
        recent outdoor high is the honest signal.
        """
        cooling_high = self.entry.data.get(
            "regime_cooling_recent_high", REGIME_COOLING_RECENT_HIGH
        )
        heating_high = self.entry.data.get(
            "regime_heating_recent_high", REGIME_HEATING_RECENT_HIGH
        )
        outdoor_floor = self.entry.data.get(
            "regime_outdoor_floor", REGIME_OUTDOOR_FLOOR
        )

        recent_high = self._recent_outdoor_high()

        if recent_high is None:
            # No trend data at all - fall back to the calendar, which is still
            # right at the extremes even if it is wrong in the shoulders.
            if season == SEASON_SUMMER:
                return REGIME_COOLING
            if season == SEASON_WINTER:
                return REGIME_HEATING
            return REGIME_NEUTRAL

        if recent_high >= cooling_high and (
            season == SEASON_SUMMER or outdoor_temp >= outdoor_floor
        ):
            return REGIME_COOLING

        if recent_high <= heating_high:
            return REGIME_HEATING

        return REGIME_NEUTRAL

    async def _async_update_data(self):
        """Fetch data from API endpoint"""
        if self._check_in_progress:
            _LOGGER.debug("Climate check already in progress, skipping duplicate trigger")
            return self.data

        self._check_in_progress = True
        try:
            return await self._run_climate_check()
        finally:
            self._check_in_progress = False

    async def _run_climate_check(self):
        """Execute the climate check logic."""
        _LOGGER.info("Running SOPHIA climate check...")
        
        # Determine season and time
        season = self._get_current_season()
        is_sleep = self._is_sleep_time()
        target_temp = self._get_target_temp(season, is_sleep)
        
        # Get current state for all zones
        zones_data = {}
        missing_thermostats = []
        previous_zones = (self.data or {}).get("zones", {}) if isinstance(self.data, dict) else {}
        carried_zones = {}
        starting = self.hass.state is not CoreState.running
        
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
                missing_thermostats.append(thermostat_entity)
                if starting:
                    _LOGGER.info(
                        "Waiting for thermostat %s to load (zone %s)", thermostat_entity, zone_id
                    )
                else:
                    _LOGGER.warning(f"Thermostat not found: {thermostat_entity}")
                # Keep showing the last known readings; no decision is made.
                if zone_id in previous_zones:
                    carried_zones[zone_id] = dict(previous_zones[zone_id], stale=True)
                continue

            if str(state.state).lower() in UNAVAILABLE_STATES:
                _LOGGER.warning(
                    "Thermostat %s for zone %s is %s; skipping zone this cycle",
                    thermostat_entity, zone_id, state.state,
                )
                continue

            # Prefer the zone temp sensor; fall back to the thermostat's own
            # reading when the sensor is missing or has no number.
            indoor_temp = self._state_float(temp_sensor_entity)
            if indoor_temp is None:
                try:
                    current = state.attributes.get("current_temperature")
                    indoor_temp = float(current) if current is not None else None
                except (TypeError, ValueError):
                    indoor_temp = None
            if indoor_temp is None:
                _LOGGER.warning(
                    "No indoor temperature for zone %s (sensor %s, thermostat %s); "
                    "skipping zone this cycle",
                    zone_id, temp_sensor_entity, thermostat_entity,
                )
                continue
            
            # Get thermostat's ACTUAL current setpoint (what it's set to)
            # This is what we should compare against for efficiency, NOT SOPHIA's config
            thermostat_setpoint = state.attributes.get("temperature")

            # Each zone uses ITS OWN comfort target and prompt settings.
            zone_target = self._get_zone_target_temp(zone, season, is_sleep)
            if isinstance(zone, dict):
                zone_energy_priority = zone.get("energy_priority", "balanced")
                zone_aggressive = zone.get("aggressive_optimization", False)
                zone_special = zone.get("special_instructions", "")
                zone_priority = zone.get("priority", 5)
            else:
                zone_energy_priority = self.entry.data.get("energy_priority", "balanced")
                zone_aggressive = self.entry.data.get("aggressive_optimization", False)
                zone_special = self.entry.data.get("special_instructions", "")
                zone_priority = 5

            zones_data[zone_id] = {
                "indoor_temp": indoor_temp,
                "target_temp": thermostat_setpoint,  # Use ACTUAL setpoint, not config
                "sophia_target": zone_target,  # This zone's own target for the AI
                "hvac_mode": state.state,
                "hvac_action": state.attributes.get("hvac_action"),
                "thermostat_entity": thermostat_entity,
                "temp_sensor": temp_sensor_entity,
                "priority": zone_priority,
                "energy_priority": zone_energy_priority,
                "aggressive_optimization": zone_aggressive,
                "special_instructions": zone_special,
            }
        
        # Outdoor conditions: configured sensors, then any working weather
        # entity, then a recent remembered reading. With none of those the
        # check still completes but leaves every thermostat alone: guessing an
        # outdoor temperature could pick the wrong heating/cooling regime.
        await self._seed_outdoor_trend()
        outdoor_temp, outdoor_humidity, outdoor_source, outdoor_live = self._resolve_outdoor()
        self._report_outdoor_source(outdoor_source)

        skip_reason = None
        if outdoor_temp is None:
            skip_reason = "No outdoor temperature available - thermostat left unchanged"
            regime = None
        else:
            if outdoor_live:
                self._record_outdoor(outdoor_temp)
            # Determine which direction of conditioning is sensible right now.
            # This gates HEAT/COOL below so a target step-up in cooling weather
            # can never fire the furnace.
            regime = self._determine_regime(season, outdoor_temp)
        recent_high = self._recent_outdoor_high()
        _LOGGER.debug(
            "SOPHIA Climate regime=%s (season=%s, outdoor=%sF from %s, recent 24h high=%s)",
            regime, season, outdoor_temp, outdoor_source, recent_high,
        )

        # Make AI decision for each zone
        decisions = {}

        for zone_id, zone_data in zones_data.items():
            zone_data["hvac_regime"] = regime
            if skip_reason:
                decisions[zone_id] = {"decision": "NO_CHANGE", "reasoning": skip_reason}
                continue
            prompt = self._build_climate_prompt(
                zone_id, zone_data, outdoor_temp, outdoor_humidity, season, is_sleep, regime
            )
            
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
                    "hvac_regime": regime,
                    "recent_outdoor_high": recent_high,
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
        
        self._schedule_missing_retry(missing_thermostats)

        return {
            "zones": {**carried_zones, **zones_data},
            "missing_thermostats": missing_thermostats,
            "outdoor_temp": outdoor_temp,
            "outdoor_humidity": outdoor_humidity,
            "outdoor_source": outdoor_source,
            "season": season,
            "hvac_regime": regime,
            "recent_outdoor_high": recent_high,
            "is_sleep_time": is_sleep,
            "decisions": decisions,
            "timestamp": datetime.now().isoformat()
        }
    
    def _build_climate_prompt(self, zone_id: str, zone_data: dict, outdoor_temp: float, outdoor_humidity, season: str, is_sleep: bool, regime: str = REGIME_NEUTRAL) -> str:
        """Build prompt for LLM climate decision"""

        energy_priority = zone_data.get("energy_priority", self.entry.data.get("energy_priority", "balanced"))
        special_instructions = zone_data.get("special_instructions", self.entry.data.get("special_instructions", ""))
        aggressive_optimization = zone_data.get("aggressive_optimization", self.entry.data.get("aggressive_optimization", False))
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
        
        # Which directions of conditioning are permitted right now.
        # Derived from actual weather, not the calendar - a September morning
        # in the Southeast is fall by the calendar and cooling weather in fact.
        heat_allowed = regime != REGIME_COOLING
        cool_allowed = regime != REGIME_HEATING
        heating_regime = regime == REGIME_HEATING
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
        if is_sleep and heating_regime and indoor_temp > sophia_target + 1:
            situation = "SLEEP_DRIFT_DOWN"
            recommended = "OFF"
            situation_summary = (
                f"Sleep time in heating weather ({season}). Indoor ({indoor_temp}F) is above sleep target ({sophia_target}F). "
                f"Turning OFF - temperature will drift down naturally. "
                f"Running AC in heating weather to cool would waste energy."
            )
        elif is_sleep and heating_regime and indoor_temp < sophia_target:
            situation = "SLEEP_TOO_COLD"
            recommended = f"HEAT_{int(sophia_target)}"
            situation_summary = (
                f"Sleep time in heating weather ({season}). Indoor ({indoor_temp}F) has dropped below sleep target ({sophia_target}F). "
                f"Heating to prevent it getting too cold overnight."
            )
        elif temp_delta > tolerance and not cool_allowed:
            # Warm side of target, but we are in heating weather. Running the
            # AC to shave a couple of degrees off in winter wastes energy -
            # turn off and let the house drift down on its own.
            situation = "HEATING_WEATHER_ABOVE_TARGET"
            recommended = "OFF"
            situation_summary = (
                f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F above target ({sophia_target}F), "
                f"but the recent outdoor high says this is heating weather. "
                f"Turning OFF so the temperature drifts down naturally instead of running the AC."
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
        elif temp_delta < -tolerance and not heat_allowed:
            # Below target in cooling weather. This is the 6 AM case: the
            # target steps up from the sleep value to the daytime value and
            # the house suddenly reads "too cold" in August. The house will
            # warm itself for free - the only thing to do is stop cooling to
            # the old, lower setpoint. Never fire the furnace here.
            situation = "COOLING_WEATHER_BELOW_TARGET"
            if current_hvac_mode == "cool" and thermostat_setpoint == sophia_target:
                recommended = "NO_CHANGE"
                situation_summary = (
                    f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F below target ({sophia_target}F) in cooling weather. "
                    f"Thermostat is already parked at {sophia_target}F and idle - the house will warm up on its own. "
                    f"Heating is not appropriate in cooling weather."
                )
            else:
                recommended = f"COOL_{int(sophia_target)}"
                situation_summary = (
                    f"Indoor ({indoor_temp}F) is {temp_delta:+.1f}F below target ({sophia_target}F) in cooling weather. "
                    f"This is a target change, not a cold house. Parking the thermostat in COOL at {sophia_target}F "
                    f"so it stops cooling to the old setpoint and idles while the house drifts up naturally. "
                    f"Heating is not appropriate in cooling weather."
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
- HVAC regime: {regime} (heating allowed: {heat_allowed}, cooling allowed: {cool_allowed})
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
            
            energy_priority = zone_data.get("energy_priority", self.entry.data.get("energy_priority", "balanced"))
            aggressive = zone_data.get(
                "aggressive_optimization",
                self.entry.data.get("aggressive_optimization", False),
            )
            tolerance_map = {"comfort": 0.5 if aggressive else 1.0,
                             "balanced": 1.0 if aggressive else 2.0,
                             "savings": 2.0 if aggressive else 3.0}
            tolerance = tolerance_map.get(energy_priority, 2.0)

            regime = zone_data.get("hvac_regime", REGIME_NEUTRAL)
            thermostat = zone_data.get("thermostat_entity", "unknown")

            # REGIME LOCKOUT - runs before the physics checks below.
            # In cooling weather the furnace never runs to close a comfort
            # gap, no matter what the LLM asks for. Being below target in
            # August means the target moved, not that the house is cold.
            if action == "HEAT" and regime == REGIME_COOLING:
                _LOGGER.warning(
                    "[SOPHIA REGIME LOCKOUT] %s: LLM requested %s but recent outdoor "
                    "highs put us in cooling weather. Heating is locked out. "
                    "Indoor %sF, target %sF.",
                    thermostat, decision_str, indoor_temp, sophia_target,
                )
                return {
                    "decision": f"COOL_{int(sophia_target)}",
                    "reasoning": (
                        f"[Regime lockout] Heating is not appropriate in cooling weather. "
                        f"Indoor {indoor_temp}F is below target {sophia_target}F because the target "
                        f"changed, not because the house is cold. Parking the thermostat in COOL at "
                        f"{sophia_target}F so it idles and the house warms up on its own."
                    )
                }

            if action == "COOL" and regime == REGIME_HEATING:
                _LOGGER.warning(
                    "[SOPHIA REGIME LOCKOUT] %s: LLM requested %s but recent outdoor "
                    "highs put us in heating weather. Cooling is locked out. "
                    "Indoor %sF, target %sF.",
                    thermostat, decision_str, indoor_temp, sophia_target,
                )
                return {
                    "decision": "OFF",
                    "reasoning": (
                        f"[Regime lockout] Running the AC is not appropriate in heating weather. "
                        f"Indoor {indoor_temp}F versus target {sophia_target}F - turning OFF so the "
                        f"temperature drifts down naturally instead."
                    )
                }

            # HEAT when already too warm - physically impossible, override
            if action == "HEAT" and temp_delta > 0:
                original = decision_str
                if temp_delta > tolerance:
                    corrected = f"COOL_{int(sophia_target)}"
                    _LOGGER.warning(
                        f"[SOPHIA SAFETY OVERRIDE] {thermostat}: "
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
            
            # COOL when already too cold - physically wasteful, override.
            # In cooling weather this is legitimate: parking the thermostat in
            # COOL at the new target is how we stop it cooling to the old,
            # lower setpoint. Only flip to HEAT when heating is permitted.
            if action == "COOL" and temp_delta < 0:
                original = decision_str
                if abs(temp_delta) > tolerance and regime == REGIME_COOLING:
                    already_parked = (
                        zone_data.get("target_temp") == sophia_target
                        and zone_data.get("hvac_mode") == "cool"
                    )
                    if already_parked:
                        _LOGGER.debug(
                            "Thermostat already parked at %sF in cool - no change needed",
                            sophia_target,
                        )
                        return {
                            "decision": "NO_CHANGE",
                            "reasoning": (
                                f"Indoor {indoor_temp}F is below target {sophia_target}F in cooling "
                                f"weather. Thermostat is already parked at {sophia_target}F and idle - "
                                f"the house will warm up on its own."
                            )
                        }
                    return {
                        "decision": f"COOL_{int(sophia_target)}",
                        "reasoning": (
                            f"Indoor {indoor_temp}F is below target {sophia_target}F in cooling weather. "
                            f"Parking the thermostat in COOL at {sophia_target}F so it idles until the "
                            f"house drifts up. Heating is not appropriate in cooling weather."
                        )
                    }
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
                
                # Turn off fan (skipped when it already stopped with the HVAC)
                fan_ok = await self._set_fan_mode_best_effort(thermostat_entity, "off")

                _LOGGER.info(
                    "Successfully turned off %s (HVAC%s)",
                    thermostat_entity, " + fan" if fan_ok else "; fan left as is",
                )
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
                
                # Turn on fan for circulation. Some thermostats (Nest) do not
                # allow the fan to run while HVAC is off; HVAC stays off then.
                if await self._set_fan_mode_best_effort(thermostat_entity, "on"):
                    _LOGGER.info(
                        "Successfully set %s to circulation mode (HVAC off, fan on)",
                        thermostat_entity,
                    )
                else:
                    _LOGGER.info(
                        "Set %s HVAC off; the thermostat would not run the fan "
                        "for circulation",
                        thermostat_entity,
                    )
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
            await self._set_fan_mode_best_effort(thermostat_entity, "off")
            
            _LOGGER.info(f"Successfully set {thermostat_entity} to {action} at {target_temp} degrees F with auto fan")
            
        except Exception as e:
            _LOGGER.error(f"Error executing decision for {zone_id}: {e}")
            import traceback
            _LOGGER.error(traceback.format_exc())