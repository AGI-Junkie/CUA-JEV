"""Expose an installed Playwright Chromium without making browser tests skip."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    binary = Path(playwright.chromium.executable_path)
if not binary.is_file():
    raise SystemExit("Install Playwright Chromium first; required tests must not silently skip")
if not shutil.which("chromium"):
    directory = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "cua-jev-ci-browser"
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "chromium"
    link.unlink(missing_ok=True)
    link.symlink_to(binary)
    path_file = os.environ.get("GITHUB_PATH")
    if not path_file:
        raise SystemExit(f"Add {directory} to PATH before running required browser tests")
    with Path(path_file).open("a", encoding="utf-8") as stream:
        stream.write(str(directory) + "\n")
print("Installed Chromium is ready for mandatory browser tests")
