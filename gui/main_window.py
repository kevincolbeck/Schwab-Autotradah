"""Main application window assembling all GUI panels."""

import logging
from pathlib import Path
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QGroupBox, QStatusBar, QMessageBox, QTabWidget,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, Slot
from PySide6.QtGui import QShortcut, QKeySequence, QIcon

from core.constants import Regime
from core.engine import TradingEngine
from core.scheduler import TradingScheduler
from gui.styles import DARK_THEME
from gui.controls import ControlPanel
from gui.dashboard import RegimeDisplay, PositionsTable, OrdersTable, PnLDisplay
from gui.log_viewer import LogViewer
from gui.analytics import AnalyticsTab
from gui.trade_history import TradeHistoryTab
from gui.risk_monitor import RiskMonitorTab
from gui.signal_monitor import SignalMonitorTab
from gui.ai_tab import AITab

logger = logging.getLogger(__name__)


class EngineThread(QThread):
    """Runs the trading engine scheduler in a background thread."""
    error_occurred = Signal(str)

    def __init__(self, scheduler: TradingScheduler):
        super().__init__()
        self.scheduler = scheduler

    def run(self):
        try:
            self.scheduler.start()
            self.exec()
        except Exception as e:
            self.error_occurred.emit(str(e))

    def stop(self):
        self.scheduler.stop()
        self.quit()
        self.wait(5000)


class MainWindow(QMainWindow):
    """Main application window for the Schwab Momentum Bot."""

    def __init__(self, engine: TradingEngine, config):
        super().__init__()
        self.engine = engine
        self.config = config
        self.scheduler = TradingScheduler(engine, config)
        self.engine_thread: EngineThread = None

        self.setWindowTitle("Schwab Live Trader")
        self.setMinimumSize(1400, 850)
        self.setStyleSheet(DARK_THEME)

        # Set window icon
        icon_path = Path(__file__).parent.parent / "assets" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._init_ui()
        self._connect_signals()
        self._setup_shortcuts()
        self._setup_refresh_timer()

        # Set initial states
        self.controls.shadow_cb.setChecked(config.app.shadow_mode)
        self.controls.risk_spin.setValue(config.sizing.risk_per_trade_pct)
        self.controls.manage_exits_cb.setChecked(config.app.manage_exits_while_off)

        # Register engine callbacks
        self.engine.set_callbacks(
            on_regime_change=self._on_regime_change,
            on_trade_update=self._on_trade_update,
            on_log_event=self._on_log_event,
            on_pnl_update=self._on_pnl_update,
        )

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # Left panel: controls
        self.controls = ControlPanel()
        self.controls.setFixedWidth(280)
        main_layout.addWidget(self.controls)

        # Right panel: tabbed dashboard
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #3a3a5c;
                border-radius: 4px;
                background-color: #1a1a2e;
            }
            QTabBar::tab {
                background-color: #16162b;
                color: #888;
                border: 1px solid #3a3a5c;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 16px;
                margin-right: 2px;
                font-size: 12px;
            }
            QTabBar::tab:selected {
                background-color: #1a1a2e;
                color: #e0e0e0;
                font-weight: bold;
                border-bottom: 2px solid #4488ff;
            }
            QTabBar::tab:hover {
                background-color: #22224a;
                color: #ccc;
            }
        """)

        # Tab 1: Dashboard (original view)
        dashboard_tab = self._build_dashboard_tab()
        self.tabs.addTab(dashboard_tab, "Dashboard")

        # Tab 2: Performance Analytics
        self.analytics_tab = AnalyticsTab(self.engine)
        self.tabs.addTab(self.analytics_tab, "Performance")

        # Tab 3: Trade History
        self.trade_history_tab = TradeHistoryTab(self.engine)
        self.tabs.addTab(self.trade_history_tab, "Trade History")

        # Tab 4: Risk Monitor
        self.risk_tab = RiskMonitorTab(self.engine, self.config)
        self.tabs.addTab(self.risk_tab, "Risk")

        # Tab 5: Signal Monitor
        self.signal_tab = SignalMonitorTab(self.engine, self.config)
        self.tabs.addTab(self.signal_tab, "Signals")

        # Tab 6: AI Strategist
        self.ai_tab = None
        if self.engine.ai_strategist:
            self.ai_tab = AITab(self.engine.ai_strategist, self.engine)
            self.tabs.addTab(self.ai_tab, "AI Strategist")

        # Refresh tab data when switching
        self.tabs.currentChanged.connect(self._on_tab_changed)

        main_layout.addWidget(self.tabs)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    def _build_dashboard_tab(self) -> QWidget:
        """Build the original dashboard tab with regime, positions, orders, log."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 4, 0, 0)

        right_splitter = QSplitter(Qt.Vertical)

        # Top section: regime + PnL
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)

        self.regime_display = RegimeDisplay()
        top_layout.addWidget(self.regime_display)

        self.pnl_display = PnLDisplay()
        top_layout.addWidget(self.pnl_display)

        right_splitter.addWidget(top_widget)

        # Middle: positions + orders
        tables_widget = QWidget()
        tables_layout = QVBoxLayout(tables_widget)
        tables_layout.setContentsMargins(0, 0, 0, 0)

        pos_group = QGroupBox("Open Positions")
        pos_layout = QVBoxLayout()
        self.positions_table = PositionsTable()
        pos_layout.addWidget(self.positions_table)
        pos_group.setLayout(pos_layout)
        tables_layout.addWidget(pos_group)

        ord_group = QGroupBox("Pending Orders")
        ord_layout = QVBoxLayout()
        self.orders_table = OrdersTable()
        ord_layout.addWidget(self.orders_table)
        ord_group.setLayout(ord_layout)
        tables_layout.addWidget(ord_group)

        right_splitter.addWidget(tables_widget)

        # Bottom: log viewer
        log_group = QGroupBox("Event Log")
        log_layout = QVBoxLayout()
        self.log_viewer = LogViewer()
        log_layout.addWidget(self.log_viewer)
        log_group.setLayout(log_layout)
        right_splitter.addWidget(log_group)

        right_splitter.setSizes([120, 350, 250])
        tab_layout.addWidget(right_splitter)

        return tab

    def _on_tab_changed(self, index: int):
        """Refresh the newly visible tab's data."""
        widget = self.tabs.widget(index)
        if widget == self.analytics_tab:
            self.analytics_tab.refresh()
        elif widget == self.trade_history_tab:
            self.trade_history_tab.refresh()
        elif widget == self.risk_tab:
            self.risk_tab.refresh()
        elif widget == self.signal_tab:
            self.signal_tab.refresh()
        elif self.ai_tab and widget == self.ai_tab:
            self.ai_tab.refresh()

    def _connect_signals(self):
        self.controls.risk_changed.connect(self._on_risk_changed)
        self.controls.kill_switch_toggled.connect(self._on_kill_switch)
        self.controls.manage_exits_toggled.connect(self._on_manage_exits)
        self.controls.start_requested.connect(self._on_start)
        self.controls.stop_requested.connect(self._on_stop)
        self.controls.shadow_mode_toggled.connect(self._on_shadow_mode)
        self.positions_table.reenable_requested.connect(self._on_reenable)

    def _setup_shortcuts(self):
        kill_shortcut = QShortcut(QKeySequence("Ctrl+Shift+K"), self)
        kill_shortcut.activated.connect(self._toggle_kill_switch)

    def _setup_refresh_timer(self):
        """Refresh GUI data every 2 seconds."""
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_display)
        self.refresh_timer.start(2000)

    # --- Slots ---

    @Slot(float)
    def _on_risk_changed(self, value: float):
        self.engine.risk_per_trade_pct = value
        self.status_bar.showMessage(f"Risk per trade: {value:.2f}%", 3000)

    @Slot(bool)
    def _on_kill_switch(self, active: bool):
        if active:
            from data.models import init_db
            session = self.engine.db_session_factory()
            try:
                self.engine.kill_switch.activate(
                    self.engine.order_mgr, session, self.engine.shadow_mode
                )
            finally:
                session.close()
            self.status_bar.showMessage("KILL SWITCH ACTIVATED", 5000)
        else:
            self.engine.kill_switch.deactivate()
            self.status_bar.showMessage("Kill switch deactivated", 3000)

    @Slot(bool)
    def _on_manage_exits(self, checked: bool):
        self.engine.kill_switch.manage_exits = checked

    @Slot()
    def _on_start(self):
        try:
            self.engine.start()
            self.engine_thread = EngineThread(self.scheduler)
            self.engine_thread.error_occurred.connect(self._on_engine_error)
            self.engine_thread.start()
            self.controls.set_engine_running(True)
            self.status_bar.showMessage("Engine started", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start engine: {e}")

    @Slot()
    def _on_stop(self):
        self.engine.stop()
        if self.engine_thread:
            self.engine_thread.stop()
            self.engine_thread = None
        self.controls.set_engine_running(False)
        self.status_bar.showMessage("Engine stopped", 3000)

    @Slot(bool)
    def _on_shadow_mode(self, checked: bool):
        self.engine.shadow_mode = checked
        self.controls.set_mode(checked)
        mode = "Shadow" if checked else "LIVE"
        self.status_bar.showMessage(f"Mode: {mode}", 3000)

    @Slot(str)
    def _on_reenable(self, symbol: str):
        reply = QMessageBox.question(
            self, "Re-enable Bot Management",
            f"Re-enable bot management for {symbol}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.engine.reconciler.reenable_bot_management(symbol)
            self.status_bar.showMessage(f"Bot management re-enabled for {symbol}", 3000)

    def _toggle_kill_switch(self):
        self.controls.kill_btn.toggle()

    @Slot(str)
    def _on_engine_error(self, error: str):
        logger.error(f"Engine thread error: {error}")
        self.status_bar.showMessage(f"Engine error: {error}", 10000)

    # --- Callbacks from engine (may be called from background thread) ---

    def _on_regime_change(self, regime: Regime, details: dict):
        # Use QTimer to safely update GUI from background thread
        QTimer.singleShot(0, lambda: self.regime_display.update_regime(regime, details))

    def _on_trade_update(self):
        QTimer.singleShot(0, self._refresh_display)

    def _on_log_event(self, event_type: str, message: str):
        QTimer.singleShot(0, lambda: self.log_viewer.add_event(event_type, message))

    def _on_pnl_update(self):
        QTimer.singleShot(0, self._refresh_pnl)

    # --- Periodic refresh ---

    def _refresh_display(self):
        """Refresh all dashboard data."""
        try:
            # Positions
            trades = self.engine.get_open_trades()
            self.positions_table.update_positions(trades)

            # Orders
            orders = self.engine.get_pending_orders()
            self.orders_table.update_orders(orders)

            # PnL
            self._refresh_pnl()

            # Equity
            if self.engine._equity > 0:
                self.controls.set_equity(self.engine._equity)

            # Regime
            self.regime_display.update_regime(
                self.engine.current_regime, self.engine._regime_details
            )

        except Exception as e:
            logger.debug(f"Refresh error: {e}")

    def _refresh_pnl(self):
        try:
            pnl = self.engine.get_daily_pnl()
            self.pnl_display.update_pnl(pnl)
        except Exception:
            pass

    def closeEvent(self, event):
        """Handle window close: stop engine gracefully."""
        if self.engine.running:
            reply = QMessageBox.question(
                self, "Confirm Exit",
                "Engine is running. Stop engine and exit?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.No:
                event.ignore()
                return

            self._on_stop()

        event.accept()
