# -*- coding: utf-8 -*-
"""Test-only stand-in for sophia_core so sophia_climate's dependency resolves."""
DOMAIN = "sophia_core"


async def async_setup(hass, config):
    return True
