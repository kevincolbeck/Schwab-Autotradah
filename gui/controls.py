"""Control widgets: risk spinner, kill switch, toggles, start/stop."""

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QDoubleSpinBox, QCheckBox, QLabel,
)
from PySide6.QtCore import Signal, Qt

logger = logging.getLogger(__name__)


class ControlPanel(QWidget):
    """Panel with all user controls."""

    # Signals
    risk_changed = Signal(float)
    kill_switch_toggled = Signal(bool)
    manage_exits_toggled = Signal(bool)
    start_requested = Signal()
    stop_requested = Signal()
    shadow_mode_toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # --- Risk Per Trade ---
        risk_group = QGroupBox("Risk Per Trade")
        risk_layout = QHBoxLayout()

        risk_label = QLabel("Risk %:")
        self.risk_spin = QDoubleSpinBox()
        self.risk_spin.setRange(0.05, 2.0)
        self.risk_spin.setSingleStep(0.05)
        self.risk_spin.setValue(0.50)
        self.risk_spin.setDecimals(2)
        self.risk_spin.setSuffix(" %")
        self.risk_spin.setMinimumWidth(120)
        self.risk_spin.valueChanged.connect(self.risk_changed.emit)

        risk_layout.addWidget(risk_label)
        risk_layout.addWidget(self.risk_spin)
        risk_layout.addStretch()
        risk_group.setLayout(risk_layout)
        layout.addWidget(risk_group)

        # --- Kill Switch ---
        kill_group = QGroupBox("Kill Switch")
        kill_layout = QVBoxLayout()

        self.kill_btn = QPushButton("KILL SWITCH - OFF")
        self.kill_btn.setObjectName("kill_switch")
        self.kill_btn.setCheckable(True)
        self.kill_btn.clicked.connect(self._on_kill_switch)
        self.kill_btn.setToolTip("Ctrl+Shift+K")
        kill_layout.addWidget(self.kill_btn)

        self.manage_exits_cb = QCheckBox("Manage exits while OFF")
        self.manage_exits_cb.setChecked(True)
        self.manage_exits_cb.toggled.connect(self.manage_exits_toggled.emit)
        kill_layout.addWidget(self.manage_exits_cb)

        kill_group.setLayout(kill_layout)
        layout.addWidget(kill_group)

        # --- Engine Controls ---
        engine_group = QGroupBox("Engine")
        engine_layout = QVBoxLayout()

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start Engine")
        self.start_btn.setObjectName("start_button")
        self.start_btn.clicked.connect(self.start_requested.emit)
        btn_row.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop Engine")
        self.stop_btn.setObjectName("stop_button")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        btn_row.addWidget(self.stop_btn)
        engine_layout.addLayout(btn_row)

        self.shadow_cb = QCheckBox("Shadow Mode (signals only, no orders)")
        self.shadow_cb.setChecked(True)
        self.shadow_cb.toggled.connect(self.shadow_mode_toggled.emit)
        engine_layout.addWidget(self.shadow_cb)

        engine_group.setLayout(engine_layout)
        layout.addWidget(engine_group)

        # --- Connection Status ---
        status_group = QGroupBox("Status")
        status_layout = QVBoxLayout()

        self.auth_label = QLabel("Auth: Not connected")
        self.equity_label = QLabel("Equity: --")
        self.mode_label = QLabel("Mode: Shadow")

        status_layout.addWidget(self.auth_label)
        status_layout.addWidget(self.equity_label)
        status_layout.addWidget(self.mode_label)
        status_group.setLayout(status_layout)
        layout.addWidget(status_group)

        layout.addStretch()

    def _on_kill_switch(self, checked: bool):
        if checked:
            self.kill_btn.setText("KILL SWITCH - ON")
            self.kill_btn.setStyleSheet("background-color: #ff0000; color: white; font-weight: bold;")
        else:
            self.kill_btn.setText("KILL SWITCH - OFF")
            self.kill_btn.setStyleSheet("")
        self.kill_switch_toggled.emit(checked)

    def set_engine_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def set_auth_status(self, connected: bool, detail: str = ""):
        if connected:
            self.auth_label.setText(f"Auth: Connected {detail}")
            self.auth_label.setStyleSheet("color: #00ff88;")
        else:
            self.auth_label.setText(f"Auth: Disconnected {detail}")
            self.auth_label.setStyleSheet("color: #ff4444;")

    def set_equity(self, equity: float):
        self.equity_label.setText(f"Equity: ${equity:,.2f}")

    def set_mode(self, shadow: bool):
        self.mode_label.setText(f"Mode: {'Shadow' if shadow else 'LIVE'}")
        self.mode_label.setStyleSheet(
            "color: #ffaa00;" if shadow else "color: #ff4444; font-weight: bold;"
        )
