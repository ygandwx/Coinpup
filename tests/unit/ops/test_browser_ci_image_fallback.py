"""Registry failures cannot change image content or hide a failed browser prerequisite."""

import subprocess
from types import SimpleNamespace

import pytest

from scripts.prepare_browser_ci_image import (
    BASES,
    POSTGRES,
    ROOT,
    mirrored_dockerfile,
    official_sources,
    prepare,
    pull_official_image,
)

REFERENCES = [POSTGRES, *(line.removeprefix("FROM ").split(" AS ")[0] for line in BASES.values())]


@pytest.mark.parametrize("reference", REFERENCES)
def test_successful_preferred_pull_does_not_try_another_registry(reference):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    assert pull_official_image(reference, run) == reference
    assert calls == [(["docker", "pull", reference], {"check": False, "timeout": 120})]


@pytest.mark.parametrize("reference", REFERENCES)
@pytest.mark.parametrize("timeout", [False, True])
def test_failed_preferred_pull_uses_same_digest_at_official_hub(reference, timeout):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            if timeout:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    fallback = official_sources(reference)[1]
    assert pull_official_image(reference, run) == fallback
    assert calls == [["docker", "pull", reference], ["docker", "pull", fallback]]
    assert reference.split("@", 1)[1] == fallback.split("@", 1)[1]


def test_both_failures_are_fatal_and_bounded():
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1)

    with pytest.raises(RuntimeError, match="Both official sources failed"):
        pull_official_image(POSTGRES, run)
    assert len(calls) == 2


def test_preparation_renders_only_selected_exact_bases_and_postgres():
    calls = []

    def run(args, **_kwargs):
        calls.append(args[2])
        return SimpleNamespace(returncode=int(args[2].startswith("public.ecr.aws/")))

    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "ops/compose.browser.yaml").read_text(encoding="utf-8")
    rendered, override = prepare(source, compose, pull=True, run=run)
    assert len(calls) == 6
    assert override == f"services:\n  postgres:\n    image: {official_sources(POSTGRES)[1]}\n"
    expected = mirrored_dockerfile(source).replace(
        "public.ecr.aws/docker/library/", "docker.io/library/"
    )
    assert rendered == expected
    assert (
        rendered.split("FROM ${OCR_NATIVE_IMAGE}")[1] == source.split("FROM ${OCR_NATIVE_IMAGE}")[1]
    )
    assert (ROOT / "Dockerfile").read_text(encoding="utf-8") == source
    assert (ROOT / "ops/compose.browser.yaml").read_text(encoding="utf-8") == compose


@pytest.mark.parametrize("changed", ["dockerfile", "compose"])
def test_stale_mapping_is_rejected_before_pulls(changed):
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "ops/compose.browser.yaml").read_text(encoding="utf-8")
    if changed == "dockerfile":
        source = source.replace("FROM node:24-bookworm-slim AS web", "FROM node:25 AS web")
    else:
        compose = compose.replace(POSTGRES, "postgres:18")

    def forbidden(*_args, **_kwargs):
        pytest.fail("A stale mapping attempted to pull an image")

    with pytest.raises(ValueError, match="mapping is stale"):
        prepare(source, compose, pull=True, run=forbidden)


@pytest.mark.parametrize(
    "reference", ["docker.io/library/node:latest", "untrusted.invalid/node@sha256:a"]
)
def test_selection_cannot_change_registry_or_digest(reference):
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="exact official image digest"):
        mirrored_dockerfile(source, {next(iter(BASES)): reference})


def test_default_preparation_never_pulls_images():
    def forbidden(*_args, **_kwargs):
        pytest.fail("Local source rendering attempted a pull")

    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "ops/compose.browser.yaml").read_text(encoding="utf-8")
    rendered, override = prepare(source, compose, run=forbidden)
    assert rendered == mirrored_dockerfile(source)
    assert POSTGRES in override
