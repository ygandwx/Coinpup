"""Start only the disposable GitHub CI database, with pinned official image fallback."""

import argparse
import os
import subprocess
import time

from scripts.prepare_browser_ci_image import POSTGRES, pull_official_image

NAME = "coinpup-ci-postgres"


def start(*, run=subprocess.run, sleep=time.sleep):
    image = pull_official_image(POSTGRES, run)
    run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            NAME,
            "--publish",
            "127.0.0.1:5432:5432",
            "--env",
            "POSTGRES_DB=coinpup",
            "--env",
            "POSTGRES_USER=coinpup",
            # Explicitly fictional credentials for this disposable CI container.
            "--env",
            "POSTGRES_PASSWORD=test-only",
            "--health-cmd",
            "pg_isready -U coinpup -d coinpup",
            "--health-interval",
            "10s",
            "--health-timeout",
            "5s",
            "--health-retries",
            "5",
            image,
        ],
        check=True,
        timeout=60,
    )
    for _ in range(30):
        result = run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", NAME],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        status = result.stdout.strip()
        if status == "healthy":
            return
        if status != "starting":
            break
        sleep(2)
    run(["docker", "logs", NAME], check=False, timeout=15)
    raise RuntimeError("Disposable PostgreSQL did not become healthy; database tests cannot run")


def stop(*, run=subprocess.run):
    result = run(
        ["docker", "ps", "--all", "--filter", f"name=^/{NAME}$", "--format", "{{.ID}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.stdout.strip():
        run(["docker", "rm", "--force", "--volumes", NAME], check=True, timeout=60)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="Remove only the named CI container")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This entry point only operates inside GitHub Actions")
    stop() if args.stop else start()


if __name__ == "__main__":
    main()
