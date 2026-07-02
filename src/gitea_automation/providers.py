"""SCM provider abstractions for Gitea, GitHub, and GitLab."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol, cast

from gitea_automation.models import PullRequestReadiness


class ProviderApiError(Exception):
    """Raised when provider API request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        url: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.url = url
        self.body = body


class ScmProvider(Protocol):
    """Protocol for source control provider API adapters."""

    name: str
    token_env_var: str

    def get_commit_status(
        self,
        base_url: str,
        owner: str,
        repo: str,
        sha: str,
        token: str,
    ) -> dict[str, Any]: ...

    def find_open_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
    ) -> dict[str, Any] | None: ...

    def create_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
        title_prefix: str,
    ) -> dict[str, Any]: ...

    def get_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> dict[str, Any]: ...

    def get_pull_request_readiness(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> PullRequestReadiness: ...

    def merge_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
        merge_style: str,
    ) -> None: ...

    def get_branch_remote_sha(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
    ) -> str: ...


class BaseProvider:
    """Shared HTTP utilities for provider implementations."""

    name = "base"
    token_env_var = ""

    def _repo_api_base(self, base_url: str, owner: str, repo: str) -> str:
        raise NotImplementedError

    def _build_headers(self, token: str) -> dict[str, str]:
        raise NotImplementedError

    def _request(
        self,
        method: str,
        url: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data: bytes | None = None
        headers = self._build_headers(token)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url=url, method=method, headers=headers, data=data)

        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="ignore")
            raise ProviderApiError(
                f"API request failed: {method} {url} [{error.code}] {body}",
                status_code=error.code,
                method=method,
                url=url,
                body=body,
            ) from error
        except urllib.error.URLError as error:
            raise ProviderApiError(
                f"API request failed: {method} {url} ({error.reason})",
                method=method,
                url=url,
            ) from error


class GiteaProvider(BaseProvider):
    """Gitea API adapter."""

    name = "gitea"
    token_env_var = "GITEA_TOKEN"

    def _repo_api_base(self, base_url: str, owner: str, repo: str) -> str:
        return f"{base_url.rstrip('/')}/api/v1/repos/{owner}/{repo}"

    def _build_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"token {token}", "Accept": "application/json"}

    def get_commit_status(
        self, base_url: str, owner: str, repo: str, sha: str, token: str
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/commits/{sha}/status"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError("Commit status response is not a JSON object")
        return cast(dict[str, Any], response)

    def find_open_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
    ) -> dict[str, Any] | None:
        query = urllib.parse.urlencode(
            {"state": "open", "head": f"{owner}:{branch}", "base": target_branch}
        )
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls?{query}"
        pulls = self._request("GET", url, token)
        if isinstance(pulls, list) and pulls and isinstance(pulls[0], dict):
            return cast(dict[str, Any], pulls[0])
        return None

    def create_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
        title_prefix: str,
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls"
        payload = {
            "base": target_branch,
            "head": branch,
            "title": f"{title_prefix}: {branch} -> {target_branch}",
            "body": "Auto-created by gitea-automation.",
        }
        response = self._request("POST", url, token, payload)
        if not isinstance(response, dict):
            raise ProviderApiError("Create pull request response is not a JSON object")
        return cast(dict[str, Any], response)

    def get_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls/{pull_number}"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError("Pull request response is not a JSON object")
        return cast(dict[str, Any], response)

    def get_pull_request_readiness(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> PullRequestReadiness:
        pull = self.get_pull_request(base_url, owner, repo, token, pull_number)
        mergeable = pull.get("mergeable")
        return PullRequestReadiness(
            mergeable=mergeable if isinstance(mergeable, bool) else None,
            draft=bool(pull.get("draft", False)),
            has_conflicts=bool(pull.get("has_conflicts", False)),
        )

    def merge_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
        merge_style: str,
    ) -> None:
        do_map = {
            "merge": "merge",
            "rebase": "rebase",
            "rebase-merge": "rebase-merge",
            "squash": "squash",
            "fast-forward-only": "fast-forward-only",
        }
        payload = {"Do": do_map[merge_style]}
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls/{pull_number}/merge"
        self._request("POST", url, token, payload)

    def get_branch_remote_sha(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
    ) -> str:
        url = f"{self._repo_api_base(base_url, owner, repo)}/branches/{branch}"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError(f"Branch response is not a JSON object: {branch}")
        commit = response.get("commit", {})
        sha = commit.get("id", "") if isinstance(commit, dict) else ""
        if not isinstance(sha, str) or not sha:
            raise ProviderApiError(f"Cannot read SHA for remote branch: {branch}")
        return sha


class GitHubProvider(BaseProvider):
    """GitHub API adapter."""

    name = "github"
    token_env_var = "GITHUB_TOKEN"

    def _repo_api_base(self, base_url: str, owner: str, repo: str) -> str:
        return f"{base_url.rstrip('/')}/repos/{owner}/{repo}"

    def _build_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_commit_status(
        self, base_url: str, owner: str, repo: str, sha: str, token: str
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/commits/{sha}/status"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError("Commit status response is not a JSON object")
        statuses = response.get("statuses", [])
        normalized: list[dict[str, Any]] = []
        if isinstance(statuses, list):
            for row in statuses:
                if not isinstance(row, dict):
                    continue
                normalized.append(
                    {
                        "context": row.get("context", "unknown"),
                        "status": row.get("state", "unknown"),
                        "description": row.get("description", ""),
                        "target_url": row.get("target_url", ""),
                    }
                )
        return {
            "state": response.get("state", "unknown"),
            "total_count": response.get("total_count", len(normalized)),
            "statuses": normalized,
        }

    def find_open_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
    ) -> dict[str, Any] | None:
        query = urllib.parse.urlencode(
            {"state": "open", "head": f"{owner}:{branch}", "base": target_branch}
        )
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls?{query}"
        pulls = self._request("GET", url, token)
        if isinstance(pulls, list) and pulls and isinstance(pulls[0], dict):
            return cast(dict[str, Any], pulls[0])
        return None

    def create_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
        title_prefix: str,
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls"
        payload = {
            "base": target_branch,
            "head": branch,
            "title": f"{title_prefix}: {branch} -> {target_branch}",
            "body": "Auto-created by gitea-automation.",
        }
        response = self._request("POST", url, token, payload)
        if not isinstance(response, dict):
            raise ProviderApiError("Create pull request response is not a JSON object")
        return cast(dict[str, Any], response)

    def get_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls/{pull_number}"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError("Pull request response is not a JSON object")
        return cast(dict[str, Any], response)

    def get_pull_request_readiness(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> PullRequestReadiness:
        pull = self.get_pull_request(base_url, owner, repo, token, pull_number)
        mergeable = pull.get("mergeable")
        mergeable_state = str(pull.get("mergeable_state", "")).lower()
        return PullRequestReadiness(
            mergeable=mergeable if isinstance(mergeable, bool) else None,
            draft=bool(pull.get("draft", False)),
            has_conflicts=mergeable_state == "dirty",
        )

    def merge_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
        merge_style: str,
    ) -> None:
        merge_method_map = {
            "merge": "merge",
            "rebase": "rebase",
            "squash": "squash",
        }
        if merge_style not in merge_method_map:
            raise ProviderApiError(
                f"Merge style '{merge_style}' is not supported by GitHub provider"
            )
        payload = {"merge_method": merge_method_map[merge_style]}
        url = f"{self._repo_api_base(base_url, owner, repo)}/pulls/{pull_number}/merge"
        self._request("PUT", url, token, payload)

    def get_branch_remote_sha(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
    ) -> str:
        url = f"{self._repo_api_base(base_url, owner, repo)}/branches/{branch}"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError(f"Branch response is not a JSON object: {branch}")
        commit = response.get("commit", {})
        sha = commit.get("sha", "") if isinstance(commit, dict) else ""
        if not isinstance(sha, str) or not sha:
            raise ProviderApiError(f"Cannot read SHA for remote branch: {branch}")
        return sha


class GitLabProvider(BaseProvider):
    """GitLab API adapter."""

    name = "gitlab"
    token_env_var = "GITLAB_TOKEN"

    def _project_id(self, owner: str, repo: str) -> str:
        return urllib.parse.quote(f"{owner}/{repo}", safe="")

    def _repo_api_base(self, base_url: str, owner: str, repo: str) -> str:
        project_id = self._project_id(owner, repo)
        return f"{base_url.rstrip('/')}/api/v4/projects/{project_id}"

    def _build_headers(self, token: str) -> dict[str, str]:
        return {"PRIVATE-TOKEN": token, "Accept": "application/json"}

    def get_commit_status(
        self, base_url: str, owner: str, repo: str, sha: str, token: str
    ) -> dict[str, Any]:
        url = (
            f"{self._repo_api_base(base_url, owner, repo)}/repository/commits/"
            f"{sha}/statuses?all=true"
        )
        response = self._request("GET", url, token)
        if not isinstance(response, list):
            raise ProviderApiError("Commit statuses response is not a JSON array")

        normalized: list[dict[str, Any]] = []
        states: list[str] = []
        for row in response:
            if not isinstance(row, dict):
                continue
            state = str(row.get("status", "unknown")).lower()
            states.append(state)
            normalized.append(
                {
                    "context": row.get("name", "unknown"),
                    "status": state,
                    "description": row.get("description", ""),
                    "target_url": row.get("target_url", ""),
                }
            )

        failed_states = {"failed", "canceled", "cancelled"}
        pending_states = {"pending", "running", "created", "waiting_for_resource", "preparing"}
        success_states = {"success", "skipped", "manual"}

        combined_state = "unknown"
        if not states:
            combined_state = "pending"
        elif any(state in failed_states for state in states):
            combined_state = "failure"
        elif all(state in success_states for state in states):
            combined_state = "success"
        elif any(state in pending_states for state in states):
            combined_state = "pending"
        else:
            combined_state = "pending"

        return {
            "state": combined_state,
            "total_count": len(normalized),
            "statuses": normalized,
        }

    def find_open_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
    ) -> dict[str, Any] | None:
        query = urllib.parse.urlencode(
            {
                "state": "opened",
                "source_branch": branch,
                "target_branch": target_branch,
            }
        )
        url = f"{self._repo_api_base(base_url, owner, repo)}/merge_requests?{query}"
        merge_requests = self._request("GET", url, token)
        if isinstance(merge_requests, list) and merge_requests and isinstance(merge_requests[0], dict):
            item = cast(dict[str, Any], merge_requests[0])
            if "iid" in item and "number" not in item:
                item = dict(item)
                item["number"] = item["iid"]
            return item
        return None

    def create_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
        target_branch: str,
        title_prefix: str,
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/merge_requests"
        payload = {
            "source_branch": branch,
            "target_branch": target_branch,
            "title": f"{title_prefix}: {branch} -> {target_branch}",
            "description": "Auto-created by gitea-automation.",
            "remove_source_branch": False,
        }
        response = self._request("POST", url, token, payload)
        if not isinstance(response, dict):
            raise ProviderApiError("Create merge request response is not a JSON object")
        item = cast(dict[str, Any], response)
        if "iid" in item and "number" not in item:
            item = dict(item)
            item["number"] = item["iid"]
        return item

    def get_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> dict[str, Any]:
        url = f"{self._repo_api_base(base_url, owner, repo)}/merge_requests/{pull_number}"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError("Merge request response is not a JSON object")
        item = cast(dict[str, Any], response)
        if "iid" in item and "number" not in item:
            item = dict(item)
            item["number"] = item["iid"]
        return item

    def get_pull_request_readiness(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
    ) -> PullRequestReadiness:
        pull = self.get_pull_request(base_url, owner, repo, token, pull_number)

        title = str(pull.get("title", ""))
        is_draft = bool(pull.get("draft", False)) or bool(pull.get("work_in_progress", False))
        is_draft = is_draft or title.lower().startswith("draft")

        detailed_status = str(pull.get("detailed_merge_status", "")).lower()
        merge_status = str(pull.get("merge_status", "")).lower()
        mergeable: bool | None = None
        if detailed_status:
            if detailed_status in {"mergeable", "ci_must_pass", "checking", "approvals_syncing"}:
                mergeable = detailed_status == "mergeable"
            elif detailed_status in {"conflict", "not_open", "broken_status", "requested_changes"}:
                mergeable = False
        elif merge_status:
            mergeable = merge_status == "can_be_merged"

        has_conflicts = bool(pull.get("has_conflicts", False))
        if detailed_status == "conflict":
            has_conflicts = True

        return PullRequestReadiness(
            mergeable=mergeable,
            draft=is_draft,
            has_conflicts=has_conflicts,
        )

    def merge_pull_request(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        pull_number: int,
        merge_style: str,
    ) -> None:
        if merge_style not in {"merge", "squash"}:
            raise ProviderApiError(
                f"Merge style '{merge_style}' is not supported by GitLab provider"
            )
        payload: dict[str, Any] = {
            "merge_when_pipeline_succeeds": False,
            "squash": merge_style == "squash",
        }
        url = f"{self._repo_api_base(base_url, owner, repo)}/merge_requests/{pull_number}/merge"
        self._request("PUT", url, token, payload)

    def get_branch_remote_sha(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str,
        branch: str,
    ) -> str:
        encoded_branch = urllib.parse.quote(branch, safe="")
        url = f"{self._repo_api_base(base_url, owner, repo)}/repository/branches/{encoded_branch}"
        response = self._request("GET", url, token)
        if not isinstance(response, dict):
            raise ProviderApiError(f"Branch response is not a JSON object: {branch}")
        commit = response.get("commit", {})
        sha = commit.get("id", "") if isinstance(commit, dict) else ""
        if not isinstance(sha, str) or not sha:
            raise ProviderApiError(f"Cannot read SHA for remote branch: {branch}")
        return sha


def create_provider(name: str) -> ScmProvider:
    """Factory for SCM providers by name."""
    normalized = name.strip().lower()
    if normalized == "gitea":
        return GiteaProvider()
    if normalized == "github":
        return GitHubProvider()
    if normalized == "gitlab":
        return GitLabProvider()
    raise ValueError(f"Unsupported provider: {name}")
