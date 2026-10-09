"""Build the CI-only Dockerfile using identical, pinned Docker official public images."""

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


def mirrored_dockerfile(source: str) -> str:
    lines = source.splitlines(keepends=True)
    if any(sum(line.rstrip("\r\n") == original for line in lines) != 1 for original in BASES):
        raise ValueError("CI base-image mapping is stale; verify official digests before updating")
    return "".join(
        BASES.get(line.rstrip("\r\n"), line.rstrip("\r\n")) + line[len(line.rstrip("\r\n")) :]
        for line in lines
    )


if __name__ == "__main__":
    rendered = mirrored_dockerfile((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    output = ROOT / "build" / "Dockerfile.browser-ci"
    output.parent.mkdir(exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print("Prepared CI-only Dockerfile with pinned Docker official images.")
