# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The held no-follow chain walk.

Both routes are exercised here on one host. The walk itself is ordinary Python over
two platform primitives, so the by-name route is testable wherever those primitives
answer -- and on POSIX they do: ``open_entry_no_follow`` opens with ``O_NOFOLLOW`` and
``is_reparse_point_fd`` is always False. What is NOT testable off Windows is the
guarantee the held descriptors buy, because a handle that denies ``FILE_SHARE_DELETE``
is the mechanism blocking the rename a swap needs. The Windows CI shard covers that;
what these tests pin is the walk's verdict for every state a component can be in, and
that each verdict is reached without following anything.
"""

from __future__ import annotations

import errno
import os

import pytest

from kiro_crew import pinned_fs, platform_compat

_DEEP = 255


def _walk(path, **kwargs):
    kwargs.setdefault("max_depth", _DEEP)
    return pinned_fs.hold_no_follow_chain(str(path), **kwargs)


class TestOutcomes:
    def test_a_whole_real_chain_is_held(self, tmp_path):
        """Every component exists and none is a link, so the walk reaches the leaf and
        holds one descriptor per component it proved."""
        leaf = tmp_path / "a" / "b" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")

        chain = _walk(leaf)
        try:
            assert chain.outcome == pinned_fs.CHAIN_HELD
            assert chain.stopped_at is None
            assert chain.fds
        finally:
            pinned_fs.close_all(chain.fds)

    def test_a_missing_leaf_stops_the_walk_without_refusing(self, tmp_path):
        """The shape every write caller hands in. The name holds nothing, so nothing
        below it can redirect a resolution and the walk reports where it ran out."""
        chain = _walk(tmp_path / "not-created-yet.txt")
        try:
            assert chain.outcome == pinned_fs.CHAIN_MISSING
            assert chain.stopped_at == str(tmp_path / "not-created-yet.txt")
        finally:
            pinned_fs.close_all(chain.fds)

    def test_a_file_part_way_along_the_path_reads_as_missing(self, tmp_path):
        """A regular file cannot carry the rest of the path, so the components under it
        name nothing -- the same fact as a missing component, not a failure."""
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")

        chain = _walk(blocker / "below" / "doc.txt")
        try:
            assert chain.outcome == pinned_fs.CHAIN_MISSING
        finally:
            pinned_fs.close_all(chain.fds)

    @pytest.mark.skipif(
        not pinned_fs.supports_pinned_walk(), reason="the descriptor route needs openat"
    )
    def test_a_symlinked_component_is_reported_not_followed(self, tmp_path):
        """The descriptor route's link report. ``O_NOFOLLOW`` refuses the component, so
        the target is never opened and the walk names the link it stopped on."""
        target = tmp_path / "target"
        target.mkdir()
        (target / "doc.txt").write_text("payload", encoding="utf-8")
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)

        chain = _walk(alias / "doc.txt")
        try:
            assert chain.outcome == pinned_fs.CHAIN_REPARSE
            assert chain.stopped_at == str(alias)
        finally:
            pinned_fs.close_all(chain.fds)

    @pytest.mark.skipif(
        not pinned_fs.supports_pinned_walk(), reason="the descriptor route needs openat"
    )
    def test_a_symlinked_leaf_is_reported_not_followed(self, tmp_path):
        """The leaf is opened without ``O_DIRECTORY`` so an ordinary file works, but it
        still carries ``O_NOFOLLOW``: a link at the final name is reported too."""
        real = tmp_path / "real.txt"
        real.write_text("payload", encoding="utf-8")
        alias = tmp_path / "alias.txt"
        alias.symlink_to(real)

        chain = _walk(alias)
        try:
            assert chain.outcome == pinned_fs.CHAIN_REPARSE
            assert chain.stopped_at == str(alias)
        finally:
            pinned_fs.close_all(chain.fds)

    def test_a_relative_path_is_refused(self, tmp_path, monkeypatch):
        """A relative path's components resolve against a current directory the walk
        never inspected, so there is no chain for it to hold."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError):
            _walk("doc.txt")

    def test_a_path_deeper_than_the_bound_is_refused_before_the_walk(self, tmp_path):
        """One open per component makes an adversarially deep path a stall inside the
        guard, so the depth is judged before any of them run."""
        deep = os.sep + os.sep.join("a" for _ in range(_DEEP + 1))
        with pytest.raises(ValueError):
            _walk(deep, max_depth=_DEEP)


class TestByNameRoute:
    """The route Windows takes, exercised here through the POSIX primitives it uses."""

    def test_it_holds_a_real_chain(self, tmp_path):
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))

        fds: list[int] = []
        try:
            chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_HELD
            # The anchor is not opened: a drive or share root cannot be a link, and on
            # a share the open would be one more round-trip to an admitted host.
            assert len(chain.fds) == len(components)
        finally:
            pinned_fs.close_all(fds)

    def test_a_descriptor_reported_as_a_reparse_point_stops_the_walk(self, tmp_path):
        """The Windows link report, which is a question asked of the DESCRIPTOR. The
        classifier answers False on every POSIX descriptor by design, so the walk's
        handling of a True is pinned by substituting it."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "is_reparse_point_fd", lambda _fd: True)
                chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_REPARSE
            # Reported at the FIRST component, because the walk stops at the first link
            # rather than reading past it -- which is the property, not a detail.
            assert chain.stopped_at == os.path.join(anchor, components[0])
        finally:
            pinned_fs.close_all(fds)

    def test_a_missing_component_stops_the_walk(self, tmp_path):
        anchor, components = pinned_fs._chain_components(str(tmp_path / "absent" / "doc.txt"))
        fds: list[int] = []
        try:
            chain = pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert chain.outcome == pinned_fs.CHAIN_MISSING
        finally:
            pinned_fs.close_all(fds)

    def test_any_other_open_failure_propagates(self, tmp_path):
        """A component whose state cannot be read is the one case where what sits there
        is unknown. The walk raises so its caller fails closed instead of resolving."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")
        anchor, components = pinned_fs._chain_components(str(leaf))

        def _denied(_path):
            raise OSError(errno.EACCES, "permission denied")

        fds: list[int] = []
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(platform_compat, "open_entry_no_follow", _denied)
                with pytest.raises(OSError) as caught:
                    pinned_fs._hold_chain_by_path(anchor, components, fds)
            assert caught.value.errno == errno.EACCES
        finally:
            pinned_fs.close_all(fds)


class TestHeldContextManager:
    def test_it_releases_every_descriptor(self, tmp_path):
        """The guarantee lasts exactly as long as the descriptors, so the block is where
        a caller resolves -- and leaving it must not leak a held component."""
        leaf = tmp_path / "a" / "doc.txt"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("payload", encoding="utf-8")

        with pinned_fs.held_no_follow_chain(str(leaf), max_depth=_DEEP) as chain:
            assert chain.outcome == pinned_fs.CHAIN_HELD
            held = chain.fds
            for fd in held:
                assert os.fstat(fd) is not None

        for fd in held:
            with pytest.raises(OSError):
                os.fstat(fd)

    def test_it_releases_them_when_the_block_raises(self, tmp_path):
        leaf = tmp_path / "doc.txt"
        leaf.write_text("payload", encoding="utf-8")

        with pytest.raises(RuntimeError):
            with pinned_fs.held_no_follow_chain(str(leaf), max_depth=_DEEP) as chain:
                held = chain.fds
                raise RuntimeError("caller failed mid-resolution")

        for fd in held:
            with pytest.raises(OSError):
                os.fstat(fd)


class TestReparseClassifier:
    def test_it_answers_false_for_a_posix_descriptor(self, tmp_path):
        """On POSIX a descriptor cannot BE a link -- ``O_NOFOLLOW`` refuses one at the
        name -- so False is the correct answer rather than an unimplemented one."""
        f = tmp_path / "doc.txt"
        f.write_text("payload", encoding="utf-8")
        fd = platform_compat.open_entry_no_follow(str(f))
        try:
            assert platform_compat.is_reparse_point_fd(fd) is False
        finally:
            os.close(fd)

    @pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="needs O_NOFOLLOW")
    def test_the_opener_refuses_a_symlink_on_posix(self, tmp_path):
        """How a link is reported where ``OPEN_REPARSE_POINT`` does not exist: the open
        itself fails, so no descriptor for the link is ever handed back."""
        real = tmp_path / "real.txt"
        real.write_text("payload", encoding="utf-8")
        alias = tmp_path / "alias.txt"
        alias.symlink_to(real)

        with pytest.raises(OSError) as caught:
            platform_compat.open_entry_no_follow(str(alias))
        assert caught.value.errno == errno.ELOOP

    def test_the_opener_returns_a_descriptor_for_a_directory(self, tmp_path):
        """A walk needs the interior components too, so the opener must not refuse a
        directory the way the typed leaf opener does."""
        fd = platform_compat.open_entry_no_follow(str(tmp_path))
        try:
            assert os.fstat(fd).st_ino == os.stat(tmp_path).st_ino
        finally:
            os.close(fd)
