"""Merge requests on the central repository through the GitLab REST API.

Everything here is optional: GitLab push options already open a merge request
when a proposal branch is pushed. The API is used to report the merge request
URL, to open one when the server did not accept push options, and to list the
open proposals of a project so a developer sees unmerged work before adding
more. Only the standard library is used, the token comes from an environment
variable named in configuration, and it never reaches a log line.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from steersync.config import Config, remote_host
from steersync.errors import ConfigError, GitLabError
from steersync.gitops import project_path_from_url

log = logging.getLogger(__name__)

USER_AGENT = "steersync"


class GitLabClient(NamedTuple):
    """Resolved connection details for the central repository's GitLab project."""

    api_url: str
    project: str
    token: str
    target_branch: str
    timeout: int

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


def resolve_client(config: Config, env: Mapping[str, str], remote_url: str) -> GitLabClient:
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
    return GitLabClient(api_url, project, token, target, config.gitlab.timeout_seconds)


def _request(
    client: GitLabClient,
    method: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Any:  # noqa: ANN401 - JSON payloads are untyped by nature
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
        with urllib.request.urlopen(  # noqa: S310 # nosec B310 - https only, see config
            request, timeout=client.timeout
        ) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        finally:
            exc.close()
        msg = f"GitLab API {method} {path} failed with HTTP {exc.code}: {detail or exc.reason}"
        raise GitLabError(msg) from exc
    except urllib.error.URLError as exc:
        msg = f"GitLab API {method} {path} unreachable: {exc.reason}"
        raise GitLabError(msg) from exc
    except TimeoutError as exc:
        msg = f"GitLab API {method} {path} timed out after {client.timeout}s"
        raise GitLabError(msg) from exc
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        msg = f"GitLab API {method} {path} returned invalid JSON"
        raise GitLabError(msg) from exc


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
    payload = _request(client, "POST", "merge_requests", body=body)
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
