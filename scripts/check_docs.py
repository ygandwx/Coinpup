"""Check handoff entry points, Markdown links and documentation byte budgets."""

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
required = [
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "docs/engineering/status.md",
    "docs/engineering/handoff.md",
    "docs/engineering/roadmap.md",
    "docs/product/requirements.md",
    "docs/product/acceptance.md",
    "docs/architecture/overview.md",
]
BYTE_BUDGETS = {"README.md": 8 * 1024, "docs/engineering/status.md": 6 * 1024}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "site-packages",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    ".tox",
    ".nox",
    ".vite",
    "dist",
    "build",
    "htmlcov",
    "test-results",
    "playwright-report",
}


def markdown_files(root: Path) -> list[Path]:
    files = []
    for directory, children, names in os.walk(root):
        children[:] = sorted(
            name
            for name in children
            if name not in EXCLUDED_DIRECTORIES and not name.endswith(".egg-info")
        )
        files.extend(Path(directory) / name for name in sorted(names) if name.endswith(".md"))
    return files


def markdown_targets(contents: str) -> list[str]:
    # Code examples are not links. Include reference links and GFM bare URL autolinks.
    contents = re.sub(r"```.*?```|~~~.*?~~~", "", contents, flags=re.S)
    contents = re.sub(r"(`+).*?\1", "", contents, flags=re.S)
    targets = re.findall(r"\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)", contents)
    targets.extend(re.findall(r"(?m)^ {0,3}\[[^\]]+\]:[ \t]*(<[^>\n]+>|[^\s]+)", contents))
    targets.extend(re.findall(r"https?://[^\s<>\"')\]]+", contents))
    return list(dict.fromkeys(target.strip("<>") for target in targets))


def check_repository(root: Path) -> tuple[list[str], int]:
    root = root.resolve()
    errors = [f"Missing entry point: {path}" for path in required if not (root / path).is_file()]
    for name, limit in BYTE_BUDGETS.items():
        source = root / name
        if source.is_file() and (size := source.stat().st_size) > limit:
            errors.append(f"{name}: byte budget exceeded ({size} bytes; maximum {limit} bytes)")
    files = markdown_files(root)
    for source in files:
        for target in markdown_targets(source.read_text(encoding="utf-8")):
            try:
                parsed = urlsplit(target)
            except ValueError:
                errors.append(f"{source.relative_to(root)}: invalid link {target}")
                continue
            if "/actions/runs/" in unquote(parsed.path):
                errors.append(
                    f"{source.relative_to(root)}: Actions run link belongs in a PR: {target}"
                )
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            path = (source.parent / unquote(parsed.path)).resolve()
            if not path.is_relative_to(root) or not path.exists():
                errors.append(f"{source.relative_to(root)}: broken local link {target}")
    return errors, len(files)


def main() -> None:
    errors, count = check_repository(ROOT)
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Documentation entry points, links and byte budgets passed ({count} Markdown files)")


if __name__ == "__main__":
    main()
