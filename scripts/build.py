#!/usr/bin/env python3
"""
Cross-platform build script for PreflopAdvisor using PyInstaller.
Supports macOS (.app), Windows (.exe), and Linux.
"""

import io
import os
import shutil
import subprocess
import sys

# Windows decodes a piped stdout as cp1252, which cannot encode the status emoji below:
# run from a terminal the script prints, run from CI it dies on its first line.
if sys.platform == "win32" and isinstance(sys.stdout, io.TextIOWrapper):  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SPEC_FILE = os.path.join(PROJECT_ROOT, "preflop_advisor.spec")
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build")


def clean() -> None:
    """Remove previous build artifacts."""
    print("🧹 Cleaning previous build directories...")
    for directory in [BUILD_DIR, DIST_DIR]:
        if os.path.exists(directory):
            shutil.rmtree(directory)
            print(f"   Removed: {directory}")


def directory_size(path: str) -> str:
    """Size of a produced bundle, so a build that doubles it is noticed at once."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            file_path = os.path.join(root, name)
            if not os.path.islink(file_path):
                total += os.path.getsize(file_path)
    return f"{total / 1e6:.0f} MB"


def build(clean_first: bool = True) -> None:
    """Execute PyInstaller build."""
    if clean_first:
        clean()

    print(f"🚀 Building PreflopAdvisor on platform '{sys.platform}'...")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        SPEC_FILE,
    ]

    print(f"   Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, check=False)

    if result.returncode == 0:
        print("\n✅ Build succeeded!")
        if sys.platform == "darwin":
            app_path = os.path.join(DIST_DIR, "PreflopAdvisor.app")
            collected = os.path.join(DIST_DIR, "PreflopAdvisor")
            # BUNDLE copies the collected folder into the .app, so shipping both doubles
            # the download for nothing. The .app is what a macOS user wants.
            if os.path.isdir(app_path) and os.path.isdir(collected):
                shutil.rmtree(collected)
            if os.path.exists(app_path):
                print(f"   macOS App Bundle: {app_path} ({directory_size(app_path)})")
        print(f"   Output available in: {DIST_DIR}")
    else:
        print(f"\n❌ Build failed with exit code: {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    should_clean = "--no-clean" not in sys.argv
    build(clean_first=should_clean)
