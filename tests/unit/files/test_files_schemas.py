"""The reservation is immutable client intent, not an arbitrary path or media claim."""

from uuid import uuid4

import pytest
from coinpup_api.files.schemas import FileUpdate, LinkUpdate, UploadCreate
from pydantic import ValidationError


@pytest.mark.parametrize(
    "filename", ["../bill.pdf", r"folder\bill.pdf", ".", "..", " ", "a\x00b", "a\r\nb", "a" * 256]
)
def test_filename_is_a_bounded_label_not_a_path(filename):
    with pytest.raises(ValidationError):
        UploadCreate(id=uuid4(), original_filename=filename, declared_size=10)


@pytest.mark.parametrize("size", [0, -1, True, 1.5, "10", 2**63])
def test_size_is_a_positive_bounded_integer(size):
    with pytest.raises(ValidationError):
        UploadCreate(id=uuid4(), original_filename="fictional.pdf", declared_size=size)


def test_reservation_requires_stable_id_and_refuses_server_owned_fields():
    body = {"original_filename": "虚构发票.pdf", "declared_size": 10}
    with pytest.raises(ValidationError):
        UploadCreate(**body)
    for key in ("blob_key", "sha256", "media_type", "created_by", "file_id"):
        with pytest.raises(ValidationError):
            UploadCreate(**body, id=uuid4(), **{key: "forbidden"})
    assert UploadCreate(**body, id=uuid4()).original_filename == "虚构发票.pdf"


@pytest.mark.parametrize(
    "body",
    [
        {"expected_version": 1},
        {"expected_version": 1, "title": None},
        {"expected_version": 1, "title": " "},
        {"expected_version": True, "archived": True},
        {"expected_version": 1, "archived": "false"},
        {"expected_version": 1, "sha256": "changed"},
    ],
)
def test_metadata_updates_are_explicit_and_content_is_immutable(body):
    with pytest.raises(ValidationError):
        FileUpdate(**body)


def test_archive_and_restore_carry_optimistic_version():
    assert (
        FileUpdate(expected_version=2, title="Fictional statement", archived=False).archived
        is False
    )
    assert LinkUpdate(expected_version=3, archived=True).archived is True
    with pytest.raises(ValidationError):
        LinkUpdate(expected_version=1, archived=None)
