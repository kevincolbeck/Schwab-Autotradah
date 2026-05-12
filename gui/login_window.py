"""Schwab OAuth login screen shown at app startup."""

import logging
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QApplication,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap

from gui.styles import DARK_THEME

logger = logging.getLogger(__name__)


class AuthThread(QThread):
    """Runs Schwab OAuth flow in background thread."""
    auth_success = Signal(object)
    auth_failed = Signal(str)
    status_update = Signal(str)

    def __init__(self, client_id, client_secret, redirect_url, token_path):
        super().__init__()
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_url = redirect_url
        self.token_path = token_path

    def run(self):
        from broker.auth import encrypt_token_file
        token_path = self.token_path

        # Delete any stale token files so we start fresh
        for f in [token_path, token_path + ".enc"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        self.status_update.emit("Opening browser for Schwab login...")
        try:
            from schwab import auth as schwab_auth
            client = schwab_auth.client_from_login_flow(
                api_key=self.client_id,
                app_secret=self.client_secret,
                callback_url=self.redirect_url,
                token_path=token_path,
                interactive=False,
                callback_timeout=300.0,
            )
            encrypt_token_file(token_path)
            logger.info("OAuth login flow completed successfully")
            self.auth_success.emit(client)
        except Exception as e:
            logger.error(f"OAuth flow failed: {e}")
            self.auth_failed.emit(str(e))


class LoginWindow(QWidget):
    """Login screen with 'Sign in with Schwab' button."""

    login_complete = Signal(object)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.auth_thread = None

        self.setWindowTitle("Schwab Live Trader - Sign In")
        self.setFixedSize(420, 480)
        self.setStyleSheet(DARK_THEME)

        icon_path = Path(__file__).parent.parent / "assets" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._init_ui(icon_path)

    def _init_ui(self, icon_path: Path):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(16)

        layout.addStretch(1)

        # App icon
        if icon_path.exists():
            icon_label = QLabel()
            pixmap = QPixmap(str(icon_path))
            scaled = pixmap.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(scaled)
            icon_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(icon_label)
            layout.addSpacing(8)

        # Title
        title = QLabel("Schwab Live Trader")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #e0e0e0;")
        layout.addWidget(title)

        # Subtitle
        subtitle = QLabel("Momentum Trading Bot")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("font-size: 13px; color: #8888cc;")
        layout.addWidget(subtitle)

        layout.addSpacing(24)

        # Sign in button - always visible
        self.sign_in_btn = QPushButton("Sign in with Schwab")
        self.sign_in_btn.setStyleSheet("""
            QPushButton {
                background-color: #0066cc;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 14px 24px;
                font-size: 16px;
                font-weight: bold;
                min-height: 44px;
            }
            QPushButton:hover {
                background-color: #0077ee;
            }
            QPushButton:pressed {
                background-color: #0055aa;
            }
            QPushButton:disabled {
                background-color: #333355;
                color: #666;
            }
        """)
        self.sign_in_btn.clicked.connect(self._on_sign_in)
        layout.addWidget(self.sign_in_btn)

        # Status label
        self.status_label = QLabel("Click above to sign in with your Schwab account")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("font-size: 12px; color: #888;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch(2)

        # Footer
        footer = QLabel("Secure OAuth2 authentication via Schwab Developer API")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("font-size: 10px; color: #555;")
        layout.addWidget(footer)

    def _on_sign_in(self):
        """User clicked Sign in - start full OAuth browser flow."""
        self.sign_in_btn.setEnabled(False)
        self.status_label.setText(
            "Opening browser for Schwab login...\n"
            "Complete the login and accept the security warning."
        )
        self.status_label.setStyleSheet("font-size: 12px; color: #888;")

        self.auth_thread = AuthThread(
            self.config.schwab.client_id,
            self.config.schwab.client_secret,
            self.config.schwab.redirect_url,
            self.config.schwab.token_file,
        )
        self.auth_thread.auth_success.connect(self._on_auth_success)
        self.auth_thread.auth_failed.connect(self._on_auth_failed)
        self.auth_thread.status_update.connect(self._on_status_update)
        self.auth_thread.start()

    def _on_auth_success(self, client):
        self.status_label.setText("Authenticated! Loading...")
        self.status_label.setStyleSheet("font-size: 12px; color: #00ff88;")
        self.login_complete.emit(client)

    def _on_auth_failed(self, error):
        """OAuth flow failed."""
        self.status_label.setText(f"Login failed: {error}\nPlease try again.")
        self.status_label.setStyleSheet("font-size: 12px; color: #ff4444;")
        self.sign_in_btn.setEnabled(True)
        self.sign_in_btn.setVisible(True)

    def _on_status_update(self, text):
        self.status_label.setText(text)
