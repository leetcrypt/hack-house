"""Use-case tests for the hack-house multi-user clergy features (capacity cap,
roster, username + slot lifecycle). In-process via sanic-testing.
"""
import base64
import json

import srp

from cmd_chat.server.models import UserSession
from cmd_chat.server.views import _roster_frame


def _add_member(app, name):
    """Seat a verified member directly in the session store."""
    app.ctx.session_store.add(
        UserSession(user_id=f"id-{name}", ip="127.0.0.1", username=name)
    )


def _init(test_client, name):
    """Run /srp/init for a fresh username (just the first handshake leg)."""
    usr = srp.User(b"chat", b"testpassword")
    _, A = usr.start_authentication()
    return test_client.post(
        "/srp/init",
        json={"username": name, "A": base64.b64encode(A).decode()},
    )


class TestClergyCapacity:
    def test_accepts_up_to_capacity(self, app, test_client):
        app.ctx.max_users = 4
        for n in ("a", "b", "c"):
            _add_member(app, n)
        # 4th seat is still available
        _, resp = _init(test_client, "fourth")
        assert resp.status == 200

    def test_rejects_when_full(self, app, test_client):
        app.ctx.max_users = 4
        for n in ("a", "b", "c", "d"):
            _add_member(app, n)
        _, resp = _init(test_client, "fifth")
        assert resp.status == 409
        assert "full" in resp.json["error"].lower()

    def test_capacity_is_configurable(self, app, test_client):
        app.ctx.max_users = 2
        for n in ("a", "b"):
            _add_member(app, n)
        _, resp = _init(test_client, "third")
        assert resp.status == 409


class TestUsernames:
    def test_duplicate_username_rejected(self, app, test_client):
        _add_member(app, "ghost")
        _, resp = _init(test_client, "ghost")
        assert resp.status == 409
        assert "taken" in resp.json["error"].lower()


class TestRoster:
    def test_roster_frame_contents(self, app):
        _add_member(app, "alice")
        _add_member(app, "bob")
        frame = json.loads(_roster_frame(app))
        assert frame["type"] == "roster"
        assert frame["capacity"] == app.ctx.max_users
        assert {u["username"] for u in frame["users"]} == {"alice", "bob"}
        assert all("user_id" in u for u in frame["users"])


class TestSlotLifecycle:
    def test_disconnect_frees_slot_and_username(self, app, test_client):
        app.ctx.max_users = 1
        _add_member(app, "ghost")
        assert app.ctx.session_store.username_exists("ghost")
        # full room rejects a newcomer
        _, resp = _init(test_client, "intruder")
        assert resp.status == 409

        # the disconnect path removes the session (frees slot + name)
        app.ctx.session_store.remove("id-ghost")
        assert not app.ctx.session_store.username_exists("ghost")
        assert app.ctx.session_store.count() == 0

        # name is reusable and the slot is open again
        _, resp = _init(test_client, "ghost")
        assert resp.status == 200
