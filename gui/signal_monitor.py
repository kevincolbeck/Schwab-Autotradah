"""Signal Monitor tab - current strategy state, indicator values, last scan."""

import logging
import json
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QScrollArea, QGridLayout,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

logger = logging.getLogger(__name__)


class StrategyCard(QFrame):
    """Card showing loaded strategy configuration."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: #16162b;
                border: 1px solid #2a2a4e;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        self.title_label = QLabel("Strategy: --")
        self.title_label.setStyleSheet("color: #8888cc; font-size: 14px; font-weight: bold; border: none;")
        layout.addWidget(self.title_label)

        self.details_grid = QGridLayout()
        self.details_grid.setSpacing(4)
        layout.addLayout(self.details_grid)

        self._detail_labels = {}

    def set_strategy(self, name: str, details: dict):
        self.title_label.setText(f"Strategy: {name}")

        # Clear old
        for i in reversed(range(self.details_grid.count())):
            self.details_grid.itemAt(i).widget().setParent(None)
        self._detail_labels.clear()

        row = 0
        col = 0
        for key, val in details.items():
            name_lbl = QLabel(key)
            name_lbl.setStyleSheet("color: #666; font-size: 10px; border: none;")
            val_lbl = QLabel(str(val))
            val_lbl.setStyleSheet("color: #e0e0e0; font-size: 11px; border: none;")
            self.details_grid.addWidget(name_lbl, row, col * 2)
            self.details_grid.addWidget(val_lbl, row, col * 2 + 1)
            self._detail_labels[key] = val_lbl
            col += 1
            if col >= 3:
                col = 0
                row += 1


class SignalMonitorTab(QWidget):
    """Shows current strategy config, universe, and last signal scan results."""

    def __init__(self, engine, config, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.config = config
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        title = QLabel("Signal Monitor")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #8888cc;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        clayout = QVBoxLayout(content)
        clayout.setSpacing(12)

        # Strategy card
        self.strategy_card = StrategyCard()
        clayout.addWidget(self.strategy_card)

        # Status row
        status_row = QHBoxLayout()
        status_row.setSpacing(12)

        self.status_labels = {}
        for name in ["Engine Status", "Shadow Mode", "Allocated Capital",
                      "Rolling Balance", "Next Scan", "Last Scan"]:
            frame = QFrame()
            frame.setStyleSheet("""
                QFrame {
                    background-color: #16162b;
                    border: 1px solid #2a2a4e;
                    border-radius: 6px;
                    padding: 4px 8px;
                }
            """)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(8, 4, 8, 4)
            fl.setSpacing(0)
            n_lbl = QLabel(name)
            n_lbl.setStyleSheet("color: #666; font-size: 9px; border: none;")
            n_lbl.setAlignment(Qt.AlignCenter)
            v_lbl = QLabel("--")
            v_lbl.setStyleSheet("color: #e0e0e0; font-size: 12px; font-weight: bold; border: none;")
            v_lbl.setAlignment(Qt.AlignCenter)
            fl.addWidget(n_lbl)
            fl.addWidget(v_lbl)
            status_row.addWidget(frame)
            self.status_labels[name] = v_lbl

        clayout.addLayout(status_row)

        # Universe / watchlist
        universe_group = QGroupBox("Trading Universe")
        uni_layout = QVBoxLayout(universe_group)
        self.universe_table = QTableWidget()
        self.universe_table.setColumnCount(3)
        self.universe_table.setHorizontalHeaderLabels([
            "Symbol", "In Universe", "Status"
        ])
        self.universe_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.universe_table.setAlternatingRowColors(True)
        self.universe_table.verticalHeader().setVisible(False)
        uni_layout.addWidget(self.universe_table)
        clayout.addWidget(universe_group)

        # Recent events from signal evaluation
        events_group = QGroupBox("Recent Signal Events")
        events_layout = QVBoxLayout(events_group)
        self.events_table = QTableWidget()
        self.events_table.setColumnCount(4)
        self.events_table.setHorizontalHeaderLabels([
            "Time", "Type", "Symbol", "Message"
        ])
        self.events_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.events_table.setAlternatingRowColors(True)
        self.events_table.verticalHeader().setVisible(False)
        events_layout.addWidget(self.events_table)
        clayout.addWidget(events_group)

        clayout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

    def refresh(self):
        """Refresh signal monitor data."""
        # Load strategy spec
        try:
            spec_path = self.config.strategy_runtime.active_strategy_spec_path
            if spec_path and Path(spec_path).exists():
                with open(spec_path, 'r') as f:
                    spec = json.load(f)

                name = spec.get('name', spec_path)
                details = {}
                if 'symbols' in spec:
                    details['Symbols'] = str(len(spec['symbols']))
                sizing = spec.get('sizing', {})
                if sizing:
                    details['Per Position'] = f"{sizing.get('pct_equity', 0)}%"
                    details['Max Positions'] = str(sizing.get('max_positions', '--'))
                    details['Max Hold'] = f"{sizing.get('max_hold_days', '--')}d"
                if 'entry_rules' in spec:
                    rules = spec['entry_rules']
                    rule_names = [r.get('indicator', '') for r in rules.get('long', [])]
                    details['Entry Rules'] = ', '.join(rule_names) or '--'
                if 'exit_rules' in spec:
                    rules = spec['exit_rules']
                    rule_names = [r.get('indicator', '') for r in rules.get('long', [])]
                    details['Exit Rules'] = ', '.join(rule_names) or '--'
                if 'trend_filter' in spec:
                    tf = spec['trend_filter']
                    details['Trend Filter'] = f"{tf.get('symbol', 'SPY')} > SMA{tf.get('period', 50)}"

                self.strategy_card.set_strategy(name, details)
        except Exception as e:
            logger.debug(f"Strategy spec load error: {e}")

        # Status info
        engine_status = "Running" if self.engine.running else "Stopped"
        status_color = "#00ff88" if self.engine.running else "#ff4444"
        self.status_labels["Engine Status"].setText(engine_status)
        self.status_labels["Engine Status"].setStyleSheet(
            f"color: {status_color}; font-size: 12px; font-weight: bold; border: none;"
        )

        shadow = self.engine.shadow_mode
        self.status_labels["Shadow Mode"].setText("ON" if shadow else "OFF")
        self.status_labels["Shadow Mode"].setStyleSheet(
            f"color: {'#ffaa00' if shadow else '#ff4444'}; font-size: 12px; font-weight: bold; border: none;"
        )

        # Allocated capital
        alloc = getattr(self.config.strategy_runtime, 'allocated_capital', 0)
        if alloc > 0:
            self.status_labels["Allocated Capital"].setText(f"${alloc:,.0f}")
            # Try to read rolling balance
            try:
                balance_file = Path("allocated_balance.json")
                if balance_file.exists():
                    with open(balance_file, 'r') as f:
                        bal = json.load(f)
                    current = bal.get('current_balance', alloc)
                    color = "#00ff88" if current >= alloc else "#ff4444"
                    self.status_labels["Rolling Balance"].setText(f"${current:,.2f}")
                    self.status_labels["Rolling Balance"].setStyleSheet(
                        f"color: {color}; font-size: 12px; font-weight: bold; border: none;"
                    )
                else:
                    self.status_labels["Rolling Balance"].setText(f"${alloc:,.0f}")
            except Exception:
                self.status_labels["Rolling Balance"].setText("--")
        else:
            self.status_labels["Allocated Capital"].setText("Full Account")
            self.status_labels["Rolling Balance"].setText(f"${self.engine._equity:,.0f}")

        # Scan timing
        self.status_labels["Next Scan"].setText("3:50 PM ET")
        from datetime import datetime
        self.status_labels["Last Scan"].setText(datetime.now().strftime('%H:%M:%S'))

        # Universe - from strategy spec symbols
        try:
            spec_path = self.config.strategy_runtime.active_strategy_spec_path
            if spec_path and Path(spec_path).exists():
                with open(spec_path, 'r') as f:
                    spec = json.load(f)
                symbols = spec.get('symbols', [])

                # Check which have open trades
                open_trades = self.engine.get_open_trades()
                open_syms = {t['symbol'] for t in open_trades}

                self.universe_table.setRowCount(len(symbols))
                for row, sym in enumerate(sorted(symbols)):
                    self.universe_table.setItem(row, 0, QTableWidgetItem(sym))
                    check_item = QTableWidgetItem("YES")
                    check_item.setForeground(QColor("#00ff88"))
                    self.universe_table.setItem(row, 1, check_item)

                    if sym in open_syms:
                        status = QTableWidgetItem("POSITION OPEN")
                        status.setForeground(QColor("#4488ff"))
                    else:
                        status = QTableWidgetItem("Watching")
                        status.setForeground(QColor("#666"))
                    self.universe_table.setItem(row, 2, status)
        except Exception as e:
            logger.debug(f"Universe load error: {e}")

        # Recent signal events
        try:
            events = self.engine.get_recent_events(30)
            signal_events = [e for e in events if e['type'] in (
                'ENTRY_TRIGGERED', 'ORDER_PLACED', 'ORDER_FILLED',
                'ORDER_CANCELLED', 'SIGNAL_EVALUATED', 'EOD_EXIT',
            )]
            self.events_table.setRowCount(len(signal_events))
            event_colors = {
                'ENTRY_TRIGGERED': '#00ff88',
                'ORDER_PLACED': '#4488ff',
                'ORDER_FILLED': '#00ff88',
                'ORDER_CANCELLED': '#ffaa00',
                'SIGNAL_EVALUATED': '#555',
                'EOD_EXIT': '#ffaa00',
            }
            for row, ev in enumerate(reversed(signal_events)):
                self.events_table.setItem(row, 0, QTableWidgetItem(ev.get('time', '')))
                type_item = QTableWidgetItem(ev.get('type', ''))
                type_item.setForeground(QColor(event_colors.get(ev.get('type', ''), '#888')))
                self.events_table.setItem(row, 1, type_item)
                self.events_table.setItem(row, 2, QTableWidgetItem(ev.get('symbol', '')))
                self.events_table.setItem(row, 3, QTableWidgetItem(ev.get('message', '')))
        except Exception as e:
            logger.debug(f"Events load error: {e}")
