"""AI Chat panel for strategy discussion and parameter tuning."""

import logging
from enum import Enum, auto
from typing import Dict, List, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QTimer

logger = logging.getLogger(__name__)


class MessageRole(Enum):
    USER = auto()
    ASSISTANT = auto()
    SYSTEM = auto()


class ProposalCard(QFrame):
    """Widget showing proposed parameter changes with Apply/Reject buttons."""

    applied = Signal(dict)
    rejected = Signal()

    def __init__(self, changes: Dict[str, object], current_values: Dict[str, object], parent=None):
        super().__init__(parent)
        self.changes = changes
        self.setObjectName("proposal_card")
        self._build_ui(changes, current_values)

    def _build_ui(self, changes: dict, current_values: dict):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        title = QLabel("Proposed Changes")
        title.setStyleSheet("color: #ffaa00; font-weight: bold; font-size: 13px; background: transparent;")
        layout.addWidget(title)

        strategy_spec = changes.get("__strategy_spec__")
        if isinstance(strategy_spec, dict):
            strategy_name = str(strategy_spec.get("name", "AI Strategy")).strip() or "AI Strategy"
            strategy_symbols = strategy_spec.get("symbols", [])
            symbol_count = len(strategy_symbols) if isinstance(strategy_symbols, list) else 0
            strategy_label = QLabel(
                f"  strategy_spec: {strategy_name} ({symbol_count} symbols)"
            )
            strategy_label.setStyleSheet("color: #e0e0e0; background: transparent;")
            layout.addWidget(strategy_label)

        for param_path, new_value in changes.items():
            if str(param_path).startswith("__"):
                continue
            old_value = current_values.get(param_path, "?")
            row = QHBoxLayout()
            param_label = QLabel(f"  {param_path}:")
            param_label.setStyleSheet("color: #8888cc; background: transparent;")
            arrow_label = QLabel(f"{old_value}  ->  {new_value}")
            arrow_label.setStyleSheet("color: #e0e0e0; background: transparent;")
            row.addWidget(param_label)
            row.addStretch()
            row.addWidget(arrow_label)
            layout.addLayout(row)

        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply & Re-run")
        self.apply_btn.setObjectName("start_button")
        self.apply_btn.clicked.connect(lambda: self.applied.emit(self.changes))

        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setObjectName("stop_button")
        self.reject_btn.clicked.connect(self.rejected.emit)

        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.reject_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("background: transparent;")
        layout.addWidget(self.status_label)
        self.status_label.hide()

    def set_resolved(self, accepted: bool):
        """Gray out buttons after decision."""
        self.apply_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        text = "Applied" if accepted else "Rejected"
        color = "#00ff88" if accepted else "#ff4444"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold; background: transparent;")
        self.status_label.show()


class MessageBubble(QFrame):
    """A single chat message rendered as a styled frame."""

    def __init__(self, role: MessageRole, text: str,
                 proposed_changes: Optional[dict] = None,
                 current_config_values: Optional[dict] = None,
                 parent=None):
        super().__init__(parent)
        self.proposal_card: Optional[ProposalCard] = None
        self._build_ui(role, text, proposed_changes, current_config_values or {})

    def _build_ui(self, role: MessageRole, text: str,
                  proposed_changes: Optional[dict], current_values: dict):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        if role == MessageRole.USER:
            role_text, role_color, bg = "You", "#4488ff", "#1e2a4a"
        elif role == MessageRole.ASSISTANT:
            role_text, role_color, bg = "Claude", "#ffaa00", "#2a2a3e"
        else:
            role_text, role_color, bg = "", "#888888", "#1a1a2e"

        self.setStyleSheet(f"QFrame {{ background-color: {bg}; border-radius: 6px; margin: 2px 0; }}")

        if role_text:
            role_label = QLabel(role_text)
            role_label.setStyleSheet(f"color: {role_color}; font-weight: bold; font-size: 11px; background: transparent;")
            layout.addWidget(role_label)

        text_label = QLabel(text)
        text_label.setWordWrap(True)
        text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_label.setStyleSheet("color: #e0e0e0; font-size: 12px; background: transparent;")
        text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(text_label)

        if proposed_changes:
            self.proposal_card = ProposalCard(proposed_changes, current_values)
            layout.addWidget(self.proposal_card)


class ChatPanel(QWidget):
    """The complete chat panel: history + input + apply-to-live button."""

    user_message_sent = Signal(str)
    apply_to_live = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bubble_widgets: List[MessageBubble] = []
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Header
        header = QHBoxLayout()
        title = QLabel("AI Strategy Assistant")
        title.setStyleSheet("color: #ffaa00; font-weight: bold; font-size: 14px;")
        header.addWidget(title)
        header.addStretch()

        self.live_btn = QPushButton("Apply to Live Config")
        self.live_btn.setEnabled(False)
        self.live_btn.setToolTip("Save current parameter changes to config.yaml for live trading")
        self.live_btn.clicked.connect(self.apply_to_live.emit)
        header.addWidget(self.live_btn)
        layout.addLayout(header)

        # Chat history
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.history_widget = QWidget()
        self.history_layout = QVBoxLayout(self.history_widget)
        self.history_layout.setAlignment(Qt.AlignTop)
        self.history_layout.setSpacing(4)
        self.history_layout.addStretch()

        self.scroll_area.setWidget(self.history_widget)
        layout.addWidget(self.scroll_area)

        # Input row
        input_row = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Ask Claude about your strategy...")
        self.input_field.setMinimumHeight(36)
        self.input_field.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_field)

        self.send_btn = QPushButton("Send")
        self.send_btn.setMinimumHeight(36)
        self.send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self.send_btn)
        layout.addLayout(input_row)

    def add_message(
        self,
        role: MessageRole,
        text: str,
        proposed_changes: Optional[dict] = None,
        current_config_values: Optional[dict] = None,
    ) -> Optional[ProposalCard]:
        """Add a message to chat history. Returns ProposalCard if one was created."""
        bubble = MessageBubble(role, text, proposed_changes, current_config_values)
        self._bubble_widgets.append(bubble)

        # Insert before the stretch at the end
        count = self.history_layout.count()
        self.history_layout.insertWidget(count - 1, bubble)

        QTimer.singleShot(50, self._scroll_to_bottom)
        return bubble.proposal_card

    def remove_last_message(self):
        """Remove the last message bubble (used to remove 'Thinking...')."""
        if self._bubble_widgets:
            bubble = self._bubble_widgets.pop()
            self.history_layout.removeWidget(bubble)
            bubble.deleteLater()

    def set_input_enabled(self, enabled: bool):
        self.input_field.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)

    def set_live_button_enabled(self, enabled: bool):
        self.live_btn.setEnabled(enabled)

    def _on_send(self):
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self.add_message(MessageRole.USER, text)
        self.user_message_sent.emit(text)

    def _scroll_to_bottom(self):
        vbar = self.scroll_area.verticalScrollBar()
        vbar.setValue(vbar.maximum())
