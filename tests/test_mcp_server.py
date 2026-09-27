"""MCP attach mode (spec §6): join handshake, join-gate, verb→operator argv.

Every tool maps to an operator CLI verb. Tests inject a fake runner that
captures the argv instead of spawning the daemon, so nothing here needs the
`mcp` SDK or a live room.
"""
import pytest

from cmd_chat.operator.mcp_server import HackHouseMCP, NotJoinedError


class _Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        return "ok"

    def last(self):
        return self.calls[-1]


def _joined():
    rec = _Recorder()
    hh = HackHouseMCP(runner=rec)
    hh.hh_join("h", 3000, "oracle", "pw", owner="alice")
    return hh, rec


# -- handshake + gate ------------------------------------------------------- #

def test_join_records_owner_and_session():
    hh, rec = _joined()
    assert hh.joined is True
    assert hh.owner == "alice"
    assert hh.room == ("h", 3000, "oracle")
    assert hh.session == "mcp-oracle"
    join = rec.calls[0]
    assert join[0] == "up"
    assert "--no-tls" in join
    assert "--session" in join and "mcp-oracle" in join
    assert "--password" in join and "pw" in join


def test_verbs_blocked_before_join():
    hh = HackHouseMCP(runner=_Recorder())
    for call in (lambda: hh.hh_say("hi"),
                 lambda: hh.hh_read(),
                 lambda: hh.hh_keys("x"),
                 lambda: hh.hh_exec("ls"),
                 lambda: hh.hh_screen()):
        with pytest.raises(NotJoinedError):
            call()


# -- verb → argv mapping ---------------------------------------------------- #

def test_say_maps_to_say_verb():
    hh, rec = _joined()
    hh.hh_say("hello room")
    assert rec.last()[:2] == ["say", "hello room"]
    assert "--session" in rec.last()


def test_read_wait_and_timeout():
    hh, rec = _joined()
    hh.hh_read(timeout=15, wait=True)
    argv = rec.last()
    assert argv[0] == "read"
    assert "--wait" in argv and "--timeout" in argv and "15" in argv


def test_keys_enter_flag():
    hh, rec = _joined()
    hh.hh_keys("make build", enter=True)
    assert rec.last()[:3] == ["keys", "make build", "enter"]


def test_exec_and_write_and_get():
    hh, rec = _joined()
    hh.hh_exec("whoami")
    assert rec.last()[:2] == ["exec", "whoami"]
    hh.hh_write("/root/x.txt", "body")
    argv = rec.last()
    assert argv[0] == "write" and argv[1] == "/root/x.txt"
    assert "--text" in argv and "body" in argv
    hh.hh_get("/root/x.txt")
    assert rec.last()[:2] == ["get", "/root/x.txt"]


def test_watch_maps_for_in_timeout():
    hh, rec = _joined()
    hh.hh_watch("PASS|FAIL", in_="screen", timeout=20)
    argv = rec.last()
    assert argv[0] == "watch"
    assert argv[argv.index("--for") + 1] == "PASS|FAIL"
    assert argv[argv.index("--in") + 1] == "screen"


def test_spawn_go_flag():
    hh, rec = _joined()
    hh.hh_spawn("build the thing", go=True)
    argv = rec.last()
    assert argv[0] == "spawn" and argv[1] == "build the thing"
    assert "--go" in argv


# -- registry --------------------------------------------------------------- #

def test_tool_specs_cover_the_verbs():
    hh = HackHouseMCP(runner=_Recorder())
    names = [t["name"] for t in hh.tool_specs()]
    assert names[0] == "hh_join"          # handshake first
    for expected in ("hh_say", "hh_read", "hh_keys", "hh_exec", "hh_write",
                     "hh_get", "hh_screen", "hh_watch", "hh_manifest", "hh_spawn"):
        assert expected in names
    # every spec is callable and documented
    for t in hh.tool_specs():
        assert callable(t["handler"])
        assert t["description"]


def test_drive_tools_are_marked():
    # keys/exec/write are the host-gated drive verbs (spec §3).
    assert set(HackHouseMCP.DRIVE_TOOLS) == {"hh_keys", "hh_exec", "hh_write"}
