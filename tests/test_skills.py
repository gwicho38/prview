"""Tests for prview.skills — finding review skills on disk and guessing whether
one suits a PR. Every root is a tmp_path; the real ~/.claude is never read."""
from prview.skills import (
    ReviewSkill,
    discover_skills,
    fits,
    is_review_skill,
)


def _skill(root, name: str, description: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"
    )
    return d


# --- discovery ----------------------------------------------------------------

def test_a_skill_directory_is_discovered_with_its_frontmatter(tmp_path):
    _skill(tmp_path, "pr-review", "Adversarial PR review with lenses")
    found = discover_skills([tmp_path], reviews_only=False)
    assert [(s.name, s.description) for s in found] == [
        ("pr-review", "Adversarial PR review with lenses")
    ]


def test_a_symlinked_skill_is_discovered(tmp_path):
    real = tmp_path / "real"
    _skill(real, "airgap-review", "Review for airgapped deployment")
    links = tmp_path / "links"
    links.mkdir()
    (links / "airgap-review").symlink_to(real / "airgap-review")
    assert [s.name for s in discover_skills([links], reviews_only=False)] == ["airgap-review"]


def test_a_directory_without_a_skill_file_is_skipped(tmp_path):
    (tmp_path / "notaskill").mkdir()
    assert discover_skills([tmp_path], reviews_only=False) == []


def test_a_skill_file_without_frontmatter_is_skipped(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    (d / "SKILL.md").write_text("# just a heading\n")
    assert discover_skills([tmp_path], reviews_only=False) == []


def test_frontmatter_without_a_name_falls_back_to_the_directory_name(tmp_path):
    d = tmp_path / "unnamed"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: no name here\n---\n")
    assert discover_skills([tmp_path], reviews_only=False)[0].name == "unnamed"


def test_a_folded_yaml_description_is_read_as_one_line(tmp_path):
    d = tmp_path / "folded"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: folded\ndescription: >-\n  first line\n  second line\n---\n"
    )
    assert discover_skills([tmp_path], reviews_only=False)[0].description == "first line second line"


def test_a_missing_root_is_not_an_error(tmp_path):
    assert discover_skills([tmp_path / "nope"]) == []


def test_duplicate_names_across_roots_keep_the_first(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a, "pr-review", "first")
    _skill(b, "pr-review", "second")
    assert [s.description for s in discover_skills([a, b], reviews_only=False)] == ["first"]


# --- fit ----------------------------------------------------------------------

AIRGAP = ReviewSkill(
    name="airgap-review",
    description=("Review a change for airgapped deployment across ultron, "
                 "lysk-deploy — docker-compose files, Dockerfiles, ansible roles"),
    path="/x",
)

GENERIC = ReviewSkill(name="pr-review", description="Adversarial PR review", path="/x")


def test_a_skill_naming_no_paths_or_repos_always_fits():
    assert fits(GENERIC, ["anything.py"], "audio") == (True, "")


def test_a_repo_scoped_skill_does_not_fit_another_repo():
    ok, label = fits(AIRGAP, ["deploy/docker-compose.yml"], "audio")
    assert ok is False
    assert "ultron" in label


def test_a_repo_scoped_skill_fits_its_own_repo():
    ok, _ = fits(AIRGAP, ["deploy/docker-compose.yml"], "ultron")
    assert ok is True


def test_a_path_scoped_skill_does_not_fit_a_pr_without_those_files():
    ok, label = fits(AIRGAP, ["experiments/tn/normalize.py"], "ultron")
    assert ok is False
    assert "no matching files" in label


def test_the_repo_match_is_case_insensitive():
    ok, _ = fits(AIRGAP, ["deploy/docker-compose.yml"], "Ultron")
    assert ok is True


# --- the review-menu filter ---------------------------------------------------

def test_a_skill_named_review_belongs_in_the_menu():
    assert is_review_skill(ReviewSkill("pr-review", "anything", "/x")) is True


def test_a_skill_that_merely_mentions_review_does_not():
    assert is_review_skill(
        ReviewSkill("durable-objects", "…review the docs before…", "/x")) is False


def test_discovery_filters_to_review_skills_by_default(tmp_path):
    _skill(tmp_path, "pr-review", "adversarial")
    _skill(tmp_path, "turnstile-spin", "unrelated")
    assert [s.name for s in discover_skills([tmp_path])] == ["pr-review"]
