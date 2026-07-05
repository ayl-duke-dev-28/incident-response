"""GitHub adapter: pulls recent commits touching a service.

Mock mode returns deterministic fixtures so the system runs offline and in tests.
Real mode calls the GitHub REST API via httpx.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from ..models import Commit
from ..retry import async_retry

_RETRYABLE_HTTP = (httpx.HTTPError, TimeoutError)


class GitHubClient(ABC):
    @abstractmethod
    async def recent_commits(self, service: str, since: datetime, limit: int = 20) -> list[Commit]:
        ...

    @abstractmethod
    async def annotate_pr(self, pr_number: int, body: str) -> None:
        ...


class MockGitHubClient(GitHubClient):
    """Returns a small hand-crafted set of recent commits for local dev + tests."""

    def __init__(self, commits: list[Commit] | None = None) -> None:
        self._commits = commits if commits is not None else _default_fixture()
        self.annotations: list[tuple[int, str]] = []

    async def recent_commits(self, service: str, since: datetime, limit: int = 20) -> list[Commit]:
        matched = [c for c in self._commits if c.timestamp >= since]
        matched.sort(key=lambda c: c.timestamp, reverse=True)
        return matched[:limit]

    async def annotate_pr(self, pr_number: int, body: str) -> None:
        self.annotations.append((pr_number, body))


class RestGitHubClient(GitHubClient):
    def __init__(self, token: str, repo: str) -> None:
        self._token = token
        self._repo = repo

    @async_retry(attempts=3, base_delay=0.5, retry_on=_RETRYABLE_HTTP)
    async def recent_commits(self, service: str, since: datetime, limit: int = 20) -> list[Commit]:
        url = f"https://api.github.com/repos/{self._repo}/commits"
        params = {"since": since.isoformat(), "per_page": str(limit)}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        commits: list[Commit] = []
        for item in payload:
            sha = item["sha"]
            detail_url = f"https://api.github.com/repos/{self._repo}/commits/{sha}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                detail = (await client.get(detail_url, headers=headers)).json()
            files = [f["filename"] for f in detail.get("files", [])]
            stats = detail.get("stats", {})
            commit_info = item["commit"]
            commits.append(
                Commit(
                    sha=sha,
                    author=commit_info["author"]["name"],
                    message=commit_info["message"],
                    timestamp=datetime.fromisoformat(
                        commit_info["author"]["date"].replace("Z", "+00:00")
                    ),
                    files_changed=files,
                    additions=stats.get("additions", 0),
                    deletions=stats.get("deletions", 0),
                )
            )
        return commits

    @async_retry(attempts=3, base_delay=0.5, retry_on=_RETRYABLE_HTTP)
    async def annotate_pr(self, pr_number: int, body: str) -> None:
        # GitHub uses the "issue comments" endpoint for PR-level comments.
        url = f"https://api.github.com/repos/{self._repo}/issues/{pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json={"body": body}, headers=headers)
            resp.raise_for_status()


def _default_fixture() -> list[Commit]:
    now = datetime.now(timezone.utc)
    return [
        Commit(
            sha="a1b2c3d",
            author="jamie",
            message="perf: switch checkout to new pricing cache",
            timestamp=now - timedelta(minutes=8),
            files_changed=["services/checkout/pricing.py", "services/checkout/cache.py"],
            additions=142,
            deletions=37,
            pr_number=4821,
            pr_url="https://github.com/example/repo/pull/4821",
        ),
        Commit(
            sha="e4f5g6h",
            author="priya",
            message="chore: bump redis-py to 5.1",
            timestamp=now - timedelta(minutes=42),
            files_changed=["requirements.txt"],
            additions=1,
            deletions=1,
            pr_number=4820,
            pr_url="https://github.com/example/repo/pull/4820",
        ),
        Commit(
            sha="i7j8k9l",
            author="dana",
            message="docs: update README for onboarding",
            timestamp=now - timedelta(hours=2),
            files_changed=["README.md"],
            additions=12,
            deletions=4,
        ),
    ]


def build_github_client(mode: str, token: str, repo: str) -> GitHubClient:
    if mode == "rest":
        return RestGitHubClient(token=token, repo=repo)
    return MockGitHubClient()
