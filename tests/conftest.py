# -*- coding: utf-8 -*-
"""Test root setup.

pytest-homeassistant-custom-component ships its own custom_components package
under testing_config, which shadows this repo's folder. Add this repo's
custom_components (and a stub sophia_core, the declared dependency) to that
package's search path so HA finds them.
"""
from pathlib import Path

import pytest

HERE = Path(__file__).absolute().parent
SEARCH = [
    str(HERE.parent / "custom_components"),
    str(HERE / "stub_components"),
]


@pytest.fixture(autouse=True)
def _repo_custom_components():
    import custom_components
    paths = custom_components.__path__
    for p in reversed(SEARCH):
        if p not in list(paths):
            if isinstance(paths, list):
                paths.insert(0, p)
            else:
                paths.append(p)
    yield
