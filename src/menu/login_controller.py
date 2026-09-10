# ── login_controller.py ───────────────────────────────────────────────────────
# Authentication logic, completely decoupled from any UI.
# Swap the internals (hash check, DB call, API request…) without touching views.

from __future__ import annotations
from dataclasses import dataclass
import src.menu.authentication.encrypt as encrypt
import os
import src.menu.authentication.firebase as firebase

@dataclass
class AuthResult:
    success: bool
    username: str = ""
    error: str    = ""


class LoginController:
    """
    Handles credential validation and session state.

    The view calls `authenticate(username, password)` and receives an
    AuthResult.  No tkinter imports here.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def authenticate(self, username: str, password: str) -> AuthResult:
        """
        Validate credentials and return an AuthResult.

        Replace the body of `_check_credentials` with your real auth logic
        (database lookup, hashed password comparison, API call, etc.).
        """
        # 1. Client-side validation (fast, no I/O)
        if not username:
            return AuthResult(success=False, error="⚠  Username is required.")
        if not password:
            return AuthResult(success=False, error="⚠  Password is required.")

        # 2. Credential check (swap this for real logic)
        if not self._check_credentials(username, password):
            return AuthResult(success=False, error="⚠  Invalid username or password.")
        LoginController._load_user_info(username)
        return AuthResult(success=True, username=username)

    def validate_field(self, field: str, value: str) -> str:
        """
        Return an inline error string for a single field, or "" if valid.
        Useful for real-time validation as the user types.
        """
        if field == "username" and not value:
            return "Username cannot be empty."
        if field == "password" and len(value) < 1:
            return "Password cannot be empty."
        return ""
    
    def register_user(self, username, password):
        results = firebase.register_new_user(username, password)
        LoginController._load_user_info(username)
        if(results == None):
            return False
        else:
            return True

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _load_user_info(username):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        key_path = os.path.join(current_dir, "secret.key")
        info_path = os.path.join(current_dir, "user_info.enc")
        my_key = encrypt.load_key(key_path)
        encrypt.write_encrypted_file(info_path, username, my_key)


    @staticmethod
    def _check_credentials(username: str, password: str) -> bool:
        """
        Demo: accept any non-empty credentials.
        Replace with: DB lookup, bcrypt.checkpw(), OAuth token exchange, etc.
        """
        results = firebase.check_credentials(username, password)
        if(results == None):
            return False
        else:
            return True

