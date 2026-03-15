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