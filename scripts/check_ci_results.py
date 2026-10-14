"""Fail CI on missing browser coverage or skips beyond the named Windows checks."""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

ALLOWED_SKIPS = {
    ("test_open_browser", "test_real_browser_snapshot_and_dom_execution_without_external_site"),
    ("test_suites", "test_edge_task_completes_real_dom_and_download_cycle"),
    ("test_suites", "test_excel_task_completes_real_workbook_cycle"),
}
REQUIRED = {"test_sessions", "test_workbench_api", "test_controlled_acceptance", "test_session_vision"}


def validate(path: Path) -> dict:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("No tests were recorded")
    covered = set()
    unexpected = []
    for case in cases:
        module = case.get("classname", "").split(".")[-1]
        key = (module, case.get("name", "").split("[")[0])
        if case.find("failure") is not None or case.find("error") is not None:
            raise ValueError(f"Failing test: {key}")
        if case.find("skipped") is not None:
            if key not in ALLOWED_SKIPS:
                unexpected.append(key)
        elif module in REQUIRED:
            covered.add(module)
    if unexpected:
        raise ValueError(f"Unexpected skips: {unexpected}")
    if missing := REQUIRED - covered:
        raise ValueError(f"Required browser suites did not run: {sorted(missing)}")
    return {"tests": len(cases), "required_browser_suites": sorted(covered),
            "allowed_hardware_skips": sum(c.find("skipped") is not None for c in cases)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    args = parser.parse_args()
    print(validate(args.junit))
