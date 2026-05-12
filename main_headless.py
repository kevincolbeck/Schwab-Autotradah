"""Headless entry point — runs the momentum bot on a Linux server with no GUI."""

import sys
import os
import logging
import signal
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from core.config import load_config
from broker.auth import create_client, verify_auth
from broker.schwab_client import SchwabBrokerClient
from core.engine import TradingEngine
from core.scheduler import TradingScheduler


def setup_logging(level: str = "INFO"):
    log_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(log_level)

    file_handler = logging.FileHandler("momentum_bot.log", mode="a")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)


def main():
    config = load_config("config.yaml")
    setup_logging(config.app.log_level)
    logger = logging.getLogger(__name__)
    logger.info("Schwab Momentum Bot starting (headless)...")

    # Require existing token — run `python broker/auth.py` locally first,
    # then scp token.json.enc and .token_key to the server.
    token_enc = Path(config.schwab.token_file + ".enc")
    token_plain = Path(config.schwab.token_file)
    if not token_enc.exists() and not token_plain.exists():
        logger.error(
            "No token found (%s or %s). "
            "Complete OAuth on your local machine, then upload token.json.enc and .token_key.",
            token_enc, token_plain,
        )
        sys.exit(1)

    schwab_client = create_client(
        client_id=config.schwab.client_id,
        client_secret=config.schwab.client_secret,
        redirect_url=config.schwab.redirect_url,
        token_path=config.schwab.token_file,
    )
    if not schwab_client:
        logger.error("Failed to create Schwab client. Check token files.")
        sys.exit(1)

    if not verify_auth(schwab_client):
        logger.warning("Auth verification failed — bot will start but may not trade.")

    broker = SchwabBrokerClient(schwab_client, config)
    engine = TradingEngine(broker, config)

    try:
        acct = broker.get_account_info()
        engine._equity = acct.get("equity", 0.0)
        logger.info("Account equity: $%,.2f", engine._equity)
    except Exception as e:
        logger.warning("Could not fetch account info: %s", e)

    scheduler = TradingScheduler(engine, config)

    def shutdown(signum, frame):
        logger.info("Shutdown signal received — stopping cleanly...")
        engine.stop()
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    engine.start()
    scheduler.start()
    logger.info(
        "Running headless. shadow_mode=%s strategy=%s",
        config.app.shadow_mode,
        getattr(config.strategy_runtime, "active_live_strategy", "?"),
    )

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
