"""Merge requests on the central repository through the GitLab REST API.

Everything here is optional: GitLab push options already open a merge request
when a proposal branch is pushed. The API is used to report the merge request
URL, to open one when the server did not accept push options, and to list the
open proposals of a project so a developer sees unmerged work before adding
more. Only the standard library is used, the token comes from an environment
variable named in configuration, and it never reaches a log line.
"""

import email.utils
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from steersync.config import Config, remote_host
from steersync.errors import ConfigError, GitLabError
from steersync.gitops import project_path_from_url
from steersync.retry import (
    RetryDecision,
    RetryEvent,
    RetryExhaustedError,
    RetryPolicy,
    call_with_retry,
)

log = logging.getLogger(__name__)

USER_AGENT = "steersync"


Opener = Callable[..., Any]


def _log_retry(event: RetryEvent) -> None:
    log.warning("%s", event.render())


class GitLabClient(NamedTuple):
    """Resolved connection details for the central repository's GitLab project.

    The transport pieces (opener, sleep, clock, retry observer) are fields so tests and the
    fleet UI can supply their own.
    """

    api_url: str
    project: str
    token: str
    target_branch: str
    timeout: int
    token_env: str = "GITLAB_TOKEN"  # noqa: S105 - the variable name, not a token
    policy: RetryPolicy = RetryPolicy(attempts=4, base_delay=1.0, max_delay=30.0)
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})
    retry_not_found: bool = True
    opener: Opener | None = None  # urllib.request.urlopen, looked up at call time
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    on_retry: Callable[[RetryEvent], None] = _log_retry

    @property
    def project_id(self) -> str:
        return urllib.parse.quote(self.project, safe="")


class MergeRequest(NamedTuple):
    iid: int
    url: str
    source_branch: str
    target_branch: str
    title: str
    state: str


def token_available(config: Config, env: Mapping[str, str]) -> bool:
    """True when the configured token variable is set and not empty."""
    return bool(env.get(config.gitlab.token_env, "").strip())


def resolve_client(
    config: Config,
    env: Mapping[str, str],
    remote_url: str,
    *,
    on_retry: Callable[[RetryEvent], None] | None = None,
) -> GitLabClient:
    """Build the API client from configuration, environment and the central remote URL."""
    token = env.get(config.gitlab.token_env, "").strip()
    if not token:
        msg = (
            f"no GitLab token: export {config.gitlab.token_env} with the api scope "
            "(the variable name is configurable with gitlab.token_env)"
        )
        raise GitLabError(msg)
    api_url = config.gitlab.api_url
    if not api_url:
        host = remote_host(remote_url)
        if not host:
            msg = "gitlab.api_url must be set when remote.url is a local path"
            raise ConfigError(msg)
        api_url = f"https://{host}/api/v4"
    project = config.gitlab.project or project_path_from_url(remote_url)
    target = config.gitlab.target_branch or config.remote.default_branch
    settings = config.gitlab
    return GitLabClient(
        api_url,
        project,
        token,
        target,
        settings.timeout_seconds,
        token_env=settings.token_env,
        policy=RetryPolicy(
            attempts=settings.retries + 1,
            base_delay=settings.retry_backoff_seconds,
            max_delay=settings.retry_max_backoff_seconds,
        ),
        retry_statuses=frozenset(settings.retry_statuses),
        retry_not_found=settings.retry_not_found,
        on_retry=on_retry or _log_retry,
    )


class HttpAttemptError(Exception):
    """One failed API attempt: an HTTP status, or ``None`` for a transport failure."""

    def __init__(self, status: int | None, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta seconds or an HTTP date)."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except TypeError, ValueError:
        return None
    current = now or datetime.now(UTC)
    return max(0.0, (when - current).total_seconds())


def _send(
    client: GitLabClient,
    method: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Any:  # noqa: ANN401 - JSON payloads are untyped by nature
    """One API call; failures raise :class:`HttpAttemptError`."""
    url = f"{client.api_url}/projects/{client.project_id}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310 # nosec B310 - https only, see config
        url,
        data=data,
        method=method,
        headers={
            "PRIVATE-TOKEN": client.token,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        opener = client.opener or urllib.request.urlopen
        with opener(request, timeout=client.timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        finally:
            exc.close()
        retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        raise HttpAttemptError(exc.code, detail or str(exc.reason), retry_after) from exc
    except urllib.error.URLError as exc:
        raise HttpAttemptError(None, f"unreachable: {exc.reason}") from exc
    except (TimeoutError, ConnectionError) as exc:
        raise HttpAttemptError(None, f"{type(exc).__name__}: {exc}") from exc
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        msg = f"GitLab API {method} {path} returned invalid JSON"
        raise GitLabError(msg) from exc


def _retry_rule(
    client: GitLabClient, method: str
) -> Callable[[BaseException, int], RetryDecision | None]:
    """Which failures to retry for ``method`` (see the resilience table in the docs)."""

    def decide(exc: BaseException, attempt: int) -> RetryDecision | None:
        del attempt
        if not isinstance(exc, HttpAttemptError):
            return None
        status = exc.status
        if status is None:
            return RetryDecision(exc.detail)
        if status in client.retry_statuses:
            return RetryDecision(f"GitLab {status}", delay=exc.retry_after)
        if status == 500 and method == "GET":
            return RetryDecision("GitLab 500")
        if status == 404 and method == "GET" and client.retry_not_found:
            return RetryDecision("GitLab 404", max_attempts=3)
        return None

    return decide


def _as_error(client: GitLabClient, method: str, path: str, failure: HttpAttemptError) -> str:
    what = f"GitLab API {method} {path}"
    if failure.status in {401, 403}:
        return (
            f"{what} was refused with HTTP {failure.status}: check that "
            f"{client.token_env} holds a token with the api scope and access to "
            f"{client.project} ({failure.detail})"
        )
    if failure.status == 404:
        return (
            f"{what} answered HTTP 404. Either the project path {client.project!r} is wrong "
            f"(set gitlab.project), the token in {client.token_env} lacks the api scope or "
            "access to the project, or the object does not exist"
        )
    if failure.status is None:
        return f"{what} failed: {failure.detail}"
    return f"{what} failed with HTTP {failure.status}: {failure.detail}"


def _request(
    client: GitLabClient,
    method: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Any:  # noqa: ANN401 - JSON payloads are untyped by nature
    """An API call with the retry policy for ``method``; failures raise GitLabError."""
    try:
        return call_with_retry(
            lambda: _send(client, method, path, query=query, body=body),
            client.policy,
            operation=f"GitLab API {method} {path}",
            should_retry=_retry_rule(client, method),
            on_retry=client.on_retry,
            sleep=client.sleep,
            clock=client.clock,
        )
    except RetryExhaustedError as exc:
        last = exc.last
        status = (
            f"HTTP {last.status}"
            if isinstance(last, HttpAttemptError) and last.status
            else str(last)
        )
        msg = (
            f"GitLab API {method} {path} failed with {status} after {exc.attempts} "
            f"attempts over {exc.elapsed:.0f}s"
        )
        if isinstance(last, HttpAttemptError) and last.status == 404:
            msg = _as_error(client, method, path, last) + f" ({exc.attempts} attempts)"
        raise GitLabError(msg) from exc
    except HttpAttemptError as exc:
        raise GitLabError(_as_error(client, method, path, exc)) from exc


def _merge_request(item: Mapping[str, Any]) -> MergeRequest:
    return MergeRequest(
        int(item["iid"]),
        str(item.get("web_url", "")),
        str(item.get("source_branch", "")),
        str(item.get("target_branch", "")),
        str(item.get("title", "")),
        str(item.get("state", "")),
    )


def open_merge_requests(
    client: GitLabClient, *, source_branches: Sequence[str] | None = None
) -> list[MergeRequest]:
    """Return the open merge requests, optionally only those from ``source_branches``."""
    payload = _request(
        client,
        "GET",
        "merge_requests",
        query={"state": "opened", "target_branch": client.target_branch, "per_page": "100"},
    )
    if not isinstance(payload, list):
        return []
    found = [_merge_request(item) for item in payload if isinstance(item, Mapping)]
    if source_branches is not None:
        wanted = set(source_branches)
        found = [mr for mr in found if mr.source_branch in wanted]
    return found


def find_open_merge_request(client: GitLabClient, source_branch: str) -> MergeRequest | None:
    """Return the open merge request for ``source_branch``, if there is one."""
    found = open_merge_requests(client, source_branches=[source_branch])
    return found[0] if found else None


def create_merge_request(
    client: GitLabClient,
    config: Config,
    *,
    source_branch: str,
    title: str,
    description: str = "",
) -> MergeRequest:
    """Create a merge request from ``source_branch`` into the target branch."""
    body: dict[str, Any] = {
        "source_branch": source_branch,
        "target_branch": client.target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": config.gitlab.remove_source_branch,
        "squash": config.gitlab.squash,
    }
    if config.gitlab.labels:
        body["labels"] = ",".join(config.gitlab.labels)
    attempts = 0

    def create_once() -> Any:  # noqa: ANN401 - JSON payload
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            # The previous attempt may have created it before the connection failed.
            existing = find_open_merge_request(client, source_branch)
            if existing is not None:
                return existing
        return _send(client, "POST", "merge_requests", body=body)

    rule = _retry_rule(client, "POST")
    try:
        payload = call_with_retry(
            create_once,
            client.policy,
            operation="GitLab API POST merge_requests",
            should_retry=rule,
            on_retry=client.on_retry,
            sleep=client.sleep,
            clock=client.clock,
        )
    except RetryExhaustedError as exc:
        msg = f"GitLab API POST merge_requests failed after {exc.attempts} attempts: {exc.last}"
        raise GitLabError(msg) from exc
    except HttpAttemptError as exc:
        raise GitLabError(_as_error(client, "POST", "merge_requests", exc)) from exc
    if isinstance(payload, MergeRequest):
        return payload
    if not isinstance(payload, Mapping) or "iid" not in payload:
        msg = "GitLab API did not return the created merge request"
        raise GitLabError(msg)
    return _merge_request(payload)


def ensure_merge_request(
    client: GitLabClient,
    config: Config,
    *,
    source_branch: str,
    title: str,
    description: str = "",
) -> tuple[MergeRequest, bool]:
    """Return the open merge request for the branch, creating it when missing.

    The second element is ``True`` when the merge request was created by this call.
    """
    existing = find_open_merge_request(client, source_branch)
    if existing is not None:
        return existing, False
    return (
        create_merge_request(
            client, config, source_branch=source_branch, title=title, description=description
        ),
        True,
    )
