"""Private immutable blobs; no user filename is ever used as a filesystem path.

Signatures are an upload format boundary, not a decoder or malware verdict. Full
document parsing belongs to the separately bounded OCR worker. Linux uses pinned
directory descriptors and fsync; Windows is supported for development tests.
"""

import hashlib
import os
import re
import stat
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import anyio

KEY = re.compile(r"[0-9a-f]{32}\Z")
SUPPORTED_MEDIA_TYPES = ("application/pdf", "image/jpeg", "image/png", "image/webp")


class FileStoreError(Exception):
    def __init__(self, code: str, status_code: int = 503):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class StagedFile:
    token: str
    sha256: str
    byte_size: int
    media_type: str


def _media_type(head: bytes, tail: bytes, size: int) -> str:
    if re.match(rb"%PDF-[12]\.[0-9]", head) and b"%%EOF" in tail:
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff") and tail.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if (
        head.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        and tail.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        and size >= 45
    ):
        return "image/png"
    if (
        head.startswith(b"RIFF")
        and len(head) >= 20
        and head[8:12] == b"WEBP"
        and head[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
        and int.from_bytes(head[4:8], "little") + 8 == size
    ):
        return "image/webp"
    raise FileStoreError("unsupported_file_format", 415)


class FileStore:
    def __init__(self, root: Path, max_bytes: int, timeout_seconds: int):
        self.root = root.absolute()  # Do not resolve away forbidden symlinks.
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    @contextmanager
    def _directory(self, name: str, *, create: bool = False) -> Iterator[int | Path]:
        path = self.root / name
        descriptor = None
        try:
            if os.name == "posix":
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(path.anchor, flags)
                for part in path.parts[1:]:
                    if part in {".", ".."}:
                        raise FileStoreError("file_storage_unavailable")
                    if create:
                        try:
                            os.mkdir(part, 0o700, dir_fd=descriptor)
                            os.fsync(descriptor)
                        except FileExistsError:
                            pass
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                yield descriptor
            else:
                current = Path(path.anchor)
                for part in path.parts[1:]:
                    if part in {".", ".."}:
                        raise FileStoreError("file_storage_unavailable")
                    current /= part
                    if create:
                        current.mkdir(mode=0o700, exist_ok=True)
                    metadata = current.lstat()
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or current.is_symlink()
                        or current.is_junction()
                    ):
                        raise FileStoreError("file_storage_unavailable")
                yield current
        except OSError:
            raise FileStoreError("file_storage_unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _key(value: str) -> str:
        if not KEY.fullmatch(value):
            raise FileStoreError("file_storage_unavailable")
        return value

    @staticmethod
    def _open(directory: int | Path, key: str, *, new: bool = False) -> BinaryIO:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if new else os.O_RDONLY
        flags |= (
            getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        if isinstance(directory, int):
            descriptor = os.open(key, flags, 0o600, dir_fd=directory)
        else:
            path = directory / key
            if path.is_symlink() or path.is_junction():
                raise FileStoreError("file_storage_unavailable")
            descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise FileStoreError("file_storage_unavailable")
            return os.fdopen(descriptor, "wb" if new else "rb")
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _sync(directory: int | Path) -> None:
        if isinstance(directory, int):
            os.fsync(directory)

    def _new_stage(self, token: str) -> BinaryIO:
        with self._directory("staging", create=True) as directory:
            return self._open(directory, token, new=True)

    async def stage(self, chunks: AsyncIterable[bytes], declared_size: int) -> StagedFile:
        if declared_size <= 0 or declared_size > self.max_bytes:
            raise FileStoreError("upload_too_large" if declared_size > 0 else "empty_file", 413)
        token = uuid4().hex
        stream = None
        completed = False
        digest = hashlib.sha256()
        count, head, tail = 0, b"", b""
        try:
            with anyio.fail_after(self.timeout_seconds):
                stream = await anyio.to_thread.run_sync(self._new_stage, token)
                async for chunk in chunks:
                    if not chunk:
                        continue
                    count += len(chunk)
                    if count > self.max_bytes or count > declared_size:
                        raise FileStoreError("upload_too_large", 413)
                    head = (head + chunk[:1024])[:1024]
                    tail = (tail + chunk)[-1024:]
                    await anyio.to_thread.run_sync(self._write, stream, digest, chunk)
                if count != declared_size:
                    raise FileStoreError("upload_size_mismatch", 422)
                media_type = _media_type(head, tail, count)
                await anyio.to_thread.run_sync(self._finish, stream)
                await anyio.lowlevel.checkpoint()
                await anyio.to_thread.run_sync(stream.close)
                stream = None
                await anyio.lowlevel.checkpoint()
                completed = True
                return StagedFile(token, digest.hexdigest(), count, media_type)
        except TimeoutError:
            raise FileStoreError("upload_timeout", 408) from None
        except ConnectionError:
            raise FileStoreError("upload_interrupted", 400) from None
        except OSError:
            raise FileStoreError("file_storage_unavailable") from None
        finally:
            # Cleanup must finish even after disconnect/timeout cancellation.
            with anyio.CancelScope(shield=True):
                if stream is not None:
                    try:
                        await anyio.to_thread.run_sync(stream.close)
                    except OSError:
                        pass  # Preserve the processing failure and still attempt unlink.
                if not completed:
                    await anyio.to_thread.run_sync(self._discard_token, token)

    @staticmethod
    def _write(stream: BinaryIO, digest, chunk: bytes) -> None:
        stream.write(chunk)
        digest.update(chunk)

    @staticmethod
    def _finish(stream: BinaryIO) -> None:
        stream.flush()
        os.fsync(stream.fileno())

    def _verified(self, directory: int | Path, key: str, sha256: str, size: int) -> BinaryIO:
        stream = self._open(directory, self._key(key))
        try:
            if os.fstat(stream.fileno()).st_size != size:
                raise FileStoreError("file_integrity_error")
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != sha256:
                raise FileStoreError("file_integrity_error")
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise

    def publish(self, staged: StagedFile) -> str:
        token = self._key(staged.token)
        key = uuid4().hex
        with self._directory("staging") as source, self._directory("blobs", create=True) as target:
            with self._verified(source, token, staged.sha256, staged.byte_size):
                if isinstance(source, int) and isinstance(target, int):
                    os.link(token, key, src_dir_fd=source, dst_dir_fd=target, follow_symlinks=False)
                else:
                    os.link(source / token, target / key, follow_symlinks=False)
                self._sync(target)
        self.discard(staged)
        return key

    def _discard_token(self, token: str) -> None:
        try:
            with self._directory("staging") as directory:
                if isinstance(directory, int):
                    os.unlink(self._key(token), dir_fd=directory)
                else:
                    (directory / self._key(token)).unlink(missing_ok=True)
                self._sync(directory)
        except (FileStoreError, FileNotFoundError):
            # Cleanup failure cannot convert an already committed object to failure.
            pass

    def discard(self, staged: StagedFile) -> None:
        self._discard_token(staged.token)

    def open_blob(self, blob_key: str, sha256: str, byte_size: int) -> BinaryIO:
        with self._directory("blobs") as directory:
            return self._verified(directory, blob_key, sha256, byte_size)
