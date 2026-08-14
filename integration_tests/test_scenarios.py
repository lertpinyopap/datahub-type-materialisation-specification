from __future__ import annotations

from pathlib import Path

import pytest

from tms_integration.database import live_database_enabled
from tms_integration.runner import run_live_scenario
from tms_integration.scenario import discover_scenarios, load_scenario


SCENARIO_ROOTS = discover_scenarios(Path(__file__).resolve().parent)


@pytest.mark.parametrize("scenario_root", SCENARIO_ROOTS, ids=lambda path: path.name)
def test_live_scenario(scenario_root: Path, tmp_path: Path) -> None:
    if not live_database_enabled():
        pytest.skip("set TMS_RUN_INTEGRATION=1 to run live database integration tests")
    scenario = load_scenario(scenario_root)
    run_live_scenario(scenario, tmp_path)
