# SOPHIA Climate

<p align="center">
  <img src="https://raw.githubusercontent.com/williasj/ha-sophia-climate/main/images/sophia_logo.png" alt="SOPHIA Logo" width="200"/>
</p>

<p align="center">
  <a href="https://github.com/custom-components/hacs"><img src="https://img.shields.io/badge/HACS-Custom-orange.svg" alt="HACS Custom"/></a>
  <a href="https://github.com/williasj/ha-sophia-climate/releases"><img src="https://img.shields.io/github/v/release/williasj/ha-sophia-climate" alt="Release"/></a>
  <a href="https://www.home-assistant.io/"><img src="https://img.shields.io/badge/Home%20Assistant-2024.4.0+-blue.svg" alt="HA Minimum Version"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-PolyForm%20NC%201.0-lightgrey.svg" alt="License"/></a>
</p>

<p align="center">
  AI-powered multi-zone HVAC control for Home Assistant.<br/>
  Part of the S.O.P.H.I.A. ecosystem.
</p>

---

## Overview

SOPHIA Climate uses your local LLM (via [SOPHIA Core](https://github.com/williasj/ha-sophia-core)) to make intelligent HVAC decisions for every zone in your home. It evaluates indoor temperature, outdoor conditions, humidity, season, time of day, and your energy priority preferences — then acts on your thermostat automatically.

Key capabilities:

- **Multi-zone support** — configure each zone with its own thermostat and optional temperature sensor
- **Seasonal temperature profiles** — separate day/sleep targets per season (winter, spring, summer, fall)
- **Passive cooling logic** — suppresses AC when outdoor conditions allow natural cooling; blocks passive cooling when outdoor humidity is too high
- **Physics-based safety override** — validates every LLM decision against actual indoor/outdoor temps before execution; corrects impossible decisions (e.g. HEAT when already too warm)
- **Efficiency scoring** — continuously rates SOPHIA's performance based on temperature stability and accuracy
- **File-based decision history** — stores up to 500 decisions on disk with in-memory cache; avoids HA database bloat
- **Energy priority modes** — Comfort / Balanced / Savings with optional aggressive optimization
- **Special instructions** — free-text field passed to the LLM for custom household rules

---

## Requirements

- Home Assistant 2024.4.0 or later
- [SOPHIA Core](https://github.com/williasj/ha-sophia-core) installed and running
- A local Ollama instance with a compatible model (configured in SOPHIA Core)
- At least one thermostat entity in Home Assistant
- An outdoor temperature sensor entity

---

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → **Custom repositories**
3. Add `https://github.com/williasj/ha-sophia-climate` with category **Integration**
4. Search for **SOPHIA Climate** and install
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration**
7. Search for **SOPHIA Climate**

### Manual

1. Copy `custom_components/sophia_climate/` into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Add the integration via **Settings → Devices & Services**

---

## Configuration

### Initial Setup

The setup wizard asks for three things:

| Field | Description | Example |
|---|---|---|
| Zone Name | Human-readable name for your first zone | `Downstairs` |
| Thermostat Entity | HA entity ID of your thermostat | `climate.my_thermostat` |
| Outdoor Temp Sensor | HA entity ID for outdoor temperature | `sensor.outdoor_temperature` |

Additional zones can be added by editing the config entry data after setup. Full multi-zone UI wizard is planned for a future release.

### Options (after setup)

All settings are configurable via the integration options panel:

- **Check Interval** — how often SOPHIA evaluates each zone (5–120 minutes, default 30)
- **Seasonal Temperatures** — day and sleep targets per season in °F
- **Energy Priority** — Comfort / Balanced / Savings, with optional aggressive mode
- **Special Instructions** — free-text rules passed to the AI on every decision cycle
- **Sensor Configuration** — outdoor temp sensor, optional humidity sensor, passive cooling humidity threshold

---

## Entities

### Sensors

| Entity | Description |
|---|---|
| `sensor.sophia_climate_status` | Overall status: `active` or `idle` |
| `sensor.sophia_climate_summary` | Current indoor temp and delta from target |
| `sensor.sophia_climate_decision_history` | Latest decision + full statistics |
| `sensor.sophia_efficiency_score` | 0–100 performance score |
| `sensor.sophia_impact_score` | Qualitative rating (Excellent → Needs Improvement) |
| `sensor.temperature_delta` | Average absolute delta from target across zones |
| `sensor.sophia_{zone_id}_zone_status` | Per-zone status, current temps, last decision |

### Switches

| Entity | Description |
|---|---|
| `switch.sophia_climate_enabled` | Master enable/disable for all SOPHIA climate actions |
| `switch.sophia_{zone_id}_zone` | Per-zone enable/disable |

### Services

| Service | Description |
|---|---|
| `sophia_climate.run_climate_check` | Manually trigger a decision cycle |
| `sophia_climate.set_zone_target` | Set target temperature for a zone |
| `sophia_climate.enable_zone` | Enable a zone |
| `sophia_climate.disable_zone` | Disable a zone |
| `sophia_climate.cleanup_history` | Remove decisions older than N days |
| `sophia_climate.export_history` | Export full decision history to CSV |

---

## Decision Logic

SOPHIA Climate pre-analyzes the situation before calling the LLM and provides a structured summary. The LLM's role is to write the reasoning, not to derive the decision from scratch. A physics-based validator runs after the LLM response and before execution — it will override any physically impossible decision (e.g. requesting HEAT when the room is already above target).

Decision outcomes:

| Decision | Action |
|---|---|
| `NO_CHANGE` | No thermostat action taken |
| `HEAT_{temp}` | Set thermostat to heat mode at specified temperature |
| `COOL_{temp}` | Set thermostat to cool mode at specified temperature |
| `OFF` | Turn HVAC fully off |
| `OFF_FAN_ON` | Turn HVAC off but run fan for circulation |

---

## Passive Cooling

When outdoor temperature is below indoor and below the target (plus tolerance), SOPHIA considers passive cooling available. However, if outdoor humidity exceeds the configured threshold (default 60%), passive cooling is suppressed — bringing in humid air raises indoor moisture and stresses equipment. The threshold is configurable in the sensor options.

---

## Efficiency Score

The efficiency score (0–100) weights two components:

- **Stability (60%)** — percentage of decisions that were `NO_CHANGE`. High stability means the system is holding temperature without constant intervention.
- **Temperature Accuracy (40%)** — average absolute delta between indoor temp and target. Smaller delta = higher score.

Requires at least 5 recorded decisions before scoring begins.

---

## Decision History

Decisions are stored in `config/custom_components/sophia_climate/climate_decisions.json`. The last 20 decisions are kept in memory for sensor attributes. Up to 500 decisions are stored on disk. Use the `export_history` service to export to CSV for offline analysis, or `cleanup_history` to prune old entries.

---

## SOPHIA Ecosystem

| Module | Repository | Status |
|---|---|---|
| SOPHIA Core | [ha-sophia-core](https://github.com/williasj/ha-sophia-core) | Released |
| SOPHIA Climate | [ha-sophia-climate](https://github.com/williasj/ha-sophia-climate) | Released |
| SOPHIA Systems | ha-sophia-systems | Coming soon |
| SOPHIA Presence | ha-sophia-presence | Coming soon |

---

## Support

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/sophiadev)

If SOPHIA Climate is useful to you, consider supporting development. All contributions go toward hardware, testing time, and keeping the project open.

---

## License

[PolyForm Noncommercial License 1.0.0](LICENSE)

Copyright Scott Williams — [Scott.J.Williams14@gmail.com](mailto:Scott.J.Williams14@gmail.com) — [@williasj](https://github.com/williasj)

Free for personal, non-commercial use. Contact for commercial licensing.
