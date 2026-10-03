"""Check handoff entry points and repository-local Markdown file links."""

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
errors = [f"Missing entry point: {path}" for path in required if not (ROOT / path).is_file()]
files = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
for source in files:
    contents = source.read_text(encoding="utf-8")
    # Code examples are not Markdown links. Anchor existence is intentionally not checked.
    contents = re.sub(r"```.*?```", "", contents, flags=re.S)
    for target in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", contents):
        parsed = urlsplit(target.strip("<>"))
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        path = (source.parent / unquote(parsed.path)).resolve()
        if not path.is_relative_to(ROOT) or not path.exists():
            errors.append(f"{source.relative_to(ROOT)}: broken local link {target}")
if errors:
    raise SystemExit("\n".join(errors))
print(f"Documentation entry points and local file links passed ({len(files)} Markdown files)")
