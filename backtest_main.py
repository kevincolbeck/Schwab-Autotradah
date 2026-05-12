"""Standalone entry point for the backtester GUI."""

import sys
import logging
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication
from gui.backtest_window import BacktestWindow


def main():
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("backtest.log", mode="a"),
        ],
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Schwab Momentum Bot - Backtester")
    window = BacktestWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
