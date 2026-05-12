"""PySide6 GUI for the backtesting system."""

import copy
import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QGroupBox, QLabel, QPushButton, QLineEdit, QDoubleSpinBox,
    QSpinBox, QDateEdit, QTextEdit, QTableWidget, QTableWidgetItem, QCheckBox,
    QHeaderView, QProgressBar, QStatusBar, QTabWidget, QMessageBox,
)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QDate
from PySide6.QtGui import QColor

from gui.styles import DARK_THEME
from gui.backtest_charts import EquityCurveChart, TradeDistributionChart, RHistogramChart
from gui.chat_panel import ChatPanel, MessageRole
from gui.chat_controller import (
    ClaudeWorker, build_system_prompt, parse_proposed_changes,
    parse_proposed_strategy_spec,
    strip_json_block, apply_changes_to_config, get_tunable_params,
)
from gui.live_diff_dialog import LiveDiffDialog
from core.config import load_config, save_config, Config
from backtest.config_overrides import BacktestConfig
from backtest.data_provider import HistoricalDataProvider
from backtest.data_adapter import BacktestDataCache
from backtest.engine import BacktestEngine
from backtest.rule_based_engine import (
    RuleBasedBacktestEngine,
    load_strategy_spec,
    referenced_price_symbols,
    save_strategy_spec,
    strategy_spec_uses_auto_us_universe,
    symbols_from_strategy_spec,
    trade_symbols_from_strategy_spec,
)
from strategy.scanner_history import ScannerHistory
from data.us_universe import USMarketUniverseProvider

logger = logging.getLogger(__name__)


def _pack_cached_data(
    daily_data: Dict[str, object], intraday_data: Optional[Dict[str, object]] = None
) -> dict:
    return {"daily": daily_data, "intraday": intraday_data or {}}


def _unpack_cached_data(payload: Optional[dict]) -> Tuple[Dict[str, object], Dict[str, object]]:
    if isinstance(payload, dict) and isinstance(payload.get("daily"), dict):
        intraday = payload.get("intraday", {})
        return payload["daily"], (intraday if isinstance(intraday, dict) else {})
    if isinstance(payload, dict):
        # Backward compatibility for legacy cached payloads (daily-only map).
        return payload, {}
    return {}, {}


class BacktestWorker(QThread):
    """Runs the backtest in a background thread."""
    progress = Signal(int, int, str)
    finished = Signal(dict)
    error = Signal(str)
    data_progress = Signal(int, int, str)
    data_fetched = Signal(dict)

    def __init__(self, config: Config, bt_config: BacktestConfig):
        super().__init__()
        self.config = config
        self.bt_config = bt_config
        self._engine: Optional[BacktestEngine] = None

    def run(self):
        try:
            # Phase 1: Fetch data
            provider = HistoricalDataProvider(
                data_source=self.config.data.backtest_data_source,
                polygon_api_key=self.config.data.polygon_api_key,
                default_history_years=self.config.data.default_history_years,
            )
            strategy_type = "rule_based"
            strategy_spec = None

            strategy_spec = load_strategy_spec(self.bt_config)
            auto_universe_requested = strategy_spec_uses_auto_us_universe(strategy_spec)
            if auto_universe_requested:
                auto_symbols = USMarketUniverseProvider().get_symbols()
                if self.bt_config.auto_universe_max_symbols > 0:
                    auto_symbols = auto_symbols[:self.bt_config.auto_universe_max_symbols]
                if not auto_symbols:
                    self.error.emit("Automatic US universe load failed for strategy spec.")
                    provider.close()
                    return
                base_symbols = auto_symbols
            else:
                base_symbols = trade_symbols_from_strategy_spec(strategy_spec)
                if not base_symbols:
                    self.error.emit(
                        "Rule-based strategy has no trade symbols. "
                        "Use concrete tickers or ALL_US token."
                    )
                    provider.close()
                    return

            ref_symbols = referenced_price_symbols(strategy_spec)
            self.bt_config.symbols = sorted(
                set(base_symbols + ref_symbols + [self.bt_config.benchmark])
            )

            if self.bt_config.use_scanner_history:
                scanner_history = ScannerHistory.from_directory(
                    self.bt_config.scanner_history_dir,
                    self.bt_config.scanner_names,
                    start_date=self.bt_config.start_date,
                    end_date=self.bt_config.end_date,
                )
                if not scanner_history.has_data():
                    self.error.emit(
                        "No scanner history found. Export dated scanner CSVs first "
                        "or disable scanner-history mode."
                    )
                    provider.close()
                    return
                self.bt_config.scanner_history = scanner_history
                self.bt_config.symbols = scanner_history.all_symbols()
                if not self.bt_config.symbols:
                    self.error.emit(
                        "Scanner history loaded, but no symbols matched configured scanner names."
                    )
                    provider.close()
                    return
            elif self.bt_config.auto_us_universe:
                universe_provider = USMarketUniverseProvider()
                auto_symbols = universe_provider.get_symbols()
                if self.bt_config.auto_universe_max_symbols > 0:
                    auto_symbols = auto_symbols[:self.bt_config.auto_universe_max_symbols]
                if not auto_symbols:
                    self.error.emit("Automatic US universe load failed.")
                    provider.close()
                    return
                self.bt_config.symbols = auto_symbols

            if not self.bt_config.symbols:
                self.error.emit("No symbols configured for this backtest.")
                provider.close()
                return

            all_symbols = list(set(self.bt_config.symbols + [self.bt_config.benchmark]))

            data = provider.fetch_universe(
                all_symbols,
                self.bt_config.start_date,
                self.bt_config.end_date,
                progress_callback=lambda cur, tot, sym: self.data_progress.emit(cur, tot, sym),
            )
            intraday_data = {}
            intraday_intervals = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}
            if (
                strategy_type == "rule_based"
                and strategy_spec is not None
                and (
                    strategy_spec.use_intraday_vwap_stop
                    or strategy_spec.backtest_timeframe in intraday_intervals
                )
            ):
                intraday_backtest_mode = strategy_spec.backtest_timeframe in intraday_intervals
                intraday_interval = (
                    strategy_spec.backtest_timeframe
                    if intraday_backtest_mode
                    else strategy_spec.intraday_interval
                )
                trade_symbols = trade_symbols_from_strategy_spec(strategy_spec)
                if trade_symbols:
                    intraday_symbols = list(trade_symbols)
                else:
                    intraday_symbols = list(self.bt_config.symbols)
                intraday_symbols.extend(referenced_price_symbols(strategy_spec))
                intraday_symbols.append(self.bt_config.benchmark)
                intraday_symbols = sorted({s for s in intraday_symbols if s})
                intraday_data = provider.fetch_intraday_universe(
                    intraday_symbols,
                    self.bt_config.start_date,
                    self.bt_config.end_date,
                    interval=intraday_interval,
                    progress_callback=lambda cur, tot, sym: self.data_progress.emit(cur, tot, sym),
                )
                if not intraday_data:
                    if intraday_backtest_mode:
                        self.error.emit(
                            "Intraday backtest requested but no intraday bars were fetched. "
                            "For long-range intraday, configure data.polygon_api_key and retry."
                        )
                        provider.close()
                        return
                    logger.warning(
                        "Intraday VWAP-stop mode enabled but no intraday bars were fetched. "
                        "Backtest will fall back to non-intraday exits."
                    )

            provider.close()

            if not data:
                self.error.emit("No historical data fetched")
                return

            self.data_fetched.emit(_pack_cached_data(data, intraday_data))

            # Phase 2: Run backtest
            data_cache = BacktestDataCache(data, intraday_data=intraday_data)
            if strategy_spec is None:
                strategy_spec = load_strategy_spec(self.bt_config)
            engine = RuleBasedBacktestEngine(self.config, self.bt_config, data_cache, strategy_spec)
            engine.on_progress = lambda cur, tot, dt: self.progress.emit(cur, tot, dt)
            self._engine = engine

            results = engine.run()
            self.finished.emit(results)

        except Exception as e:
            logger.error(f"Backtest error: {e}", exc_info=True)
            self.error.emit(str(e))

    def cancel(self):
        if self._engine:
            self._engine.cancel()


class ParameterRerunWorker(QThread):
    """Re-runs backtest with cached data and modified config (no data download)."""
    progress = Signal(int, int, str)
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, config: Config, bt_config: BacktestConfig, cached_data: dict):
        super().__init__()
        self.config = config
        self.bt_config = bt_config
        self.cached_data = cached_data
        self._engine = None

    def run(self):
        try:
            daily_data, intraday_data = _unpack_cached_data(self.cached_data)
            data_cache = BacktestDataCache(daily_data, intraday_data=intraday_data)
            strategy_spec = load_strategy_spec(self.bt_config)
            engine = RuleBasedBacktestEngine(self.config, self.bt_config, data_cache, strategy_spec)
            self._engine = engine
            engine.on_progress = lambda cur, tot, dt: self.progress.emit(cur, tot, dt)
            results = engine.run()
            self.finished.emit(results)
        except Exception as e:
            logger.error(f"Rerun error: {e}", exc_info=True)
            self.error.emit(str(e))

    def cancel(self):
        if self._engine:
            self._engine.cancel()


class BacktestWindow(QMainWindow):
    """Main backtest GUI window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Schwab Momentum Bot - Backtester")
        self.setMinimumSize(1400, 900)
        self.setStyleSheet(DARK_THEME)

        self.config = load_config("config.yaml")
        self.worker: Optional[BacktestWorker] = None
        self._rerun_worker: Optional[ParameterRerunWorker] = None
        self._results: Optional[dict] = None

        # Chat state
        self._cached_data: Optional[dict] = None
        self._bt_config: Optional[BacktestConfig] = None
        self._working_config: Optional[Config] = None
        self._accumulated_changes: Dict[str, object] = {}
        self._pending_strategy_spec: Optional[dict] = None
        self._conversation_history: List[dict] = []
        self._claude_worker: Optional[ClaudeWorker] = None
        self._last_run_id: Optional[str] = None
        self._last_run_snapshot_path: Optional[str] = None

        self._init_ui()
        self._connect_signals()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # Left panel: Controls (280px fixed)
        self.control_panel = self._build_control_panel()
        self.control_panel.setFixedWidth(280)
        main_layout.addWidget(self.control_panel)

        # Right panel: Results
        right_splitter = QSplitter(Qt.Vertical)

        # Top: Statistics summary
        self.stats_group = QGroupBox("Performance Summary")
        stats_layout = QVBoxLayout()
        self.stats_table = QTableWidget()
        self.stats_table.setColumnCount(2)
        self.stats_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.stats_table.setAlternatingRowColors(True)
        self.stats_table.verticalHeader().setVisible(False)
        stats_layout.addWidget(self.stats_table)
        self.stats_group.setLayout(stats_layout)
        right_splitter.addWidget(self.stats_group)

        # Middle: Charts (tabbed)
        self.chart_tabs = QTabWidget()
        self.chart_tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #3a3a5c; }
            QTabBar::tab { background: #222244; color: #8888cc; padding: 8px 16px; border: 1px solid #3a3a5c; }
            QTabBar::tab:selected { background: #2a2a4e; color: #e0e0e0; border-bottom: 2px solid #4488ff; }
        """)

        equity_tab = QWidget()
        equity_layout = QVBoxLayout(equity_tab)
        self.equity_chart = EquityCurveChart(width=12, height=5)
        equity_layout.addWidget(self.equity_chart)
        self.chart_tabs.addTab(equity_tab, "Equity Curve")

        dist_tab = QWidget()
        dist_layout = QVBoxLayout(dist_tab)
        self.dist_chart = TradeDistributionChart(width=12, height=4)
        dist_layout.addWidget(self.dist_chart)
        self.chart_tabs.addTab(dist_tab, "R per Trade")

        hist_tab = QWidget()
        hist_layout = QVBoxLayout(hist_tab)
        self.hist_chart = RHistogramChart(width=12, height=4)
        hist_layout.addWidget(self.hist_chart)
        self.chart_tabs.addTab(hist_tab, "R Distribution")

        right_splitter.addWidget(self.chart_tabs)

        # Trade log
        trades_group = QGroupBox("Trade Log")
        trades_layout = QVBoxLayout()
        self.trades_table = QTableWidget()
        self.trades_table.setColumnCount(11)
        self.trades_table.setHorizontalHeaderLabels([
            "Symbol", "Entry Date", "Exit Date", "Entry $", "Exit $",
            "Shares", "PnL $", "PnL R", "Stop", "Exit Reason", "Regime",
        ])
        self.trades_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.trades_table.setAlternatingRowColors(True)
        self.trades_table.verticalHeader().setVisible(False)
        self.trades_table.setSortingEnabled(True)
        trades_layout.addWidget(self.trades_table)
        trades_group.setLayout(trades_layout)
        right_splitter.addWidget(trades_group)

        # Chat panel
        self.chat_panel = ChatPanel()
        right_splitter.addWidget(self.chat_panel)

        right_splitter.setSizes([180, 350, 200, 220])
        main_layout.addWidget(right_splitter)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumWidth(300)
        self.status_bar.addPermanentWidget(self.progress_bar)
        self.status_bar.showMessage("Ready")

    def _build_control_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(12)

        # Date range
        date_group = QGroupBox("Date Range")
        date_layout = QVBoxLayout()

        start_row = QHBoxLayout()
        start_row.addWidget(QLabel("Start:"))
        self.start_date = QDateEdit()
        self.start_date.setDate(QDate(2016, 1, 1))
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        start_row.addWidget(self.start_date)
        date_layout.addLayout(start_row)

        end_row = QHBoxLayout()
        end_row.addWidget(QLabel("End:"))
        self.end_date = QDateEdit()
        self.end_date.setDate(QDate.currentDate())
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        end_row.addWidget(self.end_date)
        date_layout.addLayout(end_row)

        date_group.setLayout(date_layout)
        layout.addWidget(date_group)

        # Capital
        cap_group = QGroupBox("Starting Capital")
        cap_layout = QHBoxLayout()
        self.capital_spin = QDoubleSpinBox()
        self.capital_spin.setRange(1000, 10_000_000)
        self.capital_spin.setValue(100_000)
        self.capital_spin.setPrefix("$ ")
        self.capital_spin.setSingleStep(10000)
        self.capital_spin.setDecimals(0)
        cap_layout.addWidget(self.capital_spin)
        cap_group.setLayout(cap_layout)
        layout.addWidget(cap_group)

        # Universe
        uni_group = QGroupBox("Universe (comma-separated)")
        uni_layout = QVBoxLayout()
        self.symbols_input = QTextEdit()
        self.symbols_input.setMaximumHeight(80)
        self.symbols_input.setPlainText(
            "AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AMD, "
            "CRM, AVGO, NFLX, ADBE, COST, LLY, UNH"
        )
        uni_layout.addWidget(self.symbols_input)

        auto_row = QHBoxLayout()
        self.auto_universe_cb = QCheckBox("Auto US market universe")
        self.auto_universe_cb.setChecked(True)
        auto_row.addWidget(self.auto_universe_cb)
        uni_layout.addLayout(auto_row)

        auto_limit_row = QHBoxLayout()
        auto_limit_row.addWidget(QLabel("Auto limit:"))
        self.auto_universe_limit_spin = QSpinBox()
        self.auto_universe_limit_spin.setRange(0, 20000)
        self.auto_universe_limit_spin.setValue(0)
        self.auto_universe_limit_spin.setSuffix(" symbols")
        self.auto_universe_limit_spin.setSpecialValueText("all")
        auto_limit_row.addWidget(self.auto_universe_limit_spin)
        uni_layout.addLayout(auto_limit_row)

        bench_row = QHBoxLayout()
        bench_row.addWidget(QLabel("Benchmark:"))
        self.benchmark_input = QLineEdit("SPY")
        bench_row.addWidget(self.benchmark_input)
        uni_layout.addLayout(bench_row)

        uni_group.setLayout(uni_layout)
        layout.addWidget(uni_group)

        # Scanner history universe
        scanner_group = QGroupBox("Scanner Modes")
        scanner_layout = QVBoxLayout()
        self.use_computed_scanners_cb = QCheckBox("Use computed scanner logic (ThinkScript)")
        self.use_computed_scanners_cb.setChecked(False)
        scanner_layout.addWidget(self.use_computed_scanners_cb)

        self.use_scanner_history_cb = QCheckBox("Use dated scanner CSV files")
        self.use_scanner_history_cb.setChecked(False)
        scanner_layout.addWidget(self.use_scanner_history_cb)

        scanner_dir_row = QHBoxLayout()
        scanner_dir_row.addWidget(QLabel("Folder:"))
        self.scanner_dir_input = QLineEdit(self.config.universe.scanner_history_dir)
        scanner_dir_row.addWidget(self.scanner_dir_input)
        scanner_layout.addLayout(scanner_dir_row)

        scanner_names_row = QHBoxLayout()
        scanner_names_row.addWidget(QLabel("Scanners:"))
        self.scanner_names_input = QLineEdit(", ".join(self.config.universe.scanner_names))
        scanner_names_row.addWidget(self.scanner_names_input)
        scanner_layout.addLayout(scanner_names_row)

        scanner_carry_row = QHBoxLayout()
        scanner_carry_row.addWidget(QLabel("Carry fwd:"))
        self.scanner_carry_spin = QSpinBox()
        self.scanner_carry_spin.setRange(0, 10)
        self.scanner_carry_spin.setValue(self.config.universe.scanner_carry_forward_days)
        self.scanner_carry_spin.setSuffix(" days")
        scanner_carry_row.addWidget(self.scanner_carry_spin)
        scanner_layout.addLayout(scanner_carry_row)

        scanner_group.setLayout(scanner_layout)
        layout.addWidget(scanner_group)

        # Parameter overrides
        param_group = QGroupBox("Parameters")
        param_layout = QVBoxLayout()

        risk_row = QHBoxLayout()
        risk_row.addWidget(QLabel("Risk %:"))
        self.risk_spin = QDoubleSpinBox()
        self.risk_spin.setRange(0.05, 2.0)
        self.risk_spin.setValue(self.config.sizing.risk_per_trade_pct)
        self.risk_spin.setSingleStep(0.05)
        self.risk_spin.setDecimals(2)
        self.risk_spin.setSuffix(" %")
        risk_row.addWidget(self.risk_spin)
        param_layout.addLayout(risk_row)

        lookback_row = QHBoxLayout()
        lookback_row.addWidget(QLabel("Lookback:"))
        self.lookback_spin = QSpinBox()
        self.lookback_spin.setRange(10, 100)
        self.lookback_spin.setValue(self.config.breakout.lookback)
        lookback_row.addWidget(self.lookback_spin)
        param_layout.addLayout(lookback_row)

        slip_row = QHBoxLayout()
        slip_row.addWidget(QLabel("Slippage %:"))
        self.slippage_spin = QDoubleSpinBox()
        self.slippage_spin.setRange(0.0, 1.0)
        self.slippage_spin.setValue(0.05)
        self.slippage_spin.setSingleStep(0.01)
        self.slippage_spin.setDecimals(2)
        self.slippage_spin.setSuffix(" %")
        slip_row.addWidget(self.slippage_spin)
        param_layout.addLayout(slip_row)

        param_group.setLayout(param_layout)
        layout.addWidget(param_group)

        # Run / Cancel
        btn_layout = QHBoxLayout()
        self.run_btn = QPushButton("Run Backtest")
        self.run_btn.setObjectName("start_button")
        btn_layout.addWidget(self.run_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("stop_button")
        self.cancel_btn.setEnabled(False)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        # Phase indicator
        self.phase_label = QLabel("")
        self.phase_label.setAlignment(Qt.AlignCenter)
        self.phase_label.setStyleSheet("color: #8888cc; font-size: 11px;")
        layout.addWidget(self.phase_label)

        layout.addStretch()
        self._sync_universe_inputs()
        return panel

    def _connect_signals(self):
        self.run_btn.clicked.connect(self._on_run)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.auto_universe_cb.toggled.connect(self._sync_universe_inputs)
        self.use_computed_scanners_cb.toggled.connect(self._sync_universe_inputs)
        self.use_scanner_history_cb.toggled.connect(self._sync_universe_inputs)
        # Chat signals
        self.chat_panel.user_message_sent.connect(self._on_chat_message)
        self.chat_panel.apply_to_live.connect(self._on_apply_to_live)

    @Slot()
    def _sync_universe_inputs(self, *_args):
        use_history = self.use_scanner_history_cb.isChecked()
        use_auto = self.auto_universe_cb.isChecked()
        self.symbols_input.setEnabled(not use_history and not use_auto)
        self.auto_universe_limit_spin.setEnabled(use_auto and not use_history)
        self.scanner_dir_input.setEnabled(use_history)
        self.scanner_carry_spin.setEnabled(use_history)

    # ── Backtest slots ──

    @Slot()
    def _on_run(self):
        runtime_config = self._working_config or self.config
        strategy_type = "rule_based"
        strategy_spec_path = getattr(
            runtime_config.strategy_runtime, "active_strategy_spec_path", ""
        ).strip()

        if not strategy_spec_path:
            QMessageBox.warning(
                self,
                "Missing Strategy Spec",
                "No strategy spec path is configured. Set active_strategy_spec_path in config.yaml.",
            )
            return

        bt_config = BacktestConfig(
            start_date=self.start_date.date().toString("yyyy-MM-dd"),
            end_date=self.end_date.date().toString("yyyy-MM-dd"),
            starting_capital=self.capital_spin.value(),
            symbols=[],
            strategy_type=strategy_type,
            strategy_spec_path=strategy_spec_path,
            auto_us_universe=True,
            auto_universe_max_symbols=self.auto_universe_limit_spin.value(),
            use_computed_scanners=False,
            use_scanner_history=False,
            scanner_names=[],
            scanner_history_dir="",
            scanner_carry_forward_days=0,
            benchmark=self.benchmark_input.text().strip().upper(),
            slippage_pct=self.slippage_spin.value(),
        )

        config = load_config("config.yaml")
        config.sizing.risk_per_trade_pct = self.risk_spin.value()
        config.breakout.lookback = self.lookback_spin.value()

        # Store for chat re-runs
        self._bt_config = bt_config
        self._working_config = copy.deepcopy(config)
        self._accumulated_changes = {}
        self._pending_strategy_spec = None
        self._conversation_history = []

        self.worker = BacktestWorker(config, bt_config)
        self.worker.data_progress.connect(self._on_data_progress)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.data_fetched.connect(self._on_data_cached)
        self.worker.start()

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.phase_label.setText("Downloading historical data...")
        self.status_bar.showMessage("Backtest running...")

    @Slot()
    def _on_cancel(self):
        if self._rerun_worker:
            self._rerun_worker.cancel()
        if self.worker:
            self.worker.cancel()
        self.status_bar.showMessage("Cancelling...")

    @Slot(int, int, str)
    def _on_data_progress(self, current: int, total: int, symbol: str):
        self.phase_label.setText(f"Fetching data: {symbol} ({current}/{total})")
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, date_str: str):
        self.phase_label.setText(f"Simulating: {date_str}")
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    @Slot(dict)
    def _on_data_cached(self, data: dict):
        """Cache the fetched data for instant re-runs."""
        self._cached_data = data

    @Slot(dict)
    def _on_finished(self, results: dict):
        self._results = results
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.phase_label.setText("")
        self.chat_panel.set_input_enabled(True)

        if "error" in results:
            self.status_bar.showMessage(f"Error: {results['error']}")
            return

        run_record = self._record_backtest_run(results, run_type="full")
        if run_record:
            self._last_run_id = run_record.get("run_id")
            self._last_run_snapshot_path = run_record.get("snapshot_path")

        self.status_bar.showMessage(
            f"Complete: {results['total_trades']} trades, "
            f"Return: {results.get('total_return_pct', 0):.1f}%, "
            f"Sharpe: {results.get('sharpe', 0):.2f}"
        )

        self._display_statistics(results)
        self._display_charts(results)
        self._display_trades(results)

        if self._accumulated_changes or self._pending_strategy_spec:
            self.chat_panel.set_live_button_enabled(True)

        # Enable chat if API key is configured
        if self.config.ai_chat.anthropic_api_key:
            self.chat_panel.add_message(
                MessageRole.SYSTEM,
                "Backtest complete. Ask me about the results or suggest parameter changes."
            )
        if self._last_run_id and self._last_run_snapshot_path:
            self.chat_panel.add_message(
                MessageRole.SYSTEM,
                f"Run ID: {self._last_run_id} (snapshot: {self._last_run_snapshot_path})",
            )

    @Slot(str)
    def _on_error(self, error_msg: str):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.phase_label.setText("")
        self.chat_panel.set_input_enabled(True)
        self.status_bar.showMessage(f"Error: {error_msg}")
        QMessageBox.critical(self, "Backtest Error", error_msg)

    # ── Display helpers ──

    def _display_statistics(self, results: dict):
        metrics = [
            ("Starting Capital", f"${results['starting_capital']:,.0f}"),
            ("Final Equity", f"${results['final_equity']:,.2f}"),
            ("Total Return", f"{results['total_return_pct']:+.1f}%"),
            ("CAGR", f"{results['cagr']:.2f}%"),
            ("Max Drawdown", f"{results['max_drawdown']:.1f}%"),
            ("Sharpe Ratio", f"{results['sharpe']:.2f}"),
            ("Sortino Ratio", f"{results['sortino']:.2f}"),
            ("Calmar Ratio", f"{results['calmar']:.2f}"),
            ("", ""),
            ("Total Trades", str(results["total_trades"])),
            ("Win Rate", f"{results['win_rate']:.1f}%"),
            ("Avg Win (R)", f"{results['avg_win_r']:+.2f}"),
            ("Avg Loss (R)", f"{results['avg_loss_r']:+.2f}"),
            ("Avg R", f"{results['avg_r']:+.2f}"),
            ("Profit Factor", f"{results['profit_factor']:.2f}"),
            ("Expectancy (R)", f"{results['expectancy_r']:+.3f}"),
            ("Gross Profit", f"${results['gross_profit']:,.2f}"),
            ("Gross Loss", f"${results['gross_loss']:,.2f}"),
            ("Max Consec Wins", str(results["max_consec_wins"])),
            ("Max Consec Losses", str(results["max_consec_losses"])),
            ("Avg Holding Days", f"{results['avg_holding_days']:.1f}"),
        ]

        # Regime breakdowns
        for regime, stats in results.get("regime_stats", {}).items():
            metrics.append(("", ""))
            metrics.append((f"{regime} Trades", str(stats["count"])))
            metrics.append((f"{regime} Win Rate", f"{stats['win_rate']:.1%}"))
            metrics.append((f"{regime} Avg R", f"{stats['avg_r']:+.2f}"))
            metrics.append((f"{regime} Total PnL", f"${stats['total_pnl']:,.2f}"))

        self.stats_table.setRowCount(len(metrics))
        for row, (metric, value) in enumerate(metrics):
            name_item = QTableWidgetItem(metric)
            val_item = QTableWidgetItem(value)
            val_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            if value.startswith("+") and "%" in value:
                val_item.setForeground(QColor("#00ff88"))
            elif value.startswith("-") and "%" in value:
                val_item.setForeground(QColor("#ff4444"))

            self.stats_table.setItem(row, 0, name_item)
            self.stats_table.setItem(row, 1, val_item)

    def _display_charts(self, results: dict):
        equity_curve = results.get("equity_curve", [])
        trades = results.get("trades", [])

        if equity_curve:
            self.equity_chart.plot(equity_curve, results["starting_capital"])
        if trades:
            self.dist_chart.plot(trades)
            self.hist_chart.plot(trades)

    def _display_trades(self, results: dict):
        trades = results.get("trades", [])
        self.trades_table.setSortingEnabled(False)
        self.trades_table.setRowCount(len(trades))

        for row, t in enumerate(trades):
            entry_dt = t["entry_date"]
            exit_dt = t["exit_date"]
            items = [
                t["symbol"],
                entry_dt.strftime("%Y-%m-%d") if hasattr(entry_dt, "strftime") else str(entry_dt),
                exit_dt.strftime("%Y-%m-%d") if hasattr(exit_dt, "strftime") else str(exit_dt),
                f"${t['entry_price']:.2f}",
                f"${t['exit_price']:.2f}",
                str(t["shares"]),
                f"${t['pnl_dollars']:+,.2f}",
                f"{t['pnl_r']:+.2f}",
                f"${t['initial_stop']:.2f}",
                t["exit_reason"],
                t["regime_at_entry"],
            ]

            for col, val in enumerate(items):
                item = QTableWidgetItem(val)
                if col == 6:
                    item.setForeground(QColor("#00ff88" if t["pnl_dollars"] > 0 else "#ff4444"))
                elif col == 7:
                    item.setForeground(QColor("#00ff88" if t["pnl_r"] > 0 else "#ff4444"))
                self.trades_table.setItem(row, col, item)

        self.trades_table.setSortingEnabled(True)

    # ── Chat integration ──

    @Slot(str)
    def _on_chat_message(self, text: str):
        """Handle user message from chat panel."""
        api_key = self.config.ai_chat.anthropic_api_key
        if not api_key:
            self.chat_panel.add_message(
                MessageRole.SYSTEM,
                "No API key configured. Add your Anthropic API key to "
                "config.yaml under ai_chat.anthropic_api_key"
            )
            return

        if not self._ensure_chat_context_from_inputs():
            return

        # Add to conversation history
        self._conversation_history.append({"role": "user", "content": text})
        if len(self._conversation_history) > 20:
            self._conversation_history = self._conversation_history[-20:]

        # Build system prompt with current state
        current_config = self._working_config or self.config
        bt_summary = self._get_bt_summary()
        system_prompt = build_system_prompt(current_config, self._results, bt_summary)

        # Show thinking indicator
        self.chat_panel.add_message(MessageRole.SYSTEM, "Thinking...")
        self.chat_panel.set_input_enabled(False)

        # Start Claude worker
        self._claude_worker = ClaudeWorker(
            api_key, list(self._conversation_history), system_prompt
        )
        self._claude_worker.response_ready.connect(self._on_claude_response)
        self._claude_worker.error.connect(self._on_claude_error)
        self._claude_worker.start()

    @Slot(str)
    def _on_claude_response(self, response_text: str):
        """Handle Claude's response."""
        self.chat_panel.remove_last_message()
        self.chat_panel.set_input_enabled(True)

        self._conversation_history.append({"role": "assistant", "content": response_text})

        proposed = parse_proposed_changes(response_text) or {}
        strategy_spec = parse_proposed_strategy_spec(response_text)
        proposal_payload = dict(proposed)
        if strategy_spec:
            proposal_payload["__strategy_spec__"] = strategy_spec
        display_text = strip_json_block(response_text)

        current_config = self._working_config or self.config
        current_values = get_tunable_params(current_config)

        card = self.chat_panel.add_message(
            MessageRole.ASSISTANT,
            display_text,
            proposed_changes=(proposal_payload if proposal_payload else None),
            current_config_values=current_values,
        )

        if card:
            card.applied.connect(self._on_changes_approved)
            card.rejected.connect(lambda c=card: c.set_resolved(False))

    @Slot(str)
    def _on_claude_error(self, error_msg: str):
        """Handle Claude API error."""
        self.chat_panel.remove_last_message()
        self.chat_panel.set_input_enabled(True)
        self.chat_panel.add_message(MessageRole.SYSTEM, f"API Error: {error_msg}")

    @Slot(dict)
    def _on_changes_approved(self, changes: dict):
        """Apply proposed changes and re-run backtest with cached data."""
        if not self._ensure_chat_context_from_inputs():
            return

        # Resolve the proposal card
        sender = self.sender()
        if sender:
            sender.set_resolved(True)

        strategy_spec = changes.get("__strategy_spec__")
        config_changes = {k: v for k, v in changes.items() if not k.startswith("__")}

        # Apply changes to working config
        current_config = self._working_config or copy.deepcopy(self.config)
        if config_changes:
            self._working_config = apply_changes_to_config(current_config, config_changes)
            self._accumulated_changes.update(config_changes)
        else:
            self._working_config = current_config

        # Sync GUI spinners
        self._sync_spinners_from_config(self._working_config)

        if strategy_spec:
            try:
                symbols = symbols_from_strategy_spec(strategy_spec)
                auto_universe_requested = strategy_spec_uses_auto_us_universe(strategy_spec)
                if not symbols and not auto_universe_requested:
                    self.chat_panel.add_message(
                        MessageRole.SYSTEM,
                        "Strategy spec needs at least one ticker symbol or ALL_US token.",
                    )
                    return

                working_spec_path = str(Path("strategy_specs") / "working_strategy.json")
                save_strategy_spec(strategy_spec, working_spec_path)

                live_spec_path = self._working_config.strategy_runtime.active_strategy_spec_path
                self._pending_strategy_spec = strategy_spec
                self._bt_config.strategy_type = "rule_based"
                self._bt_config.strategy_spec = strategy_spec
                self._bt_config.strategy_spec_path = working_spec_path
                self._bt_config.symbols = symbols
                self._bt_config.auto_us_universe = auto_universe_requested
                self._bt_config.use_computed_scanners = False
                self._bt_config.use_scanner_history = False

                # Track intended live config updates for diff/apply.
                self._working_config.strategy_runtime.active_backtest_strategy = "rule_based"
                self._working_config.strategy_runtime.active_live_strategy = "rule_based"
                self._working_config.strategy_runtime.active_strategy_spec_path = live_spec_path
                self._accumulated_changes.update(
                    {
                        "strategy_runtime.active_backtest_strategy": "rule_based",
                        "strategy_runtime.active_live_strategy": "rule_based",
                        "strategy_runtime.active_strategy_spec_path": live_spec_path,
                    }
                )

            except Exception as exc:
                self.chat_panel.add_message(
                    MessageRole.SYSTEM,
                    f"Could not apply strategy spec: {exc}",
                )
                return

            # Strategy shape/universe changed: run a full backtest (with data refresh).
            self.chat_panel.add_message(
                MessageRole.SYSTEM,
                "Re-running full backtest with new AI strategy...",
            )
            self.chat_panel.set_input_enabled(False)
            self._start_full_backtest(
                self._working_config,
                self._bt_config,
                "Downloading historical data for AI strategy...",
            )
            return

        if not self._cached_data:
            self.chat_panel.add_message(
                MessageRole.SYSTEM,
                "No cached data yet. Running a full backtest with current settings...",
            )
            self.chat_panel.set_input_enabled(False)
            self._start_full_backtest(
                self._working_config,
                self._bt_config,
                "Downloading historical data...",
            )
            return

        # Show status
        self.chat_panel.add_message(MessageRole.SYSTEM, "Re-running backtest with new parameters...")
        self.chat_panel.set_input_enabled(False)

        # Start re-run with cached data (no download)
        self._rerun_worker = ParameterRerunWorker(
            self._working_config, self._bt_config, self._cached_data
        )
        self._rerun_worker.progress.connect(self._on_progress)
        self._rerun_worker.finished.connect(self._on_rerun_finished)
        self._rerun_worker.error.connect(self._on_rerun_error)
        self._rerun_worker.start()

        self.progress_bar.setVisible(True)
        self.phase_label.setText("Re-running with new parameters...")

    @Slot(dict)
    def _on_rerun_finished(self, results: dict):
        """Handle re-run completion."""
        self._results = results
        self.progress_bar.setVisible(False)
        self.phase_label.setText("")
        self.chat_panel.set_input_enabled(True)

        if "error" in results:
            self.chat_panel.add_message(MessageRole.SYSTEM, f"Re-run error: {results['error']}")
            return

        run_record = self._record_backtest_run(results, run_type="rerun")
        if run_record:
            self._last_run_id = run_record.get("run_id")
            self._last_run_snapshot_path = run_record.get("snapshot_path")

        self._display_statistics(results)
        self._display_charts(results)
        self._display_trades(results)

        summary = (
            f"Re-run complete: {results['total_trades']} trades, "
            f"Return: {results.get('total_return_pct', 0):+.1f}%, "
            f"Sharpe: {results.get('sharpe', 0):.2f}, "
            f"Max DD: {results.get('max_drawdown', 0):.1f}%, "
            f"Win Rate: {results.get('win_rate', 0):.1f}%"
        )
        if self._last_run_id:
            summary += f", Run ID: {self._last_run_id}"
        self.chat_panel.add_message(MessageRole.SYSTEM, summary)
        self.status_bar.showMessage(summary)

        if self._accumulated_changes or self._pending_strategy_spec:
            self.chat_panel.set_live_button_enabled(True)

    @Slot(str)
    def _on_rerun_error(self, error_msg: str):
        """Handle re-run error."""
        self.progress_bar.setVisible(False)
        self.phase_label.setText("")
        self.chat_panel.set_input_enabled(True)
        self.chat_panel.add_message(MessageRole.SYSTEM, f"Re-run error: {error_msg}")

    @Slot()
    def _on_apply_to_live(self):
        """Show diff dialog and save changes to config.yaml."""
        if not self._working_config:
            return
        if not self._accumulated_changes and not self._pending_strategy_spec:
            return

        original = load_config("config.yaml")
        original_values = get_tunable_params(original)

        diff = {}
        for path, new_val in self._accumulated_changes.items():
            old_val = original_values.get(path, self._get_config_value_by_path(original, path))
            diff[path] = (old_val, new_val)

        if self._pending_strategy_spec:
            old_name = "(none)"
            old_path = Path(original.strategy_runtime.active_strategy_spec_path)
            if old_path.exists():
                try:
                    old_name = json.loads(old_path.read_text(encoding="utf-8")).get("name", old_name)
                except Exception:
                    pass
            new_name = str(self._pending_strategy_spec.get("name", "AI Strategy")).strip() or "AI Strategy"
            diff["strategy_spec.name"] = (old_name, new_name)

        dialog = LiveDiffDialog(diff, self)
        if dialog.exec() == dialog.Accepted:
            if self._pending_strategy_spec:
                spec_path = self._working_config.strategy_runtime.active_strategy_spec_path
                save_strategy_spec(self._pending_strategy_spec, spec_path)
            save_config(self._working_config, "config.yaml")
            self.config = copy.deepcopy(self._working_config)
            self.chat_panel.add_message(
                MessageRole.SYSTEM,
                "Changes saved to config.yaml. They will take effect on next bot start."
            )
            self.chat_panel.set_live_button_enabled(False)
            self._accumulated_changes = {}
            self._pending_strategy_spec = None

    # ── Helpers ──

    def _sync_spinners_from_config(self, config: Config):
        """Update GUI spinners to reflect config changes (without triggering signals)."""
        self.risk_spin.blockSignals(True)
        self.risk_spin.setValue(config.sizing.risk_per_trade_pct)
        self.risk_spin.blockSignals(False)

        self.lookback_spin.blockSignals(True)
        self.lookback_spin.setValue(config.breakout.lookback)
        self.lookback_spin.blockSignals(False)

    def _ensure_chat_context_from_inputs(self) -> bool:
        """Initialize chat/backtest context from current UI controls when missing."""
        if self._working_config is None:
            config = load_config("config.yaml")
            config.sizing.risk_per_trade_pct = self.risk_spin.value()
            config.breakout.lookback = self.lookback_spin.value()
            self._working_config = copy.deepcopy(config)

        if self._bt_config is not None:
            return True

        runtime_config = self._working_config or self.config
        strategy_type = "rule_based"
        strategy_spec_path = getattr(
            runtime_config.strategy_runtime, "active_strategy_spec_path", ""
        ).strip()

        self._bt_config = BacktestConfig(
            start_date=self.start_date.date().toString("yyyy-MM-dd"),
            end_date=self.end_date.date().toString("yyyy-MM-dd"),
            starting_capital=self.capital_spin.value(),
            symbols=[],
            strategy_type=strategy_type,
            strategy_spec_path=strategy_spec_path,
            auto_us_universe=True,
            auto_universe_max_symbols=self.auto_universe_limit_spin.value(),
            use_computed_scanners=False,
            use_scanner_history=False,
            scanner_names=[],
            scanner_history_dir="",
            scanner_carry_forward_days=0,
            benchmark=self.benchmark_input.text().strip().upper(),
            slippage_pct=self.slippage_spin.value(),
        )
        return True

    def _start_full_backtest(self, config: Config, bt_config: BacktestConfig, phase_text: str):
        self.worker = BacktestWorker(config, bt_config)
        self.worker.data_progress.connect(self._on_data_progress)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.data_fetched.connect(self._on_data_cached)
        self.worker.start()

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.phase_label.setText(phase_text)
        self.status_bar.showMessage("Backtest running...")

    def _record_backtest_run(self, results: dict, run_type: str) -> Optional[dict]:
        """Persist run summary plus immutable replay snapshot for exact strategy recreation."""
        bt = self._bt_config
        if not bt or "error" in results:
            return None

        spec = self._load_strategy_spec_for_logging(bt)
        strategy_type = (bt.strategy_type or "").strip().lower()
        runtime_config = self._working_config or self.config
        runtime_snapshot = self._snapshot_runtime_backtest_config(runtime_config)
        bt_snapshot = self._snapshot_backtest_config(bt)
        results_summary = self._snapshot_results(results)

        fingerprint_payload = {
            "strategy_type": strategy_type,
            "strategy_spec": spec,
            "backtest_config": bt_snapshot,
            "runtime_config": runtime_snapshot,
            "results_summary": results_summary,
        }
        fingerprint_sha256 = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        timestamp = datetime.now(timezone.utc)
        run_id = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{fingerprint_sha256[:12]}"

        strategy_payload = {
            "strategy_type": bt.strategy_type,
            "strategy_spec_path": bt.strategy_spec_path,
        }
        frozen_spec_path = ""
        if strategy_type == "rule_based" and isinstance(spec, dict):
            frozen_spec_file = Path("strategy_specs") / "archived_runs" / f"{run_id}.json"
            save_strategy_spec(spec, str(frozen_spec_file))
            frozen_spec_path = str(frozen_spec_file).replace("\\", "/")
            strategy_payload.update(
                {
                    "name": spec.get("name"),
                    "symbols": spec.get("symbols"),
                    "entry_rule": spec.get("entry_rule"),
                    "entry_rule_long": spec.get("entry_rule_long"),
                    "entry_rule_short": spec.get("entry_rule_short"),
                    "exit_rule": spec.get("exit_rule"),
                    "entry_price_field": spec.get("entry_price_field"),
                    "position_size_mode": spec.get("position_size_mode"),
                    "risk_per_trade_pct": spec.get("risk_per_trade_pct"),
                    "position_size_pct": spec.get("position_size_pct"),
                    "max_positions": spec.get("max_positions"),
                    "stop_loss_pct": spec.get("stop_loss_pct"),
                    "take_profit_pct": spec.get("take_profit_pct"),
                    "max_holding_days": spec.get("max_holding_days"),
                    "frozen_spec_path": frozen_spec_path,
                    "spec_hash_sha256": hashlib.sha256(
                        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                }
            )

        snapshot_payload = {
            "version": 1,
            "run_id": run_id,
            "timestamp_utc": timestamp.isoformat(),
            "run_type": run_type,
            "fingerprint_sha256": fingerprint_sha256,
            "backtest_config": bt_snapshot,
            "runtime_config": runtime_snapshot,
            "strategy": {
                "strategy_type": bt.strategy_type,
                "strategy_spec_path": bt.strategy_spec_path,
                "frozen_spec_path": frozen_spec_path,
                "full_spec": spec if isinstance(spec, dict) else None,
            },
            "results": results_summary,
        }

        payload = {
            "timestamp_utc": timestamp.isoformat(),
            "run_id": run_id,
            "run_type": run_type,
            "start_date": bt.start_date,
            "end_date": bt.end_date,
            "starting_capital": bt.starting_capital,
            "benchmark": bt.benchmark,
            "slippage_pct": bt.slippage_pct,
            "fingerprint_sha256": fingerprint_sha256,
            "universe": {
                "auto_us_universe": bt.auto_us_universe,
                "auto_universe_max_symbols": bt.auto_universe_max_symbols,
                "use_computed_scanners": bt.use_computed_scanners,
                "use_scanner_history": bt.use_scanner_history,
                "scanner_names": bt.scanner_names,
                "symbols_count": len(bt.symbols or []),
                "symbols_preview": (bt.symbols or [])[:25],
            },
            "results": results_summary,
            "strategy": strategy_payload,
        }

        try:
            path = Path("backtest_runs.jsonl")
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload) + "\n")

            snapshot_dir = Path("backtest_runs")
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = snapshot_dir / f"{run_id}.json"
            snapshot_path.write_text(
                json.dumps(snapshot_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            snapshot_path_text = str(snapshot_path).replace("\\", "/")

            profitable_spec_path_text = ""
            if (
                strategy_type == "rule_based"
                and isinstance(spec, dict)
                and self._is_profitable_backtest(results_summary)
            ):
                profitable_dir = Path("strategy_specs") / "profitable_runs"
                profitable_dir.mkdir(parents=True, exist_ok=True)
                profitable_spec_file = profitable_dir / f"{run_id}.json"
                save_strategy_spec(spec, str(profitable_spec_file))
                profitable_spec_path_text = str(profitable_spec_file).replace("\\", "/")

                profitable_manifest_row = {
                    "timestamp_utc": timestamp.isoformat(),
                    "run_id": run_id,
                    "snapshot_path": snapshot_path_text,
                    "profitable_spec_path": profitable_spec_path_text,
                    "results": results_summary,
                    "strategy": {
                        "name": spec.get("name"),
                        "symbols": spec.get("symbols"),
                        "spec_hash_sha256": strategy_payload.get("spec_hash_sha256"),
                    },
                }
                profitable_manifest = Path("profitable_backtests.jsonl")
                with profitable_manifest.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(profitable_manifest_row) + "\n")

                payload["strategy"]["profitable_spec_path"] = profitable_spec_path_text
                snapshot_payload["strategy"]["profitable_spec_path"] = profitable_spec_path_text
                snapshot_path.write_text(
                    json.dumps(snapshot_payload, indent=2, sort_keys=True),
                    encoding="utf-8",
                )

            logger.info(
                "Backtest run recorded: id=%s type=%s trades=%s cagr=%.2f%% file=%s",
                run_id,
                run_type,
                payload["results"]["total_trades"],
                float(payload["results"]["cagr"] or 0.0),
                str(path),
            )
            return {
                "run_id": run_id,
                "summary_path": str(path).replace("\\", "/"),
                "snapshot_path": snapshot_path_text,
                "profitable_spec_path": profitable_spec_path_text,
            }
        except Exception:
            logger.warning("Failed to write backtest run record", exc_info=True)
            return None

    @staticmethod
    def _load_strategy_spec_for_logging(bt: BacktestConfig) -> Optional[dict]:
        if isinstance(bt.strategy_spec, dict):
            return bt.strategy_spec
        path_text = (bt.strategy_spec_path or "").strip()
        if not path_text:
            return None
        path = Path(path_text)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _snapshot_backtest_config(bt: BacktestConfig) -> dict:
        return {
            "start_date": bt.start_date,
            "end_date": bt.end_date,
            "starting_capital": bt.starting_capital,
            "symbols": list(bt.symbols or []),
            "strategy_type": bt.strategy_type,
            "strategy_spec_path": bt.strategy_spec_path,
            "auto_us_universe": bt.auto_us_universe,
            "auto_universe_max_symbols": bt.auto_universe_max_symbols,
            "use_computed_scanners": bt.use_computed_scanners,
            "use_scanner_history": bt.use_scanner_history,
            "scanner_names": list(bt.scanner_names or []),
            "scanner_history_dir": bt.scanner_history_dir,
            "scanner_carry_forward_days": bt.scanner_carry_forward_days,
            "benchmark": bt.benchmark,
            "slippage_pct": bt.slippage_pct,
            "commission_per_share": bt.commission_per_share,
            "min_daily_vol_ratio": bt.min_daily_vol_ratio,
            "breakeven_before_stop_on_same_bar": bt.breakeven_before_stop_on_same_bar,
            "skip_spread_check": bt.skip_spread_check,
        }

    @staticmethod
    def _snapshot_runtime_backtest_config(config: Config) -> dict:
        return {
            "universe": dataclasses.asdict(config.universe),
            "eligibility": dataclasses.asdict(config.eligibility),
            "trend_quality": dataclasses.asdict(config.trend_quality),
            "contraction": dataclasses.asdict(config.contraction),
            "regime": dataclasses.asdict(config.regime),
            "breakout": dataclasses.asdict(config.breakout),
            "pullback": dataclasses.asdict(config.pullback),
            "stop": dataclasses.asdict(config.stop),
            "sizing": dataclasses.asdict(config.sizing),
            "eod_exit": dataclasses.asdict(config.eod_exit),
            "safety": dataclasses.asdict(config.safety),
            "strategy_runtime": dataclasses.asdict(config.strategy_runtime),
        }

    @staticmethod
    def _snapshot_results(results: dict) -> dict:
        return {
            "total_trades": results.get("total_trades"),
            "cagr": results.get("cagr"),
            "total_return_pct": results.get("total_return_pct"),
            "sharpe": results.get("sharpe"),
            "max_drawdown": results.get("max_drawdown"),
            "win_rate": results.get("win_rate"),
            "profit_factor": results.get("profit_factor"),
            "final_equity": results.get("final_equity"),
            "avg_r": results.get("avg_r"),
            "expectancy_r": results.get("expectancy_r"),
        }

    @staticmethod
    def _is_profitable_backtest(results_summary: dict) -> bool:
        trades = int(results_summary.get("total_trades") or 0)
        total_return = float(results_summary.get("total_return_pct") or 0.0)
        cagr = float(results_summary.get("cagr") or 0.0)
        final_equity = results_summary.get("final_equity")
        if final_equity is not None and float(final_equity) <= 0:
            return False
        return trades > 0 and total_return > 0 and cagr > 0

    def _get_bt_summary(self) -> str:
        """Build a backtest setup summary string for the system prompt."""
        if not self._bt_config:
            return "No backtest configured yet."
        bt = self._bt_config
        strategy_type = (bt.strategy_type or "rule_based").strip().lower()

        if strategy_type == "rule_based":
            strategy_name = "rule_based"
            intraday_text = ""
            universe_text = ""
            if isinstance(bt.strategy_spec, dict):
                strategy_name = bt.strategy_spec.get("name", strategy_name)
                if bt.strategy_spec.get("use_intraday_vwap_stop"):
                    intraday_text = f", intraday={bt.strategy_spec.get('intraday_interval', '5m')}"
                if strategy_spec_uses_auto_us_universe(bt.strategy_spec):
                    limit_text = "all symbols" if bt.auto_universe_max_symbols <= 0 else (
                        f"{bt.auto_universe_max_symbols} symbols"
                    )
                    universe_text = f", universe=auto US ({limit_text})"
            symbols_text = (
                f"Rule-based strategy '{strategy_name}' "
                f"from {bt.strategy_spec_path or 'inline spec'}{intraday_text}{universe_text}"
            )
        elif bt.use_scanner_history:
            symbols_text = (
                f"Scanner history ({', '.join(bt.scanner_names)}) from "
                f"{bt.scanner_history_dir}, carry-forward={bt.scanner_carry_forward_days}d"
            )
        elif bt.auto_us_universe:
            limit_text = "all symbols" if bt.auto_universe_max_symbols <= 0 else (
                f"{bt.auto_universe_max_symbols} symbols"
            )
            if bt.use_computed_scanners:
                symbols_text = (
                    f"Computed scanners ({', '.join(bt.scanner_names)}) "
                    f"over auto US market universe ({limit_text})"
                )
            else:
                symbols_text = f"Auto US market universe ({limit_text})"
        elif bt.use_computed_scanners:
            base_symbols = ", ".join(bt.symbols[:10])
            if len(bt.symbols) > 10:
                base_symbols += f"... ({len(bt.symbols)} total)"
            symbols_text = (
                f"Computed scanners ({', '.join(bt.scanner_names)}) "
                f"over base universe: {base_symbols}"
            )
        else:
            symbols_text = ", ".join(bt.symbols[:10])
            if len(bt.symbols) > 10:
                symbols_text += f"... ({len(bt.symbols)} total)"
        return (
            f"Date range: {bt.start_date} to {bt.end_date}\n"
            f"Starting capital: ${bt.starting_capital:,.0f}\n"
            f"Universe: {symbols_text}\n"
            f"Benchmark: {bt.benchmark}\n"
            f"Slippage: {bt.slippage_pct}%"
        )

    @staticmethod
    def _get_config_value_by_path(config: Config, path: str):
        parts = path.split(".")
        if len(parts) != 2:
            return "?"
        section, field_name = parts
        if not hasattr(config, section):
            return "?"
        sub = getattr(config, section)
        return getattr(sub, field_name, "?")
