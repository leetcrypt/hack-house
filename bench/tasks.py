"""Native-harness benchmark task matrix.

Each task exercises the `/ai <model> !<task>` native tool-calling loop
(cmd_chat/agent/bridge.py:_run_native) end-to-end and is graded by GROUND
TRUTH inside the sandbox container — never by the model's own summary, which
the weak CPU model is known to fabricate (see
docs/findings-native-harness-2026-06-10.md).

Four categories, each with an easy / medium / hard tier:

  shell  — run_shell only (filesystem side effects)
  code   — write_file + run_shell (author a program, execute it, capture output)
  git    — clone/inspect a remote repo (needs container network + git)
  multi  — multi-step / recovery chains (the loop's hardest cases)

Conventions every task obeys so grading is deterministic:
  * Tasks operate in the agent's real cwd — the container HOME (/root) — and
    prompts use BARE filenames ("write who.txt"). The weak model writes
    `run_shell` redirects relative to its cwd, so pinning an absolute
    /root/bench path instead just measures absolute-path discipline (which it
    fails ~always) and drowns out every other signal. Verify snippets use the
    absolute /root/<name> form so grading is unambiguous.
  * `CLEAN` wipes ONLY the suite's known artifacts before each task — it never
    `rm -rf`s the home directory.
  * `verify` is a POSIX-sh snippet run via `podman exec`; exit 0 == PASS.
  * `setup` (optional) is a per-task sh snippet run before the task (e.g. to
    seed an input file the task is supposed to consume).
"""

from dataclasses import dataclass, field

HOME = "/root"

# Every file/dir the suite can create. Only these are wiped before each task,
# so one task can't leak state into the next without nuking the home dir.
ARTIFACTS = [
    "who.txt", "a", "conf_count.txt", "add.py", "add.out", "greet.sh",
    "greet.out", "fib.py", "fib.out", "hw", "hw2", "hw3", "readme.txt",
    "branch.txt", "proj", "fruit.txt", "sorted.txt", "words.txt", "wc.py",
    "wc.out",
]
CLEAN = f"cd {HOME} && rm -rf " + " ".join(ARTIFACTS)


@dataclass(frozen=True)
class Task:
    id: str
    category: str          # shell | code | git | multi
    tier: str              # easy | medium | hard
    prompt: str            # the text sent after `/ai <model> !`
    verify: str            # sh snippet; exit 0 == task succeeded (ground truth)
    setup: str = ""        # sh snippet run before the task (seed inputs)
    needs_net: bool = field(default=False)


# fmt: off
TASKS = [
    # ───────────────────────── shell ─────────────────────────
    Task(
        "shell-easy", "shell", "easy",
        "run whoami and write its output to a file named who.txt in your home directory",
        f'test "$(cat {HOME}/who.txt)" = root',
    ),
    Task(
        "shell-medium", "shell", "medium",
        "create the nested directory a/b/c in your home directory and write the "
        "text OK into a/b/c/marker.txt",
        f'test "$(cat {HOME}/a/b/c/marker.txt)" = OK',
    ),
    Task(
        "shell-hard", "shell", "hard",
        "count how many files under /etc end in .conf and write just that number "
        "to conf_count.txt in your home directory",
        f'test "$(cat {HOME}/conf_count.txt)" = "$(find /etc -name "*.conf" -type f 2>/dev/null | wc -l | tr -d " ")"',
    ),

    # ───────────────────────── code ──────────────────────────
    Task(
        "code-easy", "code", "easy",
        "write a Python file add.py that prints the result of 2+3, run it with "
        "python3 and save the output to add.out",
        f'test "$(cat {HOME}/add.out)" = 5',
    ),
    Task(
        # the documented give-up case: write + chmod +x + ./run. A weak model
        # tends to skip chmod, hit exit 126, and quit — the nudge loop's target.
        "code-medium", "code", "medium",
        "write a shell script greet.sh that echoes hello, make it executable with "
        "chmod, run it as ./greet.sh and save the output to greet.out",
        f'test -x {HOME}/greet.sh && test "$(cat {HOME}/greet.out)" = hello',
    ),
    Task(
        "code-hard", "code", "hard",
        "write a Python script fib.py that prints the first 10 Fibonacci numbers "
        "(starting 0 1) space-separated on one line, run it and save the output "
        "to fib.out",
        f'test "$(cat {HOME}/fib.out)" = "0 1 1 2 3 5 8 13 21 34"',
    ),

    # ───────────────────────── git ───────────────────────────
    Task(
        "git-easy", "git", "easy",
        "git clone https://github.com/octocat/Hello-World into a directory hw",
        f'test -d {HOME}/hw/.git',
        needs_net=True,
    ),
    Task(
        "git-medium", "git", "medium",
        "clone https://github.com/octocat/Hello-World into hw2, then write the "
        "contents of its README to readme.txt",
        f'grep -qi "hello world" {HOME}/readme.txt',
        needs_net=True,
    ),
    Task(
        "git-hard", "git", "hard",
        "clone https://github.com/octocat/Hello-World into hw3, then write the "
        "name of its current branch to branch.txt",
        f'test "$(tr -d " \n" < {HOME}/branch.txt)" = master',
        needs_net=True,
    ),

    # ──────────────────────── multi ──────────────────────────
    Task(
        # validated multi-step case (mkdir -> write -> list) that used to stall
        "multi-easy", "multi", "easy",
        "make a directory proj, create proj/notes.txt containing the text hello "
        "world, then list proj",
        f'test "$(cat {HOME}/proj/notes.txt)" = "hello world"',
    ),
    Task(
        "multi-medium", "multi", "medium",
        "the file fruit.txt has the lines apple, banana, cherry; write those "
        "lines reverse-sorted into sorted.txt",
        setup=f'printf "apple\\nbanana\\ncherry\\n" > {HOME}/fruit.txt',
        verify=f'test "$(cat {HOME}/sorted.txt)" = "$(printf \'cherry\\nbanana\\napple\')"',
    ),
    Task(
        "multi-hard", "multi", "hard",
        "the file words.txt contains 'one two three'; write a Python script wc.py "
        "that counts the words in that file and prints only the number, run it "
        "and save the output to wc.out",
        setup=f'printf "one two three\\n" > {HOME}/words.txt',
        verify=f'test "$(tr -d " \n" < {HOME}/wc.out)" = 3',
    ),
]
# fmt: on


def select(ids=None, categories=None, tiers=None, allow_net=True):
    """Filter the matrix by id / category / tier, optionally dropping net tasks."""
    out = []
    for t in TASKS:
        if ids and t.id not in ids:
            continue
        if categories and t.category not in categories:
            continue
        if tiers and t.tier not in tiers:
            continue
        if t.needs_net and not allow_net:
            continue
        out.append(t)
    return out
