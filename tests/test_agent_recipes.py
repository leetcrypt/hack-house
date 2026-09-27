"""Instance recipes — durable launch definitions (spec §5).

A recipe stores the reusable part of an `/ai start` (model + scope) but never
credentials or runtime coordinates. These tests drive the store through an
`$HH_INSTANCES_FILE` pointed at a tmp path so the real ~/.hh is untouched.
"""
from cmd_chat.agent import recipes
from cmd_chat.agent.recipes import Recipe


def _store(tmp_path):
    return tmp_path / "instances.json"


def test_save_load_roundtrip(tmp_path):
    p = _store(tmp_path)
    r = Recipe(name="oracle", profile="groq-llama", query_acl="private",
               allow=["bob", "carol"], ask_mode=True, owner="alice")
    recipes.save(r, path=p)
    got = recipes.load("oracle", path=p)
    assert got == r


def test_list_and_delete(tmp_path):
    p = _store(tmp_path)
    recipes.save(Recipe(name="a"), path=p)
    recipes.save(Recipe(name="b"), path=p)
    assert recipes.list_recipes(path=p) == ["a", "b"]
    assert recipes.delete("a", path=p) is True
    assert recipes.list_recipes(path=p) == ["b"]
    assert recipes.delete("missing", path=p) is False


def test_load_missing_returns_none(tmp_path):
    assert recipes.load("nope", path=_store(tmp_path)) is None


def test_to_argv_profile_form():
    r = Recipe(name="oracle", profile="groq-llama", query_acl="private",
               allow=["bob"], ask_mode=True, owner="alice")
    argv = r.to_argv()
    assert argv[:3] == ["--name", "oracle", "--profile"]
    assert "--provider" not in argv          # profile carries provider/model
    assert "--query-acl" in argv and "private" in argv
    assert "--allow" in argv and "bob" in argv
    assert "--ask-mode" in argv
    assert "--owner" in argv and "alice" in argv


def test_to_argv_provider_model_form():
    r = Recipe(name="q", provider="ollama", model="qwen2.5:3b")
    argv = r.to_argv()
    assert "--provider" in argv and "ollama" in argv
    assert "--model" in argv and "qwen2.5:3b" in argv
    assert "--profile" not in argv
    assert "--ask-mode" not in argv          # default off → omitted


def test_to_argv_public_default_omits_ask_and_allow():
    r = Recipe(name="q", model="m")
    argv = r.to_argv()
    assert "--ask-mode" not in argv
    assert "--allow" not in argv
    assert "--query-acl" in argv and "public" in argv


def test_env_override_path(tmp_path, monkeypatch):
    target = tmp_path / "custom.json"
    monkeypatch.setenv("HH_INSTANCES_FILE", str(target))
    recipes.save(Recipe(name="x"), path=None)  # None → uses env path
    assert target.exists()
    assert recipes.list_recipes() == ["x"]


def test_load_tolerates_extra_keys(tmp_path):
    p = _store(tmp_path)
    p.write_text('{"oracle": {"model": "m", "bogus_key": 1, "query_acl": "private"}}')
    r = recipes.load("oracle", path=p)
    assert r is not None
    assert r.model == "m"
    assert r.query_acl == "private"
