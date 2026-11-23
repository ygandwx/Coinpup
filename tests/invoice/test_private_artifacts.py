"""Private original durability, failure and cancellation boundaries, with fictional inputs."""

import asyncio
import os
import stat
import threading
from dataclasses import FrozenInstanceError, replace

import anyio
import pytest
from coinpup_api.business import invoice_artifacts as artifacts
from coinpup_api.business.invoice_render_process import RenderedInvoicePdf
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.errors import MoneyError

from tests.invoice.test_logo_rendering import frozen_logo, image_bytes
from tests.invoice.test_pdf_render_core import fonts as fonts
from tests.invoice.test_pdf_render_core import inspect
from tests.unit.business.test_invoice_render_snapshot import freeze, inputs

pytestmark = pytest.mark.invoice
# Signature-only storage fault fixtures; the positive path below renders real PDFs.
PAIR = tuple(
    RenderedInvoicePdf(lang, f"%PDF-1.7\nFictional {lang}\n%%EOF\n".encode())
    for lang in ("zh", "en")
)


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path / "private", max_bytes=8 * 1024 * 1024, timeout_seconds=5)


@pytest.fixture
def fake_renderer(monkeypatch):
    monkeypatch.setattr(artifacts, "render_invoice_pair", lambda *args, **kwargs: PAIR)


def prepared(store, fonts="unused-test-path", frozen=None, logo=None):
    frozen = frozen or freeze(*inputs())
    return artifacts.prepare_invoice_blobs(
        frozen.content,
        expected_sha256=frozen.sha256,
        font_directory=fonts,
        store=store,
        logo_content=logo,
    )


def blobs(store):
    return sorted((store.root / "blobs").glob("*"))


def staging(store):
    return sorted((store.root / "staging").glob("*"))


def test_actual_child_pair_persists_exact_private_bytes_and_retry_keeps_originals(store, fonts):
    logo = image_bytes()
    frozen = frozen_logo(logo, count=200)
    first = asyncio.run(prepared(store, fonts, frozen, logo))
    assert first.snapshot_sha256 == frozen.sha256
    assert [x.language for x in first.artifacts] == ["zh", "en"]
    assert len({x.blob_key for x in first.artifacts}) == 2
    saved = []
    for item in first.artifacts:
        with store.open_blob(item.blob_key, item.sha256, item.byte_size) as stream:
            content = stream.read()
        assert "Fictional" in inspect(content)[0]
        assert item.media_type == "application/pdf"
        assert (store.root / "blobs" / item.blob_key).stat().st_mode & 0o777 == 0o600
        saved.append(content)
        with pytest.raises(FrozenInstanceError):
            item.byte_size = 0
    assert not staging(store)
    retry = asyncio.run(prepared(store, fonts, frozen, logo))
    assert [(x.language, x.sha256, x.byte_size) for x in retry.artifacts] == [
        (x.language, x.sha256, x.byte_size) for x in first.artifacts
    ]
    # Canonical metadata/deduplication belongs to the future short DB transaction.
    assert len(blobs(store)) == 4 and not staging(store)
    assert [(store.root / "blobs" / x.blob_key).read_bytes() for x in first.artifacts] == saved


@pytest.mark.parametrize("pair", [PAIR[:1], PAIR[::-1], (PAIR[0], "wrong")])
def test_partial_or_misordered_render_never_touches_storage(store, monkeypatch, pair):
    monkeypatch.setattr(artifacts, "render_invoice_pair", lambda *args, **kwargs: pair)
    with pytest.raises(MoneyError) as caught:
        asyncio.run(prepared(store))
    assert caught.value.code == "invoice_render_integrity"
    assert not store.root.exists()


def test_both_are_staged_and_verified_before_either_publish(store, fake_renderer, monkeypatch):
    original = store.publish
    count = 0

    def publish(staged):
        nonlocal count
        assert len(staging(store)) == 2 - count
        count += 1
        return original(staged)

    monkeypatch.setattr(store, "publish", publish)
    assert len(asyncio.run(prepared(store)).artifacts) == 2 and count == 2


@pytest.mark.parametrize("fault", ["second-stage", "identity"])
def test_stage_failure_cleans_prepared_tokens_without_publishing(
    store, fake_renderer, monkeypatch, fault
):
    original = store.stage
    count = 0

    async def stage(chunks, size):
        nonlocal count
        count += 1
        if fault == "second-stage" and count == 2:
            raise OSError("fictional private path must not escape")
        staged = await original(chunks, size)
        return replace(staged, sha256="f" * 64) if fault == "identity" else staged

    monkeypatch.setattr(store, "stage", stage)
    with pytest.raises(MoneyError) as caught:
        asyncio.run(prepared(store))
    assert "fictional private path" not in str(caught.value)
    assert not blobs(store) and not staging(store)


@pytest.mark.parametrize("after", [False, True])
def test_second_publish_failure_retains_every_possible_original_and_retry_is_safe(
    store, fake_renderer, monkeypatch, after
):
    original = store.publish
    count = 0

    def publish(staged):
        nonlocal count
        count += 1
        if count == 2 and not after:
            raise OSError("fictional publish fault")
        key = original(staged)
        if count == 2:
            raise OSError("fictional response lost after durability")
        return key

    monkeypatch.setattr(store, "publish", publish)
    with pytest.raises(MoneyError):
        asyncio.run(prepared(store))
    retained = {p.name: p.read_bytes() for p in blobs(store)}
    assert len(retained) == 2 if after else len(retained) == 1
    assert not staging(store)
    monkeypatch.setattr(store, "publish", original)
    assert len(asyncio.run(prepared(store)).artifacts) == 2
    assert all((store.root / "blobs" / k).read_bytes() == v for k, v in retained.items())
    assert len(blobs(store)) == len(retained) + 2


@pytest.mark.parametrize("number", [1, 2])
def test_file_fsync_failure_cannot_publish_a_partial_pair(
    store, fake_renderer, monkeypatch, number
):
    original = os.fsync
    count = 0

    def sync(descriptor):
        nonlocal count
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            count += 1
            if count == number:
                raise OSError("fictional file fsync fault")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(MoneyError):
        asyncio.run(prepared(store))
    assert not staging(store) and not blobs(store)


def test_destination_directory_fsync_failure_retains_published_bytes(
    store, fake_renderer, monkeypatch
):
    original = store._sync

    def sync(directory):
        if os.readlink(f"/proc/self/fd/{directory}") == str(store.root / "blobs"):
            raise OSError("fictional directory fsync fault")
        return original(directory)

    monkeypatch.setattr(store, "_sync", sync)
    with pytest.raises(MoneyError):
        asyncio.run(prepared(store))
    assert [p.read_bytes() for p in blobs(store)] == [PAIR[0].content]
    assert not staging(store)


def test_post_publish_corruption_is_detected_without_deleting_any_blob(
    store, fake_renderer, monkeypatch
):
    original = store.publish

    def publish(staged):
        key = original(staged)
        # Explicit corruption injection into fictional output to exercise final verification.
        (store.root / "blobs" / key).write_bytes(b"corrupted")
        return key

    monkeypatch.setattr(store, "publish", publish)
    with pytest.raises(MoneyError) as caught:
        asyncio.run(prepared(store))
    assert caught.value.code == "invoice_artifact_storage_unavailable"
    assert len(blobs(store)) == 2 and not staging(store)


def test_cleanup_fault_still_attempts_remaining_staged_file(store, fake_renderer, monkeypatch):
    original = store.discard
    attempted = []

    def discard(staged):
        attempted.append(staged.token)
        if len(attempted) == 1:
            raise OSError("fictional staging cleanup fault")
        return original(staged)

    def fail_publish(staged):
        raise OSError("fictional prepublication fault")

    monkeypatch.setattr(store, "discard", discard)
    monkeypatch.setattr(store, "publish", fail_publish)
    with pytest.raises(MoneyError):
        asyncio.run(prepared(store))
    assert len(set(attempted)) == 2 and len(staging(store)) == 1 and not blobs(store)


@pytest.mark.parametrize("phase", ["render", "publish"])
def test_cancellation_waits_for_owned_thread_and_cleans_staging_without_deleting_blobs(
    store, fake_renderer, monkeypatch, phase
):
    started, release, finished = (threading.Event() for _ in range(3))
    original = store.publish

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs) if phase == "publish" else PAIR
        started.set()
        assert release.wait(3), "test cancellation handoff was not released"
        finished.set()
        return result

    if phase == "render":
        monkeypatch.setattr(artifacts, "render_invoice_pair", delayed)
    else:
        monkeypatch.setattr(store, "publish", delayed)

    async def exercise():
        returned = False
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:

                async def work():
                    nonlocal returned
                    await prepared(store)
                    returned = True

                group.start_soon(work)
                while not started.is_set():
                    await anyio.sleep(0.001)
                group.cancel_scope.cancel()
                release.set()
        assert finished.is_set() and not returned

    anyio.run(exercise)
    assert not staging(store)
    assert len(blobs(store)) == (1 if phase == "publish" else 0)
