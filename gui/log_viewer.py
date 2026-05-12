"""Scrolling log viewer for the last N events."""

import logging
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextEdit
from PySide6.QtCore import Qt

logger = logging.getLogger(__name__)


class LogViewer(QWidget):
    """Displays the last 50 log events in a scrolling text area."""

    MAX_LINES = 50

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.text_edit = QTextEdit()
        self.text_edit.setObjectName("log_viewer")
        self.text_edit.setReadOnly(True)
        self.text_edit.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.text_edit)

        self._lines = []

    def add_event(self, event_type: str, message: str):
        """Add a single event line to the log."""
        from datetime import datetime
        timestamp = datetime.now().strftime('%H:%M:%S')

        # Color coding by event type
        color = self._get_color(event_type)
        line = f'<span style="color:#666">{timestamp}</span> ' \
               f'<span style="color:{color}">[{event_type}]</span> {message}'

        self._lines.append(line)
        if len(self._lines) > self.MAX_LINES:
            self._lines = self._lines[-self.MAX_LINES:]

        self._refresh()

    def load_events(self, events: list):
        """Load a batch of events (e.g., on startup)."""
        self._lines = []
        for ev in events:
            time_str = ev.get('time', '')
            event_type = ev.get('type', '')
            symbol = ev.get('symbol', '')
            message = ev.get('message', '')
            color = self._get_color(event_type)

            prefix = f"{symbol}: " if symbol else ""
            line = f'<span style="color:#666">{time_str}</span> ' \
                   f'<span style="color:{color}">[{event_type}]</span> {prefix}{message}'
            self._lines.append(line)

        if len(self._lines) > self.MAX_LINES:
            self._lines = self._lines[-self.MAX_LINES:]

        self._refresh()

    def _refresh(self):
        """Redraw the log content."""
        html = "<br>".join(self._lines)
        self.text_edit.setHtml(html)
        # Auto-scroll to bottom
        scrollbar = self.text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _get_color(self, event_type: str) -> str:
        """Get color for an event type."""
        color_map = {
            'ENTRY_TRIGGERED': '#00ff88',
            'ORDER_PLACED': '#4488ff',
            'ORDER_FILLED': '#00ff88',
            'ORDER_CANCELLED': '#ffaa00',
            'ORDER_REJECTED': '#ff4444',
            'STOP_UPDATED': '#8888ff',
            'STOP_TO_BREAKEVEN': '#00ccff',
            'EOD_EXIT': '#ffaa00',
            'REGIME_CHANGE': '#ff88ff',
            'RISK_OFF_LIQUIDATION': '#ff0000',
            'MANUAL_CLOSE_DETECTED': '#ffaa00',
            'MANUAL_PARTIAL_DETECTED': '#ffaa00',
            'MANUAL_STOP_MODIFIED': '#ffaa00',
            'KILL_SWITCH_ACTIVATED': '#ff0000',
            'KILL_SWITCH_DEACTIVATED': '#00ff88',
            'DAILY_LOSS_LOCK': '#ff0000',
            'SIGNAL_EVALUATED': '#555',
            'ENGINE_START': '#00ff88',
            'ENGINE_STOP': '#ff4444',
        }
        return color_map.get(event_type, '#888')
