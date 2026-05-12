"""Build script to create the Schwab Live Trader executable."""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent


def build():
    icon_path = PROJECT_ROOT / "assets" / "icon.ico"
    if not icon_path.exists():
        print("Icon not found, generating...")
        subprocess.check_call([sys.executable, "create_icon.py"], cwd=str(PROJECT_ROOT))

    # Exclude heavy packages not needed for live trading
    excludes = [
        "torch", "torchvision", "torchaudio",
        "scipy", "matplotlib", "sympy",
        "PIL", "pillow", "Pillow",
        "tkinter", "_tkinter",
        "pytest", "hypothesis",
        "jupyter", "notebook", "ipython", "IPython",
        "tensorboard", "tensorflow",
        "openpyxl", "xlrd",
        "pygments",
        "setuptools", "pkg_resources",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "SchwabLiveTrader",
        "--windowed",                          # No console window
        "--onedir",                            # Faster startup than --onefile
        "--icon", str(icon_path),
        "--add-data", f"assets;assets",        # Bundle icon assets
        "--add-data", f"config.yaml;.",        # Bundle default config
        "--add-data", f"strategy_specs;strategy_specs",  # Bundle strategy specs
        "--hidden-import", "schwab",
        "--hidden-import", "schwab.client",
        "--hidden-import", "apscheduler",
        "--hidden-import", "apscheduler.schedulers.background",
        "--hidden-import", "apscheduler.triggers.interval",
        "--hidden-import", "apscheduler.triggers.cron",
        "--hidden-import", "sqlalchemy",
        "--hidden-import", "sqlalchemy.orm",
        "--hidden-import", "zoneinfo",
        "--hidden-import", "PySide6",
        "--hidden-import", "numpy",
        "--hidden-import", "pandas",
        "--hidden-import", "multiprocess",
        "--hidden-import", "psutil",
        "--hidden-import", "httpx",
        "--hidden-import", "anthropic",
        "--noconfirm",
        "--clean",
    ]
    for pkg in excludes:
        cmd.extend(["--exclude-module", pkg])
    cmd.append("main.py")

    print("Building Schwab Live Trader executable...")
    print(f"Command: {' '.join(cmd)}")
    print()

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode == 0:
        dist_path = PROJECT_ROOT / "dist" / "SchwabLiveTrader"
        exe_path = dist_path / "SchwabLiveTrader.exe"
        print()
        print("=" * 60)
        print("  BUILD SUCCESSFUL!")
        print(f"  Executable: {exe_path}")
        print(f"  Folder:     {dist_path}")
        print("=" * 60)
        print()
        print("To run: double-click SchwabLiveTrader.exe in the dist folder.")
        print("Make sure config.yaml and your Schwab token file are present.")
    else:
        print()
        print("BUILD FAILED - check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    build()
