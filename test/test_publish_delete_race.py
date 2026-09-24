"""A destroy landing inside a first publish's upload window.

A first publish uploads its object BEFORE the record naming it exists, so for the length of
that upload the store answers "not published" about content that is already served. Anything
that destroys an artifact on the strength of that answer erases the only handle able to
withdraw the copy, and no later action reaches it.

The two doors that destroy an artifact hold the artifact's publication guard across the
removal, which is what makes the store's own ``refuse_if_published`` re-read decisive. These
tests drive a real publish that parks mid-upload and send each door in while it is parked.

Two residuals are PINNED here rather than fixed, each asserting the answer that holds today:
the guard spans neither event loops nor processes, and the blank-shell settle is a third door
that does not take it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew import publish_provider, publish_sync
from kiro_crew.artifacts import (
    ArtifactFolderStore,
    ArtifactNotFoundError,
    ArtifactPublication,
    ArtifactStore,
)
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_delete,
    api_artifact_folder_delete,
)
from kiro_crew.publish_provider import PublishProvider, PublishResult, PushResult

# ── Harness ─────────────────────────────────────────────────────────────────


class ParkedUploadProvider(PublishProvider):
    """A provider whose ``publish`` parks with the object already uploaded.

    ``uploaded`` is set at the point the destination copy is public and the engine has not
    yet been handed the handle, which is exactly the window under test. ``may_return``
    releases it, so a test decides how long the window stays open.
    """

    name = publish_provider.DEFAULT_PROVIDER
    display_name = "Parked Upload Provider"
    install_hint = "the test provider is unavailable"

    def __init__(self) -> None:
        self.uploaded = asyncio.Event()
        self.may_return = asyncio.Event()
        self.withdrawn: list[str] = []

    def available(self) -> bool:
        return True

    def view_url_for(self, external_id: str) -> str:
        return f"https://destination.example/{external_id}"

    async def publish(
        self,
        *,
        file_path: str,
        content_type: str,
        title: str,
        summary: str,
        tags: list[str],
        visibility: str,
        shared_with: list[str],
    ) -> PublishResult:
        # Everything above this line is the upload. The object is public from here.
        self.uploaded.set()
        await self.may_return.wait()
        return PublishResult(
            external_id="uuid-window",
            view_url="https://destination.example/uuid-window",
            version_number=1,
            concurrency_token="sha-window",
        )

    async def push_version(
        self, *, external_id: str, file_path: str, expected_token: str
    ) -> PushResult:
        return PushResult(version_number=2, concurrency_token="sha-2")

    async def update_sharing(
        self, *, external_id: str, visibility: str, shared_with: list[str]
    ) -> None:
        return None

    async def unpublish(self, *, external_id: str) -> None:
        self.withdrawn.append(external_id)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Isolated artifact + folder stores wired into the module globals."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    fstore = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_mod, "_default_folder_store", fstore)
    return store, fstore


@pytest.fixture
def provider():
    """Register the parked-upload provider under the default provider name."""
    prov = ParkedUploadProvider()
    publish_provider.reset_providers()
    saved = dict(publish_provider._FACTORIES)
    publish_provider.register_provider(prov.name, lambda: prov)
    publish_provider._INSTANCES[prov.name] = prov
    yield prov
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved)
    publish_provider.reset_providers()


@pytest.fixture
def patch_restricted(monkeypatch):
    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


def _request(*, match: dict | None = None, query: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = match or {}
    req.query = query or {}
    req.read = AsyncMock(return_value=b"")
    state = MagicMock()
    state.get_slot.return_value = None
    req.app = {"state": state, "_restricted_session": False}
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


async def _let_the_other_task_run() -> None:
    """Yield long enough for an unguarded destroy to finish while the upload is parked."""
    for _ in range(20):
        await asyncio.sleep(0.005)


def _surviving(store: ArtifactStore, slug: str):
    try:
        return store.get(slug)
    except ArtifactNotFoundError:
        return None


# ── The two doors ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_delete_inside_the_upload_window_keeps_the_artifact_and_its_handle(
    stores, provider, patch_restricted
) -> None:
    """A single-artifact delete sent while the copy is uploading must not destroy it.

    The store cannot help here on its own: at the moment the delete reads it there is no
    publication record to find, because the engine writes one only once the upload returns.
    So the delete either waits for the publish or runs before it, and the assertion is the
    user-visible outcome of that -- the artifact is still there and still names its copy.
    """
    store, _ = stores
    art = store.create(name="Doc", content="hello", kind="text")

    publishing = asyncio.create_task(publish_sync.publish(art.slug, visibility="PUBLIC"))
    await asyncio.wait_for(provider.uploaded.wait(), timeout=5)
    assert (
        _surviving(store, art.slug).publication is None
    ), "harness check: the window is the stretch where the copy is up and unrecorded"

    deleting = asyncio.create_task(api_artifact_delete(_request(match={"slug": art.slug})))
    await _let_the_other_task_run()
    provider.may_return.set()

    publish_result = await asyncio.gather(publishing, return_exceptions=True)
    resp = await deleting

    assert not isinstance(publish_result[0], BaseException), (
        "the publish lost its artifact mid-upload, so the uploaded copy has no handle: "
        f"{publish_result[0]!r}"
    )
    survivor = _surviving(store, art.slug)
    assert survivor is not None, (
        "the artifact was destroyed while its copy was uploading, leaving a public copy "
        "with nothing able to withdraw it"
    )
    assert survivor.publication is not None, "the uploaded copy must keep its record"
    assert survivor.publication.artifact_id == "uuid-window"
    assert resp.status == 409, "the delete must be refused and say so, not silently complete"
    assert provider.withdrawn == [], "nothing was withdrawn, so the copy must stay recorded"


@pytest.mark.asyncio
async def test_a_folder_cascade_inside_the_upload_window_keeps_the_artifact(
    stores, provider, patch_restricted
) -> None:
    """The cascade is the same defect through a different door and obeys the same rule.

    Its withdrawal pass reads the same empty record the single delete does, so the guard is
    what stops the destroy from running between the upload and the record. The cascade's own
    rule then applies: an artifact it may not destroy is KEPT and reported.
    """
    store, fstore = stores
    folder = fstore.create("F")
    art = store.create(name="Doc", content="hello", kind="text", folder_id=folder["id"])

    publishing = asyncio.create_task(publish_sync.publish(art.slug, visibility="PUBLIC"))
    await asyncio.wait_for(provider.uploaded.wait(), timeout=5)

    deleting = asyncio.create_task(
        api_artifact_folder_delete(
            _request(match={"id": folder["id"]}, query={"delete_contents": "true"})
        )
    )
    await _let_the_other_task_run()
    provider.may_return.set()

    publish_result = await asyncio.gather(publishing, return_exceptions=True)
    resp = await deleting

    assert not isinstance(publish_result[0], BaseException), (
        "the cascade destroyed the artifact mid-upload, so the uploaded copy has no handle: "
        f"{publish_result[0]!r}"
    )
    survivor = _surviving(store, art.slug)
    assert survivor is not None, (
        "the cascade destroyed an artifact whose copy was uploading, leaving a public copy "
        "with nothing able to withdraw it"
    )
    assert survivor.publication is not None, "the uploaded copy must keep its record"
    assert resp.status == 200
    assert (
        art.slug in _body(resp)["kept_published_artifact_slugs"]
    ), "an artifact the cascade may not destroy must be reported as kept"


@pytest.mark.asyncio
async def test_a_cascade_leaves_alone_an_artifact_that_arrives_while_it_withdraws(
    stores, provider, patch_restricted
) -> None:
    """An artifact moved into the subtree mid-pass must not be destroyed.

    The withdrawal pass is not instantaneous: it awaits a network withdrawal per published
    copy, and a move into the folder during that stretch takes no publication guard. Such
    an artifact is outside the guarded set, so its publication state cannot be established
    from inside the store either -- a first publish in flight has uploaded its object and
    written no record. The cascade therefore leaves it in place and says so.
    """
    store, fstore = stores
    folder = fstore.create("F")
    early = store.create(name="Early", content="x", kind="text", folder_id=folder["id"])
    store.set_publication(
        early.slug,
        ArtifactPublication(
            provider=publish_provider.DEFAULT_PROVIDER,
            artifact_id="uuid-early",
            view_url="https://destination.example/uuid-early",
            visibility="PUBLIC",
        ),
    )
    arriving = store.create(name="Arriving", content="hello", kind="text", folder_id="")

    publishing = asyncio.create_task(publish_sync.publish(arriving.slug, visibility="PUBLIC"))
    await asyncio.wait_for(provider.uploaded.wait(), timeout=5)

    async def _withdraw_and_let_one_in(art):
        # The window exactly: the pass has begun, this artifact was not in its listing,
        # and its upload is already done.
        store.set_folder(arriving.slug, folder["id"])
        return publish_sync.DeleteWithdrawal.WITHDRAWN

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("kiro_crew.publish_sync.delete_for_artifact", _withdraw_and_let_one_in)
        resp = await api_artifact_folder_delete(
            _request(match={"id": folder["id"]}, query={"delete_contents": "true"})
        )

    provider.may_return.set()
    publish_result = await asyncio.gather(publishing, return_exceptions=True)

    assert not isinstance(publish_result[0], BaseException), (
        "the cascade destroyed the arriving artifact mid-upload, so its copy has no "
        f"handle: {publish_result[0]!r}"
    )
    assert resp.status == 200
    body = _body(resp)
    assert (
        arriving.slug in body["unguarded_artifact_slugs"]
    ), "an artifact the cascade could not guard must be reported, not destroyed"
    survivor = _surviving(store, arriving.slug)
    assert survivor is not None, "the arriving artifact must survive the cascade"
    assert survivor.publication is not None, "its uploaded copy must keep its record"
    assert arriving.slug in (body.get("notice") or ""), "the notice must name what survived"
    assert (
        early.slug in body["deleted_artifact_slugs"]
    ), "the guarded, withdrawn artifact must still be destroyed"


# ── Residuals, pinned at today's answer ──────────────────────────────────────


def test_the_publication_guard_spans_one_event_loop_only() -> None:
    """PIN: two live event loops are NOT excluded from each other by this guard.

    The guard binds per running loop, so two loops hold it at once -- proved by a barrier
    both threads must reach from INSIDE it. A separate process is excluded even less: it
    has its own lock table entirely. Both readings matter for the same reason: the guard
    covers the gateway, where the publish and the destroy paths run, and nothing wider.
    """
    guard = publish_sync.publication_guard("pinned-slug")
    both_inside = threading.Barrier(2, timeout=5)
    failures: list[BaseException] = []

    def _hold() -> None:
        async def _main() -> None:
            async with guard:
                both_inside.wait()

        try:
            asyncio.run(_main())
        except BaseException as exc:  # noqa: BLE001 -- recorded, asserted below
            failures.append(exc)

    threads = [threading.Thread(target=_hold, name=f"guard-{i}") for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert failures == [], (
        "the barrier was not reached from inside the guard by both loops, so the guard now "
        "spans loops -- the pin is stale and this residual is closed: "
        f"{failures!r}"
    )


def test_every_door_that_destroys_an_artifact_is_accounted_for() -> None:
    """A per-door verdict, so a door is not left out by being forgotten.

    Two doors take the guard. The blank-shell settle is a third: it destroys a pristine
    shell on the same ``publication is None`` reading and does not take it, so a shell
    published and abandoned in the same breath can still strand its object. That object is
    an empty document rather than the owner's content, which is why it is pinned here with
    the answer that holds today rather than fixed alongside the other two.
    """
    guarded = {
        "api_artifact_delete": art_handlers.api_artifact_delete,
        "api_artifact_folder_delete": art_handlers.api_artifact_folder_delete,
    }
    for name, fn in guarded.items():
        assert "publication_guard" in inspect.getsource(
            fn
        ), f"{name} destroys an artifact without holding the publication guard"

    assert "publication_guard" not in inspect.getsource(art_handlers.api_artifact_settle_blank), (
        "the blank-shell settle now takes the guard -- the pin is stale and this residual "
        "is closed"
    )
