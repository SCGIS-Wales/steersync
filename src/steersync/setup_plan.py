"""Detect what setup and init would change, and build it as one plan.

``detect_setup_state`` gathers everything first (configuration, the central repository
check, ``.kiro`` folders, the MCP registration, hooks, the repository's excludes and git
hook). ``build_setup_plan`` turns that state into steps; a step whose desired state
already holds is marked done, so running setup again on a configured machine has nothing
to do. The command shows the plan once and asks once (:mod:`steersync.plan`).

Scopes: ``machine`` (setup outside a repository, or ``--no-project``), ``all`` (setup in a
repository) and ``project`` (init).
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple

from steersync import gitops, hooks, registry
from steersync import setup as setup_steps
from steersync.config import Config, default_config_text
from steersync.paths import kiro_home
from steersync.plan import Plan, Risk, Step, StepResult

Scope = Literal["machine", "all", "project"]


class RepoState(NamedTuple):
    root: Path
    exclude_file: Path
    missing_excludes: tuple[str, ...]
    hooks_dir: Path
    hook_installed: bool
    hook_note: str | None  # an older steersync hook or a chained file that blocks install


class SetupState(NamedTuple):
    env: Mapping[str, str]
    current: Config
    config_path: Path
    existing: dict[str, Any]
    # None for init, which does not touch the machine configuration.
    remote: setup_steps.RemoteCheck | None
    local: bool
    branch: str | None
    root: Path | None
    folders: tuple[str, ...]
    home: Path
    mcp_path: Path
    mcp_present: str | None
    mcp_problem: str | None
    hooks_dir: Path
    hook_present: bool
    hook_conflict: bool
    repo: RepoState | None


def exclude_patterns(config: Config) -> list[str]:
    return list(
        dict.fromkeys(
            f"/{c.workspace_path.strip('/')}/"
            for c in config.enabled_collections().values()
            if c.workspace_path is not None and c.git_exclude
        )
    )


def detect_repo(config: Config, root: Path, env: Mapping[str, str]) -> RepoState:
    git_env = gitops.git_environment(env)
    exclude = gitops.git_dir(config, root, env=git_env) / "info" / "exclude"
    present = exclude.read_text("utf-8").splitlines() if exclude.is_file() else []
    missing = tuple(p for p in exclude_patterns(config) if p not in present)
    hooks_dir = gitops.hooks_dir(config, root, env=git_env)
    target = hooks_dir / "pre-push"
    note: str | None = None
    installed = False
    if target.is_file():
        text = target.read_text("utf-8", errors="replace")
        if hooks.HOOK_MARKER in text:
            installed = text == hooks.render_pre_push_hook()
            if not installed:
                note = f"{target} is an older steersync hook; `steersync install-hook --force`"
        elif (hooks_dir / f"pre-push{hooks.CHAINED_SUFFIX}").exists():
            note = f"{target} and pre-push{hooks.CHAINED_SUFFIX} exist; resolve them by hand"
    return RepoState(root, exclude, missing, hooks_dir, installed, note)


def detect_setup_state(
    env: Mapping[str, str],
    *,
    current: Config,
    root: Path | None,
    remote: setup_steps.RemoteCheck | None,
    local: bool = False,
    branch: str | None = None,
) -> SetupState:
    """Everything setup or init needs to decide, read without changing anything."""
    config_path = setup_steps.user_config_path(env)
    home = kiro_home(env)
    mcp_path = home / "settings" / "mcp.json"
    try:
        mcp_present = hooks.find_steersync_server(mcp_path)
        problem = None
    except ValueError as exc:
        mcp_present, problem = None, str(exc)
    if mcp_present is None and problem is None and mcp_path.is_file():
        try:
            hooks.register_mcp_server(mcp_path, hooks.setup_mcp_entry(), dry_run=True)
        except ValueError as exc:
            problem = str(exc)
    hooks_dir = home / "hooks"
    hook_present = hooks.session_hook_present(hooks_dir)
    return SetupState(
        env=env,
        current=current,
        config_path=config_path,
        existing=setup_steps.load_yaml_file(config_path),
        remote=remote,
        local=local,
        branch=branch,
        root=root,
        folders=tuple(setup_steps.unsynced_kiro_folders(root, current)) if root else (),
        home=home,
        mcp_path=mcp_path,
        mcp_present=mcp_present,
        mcp_problem=problem,
        hooks_dir=hooks_dir,
        hook_present=hook_present,
        hook_conflict=not hook_present and (hooks_dir / hooks.KIRO_HOOK_FILE).exists(),
        repo=detect_repo(current, root, env) if root is not None else None,
    )


def _step(
    step_id: str,
    title: str,
    *,
    detail: str,
    target: Path | str,
    risk: Risk,
    default: bool,
    done: bool,
    apply: Callable[[], StepResult],
    paths: tuple[Path, ...] = (),
) -> Step:
    return Step(step_id, title, detail, str(target), risk, default, done, apply, paths)


def config_data(state: SetupState) -> dict[str, Any]:
    """The user configuration setup saves: the existing file with the remote set."""
    assert state.remote is not None  # noqa: S101 - only machine scopes save configuration
    host = None if state.local else setup_steps.gitops_host(state.remote.url)
    return setup_steps.updated_user_config(
        state.existing,
        url=state.remote.url,
        branch=state.branch or setup_steps.suggested_branch(state.remote),
        host=host,
        local=state.local,
    )


def _remote_label(url: str, data: Mapping[str, Any]) -> str:
    return f"{gitops.redact_url(url)} @ {data['remote']['default_branch']}"


def _machine_steps(state: SetupState, scope: Scope, mcp_command: str) -> list[Step]:
    del mcp_command  # setup registers the uvx entry; init --mcp-command is per repository
    steps: list[Step] = []
    if state.remote is not None:
        data = config_data(state)

        def save() -> StepResult:
            backup = setup_steps.write_user_config(state.config_path, data)
            note = f" (previous: {backup.name})" if backup else ""
            return StepResult("config.save", True, f"written {state.config_path}{note}")

        steps.append(
            _step(
                "config.save",
                "Save the central repository",
                detail=_remote_label(state.remote.url, data),
                target=state.config_path,
                risk="safe",
                default=True,
                done=data == state.existing,
                apply=save,
                paths=(state.config_path,),
            )
        )
    if scope == "all":  # extra .kiro folders belong to the repository setup runs in
        steps.extend(_folder_step(state, folder) for folder in state.folders)
    if state.mcp_problem is None:

        def register() -> StepResult:
            reg = hooks.register_mcp_server(state.mcp_path, hooks.setup_mcp_entry())
            return StepResult("mcp.register", True, f"added {reg.name!r} to {state.mcp_path}")

        steps.append(
            _step(
                "mcp.register",
                "Register the steersync MCP server",
                detail="other servers stay unchanged",
                target=state.mcp_path,
                risk="review",
                default=True,
                done=state.mcp_present is not None,
                apply=register,
                paths=(state.mcp_path,),
            )
        )
    if not state.hook_conflict:
        target = state.hooks_dir / hooks.KIRO_HOOK_FILE

        def write_hook() -> StepResult:
            hooks.write_kiro_hook(state.hooks_dir)
            return StepResult("hook.session", True, f"written {target}")

        steps.append(
            _step(
                "hook.session",
                "Pull when a Kiro session starts",
                detail="global SessionStart hook",
                target=target,
                risk="safe",
                default=True,
                done=state.hook_present,
                apply=write_hook,
                paths=(target,),
            )
        )
    return steps


def _folder_step(state: SetupState, folder: str) -> Step:
    name = f"kiro-{folder}"

    repo = state.repo

    def add() -> StepResult:
        data = setup_steps.load_yaml_file(state.config_path)
        collections = dict(data.get("collections") or {})
        collections.setdefault(name, setup_steps.extra_collection(folder))
        setup_steps.write_user_config(state.config_path, {**data, "collections": collections})
        if repo is not None:  # keep the newly synced folder out of git status too
            env = gitops.git_environment(state.env)
            gitops.write_info_exclude(state.current, repo.root, f"/.kiro/{folder}/", env=env)
        return StepResult(f"folder:{folder}", True, f"syncing .kiro/{folder} as {name!r}")

    paths = (state.config_path,) + ((repo.exclude_file,) if repo is not None else ())
    return _step(
        f"folder:{folder}",
        f"Sync .kiro/{folder}",
        detail="not synced by default",
        target=state.config_path,
        risk="safe",
        default=False,
        done=False,
        apply=add,
        paths=paths,
    )


def _repo_steps(state: SetupState, *, exclude: bool, hook: bool) -> list[Step]:
    repo = state.repo
    if repo is None:
        return []
    config, env = state.current, state.env

    def add_excludes() -> StepResult:
        for pattern in repo.missing_excludes:
            gitops.write_info_exclude(config, repo.root, pattern, env=gitops.git_environment(env))
        registry.register(env, repo.root, "init")
        count = len(repo.missing_excludes)
        return StepResult("repo.exclude", True, f"{count} folder(s) added to {repo.exclude_file}")

    def install() -> StepResult:
        result = hooks.install_pre_push_hook(config, repo.root, env=gitops.git_environment(env))
        registry.register(env, repo.root, "init")
        chained = f"; existing hook kept as {result.chained.name}" if result.chained else ""
        return StepResult("repo.hook", True, f"{result.action} {result.path}{chained}")

    steps = [
        _step(
            "repo.exclude",
            "Keep synced folders out of git status",
            detail=", ".join(repo.missing_excludes) or "all excluded",
            target=repo.exclude_file,
            risk="review",
            default=exclude,
            done=not repo.missing_excludes,
            apply=add_excludes,
            paths=(repo.exclude_file,),
        )
    ]
    if repo.hook_note is None:
        target = repo.hooks_dir / "pre-push"
        steps.append(
            _step(
                "repo.hook",
                "Propose Kiro changes on git push",
                detail="pre-push hook; an existing hook is kept and chained",
                target=target,
                risk="review",
                default=hook,
                done=repo.hook_installed,
                apply=install,
                paths=(target, repo.hooks_dir / f"pre-push{hooks.CHAINED_SUFFIX}"),
            )
        )
    return steps


def _project_steps(
    state: SetupState, *, write_config: bool, kiro: bool, mcp_command: str
) -> list[Step]:
    repo = state.repo
    if repo is None:
        return []
    config = state.current
    project_file = repo.root / ".kiro" / "steersync.yaml"

    def write_project() -> StepResult:
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text(default_config_text(), encoding="utf-8")
        return StepResult("project.config", True, f"written {project_file}")

    hooks_dir = repo.root / (_workspace(config, "hooks") or ".kiro/hooks")
    hook_file = hooks_dir / hooks.KIRO_HOOK_FILE
    mcp_file = repo.root / (_workspace(config, "settings") or ".kiro/settings") / "mcp.json"

    def write_hook() -> StepResult:
        action = hooks.write_kiro_hook(hooks_dir)
        return StepResult("kiro.hook", True, f"{action} {hook_file}")

    def register() -> StepResult:
        entry = hooks.mcp_server_entry(command=mcp_command, token_env=config.gitlab.token_env)
        reg = hooks.register_mcp_server(mcp_file, entry)
        return StepResult("kiro.mcp", True, f"{reg.action} {reg.name!r} in {mcp_file}")

    return [
        _step(
            "project.config",
            "Write .kiro/steersync.yaml",
            detail="the packaged defaults, for teams that commit configuration",
            target=project_file,
            risk="safe",
            default=write_config,
            done=project_file.exists(),
            apply=write_project,
            paths=(project_file,),
        ),
        _step(
            "kiro.hook",
            "Repository session hook",
            detail="only for teams that commit it; setup adds one to ~/.kiro",
            target=hook_file,
            risk="safe",
            default=kiro,
            done=state.hook_present or hook_file.is_file(),
            apply=write_hook,
            paths=(hook_file,),
        ),
        _step(
            "kiro.mcp",
            "Repository MCP server entry",
            detail="only for teams that commit it; setup adds one to ~/.kiro",
            target=mcp_file,
            risk="review",
            default=kiro,
            done=state.mcp_present is not None or hooks.find_steersync_server(mcp_file) is not None,
            apply=register,
            paths=(mcp_file,),
        ),
    ]


def _workspace(config: Config, name: str) -> str | None:
    collection = config.collections.get(name)
    return collection.workspace_path if collection is not None else None


def build_setup_plan(
    state: SetupState,
    scope: Scope,
    *,
    exclude: bool = True,
    hook: bool = True,
    write_config: bool = False,
    kiro: bool = False,
    mcp_command: str = "uvx",
) -> Plan:
    """The steps for ``scope``; the keyword defaults decide which steps are preselected."""
    steps: list[Step] = []
    if scope in {"machine", "all"}:
        steps += _machine_steps(state, scope, mcp_command)
    if scope in {"all", "project"}:
        steps += _repo_steps(state, exclude=exclude, hook=hook)
    if scope == "project":
        steps += _project_steps(
            state, write_config=write_config, kiro=kiro, mcp_command=mcp_command
        )
    return Plan("init" if scope == "project" else "setup", tuple(steps))


def notes(state: SetupState) -> list[str]:
    """What setup leaves alone and why, printed under the plan."""
    found: list[str] = []
    if state.mcp_problem is not None:
        found.append(f"left unchanged: {state.mcp_path}: {state.mcp_problem}")
    if state.hook_conflict:
        found.append(
            f"left unchanged: {state.hooks_dir / hooks.KIRO_HOOK_FILE} exists and is not a "
            "steersync hook"
        )
    if state.repo is not None and state.repo.hook_note:
        found.append(f"left unchanged: {state.repo.hook_note}")
    if state.root is not None and (project := setup_steps.project_config_path(state.root)):
        found.append(f"note: {project} also sets the remote and wins inside this repository")
    return found
