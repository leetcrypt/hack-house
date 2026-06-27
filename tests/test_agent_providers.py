"""Unit tests for OllamaProvider tool-call argument recovery.

Weak/quantized local models intermittently emit a parameter's JSON *schema*
fragment as its *value* (e.g. run_shell(command={'type':'string',
'description':'bash ./add.py'})). Every native tool arg is a plain string, so a
dict-valued arg is always this leak. These tests pin the recovery so the harness
never runs a stringified schema dict as a command again.
"""
from cmd_chat.agent.providers import OllamaProvider as P


def test_unleak_recovers_command_from_schema_fragment():
    assert P._unleak_str({"type": "string", "description": "bash ./add.py"}) == "bash ./add.py"


def test_unleak_prefers_value_keys_over_description():
    assert P._unleak_str({"description": "help text", "value": "ls -la"}) == "ls -la"
    assert P._unleak_str({"description": "help text", "default": "echo hi"}) == "echo hi"


def test_unleak_passes_plain_strings_through():
    assert P._unleak_str("mkdir data") == "mkdir data"


def test_unleak_single_string_value_fallback():
    assert P._unleak_str({"foo": "only-one"}) == "only-one"


def test_unleak_ignores_bare_schema_meta():
    # {'type':'string'} has no real value — must NOT recover the type name.
    assert P._unleak_str({"type": "string"}) == ""
    assert P._unleak_str({}) == ""


def test_clean_args_flattens_leaked_arg_keeps_real_ones():
    out = P._clean_args({"command": {"type": "string", "description": "bash ./add.py"}, "x": "keep"})
    assert out == {"command": "bash ./add.py", "x": "keep"}


def test_clean_args_passes_non_dict_through():
    assert P._clean_args("passthrough") == "passthrough"


def test_coerce_call_cleans_recovered_arguments():
    # The text-recovery path must also unleak args.
    obj = {"name": "run_shell", "arguments": {"command": {"type": "string", "description": "ls"}}}
    call = P._coerce_call(obj, valid_names={"run_shell"})
    assert call == {"name": "run_shell", "arguments": {"command": "ls"}}
