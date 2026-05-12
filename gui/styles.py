"""Dark theme stylesheet for the trading bot GUI."""

DARK_THEME = """
QMainWindow {
    background-color: #1a1a2e;
    color: #e0e0e0;
}

QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: "Segoe UI", "Consolas", monospace;
    font-size: 12px;
}

QGroupBox {
    border: 1px solid #3a3a5c;
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    font-size: 13px;
    color: #8888cc;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}

QLabel {
    color: #e0e0e0;
}

QLabel#regime_label {
    font-size: 24px;
    font-weight: bold;
    padding: 8px 16px;
    border-radius: 6px;
}

QLabel#regime_TREND {
    color: #00ff88;
    background-color: #0a3320;
}

QLabel#regime_CHOP {
    color: #ffaa00;
    background-color: #332a0a;
}

QLabel#regime_RISK_OFF {
    color: #ff4444;
    background-color: #330a0a;
}

QPushButton {
    background-color: #2a2a4e;
    color: #e0e0e0;
    border: 1px solid #4a4a7c;
    border-radius: 4px;
    padding: 8px 16px;
    font-size: 12px;
    min-height: 28px;
}

QPushButton:hover {
    background-color: #3a3a6e;
    border-color: #6a6aac;
}

QPushButton:pressed {
    background-color: #4a4a8e;
}

QPushButton:disabled {
    background-color: #1a1a2e;
    color: #555;
    border-color: #333;
}

QPushButton#kill_switch {
    background-color: #8b0000;
    color: white;
    font-size: 16px;
    font-weight: bold;
    border: 2px solid #ff0000;
    border-radius: 8px;
    padding: 12px 24px;
    min-height: 40px;
}

QPushButton#kill_switch:hover {
    background-color: #b00000;
}

QPushButton#kill_switch:checked {
    background-color: #ff0000;
    color: white;
}

QPushButton#start_button {
    background-color: #0a4a0a;
    border-color: #00aa00;
    color: #00ff00;
    font-weight: bold;
}

QPushButton#stop_button {
    background-color: #4a0a0a;
    border-color: #aa0000;
    color: #ff4444;
    font-weight: bold;
}

QDoubleSpinBox, QSpinBox {
    background-color: #2a2a4e;
    color: #e0e0e0;
    border: 1px solid #4a4a7c;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 14px;
}

QDoubleSpinBox:focus, QSpinBox:focus {
    border-color: #6a6aff;
}

QCheckBox {
    color: #e0e0e0;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 18px;
    height: 18px;
}

QTableWidget {
    background-color: #16162b;
    alternate-background-color: #1e1e3a;
    color: #e0e0e0;
    gridline-color: #2a2a4e;
    border: 1px solid #3a3a5c;
    border-radius: 4px;
    selection-background-color: #3a3a6e;
}

QTableWidget::item {
    padding: 4px 8px;
}

QHeaderView::section {
    background-color: #222244;
    color: #8888cc;
    border: 1px solid #3a3a5c;
    padding: 6px;
    font-weight: bold;
}

QTextEdit#log_viewer {
    background-color: #0d0d1a;
    color: #00ff88;
    border: 1px solid #3a3a5c;
    border-radius: 4px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 11px;
    padding: 4px;
}

QStatusBar {
    background-color: #16162b;
    color: #888;
    border-top: 1px solid #3a3a5c;
}

QProgressBar {
    border: 1px solid #3a3a5c;
    border-radius: 4px;
    text-align: center;
    background-color: #1a1a2e;
}

QProgressBar::chunk {
    background-color: #4444aa;
}

QSplitter::handle {
    background-color: #3a3a5c;
}

/* Chat panel styles */
QFrame#proposal_card {
    background-color: #1e2a3e;
    border: 1px solid #4a6a9c;
    border-radius: 6px;
    padding: 6px;
    margin: 4px 0;
}

QScrollArea {
    border: 1px solid #3a3a5c;
    border-radius: 4px;
}

QLineEdit {
    background-color: #2a2a4e;
    color: #e0e0e0;
    border: 1px solid #4a4a7c;
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 12px;
}

QLineEdit:focus {
    border-color: #6a6aff;
}

QDateEdit {
    background-color: #2a2a4e;
    color: #e0e0e0;
    border: 1px solid #4a4a7c;
    border-radius: 4px;
    padding: 4px 8px;
}
"""
