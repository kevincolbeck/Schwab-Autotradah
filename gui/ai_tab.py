"""AI Strategist dashboard tab - shows AI reasoning, decisions, and strategy state."""

import json
import logging
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QScrollArea, QTextEdit, QPushButton, QSplitter,
)
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QColor

logger = logging.getLogger(__name__)


class AnalysisThread(QThread):
    """Run AI analysis in background."""
    analysis_complete = Signal(dict)
    analysis_error = Signal(str)

    def __init__(self, strategist, analysis_type):
        super().__init__()
        self.strategist = strategist
        self.analysis_type = analysis_type

    def run(self):
        try:
            result = self.strategist.run_analysis(self.analysis_type, min_trades=1)
            self.analysis_complete.emit(result or {})
        except Exception as e:
            self.analysis_error.emit(str(e))


class AITab(QWidget):
    """AI Strategist dashboard showing decisions, reasoning, and controls."""

    def __init__(self, strategist, engine, parent=None):
        super().__init__(parent)
        self.strategist = strategist
        self.engine = engine
        self._analysis_thread = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("AI Strategist")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #8888cc;")
        header.addWidget(title)

        self.status_label = QLabel("Always-on | Analyzing after market close")
        self.status_label.setStyleSheet("color: #00ff88; font-size: 11px;")
        header.addWidget(self.status_label)

        header.addStretch()

        # Manual trigger buttons
        self.daily_btn = QPushButton("Run Daily Analysis")
        self.daily_btn.setStyleSheet("""
            QPushButton {
                background-color: #2a2a4e; color: #4488ff;
                border: 1px solid #4488ff; border-radius: 4px;
                padding: 6px 14px; font-size: 11px;
            }
            QPushButton:hover { background-color: #3a3a6e; }
            QPushButton:disabled { color: #555; border-color: #333; }
        """)
        self.daily_btn.clicked.connect(lambda: self._run_analysis("DAILY"))
        header.addWidget(self.daily_btn)

        self.weekly_btn = QPushButton("Run Deep Analysis")
        self.weekly_btn.setStyleSheet(self.daily_btn.styleSheet().replace("#4488ff", "#ff8844"))
        self.weekly_btn.clicked.connect(lambda: self._run_analysis("WEEKLY"))
        header.addWidget(self.weekly_btn)

        layout.addLayout(header)

        # Main content splitter
        splitter = QSplitter(Qt.Vertical)

        # Top section: Current strategy + AI status
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)

        # Current strategy card
        strat_group = QGroupBox("Current Strategy Parameters")
        strat_layout = QVBoxLayout(strat_group)
        self.strat_info = QTextEdit()
        self.strat_info.setReadOnly(True)
        self.strat_info.setMaximumHeight(200)
        self.strat_info.setStyleSheet("""
            QTextEdit {
                background-color: #0d0d1a; color: #00ff88;
                border: 1px solid #3a3a5c; border-radius: 4px;
                font-family: "Consolas", monospace; font-size: 11px;
                padding: 4px;
            }
        """)
        strat_layout.addWidget(self.strat_info)
        top_layout.addWidget(strat_group, stretch=1)

        # AI activity summary
        activity_group = QGroupBox("AI Activity")
        activity_layout = QVBoxLayout(activity_group)

        self.activity_labels = {}
        for name in ["Total Analyses", "Changes Applied", "Last Run",
                      "Strategy Version", "Current Confidence"]:
            row = QHBoxLayout()
            n_lbl = QLabel(name + ":")
            n_lbl.setStyleSheet("color: #888; font-size: 11px;")
            v_lbl = QLabel("--")
            v_lbl.setStyleSheet("color: #e0e0e0; font-size: 12px; font-weight: bold;")
            v_lbl.setAlignment(Qt.AlignRight)
            row.addWidget(n_lbl)
            row.addWidget(v_lbl)
            activity_layout.addLayout(row)
            self.activity_labels[name] = v_lbl

        activity_layout.addStretch()
        top_layout.addWidget(activity_group, stretch=1)

        splitter.addWidget(top)

        # Middle: Latest reasoning
        reasoning_group = QGroupBox("Latest AI Reasoning")
        reasoning_layout = QVBoxLayout(reasoning_group)
        self.reasoning_text = QTextEdit()
        self.reasoning_text.setReadOnly(True)
        self.reasoning_text.setStyleSheet("""
            QTextEdit {
                background-color: #0d0d1a; color: #e0e0e0;
                border: 1px solid #3a3a5c; border-radius: 4px;
                font-family: "Segoe UI", sans-serif; font-size: 12px;
                padding: 8px;
            }
        """)
        reasoning_layout.addWidget(self.reasoning_text)
        splitter.addWidget(reasoning_group)

        # Bottom: Decision history table
        history_group = QGroupBox("Decision History")
        history_layout = QVBoxLayout(history_group)
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels([
            "Time", "Type", "Status", "Changes", "Confidence", "Summary"
        ])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.currentCellChanged.connect(self._on_decision_selected)
        history_layout.addWidget(self.history_table)
        splitter.addWidget(history_group)

        splitter.setSizes([200, 250, 250])
        layout.addWidget(splitter)

    def _run_analysis(self, analysis_type: str):
        """Trigger manual AI analysis."""
        if self._analysis_thread and self._analysis_thread.isRunning():
            return

        self.daily_btn.setEnabled(False)
        self.weekly_btn.setEnabled(False)
        self.status_label.setText(f"Running {analysis_type} analysis...")
        self.status_label.setStyleSheet("color: #ffaa00; font-size: 11px;")

        self._analysis_thread = AnalysisThread(self.strategist, analysis_type)
        self._analysis_thread.analysis_complete.connect(self._on_analysis_complete)
        self._analysis_thread.analysis_error.connect(self._on_analysis_error)
        self._analysis_thread.start()

    def _on_analysis_complete(self, result: dict):
        self.daily_btn.setEnabled(True)
        self.weekly_btn.setEnabled(True)

        if result.get("error"):
            self.status_label.setText(f"Error: {result['error']}")
            self.status_label.setStyleSheet("color: #ff4444; font-size: 11px;")
        elif result.get("applied"):
            self.status_label.setText("Changes applied to strategy!")
            self.status_label.setStyleSheet("color: #00ff88; font-size: 11px;")
        else:
            self.status_label.setText("Analysis complete - no changes applied")
            self.status_label.setStyleSheet("color: #4488ff; font-size: 11px;")

        # Show reasoning
        reasoning = result.get("reasoning", "")
        risk = result.get("risk_assessment", "")
        changes = result.get("changes", {})
        confidence = result.get("confidence", 0)

        html_parts = []
        if reasoning:
            html_parts.append(f'<p style="color: #e0e0e0;">{reasoning}</p>')
        if changes:
            html_parts.append(f'<p style="color: #ffaa00;"><b>Changes:</b> {json.dumps(changes, indent=2)}</p>')
        if risk:
            html_parts.append(f'<p style="color: #ff8888;"><b>Risk:</b> {risk}</p>')
        if confidence:
            html_parts.append(f'<p style="color: #888;"><b>Confidence:</b> {confidence:.0%}</p>')

        self.reasoning_text.setHtml("".join(html_parts) or "<p style='color: #888;'>No analysis available.</p>")

        self.refresh()

    def _on_analysis_error(self, error: str):
        self.daily_btn.setEnabled(True)
        self.weekly_btn.setEnabled(True)
        self.status_label.setText(f"Error: {error}")
        self.status_label.setStyleSheet("color: #ff4444; font-size: 11px;")

    def _on_decision_selected(self, row, col, prev_row, prev_col):
        """Show full reasoning when a decision is selected."""
        if row < 0 or not hasattr(self, '_decisions_cache'):
            return
        if row < len(self._decisions_cache):
            d = self._decisions_cache[row]
            reasoning = d.get('reasoning', '')
            changes_proposed = d.get('changes_proposed', '{}')
            changes_applied = d.get('changes_applied', '{}')

            html = f'<p style="color: #e0e0e0;">{reasoning}</p>'
            if changes_proposed and changes_proposed != '{}':
                html += f'<p style="color: #ffaa00;"><b>Proposed:</b><br><pre>{changes_proposed}</pre></p>'
            if changes_applied and changes_applied != '{}':
                html += f'<p style="color: #00ff88;"><b>Applied:</b><br><pre>{changes_applied}</pre></p>'

            self.reasoning_text.setHtml(html)

    def refresh(self):
        """Refresh all AI tab data."""
        # Strategy info
        try:
            spec = self.strategist.get_current_spec_summary()
            if spec:
                lines = [
                    f"Name: {spec.get('name', '--')}",
                    f"Entry: {spec.get('entry_rule', '--')}",
                    f"Exit:  {spec.get('exit_rule', '--')}",
                    f"Size:  {spec.get('position_size_pct', 0)}% per position",
                    f"Max Positions: {spec.get('max_positions', 0)}",
                    f"Max Hold: {spec.get('max_holding_days', 0)} days",
                    f"Stop Loss: {spec.get('stop_loss_pct', 0)}%",
                    f"Symbols ({spec.get('num_symbols', 0)}): {', '.join(spec.get('symbols', []))}",
                ]
                self.strat_info.setPlainText("\n".join(lines))
        except Exception as e:
            logger.debug(f"Strategy info error: {e}")

        # Decision history
        try:
            decisions = self.strategist.get_recent_decisions(30)
            self._decisions_cache = list(reversed(decisions))  # newest first
            self.history_table.setRowCount(len(self._decisions_cache))

            total_analyses = len(self._decisions_cache)
            changes_applied = sum(1 for d in self._decisions_cache if d.get('approved'))

            for row, d in enumerate(self._decisions_cache):
                ts = d.get('timestamp')
                ts_str = ts.strftime('%m/%d %H:%M') if ts else "--"
                self.history_table.setItem(row, 0, QTableWidgetItem(ts_str))
                self.history_table.setItem(row, 1, QTableWidgetItem(d.get('type', '')))

                approved = d.get('approved', False)
                status_item = QTableWidgetItem("APPLIED" if approved else "REVIEWED")
                status_item.setForeground(QColor("#00ff88") if approved else QColor("#888"))
                self.history_table.setItem(row, 2, status_item)

                changes = d.get('changes_applied', '{}')
                if changes and changes != '{}':
                    try:
                        c = json.loads(changes)
                        change_summary = ", ".join(f"{k}" for k in c.keys())
                    except Exception:
                        change_summary = changes[:50]
                else:
                    change_summary = "None"
                self.history_table.setItem(row, 3, QTableWidgetItem(change_summary))

                # Confidence from backtest
                bt = d.get('backtest_after')
                if bt:
                    try:
                        bt_data = json.loads(bt)
                        conf = f"S={bt_data.get('sharpe', 0):.1f}"
                    except Exception:
                        conf = "--"
                else:
                    conf = "--"
                self.history_table.setItem(row, 4, QTableWidgetItem(conf))

                # Summary (first 80 chars of reasoning)
                reasoning = d.get('reasoning', '')[:80]
                self.history_table.setItem(row, 5, QTableWidgetItem(reasoning))

            # Activity summary
            self.activity_labels["Total Analyses"].setText(str(total_analyses))
            self.activity_labels["Changes Applied"].setText(str(changes_applied))

            if self._decisions_cache:
                last = self._decisions_cache[0]
                last_ts = last.get('timestamp')
                if last_ts:
                    self.activity_labels["Last Run"].setText(
                        last_ts.strftime('%m/%d %H:%M')
                    )

            self.activity_labels["Strategy Version"].setText(
                f"v{total_analyses}" if total_analyses else "Base"
            )

        except Exception as e:
            logger.debug(f"Decision history error: {e}")
