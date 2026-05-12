"""Setup wizard for first-run Schwab OAuth credential configuration."""

import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def run_setup_wizard(config=None):
    """Interactive setup wizard for Schwab API credentials.
    Can be run standalone or called from main.py.
    Returns True if setup was successful.
    """
    from PySide6.QtWidgets import (
        QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
        QLineEdit, QPushButton, QGroupBox, QMessageBox,
    )
    from PySide6.QtCore import Qt

    class SetupDialog(QDialog):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Schwab Momentum Bot - Setup")
            self.setMinimumWidth(500)
            self.result_ok = False
            self._init_ui()

        def _init_ui(self):
            layout = QVBoxLayout(self)

            # Header
            header = QLabel(
                "<h2>Schwab API Setup</h2>"
                "<p>Register your app at <b>developer.schwab.com</b> to get credentials.</p>"
            )
            header.setTextFormat(Qt.RichText)
            header.setWordWrap(True)
            layout.addWidget(header)

            # Credentials group
            cred_group = QGroupBox("API Credentials")
            cred_layout = QVBoxLayout()

            # Client ID
            cred_layout.addWidget(QLabel("Client ID (API Key):"))
            self.client_id_input = QLineEdit()
            self.client_id_input.setPlaceholderText("Your Schwab app client ID")
            cred_layout.addWidget(self.client_id_input)

            # Client Secret
            cred_layout.addWidget(QLabel("Client Secret:"))
            self.client_secret_input = QLineEdit()
            self.client_secret_input.setEchoMode(QLineEdit.Password)
            self.client_secret_input.setPlaceholderText("Your Schwab app secret")
            cred_layout.addWidget(self.client_secret_input)

            # Redirect URL
            cred_layout.addWidget(QLabel("Redirect URL:"))
            self.redirect_input = QLineEdit()
            self.redirect_input.setText("https://127.0.0.1:8182/callback")
            self.redirect_input.setPlaceholderText("https://127.0.0.1:8182/callback")
            cred_layout.addWidget(self.redirect_input)

            cred_group.setLayout(cred_layout)
            layout.addWidget(cred_group)

            # Instructions
            instructions = QLabel(
                "<b>Steps:</b><br>"
                "1. Go to developer.schwab.com and create an app<br>"
                "2. Copy your Client ID and Client Secret<br>"
                "3. Set the Callback URL in your Schwab app settings<br>"
                "4. Link your brokerage account to the app<br>"
                "5. Enter credentials above and click Save"
            )
            instructions.setTextFormat(Qt.RichText)
            instructions.setWordWrap(True)
            instructions.setStyleSheet("color: #888; padding: 8px;")
            layout.addWidget(instructions)

            # Buttons
            btn_layout = QHBoxLayout()
            save_btn = QPushButton("Save && Authenticate")
            save_btn.clicked.connect(self._on_save)
            cancel_btn = QPushButton("Cancel")
            cancel_btn.clicked.connect(self.reject)
            btn_layout.addStretch()
            btn_layout.addWidget(cancel_btn)
            btn_layout.addWidget(save_btn)
            layout.addLayout(btn_layout)

        def _on_save(self):
            client_id = self.client_id_input.text().strip()
            client_secret = self.client_secret_input.text().strip()
            redirect_url = self.redirect_input.text().strip()

            if not client_id or not client_secret:
                QMessageBox.warning(self, "Missing", "Client ID and Secret are required.")
                return

            self.result_ok = True
            self.client_id = client_id
            self.client_secret = client_secret
            self.redirect_url = redirect_url
            self.accept()

    # Check if QApplication already exists
    app = QApplication.instance()
    standalone = app is None
    if standalone:
        app = QApplication(sys.argv)

    dialog = SetupDialog()

    # Pre-fill from config if available
    if config:
        if config.schwab.client_id:
            dialog.client_id_input.setText(config.schwab.client_id)
        if config.schwab.redirect_url:
            dialog.redirect_input.setText(config.schwab.redirect_url)

    if dialog.exec() and dialog.result_ok:
        if config:
            config.schwab.client_id = dialog.client_id
            config.schwab.client_secret = dialog.client_secret
            config.schwab.redirect_url = dialog.redirect_url
        return True

    return False


if __name__ == "__main__":
    from core.config import load_config, save_config

    config = load_config("config.yaml")
    if run_setup_wizard(config):
        save_config(config)
        print("Setup saved to config.yaml")
        print("You can now run: python main.py")
    else:
        print("Setup cancelled")
