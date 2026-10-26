"""Runs every no-code test in tests/nocode/specs/*.yaml (built in QA Forge Studio).

Each spec - or each data row of a data-driven spec - is one pytest test, so no-code tests get
the same evidence packs, JIRA publishing, markers and parallel execution as coded tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qaforge.nocode.runner import SpecRunner
from qaforge.nocode.spec import load_spec, spec_params

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("spec_case", spec_params(ROOT))
def test_nocode(spec_case, open_app, ctx, events, verify, evidence, settings, secrets, data_gen):
    spec_id, row = spec_case
    SpecRunner(load_spec(ROOT, spec_id), row, root=ROOT, open_app=open_app, ctx=ctx, events=events,
               verify=verify, evidence=evidence, settings=settings, secrets=secrets, gen=data_gen).run()
