"""Trade History tab - full trade log with filters and summary stats."""

import logging
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame, QLineEdit,
    QGroupBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

logger = logging.getLogger(__name__)

TIMEFRAMES = [
    ("All Time", 0),
    ("Today", 1),
    ("1 Week", 7),
    ("1 Month", 30),
    ("3 Months", 90),
    ("6 Months", 180),
]


class TradeHistoryTab(QWidget):
    """Full trade history with filtering and summary."""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._all_trades = []
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("Trade History")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #8888cc;")
        header.addWidget(title)
        header.addStretch()

        # Symbol filter
        sym_label = QLabel("Symbol:")
        sym_label.setStyleSheet("color: #888;")
        header.addWidget(sym_label)
        self.symbol_filter = QLineEdit()
        self.symbol_filter.setPlaceholderText("All")
        self.symbol_filter.setMaximumWidth(80)
        self.symbol_filter.setStyleSheet("""
            QLineEdit {
                background-color: #2a2a4e; color: #e0e0e0;
                border: 1px solid #4a4a7c; border-radius: 4px; padding: 4px 8px;
            }
        """)
        self.symbol_filter.textChanged.connect(self._apply_filters)
        header.addWidget(self.symbol_filter)

        # Result filter
        result_label = QLabel("Result:")
        result_label.setStyleSheet("color: #888;")
        header.addWidget(result_label)
        self.result_combo = QComboBox()
        self.result_combo.addItems(["All", "Winners", "Losers"])
        self.result_combo.setStyleSheet("""
            QComboBox {
                background-color: #2a2a4e; color: #e0e0e0;
                border: 1px solid #4a4a7c; border-radius: 4px; padding: 4px 12px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #2a2a4e; color: #e0e0e0;
                selection-background-color: #3a3a6e;
            }
        """)
        self.result_combo.currentIndexChanged.connect(self._apply_filters)
        header.addWidget(self.result_combo)

        # Time frame
        tf_label = QLabel("Period:")
        tf_label.setStyleSheet("color: #888;")
        header.addWidget(tf_label)
        self.timeframe_combo = QComboBox()
        for label, _ in TIMEFRAMES:
            self.timeframe_combo.addItem(label)
        self.timeframe_combo.setStyleSheet(self.result_combo.styleSheet())
        self.timeframe_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        header.addWidget(self.timeframe_combo)

        layout.addLayout(header)

        # Summary stats bar
        self.summary_frame = QFrame()
        self.summary_frame.setStyleSheet("""
            QFrame {
                background-color: #16162b;
                border: 1px solid #2a2a4e;
                border-radius: 6px;
                padding: 4px;
            }
        """)
        summary_layout = QHBoxLayout(self.summary_frame)
        summary_layout.setContentsMargins(12, 6, 12, 6)
        summary_layout.setSpacing(24)

        self.summary_labels = {}
        for name in ["Trades", "P&L", "Win Rate", "Avg Win", "Avg Loss", "Total R"]:
            container = QVBoxLayout()
            container.setSpacing(0)
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet("color: #666; font-size: 9px; border: none;")
            name_lbl.setAlignment(Qt.AlignCenter)
            val_lbl = QLabel("--")
            val_lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: bold; border: none;")
            val_lbl.setAlignment(Qt.AlignCenter)
            container.addWidget(name_lbl)
            container.addWidget(val_lbl)
            summary_layout.addLayout(container)
            self.summary_labels[name] = val_lbl

        layout.addWidget(self.summary_frame)

        # Trade table
        self.table = QTableWidget()
        self.table.setColumnCount(11)
        self.table.setHorizontalHeaderLabels([
            "Symbol", "Entry Date", "Exit Date", "Shares", "Entry $",
            "Exit $", "P&L $", "R-Multiple", "Hold Time", "Exit Reason", "Regime"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

    def _on_timeframe_changed(self, index: int):
        self.refresh()

    def refresh(self):
        """Load trades from engine and display."""
        try:
            _, days = TIMEFRAMES[self.timeframe_combo.currentIndex()]
        except IndexError:
            days = 0

        try:
            self._all_trades = self.engine.get_closed_trades(days)
        except Exception as e:
            logger.error(f"Failed to load trades: {e}")
            self._all_trades = []

        self._apply_filters()

    def _apply_filters(self):
        """Apply symbol and result filters to loaded trades."""
        trades = self._all_trades
        sym_filter = self.symbol_filter.text().strip().upper()
        result_idx = self.result_combo.currentIndex()

        if sym_filter:
            trades = [t for t in trades if sym_filter in t['symbol']]
        if result_idx == 1:  # Winners
            trades = [t for t in trades if t['pnl_dollars'] > 0]
        elif result_idx == 2:  # Losers
            trades = [t for t in trades if t['pnl_dollars'] <= 0]

        self._populate_table(trades)
        self._update_summary(trades)

    def _populate_table(self, trades: list):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(trades))

        for row, t in enumerate(trades):
            self.table.setItem(row, 0, QTableWidgetItem(t['symbol']))

            entry_dt = t.get('entry_time')
            entry_str = entry_dt.strftime('%m/%d %H:%M') if entry_dt else "--"
            self.table.setItem(row, 1, QTableWidgetItem(entry_str))

            exit_dt = t.get('exit_time')
            exit_str = exit_dt.strftime('%m/%d %H:%M') if exit_dt else "--"
            self.table.setItem(row, 2, QTableWidgetItem(exit_str))

            self.table.setItem(row, 3, QTableWidgetItem(str(t.get('shares', 0))))

            entry_p = t.get('entry_price')
            self.table.setItem(row, 4, QTableWidgetItem(f"${entry_p:.2f}" if entry_p else "--"))

            exit_p = t.get('exit_price')
            self.table.setItem(row, 5, QTableWidgetItem(f"${exit_p:.2f}" if exit_p else "--"))

            pnl = t.get('pnl_dollars', 0)
            pnl_item = QTableWidgetItem(f"${pnl:+,.2f}")
            pnl_item.setForeground(QColor("#00ff88") if pnl >= 0 else QColor("#ff4444"))
            self.table.setItem(row, 6, pnl_item)

            r_mult = t.get('pnl_r_multiple', 0)
            r_item = QTableWidgetItem(f"{r_mult:+.2f}R" if r_mult else "--")
            r_item.setForeground(QColor("#00ff88") if r_mult and r_mult >= 0 else QColor("#ff4444"))
            self.table.setItem(row, 7, r_item)

            # Hold time
            hold_str = "--"
            if entry_dt and exit_dt:
                delta = exit_dt - entry_dt
                hours = delta.total_seconds() / 3600
                if hours >= 24:
                    hold_str = f"{hours / 24:.1f}d"
                else:
                    hold_str = f"{hours:.1f}h"
            self.table.setItem(row, 8, QTableWidgetItem(hold_str))

            self.table.setItem(row, 9, QTableWidgetItem(t.get('exit_reason', '') or ''))
            self.table.setItem(row, 10, QTableWidgetItem(t.get('regime_at_entry', '') or ''))

        self.table.setSortingEnabled(True)

    def _update_summary(self, trades: list):
        n = len(trades)
        self.summary_labels["Trades"].setText(str(n))

        if not trades:
            for key in ["P&L", "Win Rate", "Avg Win", "Avg Loss", "Total R"]:
                self.summary_labels[key].setText("--")
            return

        total_pnl = sum(t['pnl_dollars'] for t in trades)
        wins = [t for t in trades if t['pnl_dollars'] > 0]
        losses = [t for t in trades if t['pnl_dollars'] <= 0]
        win_rate = len(wins) / n if n else 0
        avg_win = sum(t['pnl_dollars'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['pnl_dollars'] for t in losses) / len(losses) if losses else 0
        total_r = sum(t.get('pnl_r_multiple', 0) or 0 for t in trades)

        color = "#00ff88" if total_pnl >= 0 else "#ff4444"
        self.summary_labels["P&L"].setText(f"${total_pnl:+,.2f}")
        self.summary_labels["P&L"].setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: bold; border: none;"
        )
        self.summary_labels["Win Rate"].setText(f"{win_rate:.1%}")
        self.summary_labels["Avg Win"].setText(f"${avg_win:+,.2f}")
        self.summary_labels["Avg Win"].setStyleSheet(
            "color: #00ff88; font-size: 13px; font-weight: bold; border: none;"
        )
        self.summary_labels["Avg Loss"].setText(f"${avg_loss:+,.2f}")
        self.summary_labels["Avg Loss"].setStyleSheet(
            "color: #ff4444; font-size: 13px; font-weight: bold; border: none;"
        )

        r_color = "#00ff88" if total_r >= 0 else "#ff4444"
        self.summary_labels["Total R"].setText(f"{total_r:+.1f}R")
        self.summary_labels["Total R"].setStyleSheet(
            f"color: {r_color}; font-size: 13px; font-weight: bold; border: none;"
        )
