"""Schwab Momentum Bot — Main entry point."""

import sys
import os
import logging
import multiprocessing
from pathlib import Path

# Required for PyInstaller + multiprocessing (schwab OAuth uses multiprocess)
multiprocessing.freeze_support()
try:
    import multiprocess
    multiprocess.freeze_support()
except ImportError:
    pass

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

from core.config import load_config, save_config
from broker.auth import verify_auth
from broker.schwab_client import SchwabBrokerClient
from core.engine import TradingEngine
from gui.main_window import MainWindow
from gui.login_window import LoginWindow


def setup_logging(level: str = "INFO"):
    """Configure logging to console and file."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(log_level)

    # File handler
    file_handler = logging.FileHandler('momentum_bot.log', mode='a')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console)
    root_logger.addHandler(file_handler)


def launch_main_window(schwab_client, config, app):
    """Launch the main trading window after successful authentication."""
    logger = logging.getLogger(__name__)

    if not verify_auth(schwab_client):
        QMessageBox.warning(
            None, "Auth Warning",
            "Authentication verification failed.\n"
            "The bot will start but may not be able to trade."
        )

    # Create broker client wrapper
    broker = SchwabBrokerClient(schwab_client, config)

    # Create trading engine
    engine = TradingEngine(broker, config)

    # Try to get initial equity
    try:
        acct = broker.get_account_info()
        engine._equity = acct.get('equity', 0.0)
        logger.info(f"Account equity: ${engine._equity:,.2f}")
    except Exception as e:
        logger.warning(f"Could not fetch account info: {e}")

    # Create and show main window
    window = MainWindow(engine, config)
    window.controls.set_auth_status(True)
    if engine._equity > 0:
        window.controls.set_equity(engine._equity)
    window.controls.set_mode(config.app.shadow_mode)

    # Load recent events
    events = engine.get_recent_events(50)
    window.log_viewer.load_events(events)

    window.show()
    logger.info("GUI launched")

    # Store reference so it doesn't get garbage collected
    app._main_window = window


def main():
    # Load config
    config = load_config("config.yaml")
    setup_logging(config.app.log_level)
    logger = logging.getLogger(__name__)
    logger.info("Schwab Momentum Bot starting...")

    # Create Qt application
    app = QApplication(sys.argv)
    app.setApplicationName("Schwab Live Trader")
    icon_path = PROJECT_ROOT / "assets" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Check for Schwab credentials
    if not config.schwab.client_id or not config.schwab.client_secret:
        from setup_wizard import run_setup_wizard
        result = run_setup_wizard(config)
        if not result:
            QMessageBox.critical(
                None, "Setup Required",
                "Schwab API credentials are required.\n\n"
                "Please run setup_wizard.py or edit config.yaml with your:\n"
                "- client_id\n"
                "- client_secret\n"
                "- redirect_url\n\n"
                "Register at developer.schwab.com"
            )
            sys.exit(1)
        save_config(config)

    # Show login window
    login_window = LoginWindow(config)

    def on_login_complete(schwab_client):
        login_window.close()
        launch_main_window(schwab_client, config, app)

    login_window.login_complete.connect(on_login_complete)
    login_window.show()
    logger.info("Login screen shown")

    # Run
    exit_code = app.exec()

    # Cleanup
    logger.info("Shutting down...")
    if hasattr(app, '_main_window') and app._main_window.engine.running:
        app._main_window.engine.stop()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
