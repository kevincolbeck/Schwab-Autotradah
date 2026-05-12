"""Dashboard widgets: regime display, positions table, orders table, PnL."""

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from core.constants import Regime

logger = logging.getLogger(__name__)


class RegimeDisplay(QWidget):
    """Shows current regime state with color coding."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.regime_label = QLabel("TREND")
        self.regime_label.setObjectName("regime_label")
        self.regime_label.setAlignment(Qt.AlignCenter)
        self.regime_label.setMinimumHeight(50)
        layout.addWidget(self.regime_label)

        self.details_label = QLabel("")
        self.details_label.setAlignment(Qt.AlignCenter)
        self.details_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.details_label)

    def update_regime(self, regime: Regime, details: dict = None):
        self.regime_label.setText(regime.value)

        colors = {
            Regime.TREND: ("#00ff88", "#0a3320"),
            Regime.CHOP: ("#ffaa00", "#332a0a"),
            Regime.RISK_OFF: ("#ff4444", "#330a0a"),
        }
        fg, bg = colors.get(regime, ("#e0e0e0", "#1a1a2e"))
        self.regime_label.setStyleSheet(
            f"color: {fg}; background-color: {bg}; font-size: 24px; "
            f"font-weight: bold; padding: 8px 16px; border-radius: 6px;"
        )

        if details:
            parts = []
            if 'sma10' in details and details['sma10']:
                parts.append(f"SMA10: {details['sma10']:.2f}")
            if 'sma20' in details and details['sma20']:
                parts.append(f"SMA20: {details['sma20']:.2f}")
            if 'hit_rate' in details and details['hit_rate'] is not None:
                parts.append(f"Hit Rate: {details['hit_rate']:.1%}")
            if details.get('cooldown_remaining', 0) > 0:
                parts.append(f"Cooldown: {details['cooldown_remaining']}d")
            self.details_label.setText(" | ".join(parts))


class PositionsTable(QWidget):
    """Table showing open positions."""

    reenable_requested = Signal(str)  # symbol

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Symbol", "Status", "Shares", "Entry", "Stop",
            "R-Value", "BE", "Managed", "Action"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

    def update_positions(self, trades: list):
        self.table.setRowCount(len(trades))
        for row, trade in enumerate(trades):
            self.table.setItem(row, 0, QTableWidgetItem(trade.get('symbol', '')))
            self.table.setItem(row, 1, QTableWidgetItem(trade.get('status', '')))
            self.table.setItem(row, 2, QTableWidgetItem(str(trade.get('shares', 0))))

            entry = trade.get('entry_price')
            self.table.setItem(row, 3, QTableWidgetItem(f"${entry:.2f}" if entry else "--"))

            stop = trade.get('current_stop')
            self.table.setItem(row, 4, QTableWidgetItem(f"${stop:.2f}" if stop else "--"))

            r_val = trade.get('r_value')
            self.table.setItem(row, 5, QTableWidgetItem(f"${r_val:.2f}" if r_val else "--"))

            self.table.setItem(row, 6, QTableWidgetItem("YES" if trade.get('breakeven') else "NO"))

            managed = trade.get('bot_managed', True)
            managed_item = QTableWidgetItem("YES" if managed else "NO")
            if not managed:
                managed_item.setForeground(QColor("#ff4444"))
            self.table.setItem(row, 7, managed_item)

            # Re-enable button for manually overridden trades
            if not managed:
                btn = QPushButton("Re-enable")
                btn.setStyleSheet("font-size: 10px; padding: 2px 8px;")
                symbol = trade.get('symbol', '')
                btn.clicked.connect(lambda checked, s=symbol: self.reenable_requested.emit(s))
                self.table.setCellWidget(row, 8, btn)
            else:
                self.table.setItem(row, 8, QTableWidgetItem(""))


class OrdersTable(QWidget):
    """Table showing pending/working orders."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "Symbol", "Side", "Type", "Qty", "Stop", "Limit", "Purpose"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

    def update_orders(self, orders: list):
        self.table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            self.table.setItem(row, 0, QTableWidgetItem(order.get('symbol', '')))
            self.table.setItem(row, 1, QTableWidgetItem(order.get('side', '')))
            self.table.setItem(row, 2, QTableWidgetItem(order.get('type', '')))
            self.table.setItem(row, 3, QTableWidgetItem(str(order.get('qty', 0))))

            stop = order.get('stop_price')
            self.table.setItem(row, 4, QTableWidgetItem(f"${stop:.2f}" if stop else "--"))

            limit = order.get('limit_price')
            self.table.setItem(row, 5, QTableWidgetItem(f"${limit:.2f}" if limit else "--"))

            self.table.setItem(row, 6, QTableWidgetItem(order.get('purpose', '')))


class PnLDisplay(QWidget):
    """Shows daily PnL metrics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.pnl_label = QLabel("PnL: $0.00")
        self.pnl_label.setStyleSheet("font-size: 18px; font-weight: bold;")

        self.r_label = QLabel("R: 0.0")
        self.r_label.setStyleSheet("font-size: 18px; font-weight: bold;")

        self.trades_label = QLabel("Trades: 0")

        self.lock_label = QLabel("")
        self.lock_label.setStyleSheet("color: #ff4444; font-weight: bold;")

        layout.addWidget(self.pnl_label)
        layout.addWidget(self.r_label)
        layout.addWidget(self.trades_label)
        layout.addWidget(self.lock_label)
        layout.addStretch()

    def update_pnl(self, data: dict):
        pnl = data.get('realized_pnl', 0)
        total_r = data.get('total_r', 0)
        trades = data.get('trades_closed', 0)
        locked = data.get('locked_out', False)

        color = "#00ff88" if pnl >= 0 else "#ff4444"
        self.pnl_label.setText(f"PnL: ${pnl:,.2f}")
        self.pnl_label.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {color};")

        r_color = "#00ff88" if total_r >= 0 else "#ff4444"
        self.r_label.setText(f"R: {total_r:+.1f}")
        self.r_label.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {r_color};")

        self.trades_label.setText(f"Trades: {trades}")
        self.lock_label.setText("DAILY LOSS LOCK" if locked else "")
