"""APScheduler wrapper for periodic trading tasks."""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from core.config import Config
from core.engine import TradingEngine

logger = logging.getLogger(__name__)


class TradingScheduler:
    """Manages scheduled tasks for the trading engine."""

    def __init__(self, engine: TradingEngine, config: Config):
        self.engine = engine
        self.config = config
        self.scheduler = BackgroundScheduler(
            job_defaults={'coalesce': True, 'max_instances': 1}
        )

    def start(self):
        """Start all scheduled jobs."""
        safety = self.config.safety

        # Reconciliation: every 15-30 seconds
        self.scheduler.add_job(
            self.engine.run_reconciliation,
            IntervalTrigger(seconds=safety.reconciliation_interval_seconds),
            id='reconciliation',
            name='Broker Reconciliation',
        )

        # Signal scan: every 30 seconds during market hours
        self.scheduler.add_job(
            self.engine.run_signal_scan,
            IntervalTrigger(seconds=safety.signal_scan_interval_seconds),
            id='signal_scan',
            name='Signal Scan',
        )

        # Morning open check: fires once at 9:30 AM ET to catch overnight signals
        self.scheduler.add_job(
            self.engine.run_morning_check,
            CronTrigger(
                day_of_week='mon-fri',
                hour=9, minute=30,
                second=0,
                timezone='US/Eastern',
            ),
            id='morning_check',
            name='Morning Open Check',
        )

        # EOD check: every 30 seconds in the last 10 minutes of trading
        # (3:50 PM - 4:00 PM ET)
        self.scheduler.add_job(
            self.engine.run_eod_check,
            CronTrigger(
                day_of_week='mon-fri',
                hour=15, minute='50-59',
                second='*/30',
                timezone='US/Eastern',
            ),
            id='eod_check',
            name='EOD Exit Check',
        )

        # AI Strategist: daily analysis at 4:15 PM ET (after market close)
        if self.engine.ai_strategist:
            self.scheduler.add_job(
                self._run_ai_daily,
                CronTrigger(
                    day_of_week='mon-fri',
                    hour=16, minute=15,
                    timezone='US/Eastern',
                ),
                id='ai_daily',
                name='AI Daily Analysis',
            )

            # AI Strategist: weekly deep analysis Friday 5:00 PM ET
            self.scheduler.add_job(
                self._run_ai_weekly,
                CronTrigger(
                    day_of_week='fri',
                    hour=17, minute=0,
                    timezone='US/Eastern',
                ),
                id='ai_weekly',
                name='AI Weekly Deep Analysis',
            )
            logger.info("AI Strategist scheduled: daily at 4:15 PM ET, weekly Friday 5:00 PM ET")

        self.scheduler.start()
        logger.info("Scheduler started with reconciliation, signal scan, morning open check, EOD check, and AI analysis jobs")

    def _run_ai_daily(self):
        """Run daily AI analysis after market close."""
        try:
            if self.engine.ai_strategist:
                self.engine.ai_strategist.run_analysis("DAILY", min_trades=1)
        except Exception as e:
            logger.error(f"AI daily analysis error: {e}")

    def _run_ai_weekly(self):
        """Run weekly deep AI analysis."""
        try:
            if self.engine.ai_strategist:
                self.engine.ai_strategist.run_analysis("WEEKLY", min_trades=3)
        except Exception as e:
            logger.error(f"AI weekly analysis error: {e}")

    def stop(self):
        """Stop all scheduled jobs."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def pause(self):
        """Pause all jobs."""
        self.scheduler.pause()

    def resume(self):
        """Resume all jobs."""
        self.scheduler.resume()
