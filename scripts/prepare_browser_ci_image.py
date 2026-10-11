"""Build the CI-only Dockerfile using identical, pinned Docker official public images."""

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASES = {
    "FROM node:24-bookworm-slim AS web": (
        "FROM public.ecr.aws/docker/library/node:24-bookworm-slim"
        "@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS web"
    ),
    "FROM python:3.12-slim-trixie AS api": (
        "FROM public.ecr.aws/docker/library/python:3.12-slim-trixie"
        "@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS api"
    ),
}
ECR = "public.ecr.aws/docker/library/"
POSTGRES = (
    ECR + "postgres:17@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
)


def official_sources(reference: str) -> tuple[str, str]:
    """The fallback changes only the registry, never the pinned content identity."""
    if not reference.startswith(ECR) or "@sha256:" not in reference:
        raise ValueError("Expected a pinned Docker official ECR image")
    return reference, "docker.io/library/" + reference.removeprefix(ECR)


def pull_official_image(reference: str, run=subprocess.run) -> str:
    for source in official_sources(reference):
        try:
            result = run(["docker", "pull", source], check=False, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"Official image pull timed out: {source}", flush=True)
            continue
        if result.returncode == 0:
            return source
        print(f"Official image pull failed: {source}", flush=True)
    raise RuntimeError(f"Both official sources failed for {reference}")


def mirrored_dockerfile(source: str, selected: dict[str, str] | None = None) -> str:
    lines = source.splitlines(keepends=True)
    if any(sum(line.rstrip("\r\n") == original for line in lines) != 1 for original in BASES):
        raise ValueError("CI base-image mapping is stale; verify official digests before updating")
    replacements = dict(BASES)
    for original, chosen in (selected or {}).items():
        if original not in BASES:
            raise ValueError("Unknown CI base image")
        pinned, stage = BASES[original].removeprefix("FROM ").split(" AS ")
        if chosen not in official_sources(pinned):
            raise ValueError("CI fallback must preserve the exact official image digest")
        replacements[original] = f"FROM {chosen} AS {stage}"
    return "".join(
        replacements.get(line.rstrip("\r\n"), line.rstrip("\r\n"))
        + line[len(line.rstrip("\r\n")) :]
        for line in lines
    )


def prepare(source: str, compose: str, *, pull: bool = False, run=subprocess.run):
    # Reject stale mappings before attempting any network or Docker operation.
    rendered = mirrored_dockerfile(source)
    if compose.splitlines().count(f"    image: {POSTGRES}") != 1:
        raise ValueError("CI PostgreSQL mapping is stale; verify the official digest")
    postgres = POSTGRES
    if pull:
        postgres = pull_official_image(POSTGRES, run)
        selected = {
            original: pull_official_image(line.removeprefix("FROM ").split(" AS ")[0], run)
            for original, line in BASES.items()
        }
        rendered = mirrored_dockerfile(source, selected)
    return rendered, f"services:\n  postgres:\n    image: {postgres}\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pull", action="store_true", help="Pull exact official images with fallback"
    )
    args = parser.parse_args()
    rendered, compose = prepare(
        (ROOT / "Dockerfile").read_text(encoding="utf-8"),
        (ROOT / "ops/compose.browser.yaml").read_text(encoding="utf-8"),
        pull=args.pull,
    )
    output = ROOT / "build" / "Dockerfile.browser-ci"
    output.parent.mkdir(exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    (output.parent / "compose.browser-images.yaml").write_text(compose, encoding="utf-8")
    print("Prepared CI-only Dockerfile with pinned Docker official images.")
