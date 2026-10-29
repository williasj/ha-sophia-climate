# -*- coding: utf-8 -*-
"""Constants for SOPHIA Climate"""

DOMAIN = "sophia_climate"

# Version
VERSION = "2.0.0"

# Default configuration
DEFAULT_CHECK_INTERVAL = 30  # minutes
DEFAULT_TARGET_TEMP = 72  # Fahrenheit
DEFAULT_SLEEP_TEMP = 68  # Fahrenheit

# Climate modes
MODE_HEAT = "heat"
MODE_COOL = "cool"
MODE_OFF = "off"
MODE_AUTO = "auto"

# Seasons
SEASON_WINTER = "winter"
SEASON_SPRING = "spring"
SEASON_SUMMER = "summer"
SEASON_FALL = "fall"

# Energy priorities
ENERGY_COMFORT = "comfort"
ENERGY_BALANCED = "balanced"
ENERGY_SAVINGS = "savings"

# Time constants
SLEEP_START_HOUR = 22  # 10 PM
SLEEP_END_HOUR = 6     # 6 AM

# Zone defaults
DEFAULT_ZONE_PRIORITY = 5
MIN_ZONE_PRIORITY = 1
MAX_ZONE_PRIORITY = 10

# Temperature limits
MIN_TEMP = 60
MAX_TEMP = 80

# HVAC regime detection
# The calendar alone is a bad guide to whether heating is ever appropriate.
# SOPHIA looks at the recent outdoor high to decide which direction of
# conditioning is physically sensible right now.
REGIME_COOLING = "cooling"   # heating is locked out
REGIME_HEATING = "heating"   # cooling is locked out
REGIME_NEUTRAL = "neutral"   # shoulder conditions, both allowed

REGIME_LOOKBACK_HOURS = 24
REGIME_MAX_SAMPLES = 300

# If the outdoor high in the lookback window reached this, we are in
# cooling weather and the furnace must not run to close a comfort gap.
REGIME_COOLING_RECENT_HIGH = 75

# If the outdoor high in the lookback window never got above this, we are
# in heating weather and the AC must not run to close a comfort gap.
REGIME_HEATING_RECENT_HIGH = 60

# Outside the summer months, a warm recent high only implies cooling regime
# if it is not actually cold outside at this moment. Protects the classic
# fall pattern of a 78F afternoon followed by a 38F night.
REGIME_OUTDOOR_FLOOR = 55

# RAG decision history (stored in Qdrant via sophia_core)
RAG_COLLECTION_DECISIONS = "sophia_climate_decisions"
DEFAULT_RAG_ENABLED = True
DEFAULT_RAG_RETENTION_DAYS = 365
DEFAULT_RAG_MEMORY_ENTRIES = 20
MIN_RAG_RETENTION_DAYS = 7
MAX_RAG_RETENTION_DAYS = 3650
MIN_RAG_MEMORY_ENTRIES = 5
MAX_RAG_MEMORY_ENTRIES = 200
RAG_PURGE_INTERVAL_HOURS = 24