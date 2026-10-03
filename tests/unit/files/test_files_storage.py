import asyncio
import hashlib
import os
import time
from pathlib import Path

import anyio
import pytest
from coinpup_api.files.storage import FileStore, FileStoreError, StagedFile

PDF = b"%PDF-1.7\n% Fictional upload fixture, not an OCR parsing fixture\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17 + b"\x00\x00\x00\x00IEND\xaeB`\x82"


async def chunks(value, width=7):
    for start in range(0, len(value), width):
        yield value[start : start + width]


def stage(store, value=PDF, declared=None):
    return asyncio.run(store.stage(chunks(value), len(value) if declared is None else declared))


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path / "private", max_bytes=1024, timeout_seconds=1)


def test_constructor_has_no_filesystem_effects(tmp_path):
    root = tmp_path / "uncreated"
    FileStore(root, 1024, 1)
    assert not root.exists()


@pytest.mark.parametrize(
    ("value", "media"),
    [
        (PDF, "application/pdf"),
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0fictional\xff\xd9", "image/jpeg"),
        (
            b"RIFF" + (16).to_bytes(4, "little") + b"WEBPVP8L" + b"\x04\x00\x00\x00test",
            "image/webp",
        ),
    ],
)
def test_allowed_signature_stream_publishes_exact_immutable_bytes(store, value, media):
    staged = stage(store, value)
    assert staged.media_type == media
    assert staged.sha256 == hashlib.sha256(value).hexdigest()
    key = store.publish(staged)
    assert key != staged.token
    assert len(key) == 32
    with store.open_blob(key, staged.sha256, staged.byte_size) as stream:
        assert stream.read() == value
    assert not list((store.root / "staging").iterdir())
    store.discard(staged)  # Idempotent cleanup cannot delete a published blob.
    assert (store.root / "blobs" / key).is_file()
    if os.name == "posix":
        assert (store.root / "blobs" / key).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("value", "declared", "code"),
    [
        (PDF, 0, "empty_file"),
        (PDF, 1025, "upload_too_large"),
        (PDF, len(PDF) - 1, "upload_too_large"),
        (PDF, len(PDF) + 1, "upload_size_mismatch"),
        (b"<html>fake invoice</html>", 25, "unsupported_file_format"),
        (b"%PDF-1.7\ntruncated", 18, "unsupported_file_format"),
        (b"\xff\xd8\xfftruncated", 12, "unsupported_file_format"),
    ],
)
def test_rejected_upload_cannot_leave_a_ready_or_staged_file(store, value, declared, code):
    with pytest.raises(FileStoreError) as error:
        stage(store, value, declared)
    assert error.value.code == code
    assert not (store.root / "blobs").exists()
    assert not list((store.root / "staging").glob("*"))


def test_disconnect_cleans_partial_file(store):
    async def disconnect():
        yield PDF[:5]
        raise ConnectionError("client lost")

    with pytest.raises(FileStoreError, match="upload_interrupted"):
        asyncio.run(store.stage(disconnect(), len(PDF)))
    assert not list((store.root / "staging").iterdir())


def test_timeout_cleans_partial_file(store):
    store.timeout_seconds = 0.01

    async def slow():
        yield PDF[:5]
        await anyio.sleep(1)
        yield PDF[5:]

    with pytest.raises(FileStoreError, match="upload_timeout"):
        asyncio.run(store.stage(slow(), len(PDF)))
    assert not list((store.root / "staging").iterdir())


def test_disk_write_failure_is_generic_and_cleans_stage(store, monkeypatch):
    def broken(*_):
        raise OSError("secret-storage-location")

    monkeypatch.setattr(store, "_write", broken)
    with pytest.raises(FileStoreError) as error:
        stage(store)
    assert error.value.code == "file_storage_unavailable"
    assert "secret" not in str(error.value)
    assert not list((store.root / "staging").iterdir())


@pytest.mark.parametrize("finish_fails", [True, False])
def test_close_failure_always_cleans_stage_and_never_leaks_os_error(
    store, monkeypatch, finish_fails
):
    original = store._new_stage

    class BrokenClose:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def close(self):
            self.stream.close()
            raise OSError("private-close-details")

    monkeypatch.setattr(store, "_new_stage", lambda token: BrokenClose(original(token)))
    if finish_fails:

        def broken_finish(_):
            raise OSError("private-fsync-details")

        monkeypatch.setattr(store, "_finish", broken_finish)
    with pytest.raises(FileStoreError, match="file_storage_unavailable"):
        stage(store)
    assert not list((store.root / "staging").iterdir())


def test_deadline_is_rechecked_after_non_interruptible_file_flush(store, monkeypatch):
    store.timeout_seconds = 0.1
    original = store._finish

    def slow_finish(stream):
        time.sleep(0.15)
        original(stream)

    monkeypatch.setattr(store, "_finish", slow_finish)
    with pytest.raises(FileStoreError, match="upload_timeout"):
        stage(store)
    assert not list((store.root / "staging").iterdir())


@pytest.mark.parametrize("mode", ["length", "digest", "missing"])
def test_corrupt_or_missing_blob_is_never_returned(store, mode):
    staged = stage(store)
    key = store.publish(staged)
    path = store.root / "blobs" / key
    if mode == "missing":
        path.unlink()
    else:
        path.write_bytes(PDF + b"x" if mode == "length" else b"x" * len(PDF))
    with pytest.raises(FileStoreError):
        store.open_blob(key, staged.sha256, staged.byte_size)


@pytest.mark.parametrize(
    "key", ["../outside", "/etc/passwd", "A" * 32, "a" * 31, "a" * 32 + ".pdf"]
)
def test_path_like_keys_are_rejected_without_touching_files(store, key):
    staged = stage(store)
    store.publish(staged)
    with pytest.raises(FileStoreError):
        store.open_blob(key, staged.sha256, staged.byte_size)
    store.discard(StagedFile(key, staged.sha256, staged.byte_size, staged.media_type))
    assert len(list((store.root / "blobs").iterdir())) == 1


def test_publish_rechecks_staging_integrity(store):
    staged = stage(store)
    (store.root / "staging" / staged.token).write_bytes(b"x" * len(PDF))
    with pytest.raises(FileStoreError, match="file_integrity_error"):
        store.publish(staged)
    assert not list((store.root / "blobs").iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow descriptor boundary")
@pytest.mark.parametrize("target", ["root", "staging", "blob", "fifo"])
def test_symlink_and_non_regular_file_boundaries(store, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "root":
        store.root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(FileStoreError):
            stage(store)
    elif target == "staging":
        store.root.mkdir()
        (store.root / "staging").symlink_to(outside, target_is_directory=True)
        with pytest.raises(FileStoreError):
            stage(store)
    else:
        staged = stage(store)
        key = store.publish(staged)
        path = store.root / "blobs" / key
        path.unlink()
        if target == "blob":
            external_file = outside / "private.pdf"
            external_file.write_bytes(PDF)
            path.symlink_to(external_file)
        else:
            os.mkfifo(path)
        with pytest.raises(FileStoreError):
            store.open_blob(key, staged.sha256, staged.byte_size)
    assert not list(outside.glob("*.tmp"))


def test_symlinked_parent_is_rejected(tmp_path):
    if os.name != "posix":
        pytest.skip("Windows symlink creation requires privileges")
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    store = FileStore(Path(alias / "private"), 1024, 1)
    with pytest.raises(FileStoreError):
        stage(store)
    assert not (real / "private").exists()
