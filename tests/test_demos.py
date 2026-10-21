"""Keep the tutorial demo scenarios runnable as the tool surface evolves.

Each scenario's tool calls run against the real server over stdio; recording
the video itself needs Playwright and is not part of the test suite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

DEMOS = Path(__file__).resolve().parent.parent / "demos"
sys.path.insert(0, str(DEMOS))

from record import run_scenario  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402


@pytest.mark.parametrize("slug", sorted(SCENARIOS))
async def test_scenario_runs(slug, tmp_path):
    transcript = await run_scenario(slug, SCENARIOS[slug], out=tmp_path)
    files = tmp_path / slug / "files"
    for step in transcript["steps"]:
        if step["type"] == "call":
            assert not step["isError"], step
            for name in (step.get("show") or {}).values():
                assert (files / name).is_file(), name
    # Workspace paths are rewritten so recordings never leak local paths.
    assert str(files) not in json.dumps(transcript)
