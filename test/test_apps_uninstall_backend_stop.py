"""An uninstall must never report success while the app's backend still runs.

Both entry points that remove an app stop its backend on positive evidence.

The CLI is a separate process from the gateway, so it holds no handle on the
child the gateway spawned: stopping the backend locally is not available to it.
It hands the uninstall to a running gateway the way ``enable`` and ``disable``
do, carrying ``--purge-data``. When no gateway answers, it says plainly that it
stopped nothing instead of reporting success.

The HTTP handler stops the backend for every app, whatever ``resources`` holds.
That field is read from the app's own installed metadata, so honouring it lets
an app declare ``resources: "app"`` and switch off its own teardown.

After the stop the port is OBSERVED, because the stop's boolean answers
``False`` both for "nothing to stop" and for "something runs that I did not
stop", and ``True`` only for "the process I tracked is gone" -- which says
nothing about a worker the app spawned for itself. A port still accepting
connections is reported as ``unstopped_backend_port``.

A reported port is also RECORDED against the app name. The uninstall completes
rather than aborting, so that listener keeps the port the manifest declares, and
a later install of the same name finds a healthy answer there. Adoption reads
health and owner PIDs, never the code the listener executes, so it consults the
record and refuses. The record is dropped as soon as the port it names answers
nothing, which keeps a legitimate fixed-port app installable.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew.apps import backend as be


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


class _FakeResponse:
    """Minimal context-manager stand-in for ``urlopen``'s return value."""

    def __init__(self, payload: Any) -> None:
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _manifest(entry_point: str) -> MagicMock:
    manifest = MagicMock()
    manifest.backend.entryPoint = entry_point
    return manifest


class TestCliUninstallDelegatesToTheGateway:
    """The CLI cannot signal another process's child, so it must ask the gateway."""

    def _drive(self, *, purge_data: bool) -> tuple[list[Any], MagicMock]:
        requests: list[Any] = []

        def _open(request: Any, *, timeout: int, socket_path: Any) -> _FakeResponse:
            requests.append(request)
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _FakeResponse({"token": "dashboard-credential"})
            return _FakeResponse({"ok": True, "message": "Uninstalled demo"})

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
            patch("kiro_crew.cli_commands.uninstall_app") as local_uninstall,
            patch("kiro_crew.cli_commands.deregister_app") as local_deregister,
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.trust_grant_removal_blocked", return_value=""),
        ):
            cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=purge_data))
        local_deregister.assert_not_called()
        return requests, local_uninstall

    def test_a_running_gateway_performs_the_uninstall(self) -> None:
        """The local path must not run: it is the one that cannot stop the backend."""
        requests, local_uninstall = self._drive(purge_data=False)

        assert "/api/apps/demo/uninstall?" in requests[1].full_url
        assert requests[1].get_method() == "POST"
        local_uninstall.assert_not_called()

    @pytest.mark.parametrize("purge_data", [True, False])
    def test_the_purge_flag_travels_in_the_request_body(self, purge_data: bool) -> None:
        """The handler defaults an absent body to "preserve data".

        So a bodyless delegated request would turn ``--purge-data`` into a silent
        data-preserving uninstall — the flag has to be on the wire.
        """
        requests, _ = self._drive(purge_data=purge_data)

        action = requests[1]
        assert action.get_header("Content-type") == "application/json"
        assert json.loads(action.data) == {"purge_data": purge_data}


class TestCliFileOnlyUninstallDoesNotClaimAStop:
    """With no gateway reachable the CLI stops nothing, and must say so."""

    def _drive(self, *, entry_point: str) -> str:
        with (
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value=""),
            patch("kiro_crew.cli_commands.trust_grant_removal_blocked", return_value=""),
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.deregister_app"),
            patch("kiro_crew.cli_commands.get_app_manifest", return_value=_manifest(entry_point)),
            patch(
                "kiro_crew.cli_commands.uninstall_app",
                return_value=MagicMock(ok=True, message="Uninstalled demo", error=""),
            ),
        ):
            import contextlib

            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=False))
        return err.getvalue()

    def test_an_app_declaring_a_backend_is_reported_as_not_stopped(self) -> None:
        assert "was not stopped" in self._drive(entry_point="backend/app.py")

    def test_an_app_with_no_backend_is_not_warned_about(self) -> None:
        """A declaration is a property of the app, so it can gate the message."""
        assert "was not stopped" not in self._drive(entry_point="")

    def test_an_unreadable_manifest_is_reported_rather_than_assumed_clean(self) -> None:
        """Not knowing is not the same as knowing there is nothing to stop."""
        with patch("kiro_crew.cli_commands.get_app_manifest", side_effect=ValueError("bad json")):
            assert cc._app_declares_backend("demo") is True

    def test_a_missing_manifest_is_reported_rather_than_assumed_clean(self) -> None:
        with patch("kiro_crew.cli_commands.get_app_manifest", return_value=None):
            assert cc._app_declares_backend("demo") is True


@pytest.mark.asyncio
class TestUninstallHandlerStopsAndObservesTheBackend:
    async def _run(
        self, *, resources: str, live_port: int | None, record_ok: bool = True
    ) -> tuple[dict[str, Any], list[str], MagicMock]:
        """Drive the handler and return (response body, call order, deregister mock)."""
        calls: list[str] = []
        fake_app = {
            "name": "test-app",
            "manifest": {},
            "resources": resources,
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": "test-app"}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value={})

        def _recorded(name: str) -> int | None:
            calls.append("recorded_backend_port")
            return 9137

        def _stop(name: str, *args: Any, **kw: Any) -> bool:
            calls.append("stop_app_backend")
            return True

        def _unstopped(name: str, **kw: Any) -> int | None:
            calls.append("unstopped_backend_port")
            assert kw.get("port_hint") == 9137, "the hint must survive to the probe"
            return live_port

        def _record(app: str, port: int) -> bool:
            calls.append(f"record_unstopped_backend:{app}:{port}")
            return record_ok

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch(
                "kiro_crew.apps.routes.uninstall_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True, "name": "test-app"}),
            ),
            patch("kiro_crew.apps.routes.recorded_backend_port", side_effect=_recorded),
            patch("kiro_crew.apps.routes.stop_app_backend", side_effect=_stop),
            patch("kiro_crew.apps.routes.unstopped_backend_port", side_effect=_unstopped),
            patch("kiro_crew.apps.routes.record_unstopped_backend", side_effect=_record),
            patch("kiro_crew.apps.routes.deregister_app", return_value=None) as deregister,
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
                return_value={"removable": [], "shared": [], "userInstalled": []},
            ),
            patch(
                "kiro_crew.apps.routes.clean_dependencies", new_callable=AsyncMock, return_value=[]
            ),
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)
        assert resp.status == 200, resp.status
        return json.loads(resp.body), calls, deregister

    async def test_the_backend_is_stopped_even_for_a_self_managed_app(self) -> None:
        """``resources`` comes from the app's own metadata.

        Gating the stop on it lets an app keep its process alive through its own
        uninstall, so the stop ignores the field.
        """
        _, calls, _ = await self._run(resources="app", live_port=None)

        assert "stop_app_backend" in calls

    async def test_a_self_managed_app_still_owns_its_registrations(self) -> None:
        """The split is deliberate: the process is stopped, the entries are not
        deleted, because an app with ``resources: "app"`` created them itself."""
        _, _, deregister = await self._run(resources="app", live_port=None)

        deregister.assert_not_called()

    async def test_a_gateway_managed_app_is_still_deregistered(self) -> None:
        _, _, deregister = await self._run(resources="gateway", live_port=None)

        deregister.assert_called_once()

    async def test_the_recorded_port_is_read_before_the_stop_drops_it(self) -> None:
        """The stop drops the tracking entry and the pidfile record, which are the
        only gateway-owned evidence of the port the backend actually used."""
        _, calls, _ = await self._run(resources="gateway", live_port=None)

        assert calls == ["recorded_backend_port", "stop_app_backend", "unstopped_backend_port"]

    async def test_a_still_listening_port_is_reported_on_the_response(self) -> None:
        body, _, _ = await self._run(resources="gateway", live_port=9137)

        assert body["ok"] is True, "an app that cannot be removed is the worse outcome"
        assert any("9137" in w for w in body["warnings"])

    async def test_a_silent_port_produces_no_warning(self) -> None:
        body, _, _ = await self._run(resources="gateway", live_port=None)

        assert "warnings" not in body

    async def test_a_still_listening_port_is_recorded_against_the_app_name(self) -> None:
        """Reporting the port is not enough: a later install of the same name
        finds that listener healthy on the declared port and would adopt it."""
        _, calls, _ = await self._run(resources="gateway", live_port=9137)

        assert "record_unstopped_backend:test-app:9137" in calls

    async def test_a_proven_stop_records_nothing(self) -> None:
        _, calls, _ = await self._run(resources="gateway", live_port=None)

        assert not [c for c in calls if c.startswith("record_unstopped_backend")]

    async def test_a_record_the_gateway_cannot_write_is_reported_too(self) -> None:
        """Losing the record silently is what turns a reported leak into an
        adopted one, so the write's own failure reaches the caller."""
        body, _, _ = await self._run(resources="gateway", live_port=9137, record_ok=False)

        assert len(body["warnings"]) == 2
        assert any("could not record port 9137" in w for w in body["warnings"])

    async def test_a_recorded_port_is_reported_once(self) -> None:
        body, _, _ = await self._run(resources="gateway", live_port=9137, record_ok=True)

        assert len(body["warnings"]) == 1


class TestUninstallTombstoneGatesAdoption:
    """A listener that outlives an uninstall must not be adopted as the backend.

    Adoption establishes that something healthy answers the declared port and
    which PIDs hold the socket. Neither reads the code that listener executes,
    so the record of the uninstall is what separates "this app's backend is
    already up" from "something outlived this app's removal".
    """

    def _home(self, tmp_path: Path) -> Any:
        return patch("kiro_crew.apps.backend.config_dir", return_value=tmp_path)

    def test_a_still_listening_recorded_port_is_refused(self, tmp_path: Path) -> None:
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 9137)
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9137) is True

    def test_the_record_is_dropped_once_the_listener_exits(self, tmp_path: Path) -> None:
        """Clearing on positive evidence is what keeps a legitimate fixed-port app
        installable: an app whose backend really did stop must not be fenced out."""
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 9137)
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=False):
                assert be.adoption_refused_after_uninstall("demo", 9137) is False
            # The row is gone, so a listener on that port is adoptable again.
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9137) is False

    def test_a_record_for_another_port_does_not_gate_this_one(self, tmp_path: Path) -> None:
        """The recorded process still holds its own port, so the row stays."""
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 9137)
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9138) is False
                assert be.adoption_refused_after_uninstall("demo", 9137) is True

    def test_an_app_with_no_record_is_not_gated(self, tmp_path: Path) -> None:
        with self._home(tmp_path):
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9137) is False

    def test_a_port_the_gateway_never_allocates_is_not_recorded(self, tmp_path: Path) -> None:
        """A row naming a port outside the app range describes a process this
        gateway never started, and gating on it would fence the app out for good."""
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 22)
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 22) is False

    def test_an_unreadable_record_refuses_rather_than_ignoring_itself(self, tmp_path: Path) -> None:
        """A record the gateway cannot parse is not a record it may ignore.

        Reading it as "no record" is exactly the I/O failure that hands the
        survivor to the next install, so an unusable record refuses.
        """
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 9137)
            be._tombstone_path("demo").write_text("{not json", encoding="utf-8")
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9137) is True

    def test_one_unreadable_record_does_not_fence_another_app(self, tmp_path: Path) -> None:
        """This is why the store is one file per app. Refusing every adoption on
        the host over one corrupt file is a worse failure than the leak it guards."""
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 9137)
            be._tombstone_path("demo").write_text("{not json", encoding="utf-8")
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9137) is True
                assert be.adoption_refused_after_uninstall("other-app", 9137) is False

    def test_a_record_naming_a_different_app_refuses(self, tmp_path: Path) -> None:
        """The filename is sanitized and therefore lossy, so two names can collide.
        The name inside the record is what keeps one app's row out of another's."""
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 9137)
            path = be._tombstone_path("demo")
            path.write_text(json.dumps({"app": "someone-else", "port": 9137}), encoding="utf-8")
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9137) is True

    def test_a_record_naming_an_out_of_range_port_refuses(self, tmp_path: Path) -> None:
        with self._home(tmp_path):
            be.record_unstopped_backend("demo", 9137)
            be._tombstone_path("demo").write_text(
                json.dumps({"app": "demo", "port": 22}), encoding="utf-8"
            )
            with patch("kiro_crew.apps.backend._port_is_listening", return_value=True):
                assert be.adoption_refused_after_uninstall("demo", 9137) is True

    def test_a_landed_write_answers_true(self, tmp_path: Path) -> None:
        with self._home(tmp_path):
            assert be.record_unstopped_backend("demo", 9137) is True

    def test_a_failed_write_answers_false_without_raising(self, tmp_path: Path) -> None:
        """The uninstall must finish: the onUninstall script has already run."""
        with self._home(tmp_path):
            with patch("kiro_crew.apps.backend.atomic_write", side_effect=OSError("read-only fs")):
                assert be.record_unstopped_backend("demo", 9137) is False

    def test_the_adoption_branch_consults_the_record_before_probing_health(self) -> None:
        """Order is the point. A healthy answer from the surviving listener is
        exactly what makes it look adoptable, so the refusal has to come first."""
        source = inspect.getsource(be._start_app_backend_body)
        guard = source.find("adoption_refused_after_uninstall(app_name, port)")
        probe = source.find("_probe_adoption_health(port, manifest.backend.healthCheck)")

        assert guard != -1, "the adoption branch must consult the uninstall record"
        assert probe != -1, "the health probe anchors this ordering assertion"
        assert guard < probe
