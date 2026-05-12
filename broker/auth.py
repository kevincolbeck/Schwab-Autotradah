"""Schwab OAuth2 authentication and token management."""

import os
import json
import logging
import webbrowser
import http.server
import urllib.parse
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from typing import Optional

from schwab import auth as schwab_auth
from schwab.client import Client as SchwabClientClass

logger = logging.getLogger(__name__)

# Encryption key file for token storage
KEY_FILE = ".token_key"
ENCRYPTED_TOKEN_FILE = "token.enc"


def _get_or_create_key(key_path: str = KEY_FILE) -> bytes:
    """Get or generate an encryption key for token storage."""
    if os.path.exists(key_path):
        with open(key_path, 'rb') as f:
            return f.read()
    key = Fernet.generate_key()
    with open(key_path, 'wb') as f:
        f.write(key)
    return key


def encrypt_token_file(token_path: str, key_path: str = KEY_FILE):
    """Encrypt the plaintext token file."""
    if not os.path.exists(token_path):
        return
    key = _get_or_create_key(key_path)
    fernet = Fernet(key)
    with open(token_path, 'rb') as f:
        data = f.read()
    encrypted = fernet.encrypt(data)
    enc_path = token_path + ".enc"
    with open(enc_path, 'wb') as f:
        f.write(encrypted)
    os.remove(token_path)
    logger.info("Token file encrypted")


def decrypt_token_file(enc_path: str, token_path: str, key_path: str = KEY_FILE) -> bool:
    """Decrypt token file back to plaintext for schwab-py to use."""
    if not os.path.exists(enc_path):
        return False
    if not os.path.exists(key_path):
        logger.error("Encryption key not found, cannot decrypt tokens")
        return False
    key = _get_or_create_key(key_path)
    fernet = Fernet(key)
    with open(enc_path, 'rb') as f:
        encrypted = f.read()
    try:
        decrypted = fernet.decrypt(encrypted)
        with open(token_path, 'wb') as f:
            f.write(decrypted)
        return True
    except Exception as e:
        logger.error(f"Token decryption failed: {e}")
        return False


def create_client(
    client_id: str,
    client_secret: str,
    redirect_url: str,
    token_path: str = "token.json",
) -> Optional[SchwabClientClass]:
    """Create an authenticated Schwab client.

    Tries to load existing tokens first. If no valid tokens exist,
    initiates the OAuth flow via browser.
    """
    enc_path = token_path + ".enc"

    # Try to restore encrypted token file
    if os.path.exists(enc_path) and not os.path.exists(token_path):
        decrypt_token_file(enc_path, token_path)

    try:
        # Try loading existing token (schwab-py handles refresh automatically)
        client = schwab_auth.client_from_token_file(
            token_path=token_path,
            api_key=client_id,
            app_secret=client_secret,
        )
        logger.info("Authenticated with existing tokens")
        # Re-encrypt after potential refresh
        encrypt_token_file(token_path)
        return client
    except Exception as e:
        logger.info(f"No valid token file, starting OAuth flow: {e}")

    # Start OAuth flow
    try:
        client = schwab_auth.client_from_manual_flow(
            api_key=client_id,
            app_secret=client_secret,
            callback_url=redirect_url,
            token_path=token_path,
        )
        logger.info("OAuth flow completed successfully")
        encrypt_token_file(token_path)
        return client
    except Exception as e:
        logger.error(f"OAuth flow failed: {e}")
        return None


def verify_auth(client: SchwabClientClass) -> bool:
    """Verify that the client can make authenticated requests."""
    try:
        resp = client.get_account_numbers()
        if resp.status_code == 200:
            logger.info("Auth verification successful")
            return True
        logger.error(f"Auth verification failed: HTTP {resp.status_code}")
        return False
    except Exception as e:
        logger.error(f"Auth verification error: {e}")
        return False
