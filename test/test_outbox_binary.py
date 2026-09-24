"""Integration tests for binary file support in outbox notify + download handlers."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import file_delivery_consent
from kiro_crew.dashboard.handlers import api_outbox_download, api_outbox_notify


def _synth_flagged_png() -> bytes:
    """PNG-shaped bytes that fail UTF-8 decode and carry key-SHAPED material.

    Assembled at runtime from fragments rather than checked in as a literal, the
    same convention ``test_file_delivery_consent`` documents: a diff carrying
    literal PEM headers reads as an exfiltration recipe to a review provider, and
    the scanner sees identical bytes either way. The body is deterministic base64
    over a digest, so it is private-key-SHAPED without being a private key.

    ``\\xff\\xfe`` is what makes the UTF-8 decode raise, which is the branch under
    test; ``.png`` puts the guessed MIME type inside the allow-list so the bytes
    reach the content scan rather than stopping at the type check.
    """
    rule = "-" * 5
    begin = " ".join(["BEGIN", "RSA", "PRIVATE", "KEY"])
    end = " ".join(["END", "RSA", "PRIVATE", "KEY"])
    body = "\n".join(
        base64.b64encode(hashlib.sha256(f"kc-8779-{i}".encode()).digest() * 2).decode()
        for i in range(4)
    )
    pem = f"{rule}{begin}{rule}\n{body}\n{rule}{end}{rule}\n"
    return b"\x89PNG\r\n\x1a\n\xff\xfe" + pem.encode() + b"\x80\x81"


def _clean_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\xff\xfe" + b"\x00" * 200


def _make_app(state=None) -> web.Application:
    app = web.Application()
    app["state"] = state or MagicMock(_slots={})
    app.router.add_post("/api/outbox/notify", api_outbox_notify)
    app.router.add_get("/api/outbox/{filename}", api_outbox_download)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture(autouse=True)
def isolated_consent_store(tmp_path, monkeypatch):
    """Point the consent store at a tmp file so the host's real grant cannot leak in.

    Without this a machine that happens to hold an ``owner_dashboard`` grant would
    turn every "refused without a grant" assertion below green for the wrong
    reason.
    """
    store = tmp_path / "file_delivery_consent.json"
    monkeypatch.setattr(
        file_delivery_consent, "file_delivery_consent_path", lambda: store, raising=True
    )
    return store


def _grant() -> None:
    file_delivery_consent.record_grant(
        file_delivery_consent.CLASS_OWNER_DASHBOARD, granted_at="2026-09-05T00:00:00+00:00"
    )


@pytest.fixture
def outbox(tmp_path):
    # Not ``tmp_path``: on macOS pytest's basetemp carries the per-user
    # ``/var/folders/<..30 random chars..>/T`` segment, which trips the bare-secret
    # heuristic in redact_credentials() and has api_outbox_notify reject the path
    # with 400 before any test logic runs.
    #
    # Not a literal ``/tmp`` either: the rootdir conftest already gives the run
    # its own ``tempfile`` base -- ``/tmp/kc-pytest-<user>-<pid>-<rand>`` on macOS,
    # ``$TMPDIR/kc-pytest-...`` elsewhere -- short, low-entropy, removed at session
    # end and RESIDUE-REPORTED, whereas a directory dropped straight into the
    # shared ``/tmp`` is owned by nobody and, on hosts that reap ``/tmp``
    # mid-session, can vanish under a running test. A bare ``mkdtemp()`` lands
    # in that base; the assertion pins it so a ``dir=`` creeping back in is a
    # red test rather than residue on someone's disk. Same fixture as
    # test_outbox_notify_broadcast.py.
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp())
    assert base.is_relative_to(tempfile.gettempdir()), base
    odir = base / "outbox"
    odir.mkdir()
    try:
        with patch("kiro_crew.config.loader.outbox_dir", return_value=odir):
            yield odir
    finally:
        shutil.rmtree(base, ignore_errors=True)


class TestOutboxNotifyBinary:
    @pytest.mark.asyncio
    async def test_binary_mp3_accepted(self, outbox, mock_sel):
        """Binary MP3 file passes notify validation (in allowlist)."""
        mp3 = outbox / "test.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 50)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(mp3),
                "filename": "test.mp3",
                "description": "test audio",
                "size": mp3.stat().st_size,
            })
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_binary_exe_rejected(self, outbox, mock_sel):
        """Binary EXE file rejected (not in allowlist)."""
        exe = outbox / "payload.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8 PE header
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(exe),
                "filename": "payload.exe",
                "description": "bad file",
                "size": exe.stat().st_size,
            })
            assert resp.status == 400
            data = await resp.json()
            assert "not allowed" in data["error"]

    @pytest.mark.asyncio
    async def test_text_with_secrets_rejected(self, outbox, mock_sel):
        """Text file with AWS key is rejected."""
        txt = outbox / "secrets.txt"
        txt.write_text("key=AKIAIOSFODNN7EXAMPLE")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(txt),
                "filename": "secrets.txt",
                "description": "oops",
                "size": txt.stat().st_size,
            })
            assert resp.status == 400
            data = await resp.json()
            assert "sensitive" in data["error"]


class TestOutboxDownloadBinary:
    @pytest.mark.asyncio
    async def test_download_mp3_inline(self, outbox, mock_sel):
        """MP3 served with audio/mpeg content-type and inline disposition."""
        mp3 = outbox / "standup.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/standup.mp3")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "audio/mpeg"
            assert "inline" in resp.headers["Content-Disposition"]
            assert resp.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.asyncio
    async def test_download_exe_rejected(self, outbox, mock_sel):
        """EXE file rejected by download handler (not in allowlist).

        macOS ships /etc/apache2/mime.types which classifies .exe as
        application/x-msdownload; Linux CI lacks that file and falls
        through to application/octet-stream. Compute the expected mime
        from the same call the handler makes so the assertion is
        platform-portable.
        """
        exe = outbox / "bad.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/bad.exe")
            assert resp.status == 403
            data = await resp.json()
            assert "not allowed" in data["error"]
        expected_mime = mimetypes.guess_type("bad.exe")[0] or "application/octet-stream"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="api", source="api", tool_name="file_send",
            tool_kind="download", outcome="denied",
            error=f"binary_mime_not_allowed: {expected_mime}",
        )

    @pytest.mark.asyncio
    async def test_download_text_served(self, outbox, mock_sel):
        """Clean text file served normally."""
        txt = outbox / "readme.txt"
        txt.write_text("hello world")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/readme.txt")
            assert resp.status == 200
            body = await resp.read()
            assert b"hello world" in body

    @pytest.mark.asyncio
    async def test_download_video_inline(self, outbox, mock_sel):
        """MP4 served with video/mp4 and inline disposition."""
        mp4 = outbox / "clip.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x1cftyp\xff\xfe\x80\x81" + b"\x90" * 100)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/clip.mp4")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "video/mp4"
            assert "inline" in resp.headers["Content-Disposition"]


class TestGeneratedBinaryActuallyTrips:
    """A fixture the scanner ignores would make every case below vacuous."""

    def test_the_flagged_png_is_detected(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(_synth_flagged_png())

    def test_the_clean_png_is_not_detected(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert not binary_content_is_flagged(_clean_png())

    def test_both_fixtures_really_are_binary(self):
        for raw in (_synth_flagged_png(), _clean_png()):
            with pytest.raises(UnicodeDecodeError):
                raw.decode("utf-8")


class TestOwnerFacingGatesScanBinaryContent:
    """An allow-listed media type carrying a credential is not waved through.

    The type allow-list answers "can the browser render these bytes safely", which
    is a different question from "do these bytes contain a secret". Both
    owner-facing HTTP gates decide the second question with the same recorded
    grant their text branch already consults, so one file cannot be refused as
    text and accepted as a PNG.
    """

    @pytest.mark.asyncio
    async def test_notify_refuses_a_flagged_media_file_without_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "shot.png",
                    "description": "screenshot",
                    "size": png.stat().st_size,
                },
            )
            assert resp.status == 400
            assert "embedded credentials" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_notify_delivers_the_card_under_the_owners_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        _grant()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "shot.png",
                    "description": "screenshot",
                    "size": png.stat().st_size,
                },
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_download_refuses_flagged_media_without_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/shot.png")
            assert resp.status == 400
            assert "embedded credentials" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_download_refuses_flagged_media_for_a_non_owner_holding_a_grant(
        self, outbox, mock_sel
    ):
        """The grant releases bytes to the OWNER, not to every authenticated caller.

        Same second conjunct the text branch carries: this route needs
        authentication, which a Slack allow-listed non-owner running ``!dashboard``
        also has. The collaborator is patched rather than an identity forged onto
        the request, so what is pinned is the route's decision and not the harness.
        """
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        _grant()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/outbox/shot.png")
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_download_serves_flagged_media_to_the_owner_under_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        raw = _synth_flagged_png()
        png.write_bytes(raw)
        _grant()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=True,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/outbox/shot.png")
                assert resp.status == 200
                assert await resp.read() == raw

    @pytest.mark.asyncio
    async def test_a_clean_media_file_needs_no_grant_on_either_gate(self, outbox, mock_sel):
        """The scan must not turn ordinary media into a consent prompt."""
        png = outbox / "clean.png"
        png.write_bytes(_clean_png())
        async with TestClient(TestServer(_make_app())) as client:
            notify = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "clean.png",
                    "description": "plain",
                    "size": png.stat().st_size,
                },
            )
            assert notify.status == 200
            download = await client.get("/api/outbox/clean.png")
            assert download.status == 200

    @pytest.mark.asyncio
    async def test_a_refused_media_type_is_never_scanned(self, outbox, mock_sel):
        """Type first, content second: a type this route refuses stops at the type.

        The ordering is what keeps a 50 MB executable from being decoded and
        scanned only to be refused for its type anyway, and the audited reason
        stays the accurate one.
        """
        exe = outbox / "bad.exe"
        exe.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/bad.exe")
            assert resp.status == 403
            assert "not allowed" in (await resp.json())["error"]
        expected_mime = mimetypes.guess_type("bad.exe")[0] or "application/octet-stream"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"binary_mime_not_allowed: {expected_mime}",
        )
