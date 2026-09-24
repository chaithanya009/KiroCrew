"""The order the supervisor starts and drains its children in.

Two orderings here are correctness rules, not preferences, and neither is visible in the
output of the thing it protects:

* The authority files are restored to COMPLETION before the backend starts. The backend
  flushes the slot table from its own memory, so a backend that starts first persists an
  empty one over the restored files. The conversation list then comes up blank while the
  transcripts are still in the bucket, and nothing reports a fault.
* The sidecar is drained LAST. Its final cycle uploads what the backend's own drain
  flushed, so draining it earlier loses every turn taken since the previous interval on
  an orderly replacement, which is the common case because a deploy is one.

Both are pinned on the recorded SEQUENCE of calls. A test that only checked each step
happened would pass on either ordering, which is the failure being guarded against.

The orphan sweep is stubbed wherever the real ``_teardown`` is called. It discovers and
kills process groups on the host it runs on, and a test has no business doing that.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from container.supervisor import __main__ as sup

from ._settings_helper import make_settings


class _FakeGroup:
    """Stands in for a process group: a pid, and a record of its drain."""

    _next_pid = 9000

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        _FakeGroup._next_pid += 1
        self.pid = _FakeGroup._next_pid
        self._log = log

    def terminate(self, timeout: float) -> None:
        self._log.append(f"drain {self.name} {timeout:g}")


@pytest.fixture
def order(monkeypatch, tmp_path):
    """Neutralise every step of ``run`` except the ordering, and record the sequence."""
    calls: list[str] = []
    settings = make_settings(tmp_path, bucket="bkt", crew="crew-5", prefix="crews")

    def _record(label, result=None):
        def _fn(*_args, **_kwargs):
            calls.append(label)
            return result

        return _fn

    monkeypatch.setattr(sup, "verify_layout", _record("verify_layout"))
    monkeypatch.setattr(sup, "verify_sandbox", _record("verify_sandbox"))
    monkeypatch.setattr(sup.backend_mod, "build_backend_env", lambda s: {"E": "1"})
    monkeypatch.setattr(sup.backend_mod, "require_api_key", lambda env: None)
    monkeypatch.setattr(sup.bundle_mod, "install_bundle", _record("bundle"))
    monkeypatch.setattr(sup.backend_mod, "write_backend_config", _record("write_config"))
    monkeypatch.setattr(sup, "restore_authority", _record("restore"))
    monkeypatch.setattr(
        sup.backend_mod, "start_backend", _record("start backend", _FakeGroup("backend", calls))
    )
    monkeypatch.setattr(sup.backend_mod, "wait_until_ready", _record("backend ready"))
    monkeypatch.setattr(sup, "_start_front", _record("start front", _FakeGroup("front", calls)))
    monkeypatch.setattr(
        sup, "_start_sidecar", _record("start sidecar", _FakeGroup("sidecar", calls))
    )
    monkeypatch.setattr(sup, "_teardown", _record("teardown"))
    return calls, settings


def _run(settings):
    return sup.run(settings, wait_for_shutdown=lambda children: "signal")


def test_the_authority_files_are_restored_before_the_backend_starts(order):
    calls, settings = order

    _run(settings)

    assert calls.index("restore") < calls.index("start backend")


def test_the_backend_is_ready_before_the_front_starts(order):
    calls, settings = order

    _run(settings)

    assert calls.index("backend ready") < calls.index("start front")


def test_the_sidecar_starts_after_the_front(order):
    calls, settings = order

    _run(settings)

    assert calls.index("start front") < calls.index("start sidecar")


def test_the_crew_bundle_is_installed_before_the_restore(order):
    """The restore writes into the data home the bundle install lays out."""
    calls, settings = order

    _run(settings)

    assert calls.index("bundle") < calls.index("restore")


def test_a_failed_restore_stops_the_task_before_the_backend_starts(order, monkeypatch):
    """Booting without the slot table is the loss the restore exists to prevent."""
    calls, settings = order

    def _fail(_settings):
        calls.append("restore")
        raise RuntimeError("the bucket could not be read")

    monkeypatch.setattr(sup, "restore_authority", _fail)

    with pytest.raises(RuntimeError):
        _run(settings)

    assert "start backend" not in calls


def test_all_three_children_are_watched(order):
    calls, settings = order
    watched: list = []

    sup.run(settings, wait_for_shutdown=lambda children: watched.append(children) or "signal")

    assert [child.name for child in watched[0]] == ["backend", "front", "sidecar"]


def test_no_bucket_starts_no_sidecar(monkeypatch):
    """A writer with no destination looks exactly like a working backup, so there is none."""
    started: list = []
    monkeypatch.setattr(sup, "spawn_process_group", lambda *a, **k: started.append(a))

    assert sup._start_sidecar(SimpleNamespace(backup_bucket="")) is None
    assert started == []


def test_no_bucket_restores_nothing(monkeypatch):
    """With nothing in a bucket there is nothing to bring back, and that is not a fault."""
    monkeypatch.setattr(
        sup.restore_mod,
        "restore_authority",
        lambda *a, **k: pytest.fail("the restore must not run without a bucket"),
    )

    assert sup.restore_authority(SimpleNamespace(backup_bucket="")) is None


def test_the_sidecar_is_drained_last_so_its_final_cycle_has_something_to_upload(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sup, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)

    sup._teardown(
        _FakeGroup("front", calls), _FakeGroup("backend", calls), _FakeGroup("sidecar", calls)
    )

    assert [line.split()[1] for line in calls] == ["front", "backend", "sidecar"]


def test_teardown_without_a_sidecar_drains_the_other_two(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sup, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)

    sup._teardown(_FakeGroup("front", calls), _FakeGroup("backend", calls))

    assert [line.split()[1] for line in calls] == ["front", "backend"]
