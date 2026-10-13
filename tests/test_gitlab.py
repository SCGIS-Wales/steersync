"""GitLab merge request client, exercised against a fake urlopen."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from steersync import gitlab
from steersync.config import Config, load_config
from steersync.errors import ConfigError, GitLabError

CENTRAL = "ssh://git@gitlab.example.com/platform/kiro-config.git"


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    """Record requests and answer them from a queue of payloads."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: int = 0) -> FakeResponse:
        del timeout
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode("utf-8") if answer is not None else b"")


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> FakeOpener:
    fake = FakeOpener([])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def test_resolve_client_derives_everything(config: Config) -> None:
    client = gitlab.resolve_client(config, {"GITLAB_TOKEN": "glpat-x"}, CENTRAL)
    assert client.api_url == "https://gitlab.example.com/api/v4"
    assert client.project == "platform/kiro-config"
    assert client.project_id == "platform%2Fkiro-config"
    assert client.target_branch == "main"
    assert client.token == "glpat-x"
    assert not gitlab.token_available(config, {})
    assert not gitlab.token_available(config, {"GITLAB_TOKEN": "  "})
    assert gitlab.token_available(config, {"GITLAB_TOKEN": "t"})


def test_resolve_client_honours_configuration(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "gitlab:\n  token_env: CI_JOB_TOKEN\n  api_url: https://gl.internal/api/v4/\n"
        "  project: group/sub/central\n  target_branch: master\n"
    )
    config = load_config(env=env, explicit=path)
    with pytest.raises(GitLabError, match="CI_JOB_TOKEN"):
        gitlab.resolve_client(config, {"GITLAB_TOKEN": "ignored"}, CENTRAL)
    client = gitlab.resolve_client(config, {"CI_JOB_TOKEN": "t"}, CENTRAL)
    assert client.api_url == "https://gl.internal/api/v4"
    assert client.project == "group/sub/central"
    assert client.target_branch == "master"
    override = load_config(
        env={
            **env,
            "STEERSYNC_GITLAB_API_URL": "https://other/api/v4",
            "STEERSYNC_GITLAB_PROJECT": "a/b",
        },
        explicit=path,
    )
    assert override.gitlab.api_url == "https://other/api/v4"
    assert override.gitlab.project == "a/b"


def test_resolve_client_needs_api_url_for_local_remotes(config: Config) -> None:
    with pytest.raises(ConfigError, match="api_url"):
        gitlab.resolve_client(config, {"GITLAB_TOKEN": "t"}, "/srv/git/central.git")


@pytest.mark.parametrize(
    "text",
    [
        "gitlab:\n  api_url: http://insecure/api/v4\n",
        "gitlab:\n  project: ../escape\n",
        "gitlab:\n  token_env: lower\n",
        "gitlab:\n  timeout_seconds: 1\n",
    ],
)
def test_invalid_gitlab_configuration(env: dict[str, str], tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(env=env, explicit=path)


def test_ensure_merge_request_creates_once(config: Config, opener: FakeOpener) -> None:
    client = gitlab.resolve_client(config, {"GITLAB_TOKEN": "t"}, CENTRAL)
    created = {
        "iid": 7,
        "web_url": "https://gitlab.example.com/platform/kiro-config/-/merge_requests/7",
        "source_branch": "kiro-config/acme/api/dejan-abc",
        "target_branch": "main",
        "title": "kiro-config: update acme/api",
        "state": "opened",
    }
    opener.answers.extend([[], created, [created]])
    mr, was_created = gitlab.ensure_merge_request(
        client, config, source_branch="kiro-config/acme/api/dejan-abc", title="t", description="d"
    )
    assert was_created
    assert mr.iid == 7
    assert mr.url.endswith("/merge_requests/7")
    listing, post = opener.requests[:2]
    assert listing.get_method() == "GET"
    assert "state=opened" in listing.full_url
    assert "target_branch=main" in listing.full_url
    assert listing.get_header("Private-token") == "t"
    assert post.get_method() == "POST"
    assert isinstance(post.data, bytes)
    body = json.loads(post.data.decode("utf-8"))
    assert body["source_branch"] == "kiro-config/acme/api/dejan-abc"
    assert body["remove_source_branch"] is True
    assert body["labels"] == "kiro-config"
    again, was_created = gitlab.ensure_merge_request(
        client, config, source_branch="kiro-config/acme/api/dejan-abc", title="t"
    )
    assert not was_created
    assert again.iid == 7


def test_open_merge_requests_filters_and_tolerates_bad_payloads(
    config: Config, opener: FakeOpener
) -> None:
    client = gitlab.resolve_client(config, {"GITLAB_TOKEN": "t"}, CENTRAL)
    opener.answers.extend(
        [
            [
                {"iid": 1, "source_branch": "kiro-config/acme/api/a-1", "web_url": "u1"},
                {"iid": 2, "source_branch": "other", "web_url": "u2"},
                "garbage",
            ],
            {"not": "a list"},
        ]
    )
    found = gitlab.open_merge_requests(client, source_branches=["kiro-config/acme/api/a-1"])
    assert [mr.iid for mr in found] == [1]
    assert gitlab.open_merge_requests(client) == []


def test_api_errors_are_reported(config: Config, opener: FakeOpener) -> None:
    client = gitlab.resolve_client(config, {"GITLAB_TOKEN": "t"}, CENTRAL)
    opener.answers.append(
        urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b'{"message":"401"}'))  # type: ignore[arg-type]
    )
    with pytest.raises(GitLabError, match="HTTP 401"):
        gitlab.find_open_merge_request(client, "b")
    opener.answers.append(urllib.error.URLError("name or service not known"))
    with pytest.raises(GitLabError, match="unreachable"):
        gitlab.find_open_merge_request(client, "b")
    opener.answers.append(TimeoutError())
    with pytest.raises(GitLabError, match="timed out"):
        gitlab.find_open_merge_request(client, "b")
    opener.answers.append(None)
    assert gitlab.open_merge_requests(client) == []
    opener.answers.append({"no": "iid"})
    with pytest.raises(GitLabError, match="did not return"):
        gitlab.create_merge_request(client, config, source_branch="b", title="t")


def test_invalid_json_is_reported(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab.resolve_client(config, {"GITLAB_TOKEN": "t"}, CENTRAL)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=0: FakeResponse(b"<html>")
    )
    with pytest.raises(GitLabError, match="invalid JSON"):
        gitlab.open_merge_requests(client)
