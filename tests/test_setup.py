"""steersync setup: checks, careful mcp.json registration, and the guided command."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from steersync import hooks
from steersync import setup as steps
from steersync.cli import main
from steersync.config import load_config
from tests.conftest import git

# A typical user mcp.json: many servers, a nested entry, ${VAR:-default} values.
USER_MCP = Path(__file__).parent / "data" / "user_mcp.json"


# --- addresses and git ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (
            "https://gitlab.com/ssddgreg/kiro-test",
            ["git@gitlab.com:ssddgreg/kiro-test.git", "https://gitlab.com/ssddgreg/kiro-test.git"],
        ),
        (
            "https://gitlab.example.com/platform/sub/kiro-config/-/tree/master?ref_type=heads",
            [
                "git@gitlab.example.com:platform/sub/kiro-config.git",
                "https://gitlab.example.com/platform/sub/kiro-config.git",
            ],
        ),
        (
            "https://gitlab.example.com:8443/a/b.git",
            ["git@gitlab.example.com:a/b.git", "https://gitlab.example.com:8443/a/b.git"],
        ),
        ("git@gitlab.com:ssddgreg/kiro-test.git", ["git@gitlab.com:ssddgreg/kiro-test.git"]),
        ("  'ssh://git@h/a/b.git' ", ["ssh://git@h/a/b.git"]),
        ("", []),
        ("https://gitlab.com/", []),
    ],
)
def test_candidate_urls(typed: str, expected: list[str]) -> None:
    assert [c.url for c in steps.candidate_urls(typed)] == expected


def test_candidate_urls_for_a_local_directory(tmp_path: Path) -> None:
    (candidate,) = steps.candidate_urls(str(tmp_path))
    assert candidate.local
    assert steps.candidate_urls("file:///srv/x.git")[0].local


def test_check_git() -> None:
    assert steps.check_git().ok
    missing = steps.check_git("git-that-does-not-exist")
    assert not missing.ok
    assert "not found" in (missing.problem or "")


def bare(tmp_path: Path, env: dict[str, str], name: str, branch: str | None) -> Path:
    repo = tmp_path / f"{name}.git"
    git("init", "-q", "--bare", str(repo), cwd=tmp_path, env=env)
    if branch is not None:
        git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=repo, env=env)
        work = tmp_path / f"{name}-work"
        git("clone", "-q", str(repo), str(work), cwd=tmp_path, env=env)
        (work / "README.md").write_text("# central\n")
        git("add", "-A", cwd=work, env=env)
        git("commit", "-qm", "init", cwd=work, env=env)
        git("push", "-q", "origin", f"HEAD:{branch}", cwd=work, env=env)
        git("push", "-q", "origin", "HEAD:release", cwd=work, env=env)
    return repo


def test_probe_remote(tmp_path: Path, env: dict[str, str]) -> None:
    config = load_config(env=env).model_copy(
        update={"remote": load_config(env=env).remote.model_copy(update={"allow_local": True})}
    )
    check = steps.probe_remote(config, str(bare(tmp_path, env, "c", "master")), env)
    assert check.reachable
    assert check.head_branch == "master"
    assert set(check.branches) == {"master", "release"}
    assert steps.suggested_branch(check) == "master"
    empty = steps.probe_remote(config, str(bare(tmp_path, env, "e", None)), env)
    assert empty.reachable
    assert empty.branches == ()
    assert steps.suggested_branch(empty) == "master"
    gone = steps.probe_remote(config, str(tmp_path / "missing.git"), env)
    assert not gone.reachable
    assert gone.error


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        ("Host key verification failed.", "ssh -T git@h.example.com"),
        ("git@h.example.com: Permission denied (publickey).", "refused your SSH key"),
        ("fatal: Authentication failed for 'https://h.example.com/a.git'", "credential helper"),
        ("ssh: Could not resolve hostname h.example.com", "could not be resolved"),
        ("ssh: connect to host h.example.com port 22: Operation timed out", "altssh"),
        ("ERROR: The project you were looking for could not be found.", "no access"),
        ("something else", "git ls-remote"),
    ],
)
def test_explain_unreachable(error: str, hint: str) -> None:
    assert hint in steps.explain_unreachable("git@h.example.com:a/b.git", error)


# --- mcp.json: add only when missing, never disturb anything else --------------------------


def test_register_into_the_users_real_mcp_json(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    original = USER_MCP.read_text()
    path.write_text(original)
    entry = hooks.setup_mcp_entry()
    result = hooks.register_mcp_server(path, entry)
    assert (result.action, result.name) == ("added", "steersync")
    assert result.backup is not None
    assert result.backup.read_text() == original
    after = json.loads(path.read_text())
    before = json.loads(original)
    assert list(after["mcpServers"]) == [*before["mcpServers"], "steersync"]
    assert {k: v for k, v in after["mcpServers"].items() if k != "steersync"} == (
        before["mcpServers"]
    )
    # Purely additive: every original line is still there, in order.
    new_lines = path.read_text().splitlines()
    old_lines = original.splitlines()
    assert new_lines[: len(old_lines) - 3] == old_lines[: len(old_lines) - 3]
    assert "STEERSYNC_REMOTE" not in after["mcpServers"]["steersync"]["env"]
    again = hooks.register_mcp_server(path, entry)
    assert again.action == "present"
    assert json.loads(path.read_text()) == after


def test_existing_steersync_under_another_name_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    for spec in (
        {"command": "steersync-mcp"},
        {"command": "/home/me/.local/bin/steersync", "args": ["mcp"]},
        {"command": "uvx", "args": ["--from", "steersync[mcp]==0.5.0", "steersync", "mcp"]},
        {"command": "uvx", "args": ["steersync@latest", "mcp"]},
    ):
        path.write_text(json.dumps({"mcpServers": {"kiro-sync": spec, "other": {"command": "x"}}}))
        before = path.read_text()
        assert hooks.register_mcp_server(path, hooks.setup_mcp_entry()) == hooks.McpRegistration(
            "present", path, "kiro-sync"
        )
        assert path.read_text() == before
    unrelated = {"command": "uvx", "args": ["steersync-docs"]}
    path.write_text(json.dumps({"mcpServers": {"docs": unrelated}}))
    assert hooks.register_mcp_server(path, hooks.setup_mcp_entry()).action == "added"


def test_unexpected_files_are_never_touched(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    for content in ("{ not json", "[1, 2]", '{"mcpServers": []}'):
        path.write_text(content)
        with pytest.raises(ValueError, match=r"unchanged|not valid JSON"):
            hooks.register_mcp_server(path, hooks.setup_mcp_entry())
        assert path.read_text() == content
        assert not (tmp_path / "mcp.json.bak-steersync").exists()


def test_missing_or_empty_file_is_created(tmp_path: Path) -> None:
    path = tmp_path / "settings" / "mcp.json"
    created = hooks.register_mcp_server(path, hooks.setup_mcp_entry())
    assert (created.action, created.backup) == ("added", None)
    assert list(json.loads(path.read_text())["mcpServers"]) == ["steersync"]
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert hooks.register_mcp_server(empty, hooks.setup_mcp_entry()).action == "added"
    dry = tmp_path / "dry.json"
    assert hooks.register_mcp_server(dry, {}, dry_run=True).action == "would-add"
    assert not dry.exists()


# --- configuration and folders ------------------------------------------------------------


def test_updated_user_config_keeps_everything_else(env: dict[str, str]) -> None:
    existing = {
        "remote": {"url": "old", "allowed_hosts": ["mirror.example.com"], "push_options": []},
        "lint": {"practices": False},
    }
    data = steps.updated_user_config(
        existing, url="git@gitlab.com:a/b.git", branch="master", host="gitlab.com", local=False
    )
    assert data["lint"] == {"practices": False}
    assert data["remote"]["push_options"] == []
    assert data["remote"]["allowed_hosts"] == ["mirror.example.com", "gitlab.com"]
    assert data["remote"]["default_branch"] == "master"
    same = steps.updated_user_config(
        data, url="git@gitlab.com:a/b.git", branch="master", host="gitlab.com", local=False
    )
    assert same == data
    with pytest.raises(ValueError, match="default_branch"):
        steps.updated_user_config({}, url="u", branch="", host=None, local=False)


def realistic_kiro(root: Path) -> None:
    """A real world .kiro: notes and plan folders, flat specs, numbered steering, .bak files."""
    kiro = root / ".kiro"
    for folder, name in (
        ("agents", "app-registration.json"),
        ("agents", "app-registration.json.bak"),
        ("notes", "review-feature-5.md"),
        ("plan", "api-scopes.md"),
        ("settings", "lsp.json"),
        ("settings", "lsp.json.bak"),
        ("specs", "remediation-plan.md"),
        ("specs", "optimisation-tasks.md"),
        ("steering", "00-product.md"),
        ("steering", "08a-token-claims-migration-plan.md"),
    ):
        (kiro / folder).mkdir(parents=True, exist_ok=True)
        content = '{"name": "registrar", "tools": ["read"]}' if name.endswith(".json") else "# T\n"
        if name == "lsp.json":
            content = '{"languages": {}}'
        (kiro / folder / name).write_text(content + "\n")
    (kiro / "hooks").mkdir()


def test_unsynced_kiro_folders(tmp_path: Path, env: dict[str, str]) -> None:
    realistic_kiro(tmp_path)
    (tmp_path / ".kiro" / ".cache").mkdir()
    (tmp_path / ".kiro" / ".cache" / "x").write_text("x")
    assert steps.unsynced_kiro_folders(tmp_path, load_config(env=env)) == ["notes", "plan"]
    assert steps.unsynced_kiro_folders(tmp_path / "nowhere", load_config(env=env)) == []


# --- the command ------------------------------------------------------------------------------


@pytest.fixture
def realistic_project(project: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    realistic_kiro(project)
    monkeypatch.chdir(project)
    return project


def kiro_dir(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".kiro"


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A terminal for CliRunner, and a checkbox that ticks every offered step."""
    import questionary

    from steersync import cli_plan, interaction

    real = interaction.resolve_interaction

    def on_a_terminal(**kwargs: Any) -> interaction.Interaction:
        return real(**{**kwargs, "stdin_tty": True, "env": {}})

    monkeypatch.setattr(cli_plan, "resolve_interaction", on_a_terminal)
    offered: list[list[str]] = []

    class Ticked:
        def __init__(self, choices: list[Any]) -> None:
            self.ids = [c.value for c in choices if not c.disabled]

        def ask(self) -> list[str]:
            offered.append(self.ids)
            return self.ids

    monkeypatch.setattr(questionary, "checkbox", lambda message, choices: Ticked(choices))
    return offered


def test_setup_interactive_end_to_end(
    realistic_project: Path, env: dict[str, str], tmp_path: Path, terminal: list[list[str]]
) -> None:
    central = bare(tmp_path, env, "kiro-config", "master")
    mcp = kiro_dir(env) / "settings" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(USER_MCP.read_text())
    # The only question: the repository (a typo first). Then one checkbox for the plan.
    answers = "\n".join([str(tmp_path / "nope.git"), str(central)])
    result = CliRunner().invoke(main, ["setup"], input=answers + "\n")
    assert result.exit_code == 0, result.output
    assert "not reachable" in result.output
    assert len(terminal) == 1
    assert terminal[0] == [
        "config.save",
        "folder:notes",
        "folder:plan",
        "mcp.register",
        "hook.session",
        "repo.exclude",
        "repo.hook",
    ]
    assert "@ master" in result.output
    assert "Undo with: steersync restore " in result.output
    config_file = Path(env["HOME"]) / ".config" / "steersync" / "config.yaml"
    config = load_config(workspace_root=realistic_project, env=env)
    assert config.source_file == config_file
    assert config.remote.url == str(central)
    assert config.remote.default_branch == "master"
    assert {"kiro-notes", "kiro-plan"} <= set(config.collections)
    assert config.setup.last == tuple(terminal[0])
    servers = json.loads(mcp.read_text())["mcpServers"]
    assert list(servers)[-1] == "steersync"
    assert len(servers) == len(json.loads(USER_MCP.read_text())["mcpServers"]) + 1
    assert hooks.session_hook_present(kiro_dir(env) / "hooks")
    # The repository: excludes and pre-push hook, but no duplicate hook or MCP entry.
    assert "managed by steersync" in (realistic_project / ".git" / "hooks" / "pre-push").read_text()
    exclude = (realistic_project / ".git" / "info" / "exclude").read_text()
    assert "/.kiro/notes/" in exclude
    assert not (realistic_project / ".kiro" / "hooks" / "steersync.json").exists()
    assert not (realistic_project / ".kiro" / "settings" / "mcp.json").exists()
    assert "steersync push" in result.output

    again = CliRunner().invoke(main, ["setup"])
    assert again.exit_code == 0, again.output
    assert "Nothing to do" in again.output
    assert len(terminal) == 1  # nothing pending: no checkbox
    assert json.loads(mcp.read_text())["mcpServers"] == servers


def test_setup_undo_restores_every_file(
    realistic_project: Path, env: dict[str, str], tmp_path: Path, terminal: list[list[str]]
) -> None:
    central = bare(tmp_path, env, "kiro-config", "main")
    mcp = kiro_dir(env) / "settings" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(USER_MCP.read_text())
    result = CliRunner().invoke(main, ["setup", "--remote", str(central)])
    assert result.exit_code == 0, result.output
    stamp = result.output.split("Undo with: steersync restore ")[1].split()[0]
    undo = CliRunner().invoke(main, ["restore", stamp, "--yes"])
    assert undo.exit_code == 0, undo.output
    assert mcp.read_text() == USER_MCP.read_text()
    assert not (Path(env["HOME"]) / ".config" / "steersync" / "config.yaml").exists()
    assert not (kiro_dir(env) / "hooks" / "steersync.json").exists()
    assert not (realistic_project / ".git" / "hooks" / "pre-push").exists()


def test_setup_non_interactive(
    realistic_project: Path, env: dict[str, str], tmp_path: Path
) -> None:
    central = bare(tmp_path, env, "kiro-config", "main")
    base = ["setup", "--remote", str(central), "--no-mcp", "--no-session-hook", "--no-project"]
    # Without a terminal and without --yes: the plan, and exit 2 naming the flag.
    asked = CliRunner().invoke(main, base)
    assert asked.exit_code == 2
    assert "Re-run with --yes to apply 1 steps, or --plan to inspect." in asked.output
    planned = CliRunner().invoke(main, [*base, "--plan", "--json"])
    assert planned.exit_code == 0
    assert [s["id"] for s in json.loads(planned.stdout)["steps"]] == ["config.save"]
    result = CliRunner().invoke(main, [*base, "--yes"])
    assert result.exit_code == 0, result.output
    assert load_config(workspace_root=None, env=env).remote.default_branch == "main"
    assert "mcp.register" not in result.output
    assert not (kiro_dir(env) / "settings" / "mcp.json").exists()
    assert not (realistic_project / ".git" / "hooks" / "pre-push").exists()
    assert CliRunner().invoke(main, [*base, "--yes"]).output.strip().endswith("Nothing to do")
    wrong = CliRunner().invoke(
        main, ["setup", "--yes", "--remote", str(central), "--branch", "develop"]
    )
    assert wrong.exit_code == 2
    assert "does not exist" in wrong.output
    no_remote = CliRunner().invoke(main, ["setup", "--yes"], env={"HOME": str(tmp_path / "h")})
    assert no_remote.exit_code == 2
    assert "pass --remote" in no_remote.output


def test_setup_stops_on_an_empty_central_repository(
    realistic_project: Path, env: dict[str, str], tmp_path: Path
) -> None:
    empty = bare(tmp_path, env, "empty", None)
    result = CliRunner().invoke(main, ["setup", "--yes", "--remote", str(empty)])
    assert result.exit_code != 0
    assert "Initialize repository with a README" in result.output
    assert not (Path(env["HOME"]) / ".config" / "steersync" / "config.yaml").exists()


def test_setup_leaves_a_broken_mcp_json_alone(
    realistic_project: Path, env: dict[str, str], tmp_path: Path
) -> None:
    central = bare(tmp_path, env, "kiro-config", "master")
    mcp = kiro_dir(env) / "settings" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text("{ broken")
    foreign = kiro_dir(env) / "hooks" / "steersync.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"version": "v1", "hooks": []}')
    result = CliRunner().invoke(main, ["setup", "--yes", "--remote", str(central), "--no-project"])
    assert result.exit_code == 0, result.output
    assert "left unchanged" in result.output
    assert mcp.read_text() == "{ broken"
    assert foreign.read_text() == '{"version": "v1", "hooks": []}'


def test_setup_without_git(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(
        steps, "check_git", lambda binary="git": steps.GitCheck(None, None, False, "no git")
    )
    result = CliRunner().invoke(main, ["setup", "--yes"])
    assert result.exit_code != 0
    assert "no git" in result.output


def test_setup_plan_for_fresh_partly_and_fully_configured_machines(
    realistic_project: Path, env: dict[str, str], tmp_path: Path
) -> None:
    central = bare(tmp_path, env, "kiro-config", "main")
    base = ["setup", "--remote", str(central), "--plan", "--json"]

    def steps() -> dict[str, bool]:
        result = CliRunner().invoke(main, base)
        assert result.exit_code == 0, result.output
        return {s["id"]: s["done"] for s in json.loads(result.stdout)["steps"]}

    fresh = steps()
    assert not any(fresh.values())
    assert set(fresh) >= {"config.save", "mcp.register", "hook.session", "repo.exclude"}
    # Partly configured: everything but the MCP server.
    assert (
        CliRunner().invoke(main, ["setup", "--remote", str(central), "--yes", "--no-mcp"]).exit_code
        == 0
    )
    partly = steps()
    assert partly["mcp.register"] is False
    # Extra .kiro folders are off by default, so they stay offered until chosen.
    rest = {k: v for k, v in partly.items() if k != "mcp.register" and not k.startswith("folder")}
    assert all(rest.values())
    assert CliRunner().invoke(main, ["setup", "--yes", "--only", "mcp"]).exit_code == 0
    full = steps()
    assert all(done for step_id, done in full.items() if not step_id.startswith("folder"))
