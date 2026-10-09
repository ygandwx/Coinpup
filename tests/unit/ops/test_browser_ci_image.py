"""CI source selection must fail closed when the actual application bases change."""

import pytest

from scripts.prepare_browser_ci_image import ROOT, mirrored_dockerfile


def test_ci_mirror_preserves_every_other_dockerfile_line_and_ocr_stage():
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    result = mirrored_dockerfile(source)
    changes = [
        (old, new)
        for old, new in zip(source.splitlines(), result.splitlines(), strict=True)
        if old != new
    ]
    assert len(changes) == 2
    assert all(
        new.startswith("FROM public.ecr.aws/docker/library/") and "@sha256:" in new
        for _, new in changes
    )
    assert (
        source.split("FROM ${OCR_NATIVE_IMAGE}")[1] == result.split("FROM ${OCR_NATIVE_IMAGE}")[1]
    )
    assert (ROOT / "Dockerfile").read_text(encoding="utf-8") == source


@pytest.mark.parametrize(
    "replacement",
    [
        "FROM python:3.13-slim-trixie AS api",
        "",
        "FROM python:3.12-slim-trixie AS api\nFROM python:3.12-slim-trixie AS api",
    ],
)
def test_changed_missing_or_duplicated_base_is_rejected(replacement):
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="mapping is stale"):
        mirrored_dockerfile(source.replace("FROM python:3.12-slim-trixie AS api", replacement))
