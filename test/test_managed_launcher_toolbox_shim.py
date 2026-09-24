"""A spec that names Crew's own server by its bare Toolbox command keeps its tools.

Reported twice on 0.7.0.8 (Cloud Desktop, Toolbox install): every call on
``kirocrew-core`` / ``kirocrew-cron`` / ``kirocrew-dashboard`` answered
``identity_unattested`` while the servers mounted and listed their tools. The
spec said ``"command": "kirocrew"``; on PATH that is ``~/.local/bin/kirocrew`` ->
``~/.toolbox/bin/kirocrew`` -> one shared ``toolbox-exec`` dispatcher, whose
realpath is never the versioned ``~/.toolbox/tools/kirocrew/<ver>/bin/kirocrew``
the managed entry names. Both identity gates compared exactly that and denied.

The fixture rebuilds the Toolbox layout under ``tmp_path`` -- the shim, the
dispatcher, the ``globalInfo.json`` index the dispatcher reads, the versioned
launcher -- and points ``BUILDER_TOOLBOX_HOME`` (the root the dispatcher itself
honours) at it, so both spellings can be put through both gates. The negative
cases pin that the fence accepts nothing it did not accept before: a tree
merely shaped like Toolbox somewhere else, an index naming another file, a
managed entry outside the root, and a child environment re-rooting the
dispatcher all deny.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import managed_launcher
from kiro_crew.acp import session_mcp
from kiro_crew.mcp_gateway import gatewayd as gw

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Toolbox's POSIX shim layout")

VERSION = "0.7.0.8"


class ToolboxLayout:
    """One user's ``~/.toolbox`` plus the ``~/.local/bin`` entry Crew plants."""

    def __init__(self, home: Path, *, version: str = VERSION) -> None:
        self.home = home
        self.root = home / ".toolbox"
        self.version = version
        dispatcher_dir = self.root / "tools" / "toolbox" / "1.1.9714.0"
        dispatcher_dir.mkdir(parents=True)
        self.dispatcher = dispatcher_dir / "toolbox-exec"
        self.dispatcher.write_bytes(b"\x7fELF-not-really\n")
        self.dispatcher.chmod(0o755)
        self.tool_dir = self.root / "tools" / "kirocrew"
        self.bundle_bin = self.tool_dir / version / "bin"
        self.bundle_bin.mkdir(parents=True)
        self.versioned = self.bundle_bin / "kirocrew"
        self.versioned.write_text("#!/bin/sh\nexit 0\n")
        self.versioned.chmod(0o755)
        (self.root / "bin").mkdir()
        self.shim = self.root / "bin" / "kirocrew"
        self.shim.symlink_to(self.dispatcher)
        self.write_index(str(self.versioned))
        (home / ".local" / "bin").mkdir(parents=True)
        self.planted = home / ".local" / "bin" / "kirocrew"
        self.planted.symlink_to(self.shim)

    @property
    def index(self) -> Path:
        return self.root / "tools" / "globalInfo.json"

    def write_index(self, target: str, *, name: str = "kirocrew") -> None:
        """The shape the real dispatcher reads: ``Commands.<name>.Path``."""
        self.index.write_text(
            json.dumps(
                {
                    "Commands": {
                        name: {"Tool": "kirocrew", "Path": target, "BinPath": str(self.shim)}
                    },
                    "Symlinks": {},
                }
            )
        )

    @property
    def managed_entry(self) -> dict[str, Any]:
        return {"command": str(self.versioned), "args": ["mcp-core"]}


@pytest.fixture
def toolbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolboxLayout:
    layout = ToolboxLayout(tmp_path)
    monkeypatch.setenv(managed_launcher.TOOLBOX_HOME_ENV, str(layout.root))
    return layout


@pytest.fixture
def managed(toolbox: ToolboxLayout, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Pin the managed entry to the versioned launcher, as a Toolbox install resolves it."""
    import kiro_crew.agent as agent_mod

    entry = toolbox.managed_entry
    monkeypatch.setattr(
        agent_mod,
        "managed_mcp_spec_entry",
        lambda name, **_kw: dict(entry) if name == "kirocrew-core" else None,
    )
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: dict(entry))
    return entry


class TestToolboxRoot:
    def test_the_env_root_wins_and_must_be_a_directory(
        self, toolbox: ToolboxLayout, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert managed_launcher.toolbox_root() == str(toolbox.root.resolve())
        monkeypatch.setenv(managed_launcher.TOOLBOX_HOME_ENV, str(tmp_path / "absent"))
        assert managed_launcher.toolbox_root() is None

    def test_without_the_env_the_home_root_is_used(
        self, toolbox: ToolboxLayout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(managed_launcher.TOOLBOX_HOME_ENV)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: toolbox.home))
        assert managed_launcher.toolbox_root() == str(toolbox.root.resolve())
        monkeypatch.setattr(Path, "home", staticmethod(lambda: toolbox.home / "elsewhere"))
        assert managed_launcher.toolbox_root() is None


class TestToolboxDispatchTarget:
    def test_the_shim_resolves_to_the_indexed_launcher(self, toolbox: ToolboxLayout) -> None:
        assert managed_launcher.toolbox_dispatch_target(str(toolbox.shim)) == str(
            toolbox.versioned.resolve()
        )

    def test_the_planted_local_bin_entry_resolves_through_the_shim(
        self, toolbox: ToolboxLayout
    ) -> None:
        """``~/.local/bin/kirocrew -> ~/.toolbox/bin/kirocrew`` is the field shape."""
        assert managed_launcher.toolbox_dispatch_target(str(toolbox.planted)) == str(
            toolbox.versioned.resolve()
        )

    def test_a_non_dispatcher_resolves_to_nothing(self, toolbox: ToolboxLayout) -> None:
        assert managed_launcher.toolbox_dispatch_target(str(toolbox.versioned)) is None
        assert managed_launcher.toolbox_dispatch_target("") is None
        assert managed_launcher.toolbox_dispatch_target(str(toolbox.home / "missing")) is None

    def test_a_dispatcher_outside_the_trusted_root_is_foreign(
        self, toolbox: ToolboxLayout, tmp_path: Path
    ) -> None:
        """The reviewed attack: a tree shaped like Toolbox anywhere else, whose
        index (or bundle symlink) points at the genuine managed launcher. The
        root is anchored to this process's own Toolbox home, so the impostor
        dispatcher is a foreign binary and its index is never read."""
        forged = ToolboxLayout(tmp_path / "forged-home")
        forged.write_index(str(toolbox.versioned))
        assert managed_launcher.toolbox_dispatch_target(str(forged.shim)) is None
        assert not managed_launcher.same_managed_launcher(str(forged.shim), str(toolbox.versioned))
        assert not managed_launcher.same_managed_launcher(
            str(forged.planted), str(toolbox.versioned)
        )

    def test_a_file_merely_named_toolbox_exec_is_not_the_dispatcher(
        self, toolbox: ToolboxLayout, tmp_path: Path
    ) -> None:
        impostor = tmp_path / "elsewhere" / "toolbox-exec"
        impostor.parent.mkdir()
        impostor.write_text("#!/bin/sh\n")
        link = tmp_path / "kirocrew"
        link.symlink_to(impostor)
        assert managed_launcher.toolbox_dispatch_target(str(link)) is None
        # Under the root but not in the dispatcher's own directory either.
        stray = toolbox.root / "bin" / "toolbox-exec"
        stray.write_text("#!/bin/sh\n")
        link2 = tmp_path / "kirocrew2"
        link2.symlink_to(stray)
        assert managed_launcher.toolbox_dispatch_target(str(link2)) is None

    def test_the_invoked_name_selects_the_command(self, toolbox: ToolboxLayout) -> None:
        """The dispatcher keys on argv[0]'s basename (measured: a link under another
        name is 'not associated with any tool'), so a renamed link to the same
        file is not the ``kirocrew`` command."""
        other = toolbox.home / "renamed"
        other.symlink_to(toolbox.shim)
        assert managed_launcher.toolbox_dispatch_target(str(other)) is None

    def test_an_index_naming_another_file_names_that_file(self, toolbox: ToolboxLayout) -> None:
        elsewhere = toolbox.home / "elsewhere-k"
        elsewhere.write_text("#!/bin/sh\n")
        toolbox.write_index(str(elsewhere))
        assert managed_launcher.toolbox_dispatch_target(str(toolbox.shim)) == str(
            elsewhere.resolve()
        )
        assert not managed_launcher.same_managed_launcher(str(toolbox.shim), str(toolbox.versioned))

    @pytest.mark.parametrize(
        "corrupt",
        [
            lambda t: t.index.write_text("not json"),
            lambda t: t.index.unlink(),
            lambda t: t.index.write_text("[]"),
            lambda t: t.index.write_text(json.dumps({"Commands": {"kirocrew": "x"}})),
            lambda t: t.write_index("relative/path"),
            lambda t: t.write_index(""),
            lambda t: t.write_index(str(t.versioned), name="kirocrew-other"),
        ],
        ids=[
            "not-json",
            "missing",
            "not-object",
            "entry-not-object",
            "relative-path",
            "empty-path",
            "name-not-declared",
        ],
    )
    def test_a_broken_index_resolves_to_nothing(self, toolbox: ToolboxLayout, corrupt: Any) -> None:
        corrupt(toolbox)
        assert managed_launcher.toolbox_dispatch_target(str(toolbox.shim)) is None

    def test_a_child_environment_re_rooting_the_dispatcher_is_refused(
        self, toolbox: ToolboxLayout, tmp_path: Path
    ) -> None:
        """The child dispatcher reads ``$BUILDER_TOOLBOX_HOME`` ahead of the home
        root, so a spawn whose environment names another root would read an index
        this gate never judged. The same root spelled differently is fine."""
        forged = ToolboxLayout(tmp_path / "forged-home")
        forged.write_index(str(toolbox.versioned))
        assert (
            managed_launcher.toolbox_dispatch_target(
                str(toolbox.shim),
                child_env={managed_launcher.TOOLBOX_HOME_ENV: str(forged.root)},
            )
            is None
        )
        same_root_alias = tmp_path / "root-alias"
        same_root_alias.symlink_to(toolbox.root)
        assert managed_launcher.toolbox_dispatch_target(
            str(toolbox.shim),
            child_env={managed_launcher.TOOLBOX_HOME_ENV: str(same_root_alias)},
        ) == str(toolbox.versioned.resolve())
        assert managed_launcher.toolbox_dispatch_target(
            str(toolbox.shim), child_env={managed_launcher.TOOLBOX_HOME_ENV: ""}
        ) == str(toolbox.versioned.resolve())

    def test_a_relative_child_root_is_refused_not_resolved_here(
        self, toolbox: ToolboxLayout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``BUILDER_TOOLBOX_HOME`` resolves against the CHILD's working
        directory, which is not this process's; even one that happens to resolve
        to the trusted root from here is refused rather than judged."""
        monkeypatch.chdir(toolbox.home)
        assert (
            managed_launcher.toolbox_dispatch_target(
                str(toolbox.shim), child_env={managed_launcher.TOOLBOX_HOME_ENV: ".toolbox"}
            )
            is None
        )

    @pytest.mark.parametrize("key", ["HOME", "USERPROFILE", "home"])
    def test_a_child_home_that_is_not_this_processes_home_is_refused(
        self, toolbox: ToolboxLayout, tmp_path: Path, key: str
    ) -> None:
        """A dispatcher derives its default root from its home; a child whose home
        differs from this process's could read a root this verdict never saw, so
        it is denied without modelling which home rule its dispatcher applies.
        The process's own home, however spelled, passes."""
        forged = ToolboxLayout(tmp_path / "forged-home")
        forged.write_index(str(toolbox.versioned))
        assert (
            managed_launcher.toolbox_dispatch_target(
                str(toolbox.shim), child_env={key: str(forged.home)}
            )
            is None
        )
        assert (
            managed_launcher.toolbox_dispatch_target(str(toolbox.shim), child_env={key: "rel"})
            is None
        )
        own_home = str(Path.home())
        assert managed_launcher.toolbox_dispatch_target(
            str(toolbox.shim), child_env={key: own_home}
        ) == str(toolbox.versioned.resolve())
        alias = tmp_path / "home-alias"
        alias.symlink_to(os.path.realpath(own_home))
        assert managed_launcher.toolbox_dispatch_target(
            str(toolbox.shim), child_env={key: str(alias)}
        ) == str(toolbox.versioned.resolve())


class TestSameManagedLauncher:
    def test_realpath_equality_still_holds(self, toolbox: ToolboxLayout, tmp_path: Path) -> None:
        alias = tmp_path / "alias"
        alias.symlink_to(toolbox.versioned)
        assert managed_launcher.same_managed_launcher(str(alias), str(toolbox.versioned))
        assert managed_launcher.same_managed_launcher(
            str(toolbox.versioned), str(toolbox.versioned)
        )

    def test_the_shim_is_the_same_program_as_its_target(self, toolbox: ToolboxLayout) -> None:
        assert managed_launcher.same_managed_launcher(str(toolbox.shim), str(toolbox.versioned))
        assert managed_launcher.same_managed_launcher(str(toolbox.planted), str(toolbox.versioned))

    def test_a_managed_entry_outside_the_root_never_matches_a_dispatcher(
        self, toolbox: ToolboxLayout, tmp_path: Path
    ) -> None:
        """A non-Toolbox install (venv, pip) has no dispatcher fronting it; an
        index that names such a launcher is not a Toolbox install talking."""
        venv_launcher = tmp_path / "venv" / "bin" / "kirocrew"
        venv_launcher.parent.mkdir(parents=True)
        venv_launcher.write_text("#!/bin/sh\n")
        toolbox.write_index(str(venv_launcher))
        assert not managed_launcher.same_managed_launcher(str(toolbox.shim), str(venv_launcher))

    def test_without_a_toolbox_root_only_realpath_counts(
        self, toolbox: ToolboxLayout, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(managed_launcher.TOOLBOX_HOME_ENV, str(tmp_path / "absent"))
        assert not managed_launcher.same_managed_launcher(str(toolbox.shim), str(toolbox.versioned))
        assert managed_launcher.same_managed_launcher(
            str(toolbox.versioned), str(toolbox.versioned)
        )

    def test_a_foreign_binary_is_not(self, toolbox: ToolboxLayout, tmp_path: Path) -> None:
        evil = tmp_path / "evil"
        evil.write_text("#!/bin/sh\n")
        assert not managed_launcher.same_managed_launcher(str(evil), str(toolbox.versioned))
        assert not managed_launcher.same_managed_launcher("", str(toolbox.versioned))
        assert not managed_launcher.same_managed_launcher(str(toolbox.shim), "")


class TestGatewayTokenFence:
    """``_spawns_own_control_plane`` -- the realpath gate."""

    @pytest.mark.parametrize("spelling", ["versioned", "shim", "planted"])
    def test_both_spellings_of_our_launcher_earn_the_token(
        self, toolbox: ToolboxLayout, managed: dict[str, Any], spelling: str
    ) -> None:
        command = {
            "versioned": toolbox.versioned,
            "shim": toolbox.shim,
            "planted": toolbox.planted,
        }[spelling]
        assert gw._spawns_own_control_plane("kirocrew-core", str(command), ["mcp-core"], env={})

    def test_a_forged_dispatcher_tree_is_denied_and_says_so(
        self,
        toolbox: ToolboxLayout,
        managed: dict[str, Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The reviewed attack, end to end through the gate: a spec-declared
        command that is an attacker-shaped dispatcher whose index points at the
        genuine launcher earns nothing, because the dispatcher is not under the
        trusted root."""
        forged = ToolboxLayout(tmp_path / "forged-home")
        forged.write_index(str(toolbox.versioned))
        with caplog.at_level(logging.WARNING, logger=gw.__name__):
            assert not gw._spawns_own_control_plane(
                "kirocrew-core", str(forged.shim), ["mcp-core"], env={}
            )
        (record,) = [r for r in caplog.records if "denied the session token" in r.message]
        assert "is not the spec's" in record.message

    def test_an_index_dispatching_elsewhere_is_denied(
        self, toolbox: ToolboxLayout, managed: dict[str, Any]
    ) -> None:
        elsewhere = toolbox.home / "elsewhere-k"
        elsewhere.write_text("#!/bin/sh\n")
        toolbox.write_index(str(elsewhere))
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", str(toolbox.shim), ["mcp-core"], env={}
        )

    def test_a_child_env_re_rooting_the_dispatcher_is_denied(
        self, toolbox: ToolboxLayout, managed: dict[str, Any], tmp_path: Path
    ) -> None:
        forged = ToolboxLayout(tmp_path / "forged-home")
        forged.write_index(str(toolbox.versioned))
        assert not gw._spawns_own_control_plane(
            "kirocrew-core",
            str(toolbox.shim),
            ["mcp-core"],
            env={managed_launcher.TOOLBOX_HOME_ENV: str(forged.root)},
        )

    def test_a_child_env_re_homing_the_dispatcher_is_denied(
        self, toolbox: ToolboxLayout, managed: dict[str, Any], tmp_path: Path
    ) -> None:
        """The reviewed attack: a spec-declared ``HOME`` forwarded to the pooled
        child would let its dispatcher read a foreign index. Denied at the gate;
        the inherited daemon ``HOME`` -- this process's own -- still passes."""
        forged = ToolboxLayout(tmp_path / "forged-home")
        forged.write_index(str(toolbox.versioned))
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", str(toolbox.shim), ["mcp-core"], env={"HOME": str(forged.home)}
        )
        assert gw._spawns_own_control_plane(
            "kirocrew-core", str(toolbox.shim), ["mcp-core"], env={"HOME": str(Path.home())}
        )

    def test_the_shim_with_foreign_args_is_still_denied(
        self, toolbox: ToolboxLayout, managed: dict[str, Any]
    ) -> None:
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", str(toolbox.shim), ["mcp-cron"], env={}
        )

    def test_the_shadow_check_reads_the_dispatched_launchers_directory(
        self, toolbox: ToolboxLayout, managed: dict[str, Any]
    ) -> None:
        """Script form inspects the launcher's directory for a foreign ``kiro_crew``.
        Under the shim that directory is the PROGRAM's ``bin/``, not the dispatcher's."""
        (toolbox.bundle_bin / "kiro_crew").mkdir()
        (toolbox.bundle_bin / "kiro_crew" / "__init__.py").write_text("")
        assert not gw._spawns_own_control_plane(
            "kirocrew-core", str(toolbox.shim), ["mcp-core"], env={}
        )


class TestSessionIdentityElement:
    """``kiro_control_plane_servers`` -- the spec-declaration gate."""

    @staticmethod
    def _elements(spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch, work_dir: Path) -> list:
        monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *a, **k: spec)
        monkeypatch.setattr(session_mcp, "_global_settings", lambda **kw: {})
        monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
        return session_mcp.kiro_control_plane_servers("kirocrew", work_dir=work_dir)

    @pytest.mark.parametrize("spelling", ["versioned", "shim", "planted", "bare"])
    def test_both_spellings_of_our_launcher_earn_the_identity_element(
        self,
        toolbox: ToolboxLayout,
        managed: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        spelling: str,
    ) -> None:
        """The bare ``kirocrew`` spelling is what every externally installed spec
        carries; it resolves on the search path to the planted entry and through
        it to the shim. The element then carries the MANAGED invocation, never the
        spec's PATH-dependent spelling, so kiro-cli launches the file the gate
        judged."""
        command = {
            "versioned": str(toolbox.versioned),
            "shim": str(toolbox.shim),
            "planted": str(toolbox.planted),
            "bare": "kirocrew",
        }[spelling]
        monkeypatch.setattr(
            session_mcp, "_which_on_mcp_search_path", lambda cmd, env: str(toolbox.planted)
        )
        spec = {
            "tools": ["@kirocrew-core"],
            "mcpServers": {"kirocrew-core": {"command": command, "args": ["mcp-core"]}},
        }
        elements = self._elements(spec, monkeypatch, tmp_path)
        assert [e["name"] for e in elements] == ["kirocrew-core"]
        assert elements[0]["command"] == managed["command"]
        assert elements[0]["args"] == managed["args"]

    def test_a_bare_name_resolving_to_a_foreign_binary_earns_nothing(
        self,
        toolbox: ToolboxLayout,
        managed: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        evil = tmp_path / "evil-kirocrew"
        evil.write_text("#!/bin/sh\n")
        monkeypatch.setattr(session_mcp, "_which_on_mcp_search_path", lambda cmd, env: str(evil))
        spec = {
            "tools": ["@kirocrew-core"],
            "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]}},
        }
        assert self._elements(spec, monkeypatch, tmp_path) == []

    def test_a_bare_name_that_resolves_nowhere_earns_nothing(
        self,
        toolbox: ToolboxLayout,
        managed: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(session_mcp, "_which_on_mcp_search_path", lambda cmd, env: None)
        spec = {
            "tools": ["@kirocrew-core"],
            "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]}},
        }
        assert self._elements(spec, monkeypatch, tmp_path) == []

    def test_a_forged_dispatcher_tree_earns_nothing(
        self,
        toolbox: ToolboxLayout,
        managed: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        forged = ToolboxLayout(tmp_path / "forged-home")
        forged.write_index(str(toolbox.versioned))
        spec = {
            "tools": ["@kirocrew-core"],
            "mcpServers": {"kirocrew-core": {"command": str(forged.shim), "args": ["mcp-core"]}},
        }
        assert self._elements(spec, monkeypatch, tmp_path) == []

    def test_a_declared_home_earns_nothing_through_the_dispatcher(
        self,
        toolbox: ToolboxLayout,
        managed: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            session_mcp, "_which_on_mcp_search_path", lambda cmd, env: str(toolbox.planted)
        )
        spec = {
            "tools": ["@kirocrew-core"],
            "mcpServers": {
                "kirocrew-core": {
                    "command": "kirocrew",
                    "args": ["mcp-core"],
                    "env": {"HOME": str(tmp_path / "elsewhere")},
                }
            },
        }
        assert self._elements(spec, monkeypatch, tmp_path) == []

    def test_the_spec_env_path_is_the_search_path_that_resolves_a_bare_name(
        self,
        toolbox: ToolboxLayout,
        managed: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """No patching of the resolver: the spec's own ``env.PATH`` leads the search,
        the same composition the rewriter and the MCP probe resolve against."""
        spec = {
            "tools": ["@kirocrew-core"],
            "mcpServers": {
                "kirocrew-core": {
                    "command": "kirocrew",
                    "args": ["mcp-core"],
                    "env": {"PATH": str(toolbox.planted.parent)},
                }
            },
        }
        elements = self._elements(spec, monkeypatch, tmp_path)
        assert [e["name"] for e in elements] == ["kirocrew-core"]
        assert elements[0]["command"] == managed["command"]
