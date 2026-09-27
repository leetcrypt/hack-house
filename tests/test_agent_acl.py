"""Multi-tenant query ACL + per-prompt approval for the AI agent instance.

Covers spec-multi-tenant-model-hosting §2: query control lives in the instance
owner's runtime (this agent process), decentralized from the room host. These
tests exercise the pure predicates and the async control-verb handlers directly,
driving the bridge with a fake websocket that captures the frames it would send.

Style mirrors test_operator_bridge.py: each async body runs inside asyncio.run().
"""
import asyncio
import json

from cryptography.fernet import Fernet

from cmd_chat.agent.bridge import AgentBridge


class _FakeProvider:
    name = "ollama"
    model = "qwen2.5:3b"

    def complete(self, system, messages):  # pragma: no cover - not exercised here
        return "ok"


class _FakeWs:
    """Captures every frame the bridge sends, decrypted back to plaintext."""

    def __init__(self, fernet: Fernet):
        self._fernet = fernet
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(self._fernet.decrypt(payload.encode()).decode())

    def texts(self) -> list[str]:
        """Human-visible chat lines (not JSON control frames)."""
        return [s for s in self.sent if not s.startswith('{"_')]

    def instance_frames(self) -> list[dict]:
        out = []
        for s in self.sent:
            if s.startswith('{"_'):
                d = json.loads(s)
                if d.get("_ai") == "instance":
                    out.append(d)
        return out


def _bridge(owner="alice", query_acl="public", allow=None, ask_mode=False):
    b = AgentBridge(
        "h", 1, name="oracle", provider=_FakeProvider(),
        owner=owner, query_acl=query_acl, allow=allow, ask_mode=ask_mode,
    )
    key = Fernet.generate_key()
    b.room_fernet = Fernet(key)
    return b, _FakeWs(b.room_fernet)


# --------------------------------------------------------------------------- #
# _may_query — the standing gate
# --------------------------------------------------------------------------- #

def test_unowned_instance_answers_everyone():
    b, _ = _bridge(owner=None)
    assert b._may_query("anyone")
    assert b._may_query("stranger")


def test_public_allows_all_but_denied():
    b, _ = _bridge(owner="alice", query_acl="public")
    assert b._may_query("bob")            # random member, public
    b.denied.add("mallory")
    assert not b._may_query("mallory")    # denylist wins even in public


def test_private_allows_only_owner_managers_allowlist():
    b, _ = _bridge(owner="alice", query_acl="private", allow=["bob"])
    assert b._may_query("alice")          # owner
    assert b._may_query("bob")            # allowlisted
    assert not b._may_query("carol")      # unlisted → denied in private
    b.managers.add("carol")
    assert b._may_query("carol")          # delegated manager


# --------------------------------------------------------------------------- #
# _parse_management — named-form recognition
# --------------------------------------------------------------------------- #

def test_parse_management_named_form_only():
    b, _ = _bridge()
    assert b._parse_management("/ai oracle allow bob") == ("allow", "bob")
    assert b._parse_management("/ai oracle private") == ("private", "")
    # wrong instance name → not ours
    assert b._parse_management("/ai other allow bob") is None
    # a real question that isn't a verb
    assert b._parse_management("/ai oracle what is up") is None
    # bare question (sole form) is never a management line
    assert b._parse_management("/ai allow me in") is None


# --------------------------------------------------------------------------- #
# _handle_management — owner gating + state mutation + re-announce
# --------------------------------------------------------------------------- #

def test_non_owner_cannot_manage():
    b, ws = _bridge(owner="alice")
    asyncio.run(b._handle_management(ws, "private", "", "bob"))
    assert b.query_mode == "public"                       # unchanged
    assert any("only oracle's owner" in t for t in ws.texts())
    assert ws.instance_frames() == []                     # no state broadcast


def test_owner_sets_private_and_allow():
    b, ws = _bridge(owner="alice", query_acl="public")
    asyncio.run(b._handle_management(ws, "private", "", "alice"))
    asyncio.run(b._handle_management(ws, "allow", "bob", "alice"))
    assert b.query_mode == "private"
    assert "bob" in b.allowed
    frames = ws.instance_frames()
    assert frames and frames[-1]["query"] == "private"    # re-announced


def test_reject_denies_and_drops_pending():
    b, ws = _bridge(owner="alice")
    b._prompts = [{"id": 1, "sender": "mallory", "question": "hi"}]
    asyncio.run(b._handle_management(ws, "reject", "mallory", "alice"))
    assert "mallory" in b.denied
    assert b._prompts == []                                # pending purged


def test_grant_is_owner_only_not_manager():
    b, ws = _bridge(owner="alice")
    b.managers.add("bob")                                  # bob is a manager
    asyncio.run(b._handle_management(ws, "grant", "carol", "bob"))
    assert "carol" not in b.managers                       # managers can't delegate
    assert any("only the owner may delegate" in t for t in ws.texts())
    asyncio.run(b._handle_management(ws, "grant", "carol", "alice"))
    assert "carol" in b.managers                           # owner can


def test_ask_mode_toggle():
    b, ws = _bridge(owner="alice")
    asyncio.run(b._handle_management(ws, "ask-mode", "on", "alice"))
    assert b.ask_mode is True
    asyncio.run(b._handle_management(ws, "ask-mode", "off", "alice"))
    assert b.ask_mode is False


# --------------------------------------------------------------------------- #
# _gate_query — deny / hold / pass
# --------------------------------------------------------------------------- #

def test_gate_denies_in_private():
    b, ws = _bridge(owner="alice", query_acl="private")
    passed = asyncio.run(b._gate_query(ws, "carol", "secret question"))
    assert passed is False
    assert any("restricts who may query" in t for t in ws.texts())


def test_gate_holds_under_ask_mode():
    b, ws = _bridge(owner="alice", ask_mode=True)
    passed = asyncio.run(b._gate_query(ws, "bob", "please summarize"))
    assert passed is False
    assert len(b._prompts) == 1
    assert b._prompts[0]["sender"] == "bob"
    assert any("pending #1" in t for t in ws.texts())


def test_owner_bypasses_ask_mode():
    b, ws = _bridge(owner="alice", ask_mode=True)
    assert asyncio.run(b._gate_query(ws, "alice", "q")) is True
    assert b._prompts == []


def test_public_no_ask_passes():
    b, ws = _bridge(owner="alice")
    assert asyncio.run(b._gate_query(ws, "bob", "q")) is True


# --------------------------------------------------------------------------- #
# _resolve_prompt — approve / deny a held prompt
# --------------------------------------------------------------------------- #

def test_deny_pending_drops_it():
    b, ws = _bridge(owner="alice", ask_mode=True)
    asyncio.run(b._gate_query(ws, "bob", "the question"))
    asyncio.run(b._handle_management(ws, "deny", "1", "alice"))
    assert b._prompts == []
    assert any("owner declined" in t for t in ws.texts())


def test_approve_dispatches_to_answer(monkeypatch):
    b, ws = _bridge(owner="alice", ask_mode=True)
    asyncio.run(b._gate_query(ws, "bob", "the question"))

    seen = {}

    async def fake_dispatch(w, question, sender):
        seen["question"] = question
        seen["sender"] = sender

    monkeypatch.setattr(b, "_dispatch_question", fake_dispatch)
    asyncio.run(b._handle_management(ws, "approve", "1", "alice"))
    assert seen == {"question": "the question", "sender": "bob"}
    assert b._prompts == []


def test_approve_unknown_id_is_reported():
    b, ws = _bridge(owner="alice")
    asyncio.run(b._handle_management(ws, "approve", "99", "alice"))
    assert any("no pending prompt #99" in t for t in ws.texts())
