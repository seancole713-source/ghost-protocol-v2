"""Keep every browser installer on the runner supported by locked Playwright."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BROWSER_JOBS = [
    ("ci.yml", "playwright-smoke"),
    ("ci.yml", "browser-offline-candidate"),
    ("ci.yml", "browser-readonly-safety"),
    ("e2e.yml", "playwright-smoke"),
]


def _jobs(workflow):
    # Job keys are two spaces deep; steps and settings are nested below them.
    text = workflow.read_text().split("\njobs:\n", 1)[1]
    return dict(re.findall(
        r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.M | re.S,
    ))


@pytest.mark.parametrize("workflow,job", BROWSER_JOBS)
def test_browser_job_uses_supported_runner(workflow, job):
    block = _jobs(ROOT / ".github/workflows" / workflow)[job]
    assert "playwright install --with-deps chromium" in block
    assert re.findall(r"^    runs-on: (.+)$", block, re.M) == ["ubuntu-24.04"]


def test_all_browser_install_jobs_have_runner_coverage():
    discovered = {
        (workflow.name, job)
        for workflow in (ROOT / ".github/workflows").glob("*.y*ml")
        for job, block in _jobs(workflow).items()
        if "playwright install" in block
    }
    assert discovered == set(BROWSER_JOBS)
