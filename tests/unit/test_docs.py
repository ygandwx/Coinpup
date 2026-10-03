"""Documentation policy covers owned Markdown and measures UTF-8 bytes."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_docs.py"
SPEC = importlib.util.spec_from_file_location("coinpup_check_docs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(docs)


@pytest.fixture
def repository(tmp_path):
    for name in docs.required:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Fictional documentation\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "contents",
    [
        "[CI](https://github.com/fictional/example/actions/runs/123)",
        "[CI][run]\n\n[run]: https://github.com/fictional/example/actions/runs/123",
        "<https://github.com/fictional/example/actions/runs/123>",
        "https://github.com/fictional/example/actions/runs/123",
        '[CI](https://github.com/fictional/example/actions/runs/123 "result")',
    ],
)
def test_actions_run_links_fail_in_all_link_forms(repository, contents):
    (repository / "README.md").write_text(contents, encoding="utf-8")
    errors, _ = docs.check_repository(repository)
    assert errors and all("Actions run link belongs in a PR" in error for error in errors)


def test_plan_literal_and_code_examples_are_allowed(repository):
    (repository / "README.md").write_text(
        "禁止 `/actions/runs/` 链接。\n"
        "[PR](https://github.com/fictional/example/pull/123)\n"
        "`https://github.com/fictional/example/actions/runs/123`\n"
        "```markdown\n[CI](https://github.com/fictional/example/actions/runs/123)\n```\n"
        "~~~markdown\n[CI](https://github.com/fictional/example/actions/runs/123)\n~~~\n",
        encoding="utf-8",
    )
    assert docs.check_repository(repository)[0] == []


@pytest.mark.parametrize("name,limit", list(docs.BYTE_BUDGETS.items()))
def test_budget_accepts_boundary_and_rejects_extra_utf8_bytes(repository, name, limit):
    source = repository / name
    contents = "é" * (limit // 2)
    source.write_bytes(contents.encode("utf-8"))
    assert docs.check_repository(repository)[0] == []
    source.write_bytes((contents + "é").encode("utf-8"))
    errors, _ = docs.check_repository(repository)
    assert errors == [f"{name}: byte budget exceeded ({limit + 2} bytes; maximum {limit} bytes)"]


def test_scans_github_and_nested_agents_but_excludes_dependencies_and_builds(repository):
    owned = ["docs/new.md", ".github/PULL_REQUEST_TEMPLATE.md", "services/api/AGENTS.md"]
    excluded = [
        "node_modules/dependency/README.md",
        ".git/README.md",
        ".venv/lib/README.md",
        "venv/lib/README.md",
        "apps/web/dist/README.md",
        ".pytest_cache/README.md",
    ]
    for name in owned + excluded:
        source = repository / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("[Missing](missing.md)\n", encoding="utf-8")
    errors, count = docs.check_repository(repository)
    assert count == len(docs.required) + len(owned)
    assert len(errors) == len(owned)
    assert all(any(str(Path(name)) in error for error in errors) for name in owned)


def test_local_links_still_reject_missing_or_outside_repository_targets(repository):
    (repository / "README.md").write_text(
        "[Valid](docs/engineering/status.md) [Missing](missing.md) [Outside](../outside.md)",
        encoding="utf-8",
    )
    errors, _ = docs.check_repository(repository)
    assert len(errors) == 2
    assert all("broken local link" in error for error in errors)


def test_cli_reports_policy_failure_as_nonzero_exit(repository, monkeypatch):
    (repository / "README.md").write_text("[Missing](missing.md)", encoding="utf-8")
    monkeypatch.setattr(docs, "ROOT", repository)
    with pytest.raises(SystemExit, match="broken local link"):
        docs.main()
