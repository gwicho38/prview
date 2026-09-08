"""Discover review skills on disk and guess whether one suits a PR.

The fit result is a heuristic and is labelled as one: every skill stays
runnable, and the label reports what was observed rather than asserting the
skill does not apply.
"""
import re
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
_FIELD_RE = re.compile(r"^(name|description):\s*(.*?)\s*$", re.MULTILINE)
_FOLD_MARKERS = (">-", ">", "|", "|-")

# Tokens in a description that name a file a PR would have to touch.
_PATH_TOKENS = (
    "docker-compose", "dockerfile", "ansible", "nginx", "compose",
    "defaults.env", "stacks.toml", "package.json", "pyproject.toml",
    "requirements.txt", "terraform", "helm", "kustomization",
)

# A description naming repositories scopes the skill to them.
_REPO_RE = re.compile(r"\b(ultron|lysk-deploy|lysk-komodo|lysk-airgap-vm)\b")


@dataclass(frozen=True)
class ReviewSkill:
    name: str
    description: str
    path: str


def default_roots() -> list[Path]:
    home = Path.home() / ".claude"
    roots = [home / "skills"]
    roots.extend(sorted((home / "plugins" / "cache").glob("*/*/skills")))
    return roots


def _folded_description(block: str) -> str:
    """Join the indented continuation lines of a `description: >-` value."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        out = []
        for cont in lines[i + 1:]:
            if cont.strip() and not cont[0].isspace():
                break
            out.append(cont.strip())
        return " ".join(p for p in out if p)
    return ""


def _read_skill(directory: Path) -> ReviewSkill | None:
    try:
        head = (directory / "SKILL.md").read_text(errors="replace")[:4096]
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(head)
    if not m:
        return None
    fields = dict(_FIELD_RE.findall(m.group(1)))
    description = fields.get("description", "").strip().strip('"').strip("'")
    if description in _FOLD_MARKERS:
        description = _folded_description(m.group(1))
    return ReviewSkill(
        name=fields.get("name") or directory.name,
        description=description,
        path=str(directory),
    )


def is_review_skill(skill: ReviewSkill) -> bool:
    """Whether a skill belongs in the review menu.

    Matched on name, not description: a description substring pulls in skills
    that merely mention review in passing (cloudflare-one, durable-objects),
    which is most of them.
    """
    return "review" in skill.name.lower()


def discover_skills(roots: list[Path], reviews_only: bool = True) -> list[ReviewSkill]:
    """Skills under `roots`, the first occurrence of a name winning."""
    out: list[ReviewSkill] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            skill = _read_skill(directory)
            if skill is None or skill.name in seen:
                continue
            seen.add(skill.name)
            if reviews_only and not is_review_skill(skill):
                continue
            out.append(skill)
    return out


def fits(skill: ReviewSkill, filenames: list[str], repo: str) -> tuple[bool, str]:
    """Whether a PR looks like this skill's subject, and what was observed."""
    text = skill.description.lower()

    repos = set(_REPO_RE.findall(text))
    if repos and repo.lower() not in repos:
        return False, f"scoped to {', '.join(sorted(repos))}"

    wanted = [t for t in _PATH_TOKENS if t in text]
    if wanted:
        haystack = " ".join(filenames).lower()
        if not any(t in haystack for t in wanted):
            return False, f"no matching files ({wanted[0]})"

    return True, ""
