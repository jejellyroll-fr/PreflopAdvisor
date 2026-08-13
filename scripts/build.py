#!/usr/bin/env python3
"""
Cross-platform build script for PreflopAdvisor using PyInstaller.
Supports macOS (.app), Windows (.exe), and Linux.
"""

import os
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SPEC_FILE = os.path.join(PROJECT_ROOT, "preflop_advisor.spec")
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build")


def clean():
    """Remove previous build artifacts."""
    print("🧹 Cleaning previous build directories...")
    for directory in [BUILD_DIR, DIST_DIR]:
        if os.path.exists(directory):
            shutil.rmtree(directory)
            print(f"   Removed: {directory}")


def build(clean_first=True):
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
        print(f"   Output available in: {DIST_DIR}")
        if sys.platform == "darwin":
            app_path = os.path.join(DIST_DIR, "PreflopAdvisor.app")
            if os.path.exists(app_path):
                print(f"   macOS App Bundle: {app_path}")
    else:
        print(f"\n❌ Build failed with exit code: {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    should_clean = "--no-clean" not in sys.argv
    build(clean_first=should_clean)
