from __future__ import annotations

from ssh_server_manager.askpass import choose_descriptor


def test_askpass_never_sends_password_to_otp_or_host_key_prompt():
    descriptors = [
        {
            "credential_id": "id",
            "slot": "password",
            "alias": "box",
            "hostname": "box.example",
            "username": "alice",
        }
    ]
    assert choose_descriptor("alice@box.example's password:", descriptors)["credential_id"] == "id"
    assert choose_descriptor("Enter OTP:", descriptors) is None
    assert choose_descriptor("Are you sure you want to continue connecting (yes/no/[fingerprint])?", descriptors) is None
