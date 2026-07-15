from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any

from .db import Database


class AuthenticationError(RuntimeError):
    pass


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass
class PendingCeremony:
    kind: str
    challenge: bytes


class RevealAuth:
    def __init__(self, database: Database, *, rp_id: str = "localhost", origin: str) -> None:
        self.database = database
        self.rp_id = rp_id
        self.origin = origin
        self.pending: dict[str, PendingCeremony] = {}

    def status(self) -> dict[str, Any]:
        return {
            "passkeys": len(self.database.list_webauthn_credentials()),
            "master_password_enrolled": bool(self.database.get_setting("master_password_hash")),
            "webauthn_available": self._webauthn_available(),
        }

    @staticmethod
    def _webauthn_available() -> bool:
        try:
            import webauthn  # noqa: F401
        except ImportError:
            return False
        return True

    def _user_id(self) -> bytes:
        existing = self.database.get_setting("webauthn_user_id")
        if existing:
            return b64url_decode(existing)
        value = secrets.token_bytes(32)
        self.database.set_setting("webauthn_user_id", b64url_encode(value))
        return value

    def begin_registration(self, session_id: str) -> dict[str, Any]:
        try:
            from webauthn import generate_registration_options, options_to_json
            from webauthn.helpers.structs import (
                AuthenticatorSelectionCriteria,
                PublicKeyCredentialDescriptor,
                ResidentKeyRequirement,
                UserVerificationRequirement,
            )
        except ImportError as exc:
            raise AuthenticationError("WebAuthn support is not installed") from exc
        existing = self.database.list_webauthn_credentials()
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="SSH Server Manager",
            user_id=self._user_id(),
            user_name=os.environ.get("USER") or os.environ.get("USERNAME") or "local-user",
            user_display_name="Local SSH Server Manager user",
            exclude_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(item["credential_id"])) for item in existing],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        self.pending[session_id] = PendingCeremony("register", options.challenge)
        return json.loads(options_to_json(options))

    def finish_registration(self, session_id: str, response: dict[str, Any]) -> dict[str, Any]:
        pending = self.pending.pop(session_id, None)
        if not pending or pending.kind != "register":
            raise AuthenticationError("registration challenge is missing or expired")
        try:
            from webauthn import verify_registration_response

            verification = verify_registration_response(
                credential=response,
                expected_challenge=pending.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except Exception as exc:
            raise AuthenticationError(f"passkey registration failed: {exc}") from exc
        transports = response.get("response", {}).get("transports", [])
        record = {
            "credential_id": b64url_encode(verification.credential_id),
            "public_key": b64url_encode(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "transports": transports,
            "device_type": str(getattr(verification, "credential_device_type", "")),
            "backed_up": bool(getattr(verification, "credential_backed_up", False)),
        }
        self.database.save_webauthn_credential(record)
        return {"ok": True, "credential_id": record["credential_id"]}

    def begin_authentication(self, session_id: str) -> dict[str, Any]:
        try:
            from webauthn import generate_authentication_options, options_to_json
            from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement
        except ImportError as exc:
            raise AuthenticationError("WebAuthn support is not installed") from exc
        credentials = self.database.list_webauthn_credentials()
        if not credentials:
            raise AuthenticationError("no passkey is enrolled")
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=b64url_decode(item["credential_id"])) for item in credentials
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        self.pending[session_id] = PendingCeremony("authenticate", options.challenge)
        return json.loads(options_to_json(options))

    def finish_authentication(self, session_id: str, response: dict[str, Any]) -> dict[str, Any]:
        pending = self.pending.pop(session_id, None)
        if not pending or pending.kind != "authenticate":
            raise AuthenticationError("authentication challenge is missing or expired")
        response_id = response.get("id") or response.get("rawId")
        credential = next(
            (item for item in self.database.list_webauthn_credentials() if item["credential_id"] == response_id),
            None,
        )
        if not credential:
            raise AuthenticationError("passkey is not registered with this application")
        try:
            from webauthn import verify_authentication_response

            verification = verify_authentication_response(
                credential=response,
                expected_challenge=pending.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                credential_public_key=b64url_decode(credential["public_key"]),
                credential_current_sign_count=credential["sign_count"],
                require_user_verification=True,
            )
        except Exception as exc:
            raise AuthenticationError(f"passkey authentication failed: {exc}") from exc
        self.database.update_webauthn_sign_count(credential["credential_id"], verification.new_sign_count)
        return {"ok": True}

    def enroll_master_password(self, password: str) -> dict[str, Any]:
        if self.database.get_setting("master_password_hash"):
            raise AuthenticationError("a master password is already enrolled")
        if len(password) < 12:
            raise AuthenticationError("master password must contain at least 12 characters")
        try:
            from argon2 import PasswordHasher
        except ImportError as exc:
            raise AuthenticationError("Argon2 support is not installed") from exc
        self.database.set_setting("master_password_hash", PasswordHasher().hash(password))
        return {"ok": True}

    def verify_master_password(self, password: str) -> bool:
        encoded = self.database.get_setting("master_password_hash")
        if not encoded:
            raise AuthenticationError("no master password is enrolled")
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError

            valid = PasswordHasher().verify(encoded, password)
        except VerifyMismatchError:
            return False
        except ImportError as exc:
            raise AuthenticationError("Argon2 support is not installed") from exc
        return bool(valid)

