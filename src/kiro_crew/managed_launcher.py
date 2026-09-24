"""Is a spawn command the same program as Kiro Crew's own managed launcher?

Two gates hand a session's identity to a managed Crew server only when the
command about to run IS that server's managed invocation:
``acp.session_mcp.kiro_control_plane_servers`` decides whether a spec's
declaration earns the identity element, and
``mcp_gateway.gatewayd._spawns_own_control_plane`` decides whether a pooled
backend is handed the session token. Both compared the spawn command against
``agent.managed_mcp_spec_entry`` by realpath, which is right for a symlink -- a
link and its target are one file -- and wrong for a *dispatcher*: a launcher
whose file is shared by every tool it fronts and which ``exec``\\ s the real
binary by reading a pointer at run time. Under such a launcher the realpath of
``kirocrew`` is the dispatcher, the realpath of the managed entry is the
versioned binary, and the two never compare equal, so every spec that names
the server by its bare command loses every Crew tool to ``identity_unattested``
while the server mounts and lists its tools normally.

Toolbox (``~/.toolbox``) is that shape. ``~/.toolbox/bin/<name>`` is a symlink to
one shared ``~/.toolbox/tools/toolbox/<version>/toolbox-exec``. Measured against
the real dispatcher (toolbox 1.1.9714.0, run under an isolated
``BUILDER_TOOLBOX_HOME``): it keys on the basename of ``argv[0]`` (a link under
another name is "not associated with any tool"), reads ONE index --
``<root>/tools/globalInfo.json``, ``Commands.<name>.Path`` -- and execs that
absolute path verbatim; the per-tool ``info.json`` / ``<version>.json`` files
are not consulted for dispatch, and a ``Path`` outside the tree is still what
runs. ``<root>`` is ``$BUILDER_TOOLBOX_HOME`` when set, else the user's
``~/.toolbox``. This module reads that same index -- without executing anything
-- and lets the two gates compare the file the dispatcher WOULD exec against the
managed entry.

The fence is anchored, not widened. A dispatcher is recognised only inside the
ONE Toolbox root this process trusts -- :func:`toolbox_root`, derived from the
gateway's own environment and home, never from the command's location -- so a
tree merely shaped like Toolbox somewhere else is a foreign binary, exactly as
before. The managed entry itself must live under that root (a Toolbox install
is the only install a Toolbox dispatcher can front), the index entry's ``Path``
must be that file by realpath, and a child environment that would point the
dispatcher at a different root (``BUILDER_TOOLBOX_HOME``) denies. Trusting the
index adds no capability an attacker did not already have: it sits in the same
user-owned tree as the managed binary, whose replacement the realpath
comparison never guarded against either, and an index rewritten to name
another file compares unequal and denies.

A leaf: imports nothing heavier than the standard library, so gatewayd can read
it on its spawn path and ``agent`` can read it without a cycle.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

#: File name of Toolbox's shared dispatcher. Every ``~/.toolbox/bin`` entry is a
#: symlink to one of these under ``<root>/tools/toolbox/<version>/``.
_TOOLBOX_DISPATCHER = "toolbox-exec"

#: The one index the dispatcher reads: ``Commands.<name>.Path`` is the absolute
#: path it execs for the command it was invoked as.
_TOOLBOX_INDEX = "globalInfo.json"

#: The environment variable the dispatcher honours as its root, ahead of the
#: user's home. Read from THIS process to find the trusted root; refused in a
#: child's environment when it names any other root, since the child dispatcher
#: would then read a different index than the one judged here.
TOOLBOX_HOME_ENV = "BUILDER_TOOLBOX_HOME"

#: Environment keys that decide where a process's home -- and so its default
#: Toolbox root -- is. The measured dispatcher does not read ``HOME`` (it took
#: the account's home), but a fence that models one dispatcher version is a
#: false grant waiting for the next; a child whose home differs from this
#: process's is denied without asking which rule its dispatcher applies.
_HOME_ENV_KEYS = frozenset({"HOME", "USERPROFILE"})


def toolbox_root(env: Mapping[str, str] | None = None) -> str | None:
    """The real path of the Toolbox root this process trusts, or ``None``.

    ``$BUILDER_TOOLBOX_HOME`` from *env* (default: this process's environment)
    when set, else ``~/.toolbox``; ``None`` when neither resolves to a directory
    -- a host without Toolbox recognises no dispatcher at all.
    """
    source = os.environ if env is None else env
    declared = source.get(TOOLBOX_HOME_ENV, "")
    try:
        root = Path(declared) if declared else Path.home() / ".toolbox"
        real = os.path.realpath(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return real if os.path.isdir(real) else None


def _is_dispatcher_under(dispatcher_realpath: str, root: str) -> bool:
    """Whether a resolved file is ``<root>/tools/toolbox/<version>/toolbox-exec``."""
    dispatcher = Path(dispatcher_realpath)
    if dispatcher.name != _TOOLBOX_DISPATCHER:
        return False
    version_dir = dispatcher.parent
    tool_dir = version_dir.parent
    tools_dir = tool_dir.parent
    return (
        tool_dir.name == "toolbox"
        and tools_dir.name == "tools"
        and str(tools_dir.parent) == root
        and version_dir.name not in ("", ".", "..")
    )


def _child_root_diverges(child_env: Mapping[str, str], trusted: str) -> bool:
    """Whether *child_env* would point a dispatcher at any root but *trusted*.

    Two channels decide a dispatcher's root: ``BUILDER_TOOLBOX_HOME``, which must
    then be the trusted root itself, and the home the process derives its default
    root from, which must then be THIS process's home. Every value is judged as
    the child would see it: absolute, or it names a directory relative to the
    CHILD's working directory -- one this process cannot resolve -- and denies. A
    non-string value denies too. Keys are matched case-insensitively, since a
    spec is portable across platforms whose environments differ on that.
    """
    try:
        own_home = os.path.realpath(Path.home())
    except (OSError, RuntimeError, ValueError):
        own_home = None
    for key, value in child_env.items():
        upper = str(key).upper()
        required: str | None
        if upper == TOOLBOX_HOME_ENV:
            required = trusted
        elif upper in _HOME_ENV_KEYS:
            required = own_home
        else:
            continue
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not os.path.isabs(value) or required is None:
            return True
        try:
            if os.path.realpath(value) != required:
                return True
        except (OSError, ValueError):
            return True
    return False


def _indexed_target(root: str, executable: str) -> str | None:
    """The ``Path`` the root's index declares for *executable*, or ``None``."""
    try:
        with open(os.path.join(root, "tools", _TOOLBOX_INDEX), encoding="utf-8") as stream:
            index = json.load(stream)
    except (OSError, ValueError):
        return None
    commands = index.get("Commands") if isinstance(index, dict) else None
    entry = commands.get(executable) if isinstance(commands, dict) else None
    target = entry.get("Path") if isinstance(entry, dict) else None
    if not isinstance(target, str) or not target or not os.path.isabs(target):
        return None
    return target


def toolbox_dispatch_target(
    command: str, *, root: str | None = None, child_env: Mapping[str, str] | None = None
) -> str | None:
    """The file Toolbox's dispatcher would exec for *command*, or ``None``.

    *command* is the path about to be spawned, in the spelling the spawner will
    use: the dispatcher selects its command by the NAME it was invoked as
    (``argv[0]``'s basename), so the basename of the un-resolved *command* is
    the index key and its realpath is what has to be the dispatcher. *root* is
    the trusted Toolbox root (default :func:`toolbox_root`); *child_env* is the
    environment the spawn will run under, and denies when it could point the
    dispatcher at any other root (:func:`_child_root_diverges`).

    ``None`` for anything that is not the trusted root's dispatcher, for a
    missing or malformed index, and for a name the index does not declare.
    Never raises and never executes. Real path, so it compares directly against
    a realpath'd managed entry.
    """
    if not command:
        return None
    trusted = toolbox_root() if root is None else root
    if not trusted:
        return None
    if child_env is not None and _child_root_diverges(child_env, trusted):
        return None
    try:
        dispatcher = os.path.realpath(command)
    except (OSError, ValueError):
        return None
    if not _is_dispatcher_under(dispatcher, trusted):
        return None
    executable = os.path.basename(command)
    if not executable:
        return None
    target = _indexed_target(trusted, executable)
    if target is None:
        return None
    try:
        return os.path.realpath(target)
    except (OSError, ValueError):
        return None


def same_managed_launcher(
    command: str, expected: str, *, child_env: Mapping[str, str] | None = None
) -> bool:
    """Whether spawning *command* runs the same program as *expected*.

    True when the two are one file by realpath (a launcher and its symlink), or
    when *command* is the trusted Toolbox root's dispatcher, *expected* lives
    under that root, and the root's index dispatches *command*'s name to that
    file. Nothing else: a different binary, a dispatcher elsewhere, an index
    naming another file, a child environment re-rooting the dispatcher, and
    anything unresolvable are all ``False``.

    Raises ``OSError`` / ``ValueError`` only from the realpath of the two
    arguments themselves, so a caller that already distinguishes "unresolvable"
    from "not ours" keeps that distinction; the Toolbox half never raises.
    """
    if not command or not expected:
        return False
    expected_real = os.path.realpath(expected)
    if os.path.realpath(command) == expected_real:
        return True
    root = toolbox_root()
    if not root:
        return False
    try:
        inside = os.path.commonpath([root, expected_real]) == root
    except ValueError:
        inside = False
    if not inside:
        # Only a Toolbox install has a launcher a Toolbox dispatcher can front.
        return False
    target = toolbox_dispatch_target(command, root=root, child_env=child_env)
    return target is not None and target == expected_real
