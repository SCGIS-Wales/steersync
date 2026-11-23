"""Proposal titles, merge request descriptions and the user part of branch names."""

from pathlib import Path

import pytest

from steersync import __version__, gitops, proposal
from steersync.config import Config
from steersync.lint import LintFinding
from steersync.steering import ChangePlan

PREFIXES = {"steering": "steering", "specs": "specs", "agents": "agents"}


def plan(add: tuple[str, ...] = (), update: tuple[str, ...] = (), delete: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    return ChangePlan(add, update, delete, (), (), ())


def test_title_counts_files_and_names_the_user() -> None:
    changes = plan(add=("steering/a.md", "specs/b.md"))
    assert proposal.title("acme/payments-api", changes, "jdoe") == (
        "Kiro config: acme/payments-api (2 files, jdoe)"
    )
    assert proposal.title("global", plan(update=("steering/a.md",)), "jdoe") == (
        "Kiro config: global (1 file, jdoe)"
    )


def test_description_summarises_then_folds_the_file_list() -> None:
    changes = plan(
        add=("steering/product.md", "steering/tech.md", "specs/plan/tasks.md"),
        update=("agents/reviewer.json",),
        delete=("steering/old.md",),
    )
    text = proposal.describe(
        project="acme/payments-api",
        user="jdoe",
        plan=changes,
        prefixes=PREFIXES,
        lint_errors=(
            LintFinding("steering/product.md", "front-matter-yaml", "error", "quote it", 4),
        ),
        source="gitlab.example.com/acme/payments-api",
        base="d52d91920000",
        reason="Adds the payments steering.",
    )
    body = text.body
    assert body.startswith("Adds the payments steering.\n\n")
    assert "Kiro configuration for **acme/payments-api**, proposed by **jdoe**." in body
    assert "| steering | 2 | 0 | 1 |" in body
    assert "| specs | 1 | 0 | 0 |" in body
    assert "| agents | 0 | 1 | 0 |" in body
    # The table follows the collection order, and paths are shown inside their collection.
    assert body.index("| steering |") < body.index("| specs |") < body.index("| agents |")
    assert "**Note:** 1 file with lint errors proposed anyway" in body
    assert "- `steering/product.md`: quote it" in body
    assert "<details><summary>All 5 files</summary>" in body
    assert "- added `product.md`" in body
    assert "- deleted `old.md`" in body
    assert body.rstrip().endswith(
        f"Source: gitlab.example.com/acme/payments-api · base `main` @ d52d9192 · "
        f"steersync {__version__}"
    )
    message = text.commit_message()
    assert message.startswith("Kiro config: acme/payments-api (5 files, jdoe)\n\nAdds the")
    # Every file line is a Markdown list item, so GitLab never joins them into one paragraph.
    listed = [line for line in body.splitlines() if "`" in line and line.startswith("- ")]
    assert len(listed) == 6


def test_global_description_and_fallback() -> None:
    text = proposal.describe(
        project="global", user="jdoe", plan=plan(add=("steering/a.md",)), prefixes=PREFIXES
    )
    assert "Global Kiro configuration (`~/.kiro`), proposed by **jdoe**." in text.body
    assert "Note:" not in text.body
    fallback = proposal.fallback("acme/payments-api", "kiro-config/acme/payments-api/jdoe-1")
    assert fallback.title == "Kiro config: acme/payments-api"
    assert "`kiro-config/acme/payments-api/jdoe-1`" in fallback.body


@pytest.mark.parametrize(
    ("login", "expected"),
    [
        ("jdoe@corp.example.com", "jdoe"),
        ("CORP\\jdoe", "jdoe"),
        ("jdoe", "jdoe"),
        ("J.Doe@example.com", "j-doe"),
    ],
)
def test_domain_logins_give_a_short_user(
    config: Config, login: str, expected: str, tmp_path: Path
) -> None:
    env = {"USER": login, "HOME": str(tmp_path)}
    assert gitops.current_user_id(config, None, env) == expected


def test_pushed_commit_is_the_merge_request_description(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    """GitLab shows the commit message of a one commit merge request as its description."""
    from steersync import sync
    from tests.conftest import git

    steering = project / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "api.md").write_text("# API\n\nREST.\n")
    result = sync.push(config, workspace_root=project, env=env, message="Why: REST rules.")
    assert result.proposal is not None
    assert result.branch
    body = git("log", "-1", "--format=%B", result.branch, cwd=central, env=env)
    assert body.startswith(result.proposal.title + "\n\nWhy: REST rules.")
    assert "| steering | 1 |" in body  # the fixture's central file is deleted too
    assert "- added `api.md`" in body
