"""Tests for :mod:`kiro_crew.work_root`.

Everything runs against a monkeypatched data home under ``tmp_path``; the real
``<data home>/work`` is never touched.

The sweep cases set mtimes explicitly instead of sleeping, and they age EVERY
entry in a tree, because the module's idle signal is the tree's newest mtime
rather than the top directory's.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

if sys.platform != "win32":
    import fcntl

from kiro_crew import work_root as wr

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")


@pytest.fixture
def root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(wr, "config_dir", lambda: home)
    return home / wr.WORK_DIRNAME


def age(path: Path, seconds: float) -> None:
    """Push *path* and everything under it *seconds* into the past.

    Links are aged as THEMSELVES (``follow_symlinks=False``), never through:
    the module's idle signal lstats every entry, so a link left at the current
    time would make its whole tree read as active and a sweep case would pass
    for the wrong reason.
    """
    stamp = time.time() - seconds
    for current, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames + dirnames:
            target = Path(current) / name
            try:
                os.utime(target, (stamp, stamp), follow_symlinks=not target.is_symlink())
            except (NotImplementedError, OSError):
                pass
    os.utime(path, (stamp, stamp))


class TestAllocate:
    def test_creates_private_dir_under_managed_root(self, root: Path) -> None:
        path = wr.allocate_work("issue-6788")

        assert path == root / "issue-6788"
        assert path.is_dir()
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o700

    def test_same_key_rejoins_the_same_directory(self, root: Path) -> None:
        # The whole reason this root exists: a LATER, different process finds
        # the directory again by computing the same key.
        first = wr.allocate_work("pr-13019")
        (first / "clone.txt").write_text("state", encoding="utf-8")

        second = wr.allocate_work("pr-13019")

        assert second == first
        assert (second / "clone.txt").read_text(encoding="utf-8") == "state"

    @pytest.mark.parametrize(
        "key",
        ["", ".", "..", ".hidden", "a/b", "a\\b", "a b", "x" * 129, "-leading"],
    )
    def test_unnameable_keys_are_refused_not_rewritten(self, root: Path, key: str) -> None:
        # A sanitizer would map two distinct keys onto one directory and mix
        # their work, so the contract is a refusal.
        with pytest.raises(ValueError):
            wr.allocate_work(key)
        assert not root.exists()

    def test_non_string_key_is_refused(self, root: Path) -> None:
        with pytest.raises(ValueError):
            wr.allocate_work(7)  # type: ignore[arg-type]

    @_POSIX_ONLY
    def test_linked_managed_root_is_refused(self, root: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(wr.WorkRootBoundaryError):
            wr.allocate_work("k")
        assert list(elsewhere.iterdir()) == []

    @_POSIX_ONLY
    def test_linked_work_dir_is_refused(self, root: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        root.mkdir(parents=True)
        (root / "k").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(wr.WorkRootBoundaryError):
            wr.allocate_work("k")
        assert list(elsewhere.iterdir()) == []


class TestNoCallerCanShortenTheWindow:
    # Two runs legitimately share a key, and this root carries no owner, so a
    # caller-shortened window lets a run that lost the key speak for the run that
    # holds it: the stale signal arrives later in wall-clock order either way, so
    # nothing can tell the two apart. The window is therefore not shortenable at
    # all, and these pin that as surface rather than as prose.

    def test_the_module_exposes_no_release(self) -> None:
        assert not [name for name in dir(wr) if "release" in name.lower()]

    def test_the_sweep_takes_one_window_for_every_entry(self) -> None:
        import inspect

        params = inspect.signature(wr.sweep_work_root).parameters
        assert [p for p in params if p != "now"] == ["grace"]

    def test_a_planted_marker_does_not_hasten_reclamation(self, root: Path) -> None:
        # Aging comes AFTER the plant, so the marker's own write cannot refresh
        # the tree and mask the question being asked: the entry is genuinely old
        # AND carries the name, and it still keeps the one window every entry
        # gets. Written the other way round this passes against a sweep that
        # honours the name, which is the regression it exists to forbid.
        path = wr.allocate_work("k")
        (path / ".released").write_text("", encoding="utf-8")
        age(path, 2 * 24 * 3600)

        assert wr.sweep_work_root() == 0
        assert path.is_dir()

    def test_writing_into_the_tree_delays_reclamation(self, root: Path) -> None:
        # The other direction: a late write is content, and content refreshes the
        # newest mtime, so it can only push reclamation further out.
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)
        (path / ".released").write_text("", encoding="utf-8")

        assert wr.sweep_work_root() == 0
        assert path.is_dir()


class TestSweep:
    def test_entry_past_the_window_is_removed(self, root: Path) -> None:
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 1
        assert not path.exists()

    def test_entry_inside_the_window_is_kept(self, root: Path) -> None:
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS - 60)

        assert wr.sweep_work_root() == 0
        assert path.is_dir()

    def test_an_entry_whose_job_died_keeps_its_evidence_for_the_week(self, root: Path) -> None:
        # Nothing marks an entry done, so a crashed job's evidence is held for the
        # full window rather than reclaimed early on someone else's word.
        path = wr.allocate_work("k")
        (path / "evidence.log").write_text("why it died", encoding="utf-8")
        age(path, wr.IDLE_GRACE_SECONDS - 3600)

        assert wr.sweep_work_root() == 0
        assert (path / "evidence.log").is_file()

    def test_a_recent_nested_write_keeps_an_old_top_directory(self, root: Path) -> None:
        # A writer holding an open descriptor never touches the top directory's
        # mtime, so the idle signal has to be the tree's newest.
        path = wr.allocate_work("k")
        nested = path / "clone" / "deep"
        nested.mkdir(parents=True)
        age(path, wr.IDLE_GRACE_SECONDS + 3600)
        (nested / "active.txt").write_text("fresh", encoding="utf-8")

        assert wr.sweep_work_root() == 0
        assert path.is_dir()

    def test_a_stray_file_in_the_root_is_never_swept(self, root: Path) -> None:
        root.mkdir(parents=True)
        stray = root / "notes.txt"
        stray.write_text("x", encoding="utf-8")
        age(root, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 0
        assert stray.is_file()

    @_POSIX_ONLY
    def test_a_linked_child_is_never_swept_or_followed(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep", encoding="utf-8")
        root.mkdir(parents=True)
        link = root / "k"
        link.symlink_to(outside, target_is_directory=True)
        os.utime(root, (0, 0))

        assert wr.sweep_work_root() == 0
        assert link.is_symlink()
        assert (outside / "precious.txt").is_file()

    @_POSIX_ONLY
    def test_a_linked_managed_root_sweeps_nothing(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.mkdir()
        os.utime(victim, (0, 0))
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(outside, target_is_directory=True)

        assert wr.sweep_work_root() == 0
        assert victim.is_dir()

    def test_a_missing_root_is_not_an_error(self, root: Path) -> None:
        assert not root.exists()
        assert wr.sweep_work_root() == 0

    def test_the_window_is_caller_overridable(self, root: Path) -> None:
        path = wr.allocate_work("k")
        age(path, 120)

        assert wr.sweep_work_root(grace=60.0) == 1
        assert not path.exists()

    def test_now_is_injectable(self, root: Path) -> None:
        path = wr.allocate_work("k")

        removed = wr.sweep_work_root(now=time.time() + wr.IDLE_GRACE_SECONDS + 60)

        assert removed == 1
        assert not path.exists()

    def test_rejoin_refreshes_the_tree_so_the_next_sweep_keeps_it(self, root: Path) -> None:
        # The cross-run case: a weekly job rejoins a tree idle for a week. Without
        # the refresh in allocate_work the very next wake deletes what it handed back.
        path = wr.allocate_work("k")
        (path / "clone.txt").write_text("a week of work", encoding="utf-8")
        age(path, wr.IDLE_GRACE_SECONDS + 3600)

        rejoined = wr.allocate_work("k")

        assert wr.sweep_work_root() == 0
        assert rejoined.is_dir()
        assert (rejoined / "clone.txt").read_text(encoding="utf-8") == "a week of work"

    @_POSIX_ONLY
    def test_a_held_lock_defers_the_entry(self, root: Path) -> None:
        # flock conflicts between open file descriptions, so a second handle in
        # this process is a faithful stand-in for a rejoin in another one.
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)
        held = os.open(root / f".k{wr._LOCK_SUFFIX}", os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

            assert wr.sweep_work_root() == 0
            assert path.is_dir()
        finally:
            os.close(held)

    def test_the_lock_file_survives_its_entry(self, root: Path) -> None:
        path = wr.allocate_work("k")
        lock_file = root / f".k{wr._LOCK_SUFFIX}"
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 1
        assert not path.exists()
        assert lock_file.is_file()

    @_POSIX_ONLY
    def test_a_linked_lock_file_defers_the_entry(self, root: Path) -> None:
        elsewhere = root.parent / "elsewhere.lock"
        elsewhere.write_text("", encoding="utf-8")
        path = wr.allocate_work("k")
        real_lock = root / f".k{wr._LOCK_SUFFIX}"
        real_lock.unlink()
        real_lock.symlink_to(elsewhere)
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 0
        assert path.is_dir()


class TestSandboxMask:
    def test_the_root_is_masked_from_sandboxed_agents(self) -> None:
        # Pins the name the module owns to the name the sandbox hides, so a
        # rename on one side cannot quietly unmask the root on the other.
        from kiro_crew import sandbox

        assert wr.WORK_DIRNAME in sandbox._CREW_HIDDEN_LEAVES

    def test_the_root_is_precreated_so_the_mask_is_not_vacuous(self) -> None:
        # The root is created lazily by the first allocate_work call. The mask
        # loop is isdir-guarded, so an absent leaf gets no bind at all and every
        # sandbox spawned before the first allocation would see the real path.
        from kiro_crew import sandbox

        assert wr.WORK_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES

    def test_the_root_is_fenced_from_file_tools(self) -> None:
        # The mask covers a spawned subprocess; this covers the agent's own file
        # tools. Keys are guessable by design, and allocate_work REJOINS whatever
        # sits at one, so a plantable root is adopted as a job's prior state.
        from kiro_crew.security import paths

        assert wr.WORK_DIRNAME in paths._CREW_SECRET_LEAVES

    def test_no_environment_variable_names_the_root(self) -> None:
        # The distinction this module draws is per-process residue versus
        # cross-process state; a variable beside KIROCREW_SCRATCH collapses it.
        from kiro_crew import agent_scratch

        env = agent_scratch.scratch_env(Path("/somewhere"))
        assert not any("WORK" in name for name in env)
