"""Run the repository checks from Windows or Linux using the current Python environment."""

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Step:
    name: str
    command: list[str]
    discard_stdout: bool = False


def python_step(name: str, *arguments: str, discard_stdout: bool = False) -> Step:
    return Step(name, [sys.executable, *arguments], discard_stdout)


def web_steps(npm: str, *, build: bool) -> list[Step]:
    scripts = ["format:check", "lint"] if build else []
    scripts.extend(["typecheck", "test:unit"])
    if build:
        scripts.append("build")
    return [
        Step(f"Web {script}", [npm, "--prefix", "apps/web", "run", script]) for script in scripts
    ]


def build_steps(mode: str, fix: bool, root: Path) -> tuple[list[Step], list[str]]:
    steps = []
    fix_steps = []
    skipped = []
    if fix:
        fix_steps.extend(
            [
                python_step("Fix Python lint", "-m", "ruff", "check", "--fix", "."),
                python_step("Format Python", "-m", "ruff", "format", "."),
            ]
        )
    if mode == "assets":
        steps.extend(
            [
                python_step("Dependency consistency", "-m", "pip", "check"),
                python_step(
                    "Pinned engine input checks",
                    "-m",
                    "pytest",
                    "tests/engine_assets",
                    "-m",
                    "engine_assets",
                ),
            ]
        )
        return fix_steps + steps, skipped
    if mode == "corpus":
        steps.extend(
            [
                python_step("Dependency consistency", "-m", "pip", "check"),
                python_step("Corpus dependency notices", "scripts/check_corpus_dependencies.py"),
                python_step(
                    "Real corpus checks", "-m", "pytest", "tests/benchmark", "-m", "benchmark"
                ),
            ]
        )
        return fix_steps + steps, skipped
    if mode == "ocr":
        steps.extend(
            [
                python_step("Dependency consistency", "-m", "pip", "check"),
                python_step("Optional dependency notices", "scripts/check_ocr_dependencies.py"),
                python_step("Optional OCR checks", "-m", "pytest", "tests/ocr", "-m", "ocr"),
            ]
        )
        return fix_steps + steps, skipped
    if mode == "db":
        steps.extend(
            [
                python_step("Apply migrations", "-m", "alembic", "upgrade", "head"),
                python_step("Downgrade empty test database", "-m", "alembic", "downgrade", "base"),
                python_step("Reapply migrations", "-m", "alembic", "upgrade", "head"),
                python_step("Check migration consistency", "-m", "alembic", "check"),
                python_step(
                    "PostgreSQL integration tests",
                    "-m",
                    "pytest",
                    "tests/integration",
                    "-m",
                    "integration",
                ),
            ]
        )
        return fix_steps + steps, skipped
    if mode == "fast":
        steps.extend(
            [
                python_step("Python lint", "-m", "ruff", "check", "."),
                python_step("Python formatting", "-m", "ruff", "format", "--check", "."),
                python_step(
                    "Tests without database", "-m", "pytest", "-m", "not integration and not ocr"
                ),
                python_step("Documentation", "scripts/check_docs.py"),
                python_step("OpenAPI snapshot", "scripts/export_openapi.py", "--check"),
                python_step(
                    "Offline migration SQL",
                    "-m",
                    "alembic",
                    "upgrade",
                    "head",
                    "--sql",
                    discard_stdout=True,
                ),
            ]
        )
    if mode == "web" or (root / "apps/web/node_modules").is_dir():
        npm = shutil.which("npm")
        if npm is None:
            raise ValueError(
                "npm was not found on PATH; install Node.js/npm before running web checks"
            )
        if fix:
            fix_steps.append(Step("Format Web", [npm, "--prefix", "apps/web", "run", "format"]))
        if mode == "web":
            git = shutil.which("git")
            if git is None:
                raise ValueError("git was not found on PATH; install Git before running web checks")
            steps.extend(
                [
                    Step("Generate API types", [npm, "--prefix", "apps/web", "run", "gen:api"]),
                    Step(
                        "Generated API consistency",
                        [git, "diff", "--exit-code", "apps/web/src/generated"],
                    ),
                ]
            )
        steps.extend(web_steps(npm, build=mode == "web"))
    else:
        skipped.append("Web checks: apps/web/node_modules is absent; run npm --prefix apps/web ci")
    return fix_steps + steps, skipped


def run_steps(steps: list[Step], root: Path, skipped: list[str]) -> int:
    completed = []
    failed = None
    exit_code = 0
    started = time.perf_counter()
    for step in steps:
        print(f"\n[RUN] {step.name}", flush=True)
        step_started = time.perf_counter()
        try:
            result = subprocess.run(
                step.command,
                cwd=root,
                stdout=subprocess.DEVNULL if step.discard_stdout else None,
                check=False,
            )
            exit_code = result.returncode if result.returncode > 0 else int(result.returncode != 0)
        except OSError as error:
            print(f"Could not start check: {error}", flush=True)
            exit_code = 1
        except KeyboardInterrupt:
            print("Check interrupted", flush=True)
            exit_code = 130
        elapsed = time.perf_counter() - step_started
        if exit_code:
            failed = step.name
            print(f"[FAIL] {step.name} ({elapsed:.2f}s; exit {exit_code})", flush=True)
            break
        completed.append(step.name)
        print(f"[PASS] {step.name} ({elapsed:.2f}s)", flush=True)
    for reason in skipped:
        print(f"[SKIP] {reason}", flush=True)
    elapsed = time.perf_counter() - started
    print(
        f"\nSummary: {len(completed)} passed, {int(failed is not None)} failed, "
        f"{len(skipped)} skipped ({elapsed:.2f}s)",
        flush=True,
    )
    if failed is not None:
        print(f"Failed check: {failed}; remaining checks were not run", flush=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", nargs="?", choices=("fast", "web", "db", "ocr", "corpus", "assets"), default="fast"
    )
    parser.add_argument(
        "--fix", action="store_true", help="Run lint fixes and Python/Web formatting first"
    )
    arguments = parser.parse_args(argv)
    if arguments.mode == "db":
        if (
            os.environ.get("COINPUP_RUN_DB_TESTS") != "1"
            or not os.environ.get("COINPUP_DATABASE_URL", "").strip()
        ):
            print(
                "[FAIL] db requires explicit COINPUP_RUN_DB_TESTS=1 and COINPUP_DATABASE_URL. "
                "No checks were run.",
                file=sys.stderr,
            )
            return 1
        print(
            "WARNING: db runs migration downgrade and destructive integration fixtures. "
            "Use ONLY an explicitly configured disposable test database.",
            flush=True,
        )
    try:
        steps, skipped = build_steps(arguments.mode, arguments.fix, ROOT)
    except ValueError as error:
        print(f"[FAIL] {error}; no checks were run", file=sys.stderr)
        return 1
    return run_steps(steps, ROOT, skipped)


if __name__ == "__main__":
    raise SystemExit(main())
