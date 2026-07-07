"""Interop gate for the pure-Python SRP shim.

The server authenticates with the real ``srp`` package (C-extension backend, see
cmd_chat/server/srp_auth.py). SRP is unforgiving: any mismatch in the N/g group,
k/x/u derivation, or the M1 / H_AMK hash inputs makes authentication fail. These
tests force the *pure* client (cmd_chat/client/_srp_pure) through a full
handshake and assert success — so a broken constant can never ship silently.
"""

import base64

import pytest
import srp

from cmd_chat.client import _srp_pure as pure

# The client always runs in RFC 5054 mode (client.py calls srp.rfc5054_enable()
# at import). Match that here for both the real lib and the shim.
srp.rfc5054_enable()
pure.rfc5054_enable()

PASSWORD = b"testpassword"  # matches conftest's SRPAuthManager("testpassword")


def test_pure_user_authenticates_against_real_verifier():
    """Directly cross-check the shim's User against the real srp.Verifier —
    the same class the server instantiates."""
    salt, vkey = srp.create_salted_verification_key(
        b"chat", PASSWORD, hash_alg=srp.SHA256
    )

    usr = pure.User(b"chat", PASSWORD, hash_alg=pure.SHA256)
    _, A = usr.start_authentication()

    svr = srp.Verifier(b"chat", salt, vkey, A, hash_alg=srp.SHA256)
    s, B = svr.get_challenge()
    assert B is not None

    M = usr.process_challenge(s, B)
    assert M is not None, "pure client failed the SRP-6a safety checks"

    H_AMK = svr.verify_session(M)
    assert H_AMK is not None, "real Verifier rejected the pure client's proof M1"

    usr.verify_session(H_AMK)
    assert usr.authenticated(), "pure client rejected the server's H_AMK"
    assert svr.authenticated()


def test_pure_user_wrong_password_is_rejected():
    """Negative control: a bad password must not authenticate — proves the
    positive test isn't passing for a trivial reason."""
    salt, vkey = srp.create_salted_verification_key(
        b"chat", PASSWORD, hash_alg=srp.SHA256
    )

    usr = pure.User(b"chat", b"wrongpassword", hash_alg=pure.SHA256)
    _, A = usr.start_authentication()

    svr = srp.Verifier(b"chat", salt, vkey, A, hash_alg=srp.SHA256)
    s, B = svr.get_challenge()

    M = usr.process_challenge(s, B)
    # Either the server rejects M outright, or (defensively) our client refuses
    # to accept the resulting proof — never authenticated.
    assert svr.verify_session(M) is None
    assert not svr.authenticated()


def test_pure_client_full_http_handshake(test_client):
    """End-to-end against a real running server room: run the exact
    /srp/init -> /srp/verify flow client.py performs, but forced through the
    pure implementation, and assert the room authenticates the phone operator."""
    username = "phone-op"

    usr = pure.User(b"chat", PASSWORD, hash_alg=pure.SHA256)
    _, A = usr.start_authentication()

    _, init_resp = test_client.post(
        "/srp/init",
        json={"username": username, "A": base64.b64encode(A).decode()},
    )
    assert init_resp.status == 200, init_resp.text
    init = init_resp.json

    user_id = init["user_id"]
    B = base64.b64decode(init["B"])
    salt = base64.b64decode(init["salt"])
    assert "room_salt" in init  # client derives the Fernet room key from this

    M = usr.process_challenge(salt, B)
    assert M is not None

    _, verify_resp = test_client.post(
        "/srp/verify",
        json={
            "user_id": user_id,
            "username": username,
            "M": base64.b64encode(M).decode(),
        },
    )
    assert verify_resp.status == 200, verify_resp.text
    verify = verify_resp.json

    H_AMK = base64.b64decode(verify["H_AMK"])
    usr.verify_session(H_AMK)

    assert usr.authenticated(), "server accepted us but H_AMK did not match"
    assert verify["ws_token"], "no ws_token issued — cannot join the room"
