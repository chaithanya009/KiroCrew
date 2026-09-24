"""An installed skill must be able to tell its operator it is behind the package.

The packaged-to-installed hop is content-verified when the sync decides to copy,
but the DECISION to copy is mtime-based. An installed copy whose mtime is newer
than anything the package ships is judged up to date and skipped, so the install
keeps running superseded code and nothing anywhere says so.

``installed_skill_currency`` reports the comparison that gate throws away. The
load-bearing test here is ``test_stale_install_the_mtime_gate_skips_is_reported``:
it drives the real sync into exactly the state that hides staleness, proves the
install did NOT update, and only then asserts the check still names it. An
in-sync test alone would stay green with the comparison deleted outright, so
agreement is not evidence the instrument works.

Currency is deliberately local. "Behind" means the install does not match the
package THIS build ships; no test here reaches a network, because the check does
not either.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import skills as skills_mod
from kiro_crew.skills import (
    SKILL_INSTALL_BEHIND,
    SKILL_INSTALL_EDITED,
    SKILL_INSTALL_IN_SYNC,
    SKILL_INSTALL_UNVERIFIABLE,
    InstalledSkillCurrency,
    installed_skill_currency,
)

_MANIFEST = "---\nname: {name}\ndescription: fixture skill\n---\nbody\n"


@pytest.fixture
def trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """``(install base, packaged source root)`` with the sync pointed at both."""
    base = tmp_path / "home" / "skills"
    packaged = tmp_path / "packaged"
    base.mkdir(parents=True)
    packaged.mkdir(parents=True)
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: base)
    monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", packaged)
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: None)
    return base, packaged


def _ship(root: Path, name: str, *, script: str = "print('v1')\n") -> Path:
    """Author skill *name* under *root* shipping ``scripts/probe.py``."""
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_MANIFEST.format(name=name), encoding="utf-8")
    (skill_dir / "scripts" / "probe.py").write_text(script, encoding="utf-8")
    return skill_dir


def _state_for(name: str) -> str:
    states = {entry.state for entry in installed_skill_currency() if entry.name == name}
    assert len(states) == 1, f"{name} not reported exactly once: {states}"
    return states.pop()


def _entry_for(name: str) -> InstalledSkillCurrency:
    matches = [entry for entry in installed_skill_currency() if entry.name == name]
    assert len(matches) == 1, f"{name} not reported exactly once: {matches}"
    return matches[0]


def _names() -> set[str]:
    return {entry.name for entry in installed_skill_currency()}


def _bump_mtime(root: Path, when: float) -> None:
    """Stamp every entry under *root* so the sync's mtime gate reads it as newest."""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            os.utime(Path(dirpath) / name, (when, when))
        os.utime(dirpath, (when, when))


def test_clean_install_reads_in_sync(trees: tuple[Path, Path]) -> None:
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    assert _state_for("probe-skill") == SKILL_INSTALL_IN_SYNC


def test_package_moving_on_reads_behind(trees: tuple[Path, Path]) -> None:
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    # The package gains the fix; the install has not taken it yet.
    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )

    entry = _entry_for("probe-skill")
    assert entry.state == SKILL_INSTALL_BEHIND
    # Doctor prints this path, so it must name the tree the verdict came from.
    assert entry.source == packaged / "probe-skill"


def test_stale_install_the_mtime_gate_skips_is_reported(trees: tuple[Path, Path]) -> None:
    """The reported defect: the sync skips the install and the check still names it."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )
    # An installed copy newer than anything the package ships: the update gate
    # compares mtimes, so this is the state in which staleness goes unobserved.
    newest_packaged = skills_mod._tree_newest_mtime(packaged / "probe-skill")
    assert newest_packaged is not None
    _bump_mtime(base / "probe-skill", newest_packaged + 3600)

    skills_mod._ensure_builtin_skills(base)

    installed_body = (base / "probe-skill" / "scripts" / "probe.py").read_text(encoding="utf-8")
    assert installed_body == "print('v1')\n", "sync unexpectedly updated; scenario invalid"
    assert _state_for("probe-skill") == SKILL_INSTALL_BEHIND


def test_locally_edited_install_reads_edited(trees: tuple[Path, Path]) -> None:
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    (base / "probe-skill" / "scripts" / "probe.py").write_text("print('mine')\n", encoding="utf-8")

    assert _state_for("probe-skill") == SKILL_INSTALL_EDITED


def test_edited_outranks_behind_when_both_apply(trees: tuple[Path, Path]) -> None:
    """An edit is what has to be reconciled first, so it is the reported fact."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)

    (base / "probe-skill" / "scripts" / "probe.py").write_text("print('mine')\n", encoding="utf-8")
    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )

    assert _state_for("probe-skill") == SKILL_INSTALL_EDITED


def test_unmarked_install_cannot_be_compared(trees: tuple[Path, Path]) -> None:
    """A pre-provenance or user-authored copy carries no marker to compare."""
    base, packaged = trees
    _ship(packaged, "probe-skill", script="print('v2')\n")
    _ship(base, "probe-skill", script="print('v1')\n")

    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_install_that_is_a_link_cannot_be_compared(trees: tuple[Path, Path]) -> None:
    """The sync only creates real directories, so a link is user-made."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    elsewhere = _ship(base.parent / "elsewhere", "probe-skill")
    (base / "probe-skill").symlink_to(elsewhere, target_is_directory=True)

    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE


def test_unhashable_install_cannot_be_compared(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree over the fingerprint ceiling is unprovable, never agreement."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    assert _state_for("probe-skill") == SKILL_INSTALL_IN_SYNC

    monkeypatch.setattr(skills_mod, "_FINGERPRINT_MAX_ENTRIES", 1)

    assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE


@pytest.mark.skipif(
    # Both conditions live in ONE decorator because every skipif expression is
    # evaluated at collection time, on every platform: a second decorator
    # calling os.geteuid() would raise AttributeError on Windows during import
    # and error the whole module rather than skipping this one test. getattr
    # keeps the call off platforms that do not define it, and root is skipped
    # because it reads through the unreadable bit this test depends on.
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs POSIX mode bits and a non-root euid",
)
def test_unreadable_packaged_tree_cannot_be_compared(trees: tuple[Path, Path]) -> None:
    """An unreadable packaged entry is unprovable, so no verdict is claimed."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    assert _state_for("probe-skill") == SKILL_INSTALL_IN_SYNC

    blocked = packaged / "probe-skill" / "scripts"
    blocked.chmod(0o000)
    try:
        assert _state_for("probe-skill") == SKILL_INSTALL_UNVERIFIABLE
    finally:
        blocked.chmod(0o755)


def test_install_no_source_ships_is_absent(trees: tuple[Path, Path]) -> None:
    """With no packaged tree there is nothing to be out of step with."""
    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    _ship(base, "my-own-skill")

    reported = _names()
    assert "probe-skill" in reported
    assert "my-own-skill" not in reported


def test_packaged_but_not_installed_is_absent(trees: tuple[Path, Path]) -> None:
    """An absent directory has no currency to judge."""
    _base, packaged = trees
    _ship(packaged, "probe-skill")

    assert _names() == set()


def test_project_skill_is_judged_against_the_project_tree(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shadowing project skill must not read as behind the builtin it replaces."""
    base, packaged = trees
    project = base.parent.parent / "project-skills"
    project.mkdir(parents=True)
    _ship(project, "probe-skill", script="print('project')\n")
    _ship(packaged, "probe-skill", script="print('packaged')\n")
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: project)

    skills_mod._ensure_builtin_skills(base)

    assert (base / "probe-skill" / "scripts" / "probe.py").read_text(
        encoding="utf-8"
    ) == "print('project')\n"
    entry = _entry_for("probe-skill")
    assert entry.state == SKILL_INSTALL_IN_SYNC
    # The printed source is what makes a shadowing skill legible to the reader.
    assert entry.source == project / "probe-skill"


def test_doctor_names_the_verdict_the_source_and_a_remedy_that_works(
    trees: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed section must name the tree compared and an action that clears it.

    Restarting the gateway does not help the case this check exists for: the
    install carries the newer mtime, so the sync reads it as up to date and
    skips it again. The output has to name the action that does work, or the
    operator is left with a diagnosis and no move.
    """
    from kiro_crew.cli_doctor import _doctor_skill_currency, _safe_display

    base, packaged = trees
    _ship(packaged, "probe-skill")
    skills_mod._ensure_builtin_skills(base)
    (packaged / "probe-skill" / "scripts" / "probe.py").write_text(
        "print('v2')\n", encoding="utf-8"
    )

    issues: list[str] = []
    _doctor_skill_currency(issues)
    out = capsys.readouterr().out

    assert "Installed Skill Currency" in out
    assert "probe-skill" in out
    # Every value doctor reads off disk is printed through _safe_display, which
    # reprs it so a terminal cannot act on it. A separator that repr escapes
    # means the raw path is not a substring of the line, so the assertion
    # compares the rendering the section really emits.
    assert _safe_display(str(packaged / "probe-skill")) in out, "the compared tree must be named"
    assert "OUT of the skills directory" in out, "restart alone cannot clear this case"
    assert "remove or rename" not in out, "a rename in place publishes a second copy"
    assert issues, "a behind install must be recorded as an issue"
