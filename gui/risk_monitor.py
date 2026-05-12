"""Risk Monitor tab - exposure tracking, drawdown, regime history."""

import logging

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QGridLayout, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QScrollArea, QProgressBar,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from gui.charts import MiniLineChart, MiniBarChart

logger = logging.getLogger(__name__)


class RiskGauge(QFrame):
    """Visual gauge showing a risk metric as a progress bar with label."""

    def __init__(self, label: str, max_val: float = 100, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: #16162b;
                border: 1px solid #2a2a4e;
                border-radius: 6px;
                padding: 6px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self.name_label = QLabel(label)
        self.name_label.setStyleSheet("color: #888; font-size: 10px; border: none;")
        header.addWidget(self.name_label)

        self.value_label = QLabel("--")
        self.value_label.setStyleSheet("color: #e0e0e0; font-size: 12px; font-weight: bold; border: none;")
        self.value_label.setAlignment(Qt.AlignRight)
        header.addWidget(self.value_label)
        layout.addLayout(header)

        self.bar = QProgressBar()
        self.bar.setMaximum(int(max_val))
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setStyleSheet("""
            QProgressBar {
                background-color: #1a1a2e;
                border: 1px solid #2a2a4e;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background-color: #4488ff;
                border-radius: 3px;
            }
        """)
        layout.addWidget(self.bar)
        self._max = max_val

    def set_value(self, value: float, text: str = None, color: str = None):
        self.bar.setValue(min(int(value), int(self._max)))
        self.value_label.setText(text or f"{value:.1f}")
        if color:
            self.bar.setStyleSheet(f"""
                QProgressBar {{
                    background-color: #1a1a2e;
                    border: 1px solid #2a2a4e;
                    border-radius: 4px;
                }}
                QProgressBar::chunk {{
                    background-color: {color};
                    border-radius: 3px;
                }}
            """)


class RiskMonitorTab(QWidget):
    """Risk monitoring dashboard: exposure, drawdown, regime, daily limits."""

    def __init__(self, engine, config, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.config = config
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        title = QLabel("Risk Monitor")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #8888cc;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        clayout = QVBoxLayout(content)
        clayout.setSpacing(12)

        # --- Row 1: Risk gauges ---
        gauges_row = QHBoxLayout()
        gauges_row.setSpacing(8)

        self.gauge_exposure = RiskGauge("Portfolio Exposure", 100)
        self.gauge_positions = RiskGauge("Open Positions", 10)
        self.gauge_daily_pnl = RiskGauge("Daily P&L vs Limit", 100)
        self.gauge_drawdown = RiskGauge("Current Drawdown", 100)

        for g in [self.gauge_exposure, self.gauge_positions,
                  self.gauge_daily_pnl, self.gauge_drawdown]:
            gauges_row.addWidget(g)
        clayout.addLayout(gauges_row)

        # --- Row 2: Drawdown chart + Position concentration ---
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        dd_group = QGroupBox("Drawdown Over Time")
        dd_layout = QVBoxLayout(dd_group)
        self.drawdown_chart = MiniLineChart()
        self.drawdown_chart.setMinimumHeight(160)
        dd_layout.addWidget(self.drawdown_chart)
        row2.addWidget(dd_group, stretch=2)

        # Position detail
        pos_group = QGroupBox("Current Positions")
        pos_layout = QVBoxLayout(pos_group)
        self.pos_table = QTableWidget()
        self.pos_table.setColumnCount(5)
        self.pos_table.setHorizontalHeaderLabels([
            "Symbol", "Shares", "Value", "% of Equity", "P&L"
        ])
        self.pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.pos_table.setAlternatingRowColors(True)
        self.pos_table.verticalHeader().setVisible(False)
        pos_layout.addWidget(self.pos_table)
        row2.addWidget(pos_group, stretch=1)

        clayout.addLayout(row2)

        # --- Row 3: Regime history ---
        regime_group = QGroupBox("Regime History")
        regime_layout = QVBoxLayout(regime_group)
        self.regime_table = QTableWidget()
        self.regime_table.setColumnCount(5)
        self.regime_table.setHorizontalHeaderLabels([
            "Time", "Regime", "SPY Close", "SMA10", "SMA20"
        ])
        self.regime_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.regime_table.setAlternatingRowColors(True)
        self.regime_table.verticalHeader().setVisible(False)
        self.regime_table.setMaximumHeight(200)
        regime_layout.addWidget(self.regime_table)
        clayout.addWidget(regime_group)

        # --- Row 4: Daily loss lock history ---
        lock_group = QGroupBox("Daily Loss Lock Events")
        lock_layout = QVBoxLayout(lock_group)
        self.lock_table = QTableWidget()
        self.lock_table.setColumnCount(4)
        self.lock_table.setHorizontalHeaderLabels([
            "Date", "Realized P&L", "Trades Closed", "Regime"
        ])
        self.lock_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.lock_table.setAlternatingRowColors(True)
        self.lock_table.verticalHeader().setVisible(False)
        self.lock_table.setMaximumHeight(150)
        lock_layout.addWidget(self.lock_table)
        clayout.addWidget(lock_group)

        clayout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

    def refresh(self):
        """Refresh all risk data."""
        equity = self.engine._equity or 1.0

        # Open positions
        try:
            trades = self.engine.get_open_trades()
        except Exception:
            trades = []

        # Calculate exposure
        total_value = 0
        self.pos_table.setRowCount(len(trades))
        for row, t in enumerate(trades):
            sym = t.get('symbol', '')
            shares = t.get('shares', 0)
            entry = t.get('entry_price', 0) or 0
            value = shares * entry
            total_value += value
            pct = (value / equity * 100) if equity > 0 else 0

            self.pos_table.setItem(row, 0, QTableWidgetItem(sym))
            self.pos_table.setItem(row, 1, QTableWidgetItem(str(shares)))
            self.pos_table.setItem(row, 2, QTableWidgetItem(f"${value:,.0f}"))
            self.pos_table.setItem(row, 3, QTableWidgetItem(f"{pct:.1f}%"))
            self.pos_table.setItem(row, 4, QTableWidgetItem("--"))

        # Gauges
        exposure_pct = (total_value / equity * 100) if equity > 0 else 0
        exp_color = "#00ff88" if exposure_pct < 50 else "#ffaa00" if exposure_pct < 80 else "#ff4444"
        self.gauge_exposure.set_value(exposure_pct, f"{exposure_pct:.1f}%", exp_color)

        n_pos = len([t for t in trades if t.get('status') != 'PENDING_ENTRY'])
        pos_color = "#00ff88" if n_pos <= 2 else "#ffaa00" if n_pos <= 4 else "#ff4444"
        self.gauge_positions.set_value(n_pos, str(n_pos), pos_color)

        # Daily PnL vs limit
        try:
            daily = self.engine.get_daily_pnl()
            realized = daily.get('realized_pnl', 0)
            locked = daily.get('locked_out', False)
            # Show as percentage of some loss limit (use 2% of equity as reference)
            loss_limit = equity * 0.02
            if realized < 0 and loss_limit > 0:
                loss_pct = abs(realized) / loss_limit * 100
            else:
                loss_pct = 0
            pnl_color = "#ff4444" if locked else "#ffaa00" if loss_pct > 50 else "#00ff88"
            pnl_text = f"${realized:+,.2f}" + (" LOCKED" if locked else "")
            self.gauge_daily_pnl.set_value(min(loss_pct, 100), pnl_text, pnl_color)
        except Exception:
            pass

        # Drawdown from cumulative PnL
        try:
            daily_hist = self.engine.get_daily_pnl_history(90)
            if daily_hist:
                cumulative = []
                running = 0
                peak = 0
                dd_series = []
                labels = []
                for d in daily_hist:
                    running += d['realized_pnl']
                    cumulative.append(running)
                    if running > peak:
                        peak = running
                    dd = peak - running
                    dd_series.append(-dd)
                    labels.append(d['date'][-5:])

                current_dd = abs(dd_series[-1]) if dd_series else 0
                max_dd = max(abs(v) for v in dd_series) if dd_series else 0
                dd_pct = (current_dd / equity * 100) if equity > 0 else 0
                dd_color = "#00ff88" if dd_pct < 2 else "#ffaa00" if dd_pct < 5 else "#ff4444"
                self.gauge_drawdown.set_value(
                    min(dd_pct, 100),
                    f"${current_dd:,.0f} ({dd_pct:.1f}%)",
                    dd_color
                )

                self.drawdown_chart.set_data(
                    dd_series, labels, "Drawdown",
                    color="#ff4444", fill=True, zero_line=True
                )
        except Exception as e:
            logger.debug(f"Drawdown chart error: {e}")

        # Regime history
        try:
            regimes = self.engine.get_regime_history(50)
            self.regime_table.setRowCount(len(regimes))
            regime_colors = {
                'TREND': "#00ff88", 'CHOP': "#ffaa00", 'RISK_OFF': "#ff4444"
            }
            for row, r in enumerate(reversed(regimes)):
                ts = r.get('timestamp')
                ts_str = ts.strftime('%m/%d %H:%M') if ts else "--"
                self.regime_table.setItem(row, 0, QTableWidgetItem(ts_str))

                regime = r.get('regime', '')
                regime_item = QTableWidgetItem(regime)
                regime_item.setForeground(QColor(regime_colors.get(regime, "#888")))
                self.regime_table.setItem(row, 1, regime_item)

                bench = r.get('benchmark_close')
                self.regime_table.setItem(row, 2,
                    QTableWidgetItem(f"${bench:.2f}" if bench else "--"))

                sma10 = r.get('sma10')
                self.regime_table.setItem(row, 3,
                    QTableWidgetItem(f"{sma10:.2f}" if sma10 else "--"))

                sma20 = r.get('sma20')
                self.regime_table.setItem(row, 4,
                    QTableWidgetItem(f"{sma20:.2f}" if sma20 else "--"))
        except Exception as e:
            logger.debug(f"Regime history error: {e}")

        # Daily loss lock events
        try:
            all_daily = self.engine.get_daily_pnl_history(0)
            locked_days = [d for d in all_daily if d.get('locked_out')]
            self.lock_table.setRowCount(len(locked_days))
            for row, d in enumerate(reversed(locked_days)):
                self.lock_table.setItem(row, 0, QTableWidgetItem(d['date']))
                pnl_item = QTableWidgetItem(f"${d['realized_pnl']:+,.2f}")
                pnl_item.setForeground(QColor("#ff4444"))
                self.lock_table.setItem(row, 1, pnl_item)
                self.lock_table.setItem(row, 2, QTableWidgetItem(str(d.get('trades_closed', 0))))
                self.lock_table.setItem(row, 3, QTableWidgetItem(d.get('regime', '') or ''))
        except Exception as e:
            logger.debug(f"Lock history error: {e}")
