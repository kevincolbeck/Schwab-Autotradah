"""Dialog showing parameter diff before saving to live config.yaml."""

from typing import Dict, Tuple

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QDialogButtonBox, QHeaderView,
)
from PySide6.QtGui import QColor


class LiveDiffDialog(QDialog):
    """Shows a before/after diff and asks for confirmation."""

    def __init__(self, changes: Dict[str, Tuple[object, object]], parent=None):
        """
        Args:
            changes: {param_path: (old_value, new_value)}
        """
        super().__init__(parent)
        self.setWindowTitle("Apply to Live Config")
        self.setMinimumWidth(500)
        self.setMinimumHeight(300)

        layout = QVBoxLayout(self)

        warning = QLabel(
            "The following changes will be saved to config.yaml\n"
            "and will affect LIVE TRADING on next bot start."
        )
        warning.setStyleSheet("color: #ffaa00; font-weight: bold; font-size: 13px;")
        layout.addWidget(warning)

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Parameter", "Current (Live)", "New Value"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(changes))

        for row, (param, (old_val, new_val)) in enumerate(changes.items()):
            table.setItem(row, 0, QTableWidgetItem(param))
            old_item = QTableWidgetItem(str(old_val))
            old_item.setForeground(QColor("#888888"))
            table.setItem(row, 1, old_item)
            new_item = QTableWidgetItem(str(new_val))
            new_item.setForeground(QColor("#00ff88"))
            table.setItem(row, 2, new_item)

        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Save to config.yaml")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
