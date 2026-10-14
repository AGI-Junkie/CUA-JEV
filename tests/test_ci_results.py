"""The mandatory browser gate fails if cloud suites are absent or skipped."""
from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_results", Path(__file__).parents[1] / "scripts/check_ci_results.py"
)
ci_results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_results)


def report(tmp_path, *, missing=None, skipped=None, failed=None):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for module in sorted(ci_results.REQUIRED - {missing}):
        case = ET.SubElement(suite, "testcase", classname=module, name="actual_contract")
        if module == skipped:
            ET.SubElement(case, "skipped", message="browser absent")
        if module == failed:
            ET.SubElement(case, "failure", message="real regression")
    for module, name in ci_results.ALLOWED_SKIPS:
        case = ET.SubElement(suite, "testcase", classname=module, name=name)
        ET.SubElement(case, "skipped", message="hardware unavailable")
    path = tmp_path / "tests.xml"
    ET.ElementTree(root).write(path)
    return path


def test_gate_accepts_only_documented_hardware_skips(tmp_path):
    result = ci_results.validate(report(tmp_path))
    assert result["allowed_hardware_skips"] == 3
    assert set(result["required_browser_suites"]) == ci_results.REQUIRED


@pytest.mark.parametrize("kwargs,error", [
    ({"missing": "test_sessions"}, "Required browser suites did not run"),
    ({"skipped": "test_workbench_api"}, "Unexpected skips"),
    ({"failed": "test_controlled_acceptance"}, "Failing test"),
])
def test_gate_rejects_missing_skipped_or_failing_browser_evidence(tmp_path, kwargs, error):
    with pytest.raises(ValueError, match=error):
        ci_results.validate(report(tmp_path, **kwargs))
