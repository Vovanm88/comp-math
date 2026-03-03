from __future__ import annotations

import dataclasses
import os
from typing import Any, Optional

import requests


class GitHubError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class PullRequestInfo:
    number: int
    title: str
    author_login: str
    head_repo_full_name: str
    head_ref: str
    head_sha: str
    base_ref: str
    updated_at: str


@dataclasses.dataclass(frozen=True)
class WorkflowRunInfo:
    id: int
    name: str
    head_sha: str
    status: str
    conclusion: Optional[str]
    html_url: str
    created_at: str


class GitHubClient:
    def __init__(self, token: str, repo_full_name: str, *, api_base: str = "https://api.github.com"):
        if "/" not in repo_full_name:
            raise ValueError("repo_full_name must be like 'owner/repo'")
        self._token = token
        self._repo = repo_full_name
        self._api_base = api_base.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pr-benchmark-bot",
            }
        )

    @classmethod
    def from_config(cls, github_cfg: dict[str, Any]) -> "GitHubClient":
        token_env = github_cfg.get("token_env", "GITHUB_TOKEN")
        token = os.environ.get(token_env) or github_cfg.get("token")
        if not token:
            raise ValueError(f"GitHub token not provided (env {token_env} or github.token in config)")
        repo = github_cfg["repo_full_name"]
        return cls(token=token, repo_full_name=repo)

    @property
    def repo_full_name(self) -> str:
        return self._repo

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any | None = None) -> Any:
        url = f"{self._api_base}{path}"
        resp = self._session.request(method, url, params=params, json=json, timeout=30)
        if resp.status_code >= 400:
            raise GitHubError(f"{method} {path} failed: {resp.status_code} {resp.text}")
        if resp.status_code == 204:
            return None
        return resp.json()

    def list_open_prs(self, *, per_page: int = 50) -> list[PullRequestInfo]:
        data = self._request(
            "GET",
            f"/repos/{self._repo}/pulls",
            params={"state": "open", "per_page": per_page, "sort": "updated", "direction": "desc"},
        )
        prs: list[PullRequestInfo] = []
        for pr in data:
            prs.append(
                PullRequestInfo(
                    number=int(pr["number"]),
                    title=str(pr.get("title", "")),
                    author_login=str(pr["user"]["login"]),
                    head_repo_full_name=str(pr["head"]["repo"]["full_name"]),
                    head_ref=str(pr["head"]["ref"]),
                    head_sha=str(pr["head"]["sha"]),
                    base_ref=str(pr["base"]["ref"]),
                    updated_at=str(pr["updated_at"]),
                )
            )
        return prs

    def list_workflow_runs_for_pull_request(self, pr_number: int, *, per_page: int = 50) -> list[WorkflowRunInfo]:
        # GitHub doesn't provide a direct "runs for PR" endpoint without narrowing by workflow.
        # We list recent pull_request runs and filter by pr_number presence.
        data = self._request(
            "GET",
            f"/repos/{self._repo}/actions/runs",
            params={"event": "pull_request", "per_page": per_page},
        )
        runs: list[WorkflowRunInfo] = []
        for run in data.get("workflow_runs", []):
            prs = run.get("pull_requests") or []
            if not any(int(p.get("number")) == pr_number for p in prs if p.get("number") is not None):
                continue
            runs.append(
                WorkflowRunInfo(
                    id=int(run["id"]),
                    name=str(run.get("name") or run.get("display_title") or ""),
                    head_sha=str(run["head_sha"]),
                    status=str(run.get("status", "")),
                    conclusion=run.get("conclusion"),
                    html_url=str(run.get("html_url", "")),
                    created_at=str(run.get("created_at", "")),
                )
            )
        return runs

    def list_artifacts_for_run(self, run_id: int) -> list[dict[str, Any]]:
        data = self._request("GET", f"/repos/{self._repo}/actions/runs/{run_id}/artifacts", params={"per_page": 100})
        return list(data.get("artifacts", []))

    def download_artifact_zip(self, artifact_id: int) -> bytes:
        # This endpoint returns a ZIP archive.
        url = f"{self._api_base}/repos/{self._repo}/actions/artifacts/{artifact_id}/zip"
        resp = self._session.get(url, timeout=120)
        if resp.status_code >= 400:
            raise GitHubError(f"GET artifact zip failed: {resp.status_code} {resp.text}")
        return resp.content

    def list_issue_comments(self, issue_number: int, *, per_page: int = 100) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{self._repo}/issues/{issue_number}/comments",
            params={"per_page": per_page, "sort": "created", "direction": "desc"},
        )

    def create_issue_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self._repo}/issues/{issue_number}/comments", json={"body": body})

    def update_issue_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self._request("PATCH", f"/repos/{self._repo}/issues/comments/{comment_id}", json={"body": body})

