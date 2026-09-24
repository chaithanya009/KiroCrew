"""``MAIN_REPO`` reaches git and the filesystem only through ``_repo()``.

``MAIN_REPO`` reaches git and the filesystem only through the accessors.

Dev Fleet represents "no main checkout found" as an empty string in
``MAIN_REPO``. That sentinel is fail-open at any call site that consumes the
global directly: ``git -C ""`` does not fail — it silently runs against the
backend process's working directory — and ``Path("")`` is ``Path(".")``, so an
unguarded consumer operates on an arbitrary directory and returns plausible
results. ``_repo_read()`` centralizes the guard: it returns the path or
raises ``RepoNotConfigured``, which the HMAC middleware converts to the 409
``repo_not_configured`` boundary. ``_repo()`` is the MUTATING accessor and adds
one refusal on top — a checkout served read-only — and it reaches the path
through ``_repo_read()``, so exactly one function still reads the global.

Two enforcement tiers (same pattern as ``test_apps_instances_loop_offload.py``):

- Behavior tests: ``_repo_read()`` raises on the empty sentinel and returns the
  path otherwise; ``_repo()`` additionally raises ``RepoReadOnly`` while the
  read-only state is set. Both preserve the exception types the middleware
  boundary maps.
- AST ratchet: outside the read accessor itself, a ``MAIN_REPO`` load may appear
  ONLY as a bare truthiness guard (``if MAIN_REPO:`` / ``not MAIN_REPO`` / a
  ``BoolOp`` operand). Any other load — a git argv element, a subprocess
  ``cwd=``, a ``Path(...)`` build, an f-string interpolation, a payload
  field — fails this test, so a future call site cannot silently reintroduce
  the fail-open shape.
"""

from __future__ import annotations

import ast
import inspect
import json
import locale
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    gateway_routes,
    http_api,
    live,
    repository,
    runtime,
    server,
    worktree_ops,
)

# The read accessor is the ONLY function whose body may read the bare global: it
# IS the guard, and the mutating accessor delegates to it rather than loading the
# global a second time, so the count of functions touching MAIN_REPO stays at one.
# The startup hook's discovery/re-resolve runs on a local and writes the global
# exactly once (a Store, which this ratchet ignores), so even the assignment site
# needs no exemption — and a git call added to startup, where MAIN_REPO is most
# often still unresolved, is caught like anywhere else.
_DEV_FLEET_MODULES = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
    server,
)
_ALLOWED_LOADS = {(repository.__name__, "_repo_read")}


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return None


def _is_bare_truthiness(node: ast.expr, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when the load feeds a truthiness test and nothing else.

    Walking up from the Name, only ``BoolOp`` and ``not`` may intervene before
    the expression lands as the ``test`` of an ``if``/``while`` or a ternary.
    Any other intervening node (a call argument, a container literal, an
    f-string, an assignment value) means the VALUE escapes, which is exactly
    the shape the accessor exists to prevent.
    """
    child: ast.AST = node
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.BoolOp, ast.UnaryOp)):
            if isinstance(cur, ast.UnaryOp) and not isinstance(cur.op, ast.Not):
                return False
            child = cur
            cur = parents.get(cur)
            continue
        if isinstance(cur, (ast.If, ast.While)):
            return cur.test is child
        if isinstance(cur, ast.IfExp):
            return cur.test is child
        return False
    return False


def test_main_repo_loads_only_via_accessor_or_truthiness() -> None:
    violations: list[str] = []
    for module in _DEV_FLEET_MODULES:
        tree = ast.parse(inspect.getsource(module))
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            is_main_repo = (isinstance(node, ast.Name) and node.id == "MAIN_REPO") or (
                isinstance(node, ast.Attribute) and node.attr == "MAIN_REPO"
            )
            if not is_main_repo or not isinstance(node.ctx, ast.Load):
                continue  # assignments (Store) stay on the global by design
            func = _enclosing_function(node, parents)
            if (module.__name__, func) in _ALLOWED_LOADS:
                continue
            if _is_bare_truthiness(node, parents):
                continue
            violations.append(
                f"{module.__name__}:{node.lineno}: MAIN_REPO load in "
                f"{func or '<module>'} — route it through repository._repo()"
                " (or _repo_read() for a read-only consumer)"
            )
    assert not violations, (
        "MAIN_REPO's empty-string sentinel is fail-open when consumed "
        "directly (git -C '' runs against the process CWD). Use _repo():\n" + "\n".join(violations)
    )


def test_repo_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo()


def test_repo_accessor_returns_resolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    assert repository._repo() == "/somewhere/kirocrew"


def test_read_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The weaker accessor still refuses the fail-open sentinel.

    It is the one the ratchet admits, so if it ever stopped raising here the
    ratchet would be guarding a function that hands out ``""``.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo_read()


def test_read_accessor_serves_a_checkout_the_app_may_only_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only is the whole point of the split: the generic surface still works."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    assert repository._repo_read() == "/somewhere/other-project"


def test_mutating_accessor_refuses_a_checkout_the_app_may_only_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal every mutating call site inherits without being touched.

    ``RepoReadOnly`` must keep sharing the ``RepoUnavailable`` base, because the
    degrade sites catch that base and their "not derivable" answer is the right
    one here too.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    with pytest.raises(repository.RepoReadOnly) as caught:
        repository._repo()
    assert isinstance(caught.value, repository.RepoUnavailable)
    assert "read-only" in str(caught.value)


def test_mutating_accessor_allows_a_marker_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate does not fire when the state is genuinely empty."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert repository._repo() == "/somewhere/kirocrew"


def test_read_only_reason_reports_the_state_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """One reader for the route boundary, the payload and the row fields."""
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert repository._read_only_reason() is None
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    assert repository._read_only_reason() == "read-only: no markers"


@pytest.mark.parametrize(
    "name, ok",
    [
        ("main", True),
        ("trunk", True),
        ("release/2.0", True),
        ("feature.x", True),
        # A leading dash is parsed as a FLAG by git once the name is
        # interpolated into an argv, so it must never be accepted.
        ("--exec=touch /tmp/pwn", False),
        ("-main", False),
        # ``..`` splits a rev range at the wrong place: ``origin/a..b..HEAD``.
        ("a..b", False),
        ("", False),
        ("main branch", False),
        ("main;rm", False),
    ],
)
def test_base_branch_names_are_constrained_before_reaching_an_argv(name: str, ok: bool) -> None:
    assert repository._plausible_branch_name(name) is ok


def test_every_local_base_candidate_survives_the_argv_constraint() -> None:
    """The fallback list and the argv guard must agree.

    A candidate the guard rejects would be published into ``BASE_BRANCH`` by the
    fallback loop without ever meeting ``_plausible_branch_name``, which only
    screens the remote's answer. Asserting over the tuple itself keeps a name
    added later from slipping past.
    """
    assert repository._LOCAL_BASE_CANDIDATES
    for candidate in repository._LOCAL_BASE_CANDIDATES:
        assert repository._plausible_branch_name(candidate) is True


def test_primary_checkout_resolution_preserves_the_host_text_decoder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Moving the startup probe must not reinterpret non-ASCII checkout paths."""
    primary = tmp_path / "primary"
    seen: dict[str, object] = {}

    def _run(_argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=str(primary / ".git"))

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    monkeypatch.setattr(repository.subprocess, "run", _run)

    assert repository._resolve_primary_checkout(str(tmp_path / "linked")) == str(primary)
    assert seen["text"] is True
    assert seen["encoding"] == locale.getpreferredencoding(False)


# --- base-branch resolution reads ONE remote ---------------------------------
#
# ``git remote`` lists names alphabetically, so a checkout carrying an archive or
# fork remote beside ``origin`` hands the first-listed one the casting vote. That
# remote decides ``BASE_BRANCH`` while ``_upstream_remote`` resolves to ``origin``
# independently, and ``/rebase`` rewrites onto ``{remote}/{BASE_BRANCH}`` — a base
# the upstream never published. These three pin which remote is consulted.


def _stub_base_branch_git(
    monkeypatch: pytest.MonkeyPatch, *, remotes: str, published: dict[str, str], local: set[str]
) -> list[str]:
    """Wire the two git readers ``_resolve_base_branch`` uses. Returns the ref probes."""
    probed: list[str] = []

    async def _run_cmd(argv, **_kwargs):
        assert argv[-1] == "remote", argv
        return 0, remotes, ""

    async def _git(_repo: str, *args: str) -> str | None:
        if args[0] == "symbolic-ref":
            ref = args[-1]
            probed.append(ref)
            remote = ref.split("/")[2]
            head = published.get(remote)
            return f"{remote}/{head}" if head else None
        if args[0] == "rev-parse":
            name = args[-1].removeprefix("refs/heads/")
            return name if name in local else None
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(repository, "_git", _git)
    return probed


@pytest.mark.asyncio
async def test_base_branch_ignores_a_remote_sorted_before_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alphabetically earlier remote must not decide the rebase base.

    ``archive`` publishes one default and ``origin`` another. Only ``origin`` may
    be consulted, because ``_upstream_remote`` resolves to it and the two answers
    are combined into a single rev range.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="archive\norigin\n",
        published={"archive": "legacy-default", "origin": "trunk"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert probed == ["refs/remotes/origin/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_reads_a_sole_remote_under_another_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One remote is unambiguous whatever it is called, so its answer is taken."""
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="kirocrew\n",
        published={"kirocrew": "release/3"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "release/3"
    assert probed == ["refs/remotes/kirocrew/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_falls_back_locally_when_no_remote_is_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several remotes and no ``origin`` is ambiguous: ask the local branches."""
    local_default = repository._LOCAL_BASE_CANDIDATES[-1]
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="fork\nupstream\n",
        published={"fork": "a", "upstream": "b"},
        local={local_default},
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == local_default
    assert probed == []


# --- reads leave the repository byte-identical -------------------------------


def test_optional_locks_are_off_for_every_git_this_handler_runs() -> None:
    """``git status`` rewrites the index unless optional locks are off.

    It is a read to its caller and a write to the repository: it refreshes the
    index's stat cache and saves it back under ``index.lock``. Every fleet render
    runs one per row, so without this a checkout the app may only read is modified
    on its ordinary path. Pinned on the env chokepoint rather than per call site,
    which is what makes a read added later inherit it.
    """
    assert runtime._GIT_ENV_NEUTRALIZERS["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.mark.asyncio
async def test_run_cmd_puts_the_neutralizers_in_the_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict is only a guarantee if the spawn actually carries it.

    Asserted through the spawn preparation, because that is the last place the env
    can be read before the child exists, and an entry dropped anywhere earlier
    would leave the dict stating a pin nothing applies.
    """
    seen: dict[str, str] = {}

    def _prepare(cmd, _mode, env=None):
        seen.update(env or {})
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _no_child(*_a, **_kw):
        raise AssertionError("the env is read before the child spawns")

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", _no_child)
    with pytest.raises(AssertionError):
        await runtime._run_cmd(["git", "-C", "/somewhere/other-project", "status"])

    for key, value in runtime._GIT_ENV_NEUTRALIZERS.items():
        assert seen[key] == value


# --- the gateway's own boundary carries the refusal --------------------------


@pytest.mark.asyncio
async def test_gateway_repo_resolution_refuses_a_read_only_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway routes skip the backend middleware, so the refusal lives here.

    Make Live is why it matters: it reaches ``_find_worktree_by_path`` and writes
    the live-target pointer, which would aim the running gateway at a checkout
    this app may only read.
    """

    async def _discovered() -> None:
        return None

    monkeypatch.setattr(repository, "ensure_main_repo_discovered", _discovered)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")

    refused = await gateway_routes._ensure_repo()
    assert refused is not None
    assert refused.status == 409
    assert json.loads(refused.body)["code"] == "repo_read_only"

    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert await gateway_routes._ensure_repo() is None


# --- a fenced path is refused, never served read-only ------------------------


def test_read_only_adoption_asks_the_central_path_gate() -> None:
    """The adoption branch must consult the gate, not its own path opinion.

    ``sensitive_path_refusal`` is the single definition of a protected location
    and it canonicalizes first, so a link into a fenced tree answers like the
    tree. Serving such a path read-only would disclose exactly what the fence
    forbids: its worktrees, branches and PR state.
    """
    source = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "sensitive_path_refusal(discovered)" in source
    gate_call = source.index("sensitive_path_refusal(discovered)")
    adopt = source.index("read_only_msg = (")
    assert gate_call < adopt, "the gate must be consulted before the adoption message is built"


def test_a_fenced_path_is_refused_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undecided gate answer counts as fenced, because it refuses fail-closed."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/fenced")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", "refused: protected location")

    assert repository._read_only_reason() is None
    with pytest.raises(repository.RepoUnreadable):
        repository._repo_read()


# --- the read-only denial reaches the audit trail ----------------------------


def test_read_only_denial_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal on an AUTHENTICATED request is a permission decision.

    The same middleware already records its HMAC denials, and this one is the
    feature's ordinary steady state -- every mutating request against a read-only
    checkout lands on it -- so without an event it is the one outcome this app
    reaches that leaves no trace.
    """
    source = inspect.getsource(http_api.hmac_proxy_middleware)
    handler = source.index("except repository.RepoReadOnly")
    body = source[handler : handler + 1400]
    assert "log_tool_invocation" in body
    assert 'outcome="denied"' in body
    assert 'tool_name="dev-fleet:repo-read-only"' in body
    # The 409 must survive a failing audit sink: auditing may not mask the answer.
    assert "except Exception" in body
