"""Performance Analytics tab - Sharpe, Sortino, equity curve, metrics."""

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QGroupBox, QGridLayout, QScrollArea, QFrame,
)
from PySide6.QtCore import Qt

from gui.charts import MiniLineChart, MiniBarChart, DonutChart

logger = logging.getLogger(__name__)

# Time frame options (label, days — 0 means all)
TIMEFRAMES = [
    ("All Time", 0),
    ("1 Week", 7),
    ("1 Month", 30),
    ("3 Months", 90),
    ("6 Months", 180),
    ("1 Year", 365),
]


class MetricCard(QFrame):
    """Small card showing a single metric with label and value."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            QFrame {
                background-color: #16162b;
                border: 1px solid #2a2a4e;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self.label = QLabel(label)
        self.label.setStyleSheet("color: #888; font-size: 10px; border: none;")
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label)

        self.value_label = QLabel("--")
        self.value_label.setStyleSheet("color: #e0e0e0; font-size: 18px; font-weight: bold; border: none;")
        self.value_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_label)

    def set_value(self, text: str, color: str = "#e0e0e0"):
        self.value_label.setText(text)
        self.value_label.setStyleSheet(
            f"color: {color}; font-size: 18px; font-weight: bold; border: none;"
        )


class AnalyticsTab(QWidget):
    """Performance analytics dashboard with metrics, charts, and time frame selector."""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Header: title + time frame selector
        header = QHBoxLayout()
        title = QLabel("Performance Analytics")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #8888cc;")
        header.addWidget(title)
        header.addStretch()

        tf_label = QLabel("Time Frame:")
        tf_label.setStyleSheet("color: #888;")
        header.addWidget(tf_label)

        self.timeframe_combo = QComboBox()
        for label, _ in TIMEFRAMES:
            self.timeframe_combo.addItem(label)
        self.timeframe_combo.setCurrentIndex(0)
        self.timeframe_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        self.timeframe_combo.setStyleSheet("""
            QComboBox {
                background-color: #2a2a4e;
                color: #e0e0e0;
                border: 1px solid #4a4a7c;
                border-radius: 4px;
                padding: 4px 12px;
                min-width: 100px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #2a2a4e;
                color: #e0e0e0;
                selection-background-color: #3a3a6e;
            }
        """)
        header.addWidget(self.timeframe_combo)
        layout.addLayout(header)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setSpacing(12)

        # --- Row 1: Key metrics cards ---
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(8)

        self.card_total_pnl = MetricCard("Total P&L")
        self.card_sharpe = MetricCard("Sharpe Ratio")
        self.card_sortino = MetricCard("Sortino Ratio")
        self.card_win_rate = MetricCard("Win Rate")
        self.card_profit_factor = MetricCard("Profit Factor")
        self.card_max_dd = MetricCard("Max Drawdown")

        for card in [self.card_total_pnl, self.card_sharpe, self.card_sortino,
                     self.card_win_rate, self.card_profit_factor, self.card_max_dd]:
            metrics_row.addWidget(card)

        self.content_layout.addLayout(metrics_row)

        # --- Row 2: Secondary metrics ---
        metrics_row2 = QHBoxLayout()
        metrics_row2.setSpacing(8)

        self.card_calmar = MetricCard("Calmar Ratio")
        self.card_expectancy = MetricCard("Expectancy")
        self.card_avg_r = MetricCard("Avg R-Multiple")
        self.card_total_trades = MetricCard("Total Trades")
        self.card_avg_hold = MetricCard("Avg Hold Time")
        self.card_best_day = MetricCard("Best Day")

        for card in [self.card_calmar, self.card_expectancy, self.card_avg_r,
                     self.card_total_trades, self.card_avg_hold, self.card_best_day]:
            metrics_row2.addWidget(card)

        self.content_layout.addLayout(metrics_row2)

        # --- Row 3: Charts ---
        charts_row = QHBoxLayout()
        charts_row.setSpacing(8)

        # Equity curve
        equity_group = QGroupBox("Cumulative P&L")
        equity_layout = QVBoxLayout(equity_group)
        self.equity_chart = MiniLineChart()
        self.equity_chart.setMinimumHeight(180)
        equity_layout.addWidget(self.equity_chart)
        charts_row.addWidget(equity_group, stretch=3)

        # Win/Loss donut
        donut_group = QGroupBox("Win / Loss")
        donut_layout = QVBoxLayout(donut_group)
        self.win_loss_donut = DonutChart()
        self.win_loss_donut.setMinimumHeight(180)
        donut_layout.addWidget(self.win_loss_donut)
        charts_row.addWidget(donut_group, stretch=1)

        self.content_layout.addLayout(charts_row)

        # --- Row 4: Daily PnL bar chart ---
        daily_group = QGroupBox("Daily P&L")
        daily_layout = QVBoxLayout(daily_group)
        self.daily_pnl_chart = MiniBarChart()
        self.daily_pnl_chart.setMinimumHeight(150)
        daily_layout.addWidget(self.daily_pnl_chart)
        self.content_layout.addWidget(daily_group)

        # --- Row 5: Additional stats ---
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)

        # Streaks & slippage
        extra_group = QGroupBox("Additional Stats")
        extra_grid = QGridLayout(extra_group)
        extra_grid.setSpacing(6)

        self._stat_labels = {}
        stat_names = [
            ("Max Win Streak", "streak_wins"),
            ("Max Loss Streak", "streak_losses"),
            ("Largest Win", "max_win"),
            ("Largest Loss", "max_loss"),
            ("Worst Day", "worst_day"),
            ("Avg Slippage", "avg_slippage"),
            ("Total Slippage", "total_slippage"),
            ("Avg P&L/Trade", "avg_pnl"),
        ]
        for i, (display_name, key) in enumerate(stat_names):
            row, col = divmod(i, 4)
            name_lbl = QLabel(display_name)
            name_lbl.setStyleSheet("color: #888; font-size: 10px;")
            val_lbl = QLabel("--")
            val_lbl.setStyleSheet("color: #e0e0e0; font-size: 12px; font-weight: bold;")
            extra_grid.addWidget(name_lbl, row * 2, col)
            extra_grid.addWidget(val_lbl, row * 2 + 1, col)
            self._stat_labels[key] = val_lbl

        stats_row.addWidget(extra_group)

        # By symbol breakdown
        symbol_group = QGroupBox("P&L by Symbol")
        symbol_layout = QVBoxLayout(symbol_group)
        self.symbol_chart = MiniBarChart()
        self.symbol_chart.setMinimumHeight(120)
        symbol_layout.addWidget(self.symbol_chart)
        stats_row.addWidget(symbol_group)

        self.content_layout.addLayout(stats_row)

        self.content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

    def _on_timeframe_changed(self, index: int):
        self.refresh()

    def refresh(self):
        """Refresh all analytics data."""
        try:
            _, days = TIMEFRAMES[self.timeframe_combo.currentIndex()]
        except IndexError:
            days = 0

        try:
            m = self.engine.get_performance_metrics(days)
        except Exception as e:
            logger.error(f"Failed to load metrics: {e}")
            return

        # Key metric cards
        pnl_color = "#00ff88" if m['total_pnl'] >= 0 else "#ff4444"
        self.card_total_pnl.set_value(f"${m['total_pnl']:,.2f}", pnl_color)

        sharpe_color = "#00ff88" if m['sharpe'] > 1 else "#ffaa00" if m['sharpe'] > 0 else "#ff4444"
        self.card_sharpe.set_value(f"{m['sharpe']:.2f}", sharpe_color)

        sortino_color = "#00ff88" if m['sortino'] > 1.5 else "#ffaa00" if m['sortino'] > 0 else "#ff4444"
        self.card_sortino.set_value(f"{m['sortino']:.2f}", sortino_color)

        wr = m['win_rate']
        wr_color = "#00ff88" if wr > 0.55 else "#ffaa00" if wr > 0.4 else "#ff4444"
        self.card_win_rate.set_value(f"{wr:.1%}", wr_color)

        pf = m['profit_factor']
        pf_str = f"{pf:.2f}" if pf < 100 else "INF"
        pf_color = "#00ff88" if pf > 1.5 else "#ffaa00" if pf > 1 else "#ff4444"
        self.card_profit_factor.set_value(pf_str, pf_color)

        dd_color = "#00ff88" if m['max_drawdown'] < 50 else "#ffaa00" if m['max_drawdown'] < 200 else "#ff4444"
        self.card_max_dd.set_value(f"${m['max_drawdown']:,.0f}", dd_color)

        # Secondary cards
        calmar_color = "#00ff88" if m['calmar'] > 1 else "#ffaa00" if m['calmar'] > 0 else "#ff4444"
        self.card_calmar.set_value(f"{m['calmar']:.2f}", calmar_color)

        exp_color = "#00ff88" if m['expectancy'] > 0 else "#ff4444"
        self.card_expectancy.set_value(f"${m['expectancy']:,.2f}", exp_color)

        r_color = "#00ff88" if m['avg_r'] > 0 else "#ff4444"
        self.card_avg_r.set_value(f"{m['avg_r']:+.2f}R", r_color)

        self.card_total_trades.set_value(str(m['total_trades']))

        hours = m['avg_hold_hours']
        if hours >= 24:
            self.card_avg_hold.set_value(f"{hours / 24:.1f}d")
        else:
            self.card_avg_hold.set_value(f"{hours:.1f}h")

        bd_color = "#00ff88" if m['best_day'] > 0 else "#ff4444"
        self.card_best_day.set_value(f"${m['best_day']:,.2f}", bd_color)

        # Additional stats
        self._set_stat("streak_wins", str(m['streak_wins']), "#00ff88")
        self._set_stat("streak_losses", str(m['streak_losses']), "#ff4444")
        self._set_stat("max_win", f"${m['max_win']:,.2f}", "#00ff88")
        self._set_stat("max_loss", f"${m['max_loss']:,.2f}", "#ff4444")
        self._set_stat("worst_day", f"${m['worst_day']:,.2f}", "#ff4444")
        self._set_stat("avg_slippage", f"${m['avg_slippage']:.4f}")
        self._set_stat("total_slippage", f"${m['total_slippage']:.2f}")
        avg_pnl_color = "#00ff88" if m['avg_pnl'] > 0 else "#ff4444"
        self._set_stat("avg_pnl", f"${m['avg_pnl']:,.2f}", avg_pnl_color)

        # Win/Loss donut
        self.win_loss_donut.set_data(
            [
                ("Wins", m['winners'], "#00cc66"),
                ("Losses", m['losers'], "#cc3333"),
            ],
            center_text=f"{wr:.0%}",
            center_sub=f"{m['winners']}W / {m['losers']}L",
        )

        # Equity curve (cumulative PnL from daily data)
        try:
            daily_data = self.engine.get_daily_pnl_history(days)
            if daily_data:
                cumulative = []
                running = 0
                labels = []
                for d in daily_data:
                    running += d['realized_pnl']
                    cumulative.append(running)
                    labels.append(d['date'][-5:])  # MM-DD
                color = "#00ff88" if running >= 0 else "#ff4444"
                self.equity_chart.set_data(cumulative, labels, "Cumulative P&L", color)

                # Daily PnL bars
                bar_data = [(d['date'][-5:], d['realized_pnl']) for d in daily_data]
                self.daily_pnl_chart.set_data(bar_data, "Daily Returns")
        except Exception as e:
            logger.debug(f"Chart data error: {e}")

        # Symbol breakdown chart
        by_sym = m.get('by_symbol', {})
        if by_sym:
            sorted_syms = sorted(by_sym.items(), key=lambda x: x[1]['pnl'], reverse=True)
            sym_data = [(sym, info['pnl']) for sym, info in sorted_syms[:15]]
            self.symbol_chart.set_data(sym_data, "P&L by Symbol")

    def _set_stat(self, key: str, text: str, color: str = "#e0e0e0"):
        if key in self._stat_labels:
            self._stat_labels[key].setText(text)
            self._stat_labels[key].setStyleSheet(
                f"color: {color}; font-size: 12px; font-weight: bold;"
            )
