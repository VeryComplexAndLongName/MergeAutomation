"""CLI utility to push branch to Gitea, wait CI, merge into main, and wait CI again.

This script automates a typical release flow for a local branch:
1) Push current branch to remote.
2) Wait for commit status checks (pipeline) to finish successfully.
3) Create/find pull request and merge it into target branch.
4) Wait for pipeline on target branch commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

from loguru import logger
from pydantic import ValidationError

from gitea_automation.models import AppConfig

from gitea_automation.providers import (
	ProviderApiError,
	ScmProvider,
	create_provider,
)


DEFAULT_CONFIG_PATH = Path("config.json")


def load_dotenv_file(path: Path) -> None:
	"""Load simple KEY=VALUE pairs from .env into process environment.

	Existing environment variables are not overridden.

	Args:
		path: Dotenv file path.
	"""
	if not path.exists():
		logger.debug(f"Environment file not found: {path}")
		return

	loaded_count = 0
	skipped_existing = 0

	for raw_line in path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		key = key.strip()
		if not key:
			continue
		if key in os.environ:
			skipped_existing += 1
			continue
		os.environ[key] = value.strip().strip('"').strip("'")
		loaded_count += 1

	logger.info(
		f"Loaded .env entries: {loaded_count}; kept existing env vars: {skipped_existing}"
	)


class GiteaAutomationError(Exception):
	"""Raised when the automation flow cannot continue safely."""


class InvalidConfigError(GiteaAutomationError):
	"""Raised when runtime configuration is missing or invalid."""


class CommandExecutionError(GiteaAutomationError):
	"""Raised when external command execution fails."""


class ApiRequestError(GiteaAutomationError):
	"""Raised when Gitea API request fails."""

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


class PipelineError(GiteaAutomationError):
	"""Raised when pipeline status is failed or timed out."""


def summarize_http_error_body(body: str | None, max_length: int = 300) -> str:
	"""Build a compact single-line summary for HTTP error body logging."""
	if body is None:
		return "<none>"

	text = body.strip()
	if not text:
		return "<empty>"

	try:
		decoded = json.loads(text)
		if isinstance(decoded, dict):
			for key in ("message", "error", "err"):
				value = decoded.get(key)
				if isinstance(value, str) and value.strip():
					text = value
					break
	except json.JSONDecodeError:
		pass

	compact = " ".join(text.split())
	if len(compact) > max_length:
		compact = f"{compact[:max_length]}... [truncated]"
	return compact


def is_extended_logging(config: AppConfig) -> bool:
	"""Return True when extended logging mode is enabled."""
	return config.log_mode == "extended"


def setup_logger(log_file: Path, *, log_mode: str) -> None:
	"""Configure loguru to write both to console and file.

	Args:
		log_file: Path to file sink.
	"""
	file_level = "DEBUG" if log_mode == "extended" else "INFO"
	log_file.parent.mkdir(parents=True, exist_ok=True)
	logger.remove()
	logger.add(
		sys.stdout,
		level="INFO",
		colorize=True,
		format=(
			"<cyan>{time:YYYY-MM-DD HH:mm:ss}</cyan> | "
			"<level>{level: <8}</level> | "
			"<level>{message}</level>"
		),
	)
	logger.add(
		str(log_file),
		level=file_level,
		encoding="utf-8",
		format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
	)
	logger.info(f"Logger configured. File sink: {log_file}; mode={log_mode}")


def resolve_log_mode(args: argparse.Namespace) -> str:
	"""Resolve log mode before full config parsing for early logger setup."""
	cli_mode = getattr(args, "log_mode", None)
	if cli_mode in {"basic", "extended"}:
		return str(cli_mode)

	config_path = Path(args.config)
	if not config_path.exists():
		return "extended"

	try:
		with config_path.open("r", encoding="utf-8") as file:
			data = json.load(file)
		tmode = data.get("log_mode") if isinstance(data, dict) else None
		if isinstance(tmode, str) and tmode in {"basic", "extended"}:
			return tmode
	except (json.JSONDecodeError, OSError):
		return "extended"

	return "extended"


def load_json_config(path: Path) -> dict[str, Any]:
	"""Load JSON configuration file.

	Args:
		path: Path to JSON config.

	Returns:
		Parsed config dictionary.
	"""
	if not path.exists():
		logger.warning(f"Config file not found, defaults will be used: {path}")
		return {}
	with path.open("r", encoding="utf-8") as file:
		data = json.load(file)
	logger.info(f"Loaded config file: {path}")
	if isinstance(data, dict):
		return cast(dict[str, Any], data)
	raise InvalidConfigError(f"Config file must contain a JSON object: {path}")


def _format_authors(authors_raw: Any) -> str:
	"""Format PEP 621 authors list into a concise one-line string."""
	if not isinstance(authors_raw, list) or not authors_raw:
		return "unknown"

	items: list[str] = []
	for author in authors_raw:
		if not isinstance(author, dict):
			continue
		name = str(author.get("name", "")).strip()
		email = str(author.get("email", "")).strip()
		if name and email:
			items.append(f"{name} <{email}>")
		elif name:
			items.append(name)
		elif email:
			items.append(f"<{email}>")

	if not items:
		return "unknown"
	return ", ".join(items)


def log_project_metadata(pyproject_path: Path = Path("pyproject.toml")) -> None:
	"""Log project name, version, and authors from pyproject.toml."""
	if not pyproject_path.exists():
		logger.warning(f"pyproject.toml not found: {pyproject_path}")
		return

	try:
		with pyproject_path.open("rb") as file:
			data = tomllib.load(file)
	except (tomllib.TOMLDecodeError, OSError) as error:
		logger.warning(f"Failed to read pyproject metadata: {error}")
		return

	project = data.get("project", {})
	if not isinstance(project, dict):
		logger.warning("pyproject.toml has no [project] table")
		return

	name = str(project.get("name", "unknown")).strip() or "unknown"
	version = str(project.get("version", "unknown")).strip() or "unknown"
	authors = _format_authors(project.get("authors"))
	logger.info(f"{name} v{version} author: {authors}")


def build_parser() -> argparse.ArgumentParser:
	"""Build command-line parser with all supported options."""
	parser = argparse.ArgumentParser(
		description=(
			"Push current branch to provider, wait CI, merge into target branch, "
			"and wait CI again."
		)
	)
	parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON")
	parser.add_argument(
		"--provider",
		choices=["gitea", "github", "gitlab"],
		help="SCM provider type",
	)
	parser.add_argument("--base-url", help="Provider API base URL")
	parser.add_argument("--gitea-url", help="Legacy alias for --base-url")
	parser.add_argument("--owner", help="Repository owner")
	parser.add_argument("--repo", help="Repository name")
	parser.add_argument("--repo-path", help="Path to local git repo")
	parser.add_argument("--remote", help="Git remote name, default: origin")
	parser.add_argument("--main-branch", help="Main branch to merge into, default: main")
	parser.add_argument(
		"--target-branch",
		help="Legacy alias for --main-branch",
	)
	parser.add_argument("--poll-interval", type=int, help="CI polling interval in seconds")
	parser.add_argument(
		"--branch-scan-interval",
		type=int,
		help="Interval in seconds for scanning local branches in watch mode",
	)
	parser.add_argument("--timeout-seconds", type=int, help="Timeout for CI wait in seconds")
	parser.add_argument(
		"--merge-style",
		choices=["merge", "rebase", "rebase-merge", "squash", "fast-forward-only"],
		help="Merge strategy",
	)
	parser.add_argument(
		"--merge-405-retry-threshold",
		type=int,
		help=(
			"Number of consecutive 405 merge responses before forcing an empty commit "
			"refresh on source branch"
		),
	)
	parser.add_argument(
		"--merge-refresh-cycles",
		type=int,
		help=(
			"Maximum recovery steps for repeated merge 405 "
			"(1=GET PR, 2=+PR update, 3=+empty commit)"
		),
	)
	parser.add_argument("--pr-title-prefix", help="Prefix for generated PR title")
	parser.add_argument("--token", help="Provider API token. Preferred env var depends on provider")
	parser.add_argument("--log-file", default="logs/app.log", help="Path to log file")
	parser.add_argument(
		"--log-mode",
		choices=["basic", "extended"],
		help="Logging mode: basic (main events only) or extended (full diagnostics)",
	)
	parser.add_argument(
		"--dry-run",
		action="store_true",
		default=None,
		help="Run without merge. Push and CI check still execute.",
	)
	watch_group = parser.add_mutually_exclusive_group()
	watch_group.add_argument(
		"--watch-branches",
		dest="watch_branches",
		action="store_true",
		default=None,
		help="Keep running and process newly created local branches until Ctrl+C.",
	)
	watch_group.add_argument(
		"--no-watch-branches",
		dest="watch_branches",
		action="store_false",
		help="Process one branch and exit.",
	)
	show_group = parser.add_mutually_exclusive_group()
	show_group.add_argument(
		"--show-check-details",
		dest="show_check_details",
		action="store_true",
		default=None,
		help="Show each individual commit status check.",
	)
	show_group.add_argument(
		"--hide-check-details",
		dest="show_check_details",
		action="store_false",
		help="Hide individual commit status checks.",
	)
	strict_group = parser.add_mutually_exclusive_group()
	strict_group.add_argument(
		"--strict-status",
		dest="strict_status",
		action="store_true",
		default=None,
		help="Require at least one CI check to exist; otherwise fail.",
	)
	strict_group.add_argument(
		"--no-strict-status",
		dest="strict_status",
		action="store_false",
		help="Do not require explicit CI checks when combined state is success.",
	)
	return parser


def resolve_config(args: argparse.Namespace) -> tuple[AppConfig, str]:
	"""Combine config file, environment and CLI arguments.

	Args:
		args: Parsed command line arguments.

	Returns:
		Final app config and API token.

	Raises:
		GiteaAutomationError: If mandatory fields are missing.
	"""
	file_config = load_json_config(Path(args.config))
	logger.debug(f"Resolving runtime config using CLI + file: {args.config}")
	provider_name = str((getattr(args, "provider", None) or file_config.get("provider", "gitea"))).lower()
	provider = create_provider(provider_name)

	def pick(name: str, default: Any = None) -> Any:
		cli_value = getattr(args, name, None)
		if cli_value is not None:
			return cli_value
		if name in file_config:
			return file_config[name]
		return default

	main_branch_value = (
		getattr(args, "main_branch", None)
		or getattr(args, "target_branch", None)
		or file_config.get("main_branch")
		or file_config.get("target_branch")
		or "main"
	)

	base_url = str(
		pick("base_url", pick("gitea_url", "http://hppiigit:3000"))
	).rstrip("/")
	token = args.token or os.getenv(provider.token_env_var) or file_config.get("token", "")
	if not token:
		raise InvalidConfigError(
			f"{provider.name} token is required. Set --token or {provider.token_env_var} environment variable."
		)

	token_source = "cli --token" if args.token else (
		f"{provider.token_env_var} env" if os.getenv(provider.token_env_var) else "config file"
	)
	logger.info(f"{provider.name} token source resolved: {token_source}")

	repo_path_value = Path(str(pick("repo_path", r"C:\Prog\hyper")))
	remote_value = str(pick("remote", "origin"))
	owner_value = str(pick("owner", "")).strip()
	repo_value = str(pick("repo", "")).strip()
	if (not owner_value or not repo_value) and repo_path_value.exists():
		inferred_remote = infer_owner_repo_from_remote(repo_path_value, remote_value)
		if inferred_remote is not None:
			if not owner_value:
				owner_value = inferred_remote[0]
				logger.info(f"Owner was inferred from remote URL: {owner_value}")
			if not repo_value:
				repo_value = inferred_remote[1]
				logger.info(f"Repo was inferred from remote URL: {repo_value}")

	if not owner_value or not repo_value:
		raise InvalidConfigError(
			"Repository owner/repo are required. Set 'owner' and 'repo' in config, "
			"or configure git remote so they can be inferred automatically."
		)

	config_data: dict[str, Any] = {
		"provider_name": provider.name,
		"base_url": base_url,
		"owner": owner_value,
		"repo": repo_value,
		"repo_path": repo_path_value,
		"remote": remote_value,
		"main_branch": main_branch_value,
		"poll_interval": pick("poll_interval", 10),
		"timeout_seconds": pick("timeout_seconds", 1800),
		"merge_style": pick("merge_style", "merge"),
		"pr_title_prefix": pick("pr_title_prefix", "Auto merge"),
		"dry_run": pick("dry_run", False),
		"watch_branches": pick("watch_branches", True),
		"branch_scan_interval": pick("branch_scan_interval", 5),
		"log_mode": pick("log_mode", "extended"),
		"show_check_details": pick("show_check_details", True),
		"strict_status": pick("strict_status", False),
		"merge_405_retry_threshold": pick("merge_405_retry_threshold", 8),
		"merge_refresh_cycles": pick("merge_refresh_cycles", 3),
	}

	try:
		config = AppConfig.model_validate(config_data)
	except ValidationError as error:
		raise InvalidConfigError(f"Invalid config values: {error}") from error

	if is_extended_logging(config):
		logger.info(
			"Configuration resolved: "
			f"provider={config.provider_name}, repo={config.owner}/{config.repo}, "
			f"remote={config.remote}, "
			f"main_branch={config.main_branch}, dry_run={config.dry_run}, "
			f"watch_branches={config.watch_branches}, "
			f"branch_scan_interval={config.branch_scan_interval}, "
			f"strict_status={config.strict_status}, "
			f"merge_405_retry_threshold={config.merge_405_retry_threshold}, "
			f"merge_refresh_cycles={config.merge_refresh_cycles}"
		)
	else:
		logger.info(
			"Configuration resolved: "
			f"provider={config.provider_name}, repo={config.owner}/{config.repo}, "
			f"main_branch={config.main_branch}, watch_branches={config.watch_branches}"
		)

	return config, token


def run_git(repo_path: Path, *args: str) -> str:
	"""Run git command and return stdout.

	Args:
		repo_path: Repository working directory.
		*args: Git command args without `git`.

	Returns:
		Command stdout as text.

	Raises:
		GiteaAutomationError: If git command failed.
	"""
	command = ["git", "-C", str(repo_path), *args]
	logger.debug(f"Executing git command: {' '.join(command)}")
	result = subprocess.run(command, capture_output=True, text=True, check=False)
	if result.returncode != 0:
		raise CommandExecutionError(
			f"Git command failed: {' '.join(command)}\n"
			f"stdout: {result.stdout.strip()}\n"
			f"stderr: {result.stderr.strip()}"
		)
	logger.debug("Git command completed successfully")
	return result.stdout.strip()


def parse_owner_repo_from_remote_url(remote_url: str) -> tuple[str, str] | None:
	"""Extract owner/group path and repo name from git remote URL."""
	url = remote_url.strip()
	if not url:
		return None

	path_part = ""
	if "://" in url:
		parsed = urllib.parse.urlparse(url)
		path_part = parsed.path.lstrip("/")
	else:
		match = re.match(r"^[^@]+@[^:]+:(.+)$", url)
		if match is None:
			return None
		path_part = match.group(1).lstrip("/")

	if path_part.endswith(".git"):
		path_part = path_part[:-4]

	segments = [segment for segment in path_part.split("/") if segment]
	if len(segments) < 2:
		return None

	owner = "/".join(segments[:-1])
	repo = segments[-1]
	if not owner or not repo:
		return None
	return owner, repo


def parse_owner_from_remote_url(remote_url: str) -> str | None:
	"""Extract owner/group path from git remote URL."""
	parsed = parse_owner_repo_from_remote_url(remote_url)
	if parsed is None:
		return None
	return parsed[0]


def parse_repo_from_remote_url(remote_url: str) -> str | None:
	"""Extract repository name from git remote URL."""
	parsed = parse_owner_repo_from_remote_url(remote_url)
	if parsed is None:
		return None
	return parsed[1]


def infer_owner_repo_from_remote(repo_path: Path, remote: str) -> tuple[str, str] | None:
	"""Infer owner/group path and repository name from configured git remote URL."""
	try:
		remote_url = run_git(repo_path, "remote", "get-url", remote)
	except CommandExecutionError:
		return None
	return parse_owner_repo_from_remote_url(remote_url)


def infer_owner_from_remote(repo_path: Path, remote: str) -> str | None:
	"""Infer owner/group path from configured git remote URL."""
	parsed = infer_owner_repo_from_remote(repo_path, remote)
	if parsed is None:
		return None
	return parsed[0]


def api_request(
	method: str,
	url: str,
	token: str,
	payload: dict[str, Any] | None = None,
) -> Any:
	"""Execute HTTP request against Gitea API.

	Args:
		method: HTTP method.
		url: Absolute API URL.
		token: Personal access token.
		payload: Optional JSON body.

	Returns:
		Decoded JSON response.

	Raises:
		GiteaAutomationError: For non-success responses.
	"""
	data: bytes | None = None
	headers = {
		"Authorization": f"token {token}",
		"Accept": "application/json",
	}
	if payload is not None:
		data = json.dumps(payload).encode("utf-8")
		headers["Content-Type"] = "application/json"

	request = urllib.request.Request(url=url, method=method, headers=headers, data=data)
	logger.debug(f"API request: {method} {url}")
	if payload is not None:
		logger.debug(
			f"API request payload (application/json): {json.dumps(payload, ensure_ascii=True)}"
		)
	try:
		with urllib.request.urlopen(request) as response:
			logger.debug(f"API response status: {response.status} for {method} {url}")
			raw = response.read().decode("utf-8")
			if not raw:
				logger.warning(f"API returned empty body: {method} {url}")
				return {}
			return json.loads(raw)
	except urllib.error.HTTPError as error:
		body = error.read().decode("utf-8", errors="ignore")
		raise ApiRequestError(
			f"API request failed: {method} {url} [{error.code}] {body}",
			status_code=error.code,
			method=method,
			url=url,
			body=body,
		) from error
	except urllib.error.URLError as error:
		raise ApiRequestError(
			f"API request failed: {method} {url} ({error.reason})",
			method=method,
			url=url,
		) from error


def get_current_branch(repo_path: Path) -> str:
	"""Return active branch name for local repository."""
	branch = run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
	logger.debug(f"Detected current branch: {branch}")
	if branch == "HEAD":
		raise GiteaAutomationError("Detached HEAD is not supported for this workflow")
	return branch


def get_local_branches(repo_path: Path) -> set[str]:
	"""Return local branch names from git refs/heads."""
	raw = run_git(repo_path, "for-each-ref", "refs/heads", "--format=%(refname:short)")
	branches = {line.strip() for line in raw.splitlines() if line.strip()}
	logger.debug(f"Discovered local branches: {sorted(branches)}")
	return branches


def get_branch_head_sha(repo_path: Path, branch: str) -> str:
	"""Return SHA for given local branch reference."""
	sha = run_git(repo_path, "rev-parse", branch)
	logger.debug(f"Resolved local SHA for branch '{branch}': {sha}")
	return sha


def push_branch(repo_path: Path, remote: str, branch: str) -> None:
	"""Push local branch to configured remote."""
	logger.info(f"Pushing branch '{branch}' to remote '{remote}'...")
	run_git(repo_path, "push", remote, branch)
	logger.info("Push completed")


def get_commit_status(
	gitea_url: str,
	owner: str,
	repo: str,
	sha: str,
	token: str,
) -> dict[str, Any]:
	"""Fetch combined commit status for SHA from Gitea."""
	status_url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/commits/{sha}/status"
	logger.debug(f"Fetching commit status for {sha}")
	response = api_request("GET", status_url, token)
	if isinstance(response, dict):
		return cast(dict[str, Any], response)
	raise ApiRequestError("Commit status response is not a JSON object")


def get_status_checks(status: dict[str, Any]) -> list[dict[str, Any]]:
	"""Extract individual checks from combined status response."""
	checks = status.get("statuses", [])
	if not isinstance(checks, list):
		return []
	return [item for item in checks if isinstance(item, dict)]


def log_status_checks(checks: list[dict[str, Any]]) -> None:
	"""Log each status check in a human-friendly format."""
	if not checks:
		logger.info("No individual checks returned yet")
		return

	logger.info(f"Detailed checks ({len(checks)}):")
	for check in checks:
		context = str(check.get("context", "unknown"))
		state = str(check.get("status", "unknown")).lower()
		description = str(check.get("description", "")).strip()
		target_url = str(check.get("target_url", "")).strip()
		message = f"  - [{state}] {context}"
		if description:
			message = f"{message} | {description}"
		if target_url:
			message = f"{message} | {target_url}"
		logger.info(message)


def checks_signature(checks: list[dict[str, Any]]) -> tuple[str, ...]:
	"""Build a stable signature for change detection in status checks."""
	rows: list[str] = []
	for check in checks:
		context = str(check.get("context", "unknown"))
		state = str(check.get("status", "unknown")).lower()
		description = str(check.get("description", "")).strip()
		target_url = str(check.get("target_url", "")).strip()
		rows.append(f"{context}|{state}|{description}|{target_url}")
	return tuple(sorted(rows))


def wait_for_pipeline_provider(
	provider: ScmProvider,
	config: AppConfig,
	sha: str,
	token: str,
	stage_name: str,
) -> None:
	"""Poll commit status until success or failure via selected provider."""
	logger.info(f"Waiting for pipeline ({stage_name}) on commit {sha}...")
	started = time.time()
	previous_signature: tuple[str, ...] | None = None
	has_seen_checks = False

	while True:
		base_url = str(config.base_url)
		status = provider.get_commit_status(
			base_url,
			config.owner,
			config.repo,
			sha,
			token,
		)
		state = str(status.get("state", "unknown")).lower()
		total_count = int(status.get("total_count", 0))
		logger.info(f"Pipeline state: {state}; checks: {total_count}")

		checks = get_status_checks(status)
		if total_count > 0 or checks:
			has_seen_checks = True

		detailed_checks_enabled = config.show_check_details and is_extended_logging(config)
		if detailed_checks_enabled:
			signature = checks_signature(checks)
			if signature != previous_signature:
				log_status_checks(checks)
				previous_signature = signature

		if state == "success":
			if config.strict_status and not has_seen_checks:
				raise PipelineError(
					"Pipeline reported success, but no CI checks were found "
					f"at stage: {stage_name}"
				)
			logger.info(f"Pipeline succeeded for stage: {stage_name}")
			return

		if state in {"error", "failure", "failed"}:
			if detailed_checks_enabled and checks:
				logger.error("Failed stage details:")
				log_status_checks(checks)
			raise PipelineError(f"Pipeline failed at stage: {stage_name}")

		elapsed = time.time() - started
		if elapsed >= config.timeout_seconds:
			if config.strict_status and not has_seen_checks:
				raise PipelineError(
					"Timeout waiting for CI checks to appear "
					f"at stage: {stage_name}"
				)
			raise PipelineError(f"Timeout waiting for pipeline at stage: {stage_name}")

		time.sleep(config.poll_interval)


def wait_for_pull_request_ready_provider(
	provider: ScmProvider,
	config: AppConfig,
	token: str,
	pull_number: int,
	initial_delay_seconds: int,
) -> None:
	"""Wait until pull request can be merged for selected provider."""
	if initial_delay_seconds > 0:
		logger.info(
			f"Waiting {initial_delay_seconds}s before first PR readiness check for #{pull_number}"
		)
		time.sleep(initial_delay_seconds)

	started = time.time()
	while True:
		base_url = str(config.base_url)
		readiness = provider.get_pull_request_readiness(
			base_url,
			config.owner,
			config.repo,
			token,
			pull_number,
		)
		mergeable_text = str(readiness.mergeable).lower()
		logger.info(
			f"PR #{pull_number} readiness: mergeable={mergeable_text}, "
			f"has_conflicts={readiness.has_conflicts}, draft={readiness.draft}"
		)

		if readiness.has_conflicts:
			raise PipelineError(f"PR #{pull_number} has conflicts and cannot be merged")
		if readiness.draft:
			raise PipelineError(f"PR #{pull_number} is draft and cannot be merged")
		if readiness.mergeable is True:
			logger.success(f"PR #{pull_number} is ready to merge")
			return

		elapsed = time.time() - started
		if elapsed >= config.timeout_seconds:
			raise PipelineError(f"Timeout waiting PR #{pull_number} to become mergeable")

		time.sleep(config.poll_interval)


def ensure_pull_request_provider(
	provider: ScmProvider,
	config: AppConfig,
	token: str,
	branch: str,
	allow_create: bool,
) -> dict[str, Any] | None:
	"""Return open pull request if exists, otherwise create a new one."""
	existing = provider.find_open_pull_request(
		str(config.base_url),
		config.owner,
		config.repo,
		token,
		branch,
		config.main_branch,
	)
	if existing is not None:
		logger.info(f"Using existing PR #{existing.get('number')}")
		return existing

	if not allow_create:
		logger.warning("No open PR found and create is disabled for dry-run")
		return None

	logger.info("Creating pull request...")
	created = provider.create_pull_request(
		str(config.base_url),
		config.owner,
		config.repo,
		token,
		branch,
		config.main_branch,
		config.pr_title_prefix,
	)
	logger.info(f"Created PR #{created.get('number')}")
	return created


def merge_pull_request_provider(
	provider: ScmProvider,
	config: AppConfig,
	token: str,
	pull_number: int,
	repo_path: Path,
	remote: str,
	source_branch: str,
) -> None:
	"""Merge pull request using selected provider implementation."""
	if provider.name == "gitea":
		merge_pull_request(
			gitea_url=str(config.base_url),
			owner=config.owner,
			repo=config.repo,
			token=token,
			pull_number=pull_number,
			merge_style=config.merge_style,
			repo_path=repo_path,
			remote=remote,
			source_branch=source_branch,
			poll_interval=config.poll_interval,
			timeout_seconds=config.timeout_seconds,
			show_check_details=config.show_check_details,
			strict_status=config.strict_status,
			detailed_logging=is_extended_logging(config),
			merge_405_retry_threshold=config.merge_405_retry_threshold,
			merge_refresh_cycles=config.merge_refresh_cycles,
		)
		return

	logger.info(
		f"Merging PR #{pull_number} with strategy '{config.merge_style}' "
		f"using provider '{provider.name}'..."
	)
	provider.merge_pull_request(
		str(config.base_url),
		config.owner,
		config.repo,
		token,
		pull_number,
		config.merge_style,
	)
	logger.success("PR merge completed")


def wait_for_pipeline(
	gitea_url: str,
	owner: str,
	repo: str,
	sha: str,
	token: str,
	poll_interval: int,
	timeout_seconds: int,
	stage_name: str,
	show_check_details: bool,
	strict_status: bool,
) -> None:
	"""Poll commit status until success or failure.

	Args:
		gitea_url: Base Gitea URL.
		owner: Repository owner.
		repo: Repository name.
		sha: Commit SHA to watch.
		token: Gitea token.
		poll_interval: Poll interval seconds.
		timeout_seconds: Timeout for waiting.
		stage_name: Human-friendly stage label.
		show_check_details: Whether to print detailed check list.
		strict_status: Whether at least one check is required.
	"""
	logger.info(f"Waiting for pipeline ({stage_name}) on commit {sha}...")
	started = time.time()
	previous_signature: tuple[str, ...] | None = None
	has_seen_checks = False
	while True:
		status = get_commit_status(gitea_url, owner, repo, sha, token)
		state = str(status.get("state", "unknown")).lower()
		total_count = int(status.get("total_count", 0))
		logger.info(f"Pipeline state: {state}; checks: {total_count}")
		checks = get_status_checks(status)
		if total_count > 0 or checks:
			has_seen_checks = True

		if show_check_details:
			signature = checks_signature(checks)
			if signature != previous_signature:
				log_status_checks(checks)
				previous_signature = signature

		if state == "success":
			if strict_status and not has_seen_checks:
					raise PipelineError(
					"Pipeline reported success, but no CI checks were found "
					f"at stage: {stage_name}"
				)
			logger.info(f"Pipeline succeeded for stage: {stage_name}")
			return
		if state in {"error", "failure", "failed"}:
			if show_check_details and checks:
				logger.error("Failed stage details:")
				log_status_checks(checks)
				raise PipelineError(f"Pipeline failed at stage: {stage_name}")

		elapsed = time.time() - started
		if elapsed >= timeout_seconds:
			if strict_status and not has_seen_checks:
				raise PipelineError(
					"Timeout waiting for CI checks to appear "
					f"at stage: {stage_name}"
				)
			raise PipelineError(
				f"Timeout waiting for pipeline at stage: {stage_name}"
			)
		time.sleep(poll_interval)


def find_open_pull_request(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	branch: str,
	target_branch: str,
) -> dict[str, Any] | None:
	"""Find already-open PR for branch -> target branch."""
	query = urllib.parse.urlencode(
		{"state": "open", "head": f"{owner}:{branch}", "base": target_branch}
	)
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/pulls?{query}"
	logger.info(
		f"Searching for open PR: {owner}/{repo} {branch} -> {target_branch}"
	)
	pulls = api_request("GET", url, token)
	if isinstance(pulls, list) and pulls and isinstance(pulls[0], dict):
		logger.info(f"Found open PR candidate count: {len(pulls)}")
		return cast(dict[str, Any], pulls[0])
	logger.info("No matching open PR found")
	return None


def create_pull_request(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	branch: str,
	target_branch: str,
	title_prefix: str,
) -> dict[str, Any]:
	"""Create a PR from source branch to target branch."""
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/pulls"
	payload = {
		"base": target_branch,
		"head": branch,
		"title": f"{title_prefix}: {branch} -> {target_branch}",
		"body": "Auto-created by gitea.py automation script.",
	}
	logger.info(f"Creating PR: {branch} -> {target_branch}")
	response = api_request("POST", url, token, payload)
	if isinstance(response, dict):
		return cast(dict[str, Any], response)
	raise ApiRequestError("Create PR response is not a JSON object")


def get_pull_request(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	pull_number: int,
) -> dict[str, Any]:
	"""Fetch pull request details by number."""
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/pulls/{pull_number}"
	response = api_request("GET", url, token)
	if isinstance(response, dict):
		return cast(dict[str, Any], response)
	raise ApiRequestError(f"Pull request response is not a JSON object: #{pull_number}")


def get_pull_request_reviews(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	pull_number: int,
) -> list[dict[str, Any]]:
	"""Fetch pull request reviews by number."""
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
	response = api_request("GET", url, token)
	if not isinstance(response, list):
		raise ApiRequestError(f"Pull request reviews response is not a JSON array: #{pull_number}")
	return [item for item in response if isinstance(item, dict)]


def get_repo_info(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
) -> dict[str, Any]:
	"""Fetch repository details for merge settings diagnostics."""
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}"
	response = api_request("GET", url, token)
	if isinstance(response, dict):
		return cast(dict[str, Any], response)
	raise ApiRequestError("Repository response is not a JSON object")


def get_branch_protection(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	branch: str,
) -> dict[str, Any] | None:
	"""Fetch branch protection details if endpoint is available."""
	encoded_branch = urllib.parse.quote(branch, safe="")
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/branch_protections/{encoded_branch}"
	try:
		response = api_request("GET", url, token)
		if isinstance(response, dict):
			return cast(dict[str, Any], response)
		logger.warning(
			f"Branch protection response is not a JSON object for branch '{branch}'"
		)
		return None
	except ApiRequestError as error:
		logger.warning(
			"Branch protection endpoint unavailable or inaccessible: "
			f"status={error.status_code or 'n/a'} branch={branch}"
		)
		return None


def is_merge_style_allowed(repo_info: dict[str, Any], merge_style: str) -> bool | None:
	"""Check whether requested merge style appears enabled in repository settings."""
	style_flags: dict[str, str] = {
		"merge": "allow_merge_commits",
		"rebase": "allow_rebase",
		"rebase-merge": "allow_rebase_explicit",
		"squash": "allow_squash",
		"fast-forward-only": "allow_fast_forward_only_merge",
	}
	flag_name = style_flags.get(merge_style)
	if flag_name is None:
		return None
	value = repo_info.get(flag_name)
	if isinstance(value, bool):
		return value
	return None


def update_pull_request_branch(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	pull_number: int,
) -> None:
	"""Trigger PR branch update/recalculation via Gitea API."""
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/pulls/{pull_number}/update"
	logger.warning(f"Triggering PR update endpoint for #{pull_number}")
	api_request("POST", url, token, payload={})


def _review_user_login(review: dict[str, Any]) -> str:
	"""Extract reviewer login from a review object for diagnostics output."""
	user = review.get("user", {})
	if isinstance(user, dict):
		login = user.get("login")
		if isinstance(login, str):
			return login
	return "unknown"


def log_pull_request_diagnostics(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	pull_number: int,
	*,
	reason: str,
	merge_style: str | None = None,
	status_code: int | None = None,
	error_body: str | None = None,
	include_details: bool = True,
) -> None:
	"""Log expanded pull request diagnostics for merge troubleshooting."""
	logger.warning(f"Collecting PR diagnostics for #{pull_number}; reason: {reason}")
	if status_code is not None:
		logger.warning(f"Merge API status code: {status_code}")
	if include_details and error_body is not None:
		body_summary = summarize_http_error_body(error_body, max_length=600)
		logger.warning(f"Merge API response body summary: {body_summary!r}")
	if include_details and merge_style is not None:
		logger.warning(
			"Merge request payload used: "
			f"{{\"Do\": \"{merge_style}\"}}; Content-Type: application/json"
		)
	if not include_details:
		logger.warning("Detailed diagnostics are hidden in basic log mode")
		return
	try:
		pull = get_pull_request(gitea_url, owner, repo, token, pull_number)
		state = str(pull.get("state", "unknown"))
		mergeable = str(pull.get("mergeable", "unknown")).lower()
		merged = bool(pull.get("merged", False))
		has_conflicts = bool(pull.get("has_conflicts", False))
		draft = bool(pull.get("draft", False))
		logger.warning(
			f"PR #{pull_number} core: state={state}, mergeable={mergeable}, "
			f"merged={merged}, has_conflicts={has_conflicts}, draft={draft}"
		)

		reviews = get_pull_request_reviews(gitea_url, owner, repo, token, pull_number)
		approved_by = sorted(
			{
				_review_user_login(review)
				for review in reviews
				if str(review.get("state", "")).upper() == "APPROVED"
			}
		)
		changes_requested_by = sorted(
			{
				_review_user_login(review)
				for review in reviews
				if str(review.get("state", "")).upper() in {"CHANGES_REQUESTED", "REQUEST_CHANGES"}
			}
		)
		logger.warning(
			f"PR #{pull_number} approvals: approved={len(approved_by)} ({', '.join(approved_by) or 'none'}), "
			f"changes_requested={len(changes_requested_by)} ({', '.join(changes_requested_by) or 'none'}), "
			f"total_reviews={len(reviews)}"
		)

		repo_info = get_repo_info(gitea_url, owner, repo, token)
		allow_merge_commits = repo_info.get("allow_merge_commits")
		allow_rebase = repo_info.get("allow_rebase")
		allow_rebase_explicit = repo_info.get("allow_rebase_explicit")
		allow_squash = repo_info.get("allow_squash")
		allow_fast_forward_only = repo_info.get("allow_fast_forward_only_merge")
		logger.warning(
			"Repository merge styles: "
			f"merge={allow_merge_commits}, rebase={allow_rebase}, "
			f"rebase_merge={allow_rebase_explicit}, squash={allow_squash}, "
			f"fast_forward_only={allow_fast_forward_only}"
		)
		if merge_style is not None:
			allowed = is_merge_style_allowed(repo_info, merge_style)
			if allowed is not None:
				logger.warning(
					f"Requested merge style '{merge_style}' allowed by repo settings: {allowed}"
				)

		base = pull.get("base", {})
		target_branch = ""
		if isinstance(base, dict):
			ref = base.get("ref", "")
			if isinstance(ref, str):
				target_branch = ref
		if target_branch:
			protection = get_branch_protection(
				gitea_url,
				owner,
				repo,
				token,
				target_branch,
			)
			if protection is not None:
				required_status_checks = protection.get("required_status_checks", {})
				status_check_contexts: list[str] = []
				if isinstance(required_status_checks, dict):
					contexts = required_status_checks.get("contexts", [])
					if isinstance(contexts, list):
						status_check_contexts = [
							str(item) for item in contexts if isinstance(item, str)
						]
				push_whitelist = protection.get("whitelist_usernames", [])
				merge_whitelist = protection.get("merge_whitelist_usernames", [])
				logger.warning(
					f"Branch protection for '{target_branch}': "
					f"required_checks={status_check_contexts or ['none']}, "
					f"push_whitelist={push_whitelist if isinstance(push_whitelist, list) else 'n/a'}, "
					f"merge_whitelist={merge_whitelist if isinstance(merge_whitelist, list) else 'n/a'}"
				)

		head_sha = ""
		head = pull.get("head", {})
		if isinstance(head, dict):
			head_sha_raw = head.get("sha", "")
			if isinstance(head_sha_raw, str):
				head_sha = head_sha_raw

		if not head_sha:
			logger.warning(f"PR #{pull_number} checks: head SHA is missing")
			return

		status = get_commit_status(gitea_url, owner, repo, head_sha, token)
		checks = get_status_checks(status)
		combined_state = str(status.get("state", "unknown")).lower()
		logger.warning(
			f"PR #{pull_number} checks: combined_state={combined_state}, total={len(checks)}"
		)
		for check in checks:
			context = str(check.get("context", "unknown"))
			check_state = str(check.get("status", "unknown")).lower()
			description = str(check.get("description", "")).strip()
			target_url = str(check.get("target_url", "")).strip()
			message = f"  - [{check_state}] {context}"
			if description:
				message = f"{message} | {description}"
			if target_url:
				message = f"{message} | {target_url}"
			logger.warning(message)
	except Exception:
		logger.exception(f"Failed to collect PR diagnostics for #{pull_number}")


def wait_for_pull_request_ready(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	pull_number: int,
	poll_interval: int,
	timeout_seconds: int,
	initial_delay_seconds: int,
) -> None:
	"""Wait until pull request can be merged without transient 405 errors."""
	if initial_delay_seconds > 0:
		logger.info(
			f"Waiting {initial_delay_seconds}s before first PR readiness check for #{pull_number}"
		)
		time.sleep(initial_delay_seconds)

	started = time.time()
	while True:
		pull = get_pull_request(gitea_url, owner, repo, token, pull_number)
		is_draft = bool(pull.get("draft", False))
		has_conflicts = bool(pull.get("has_conflicts", False))
		mergeable = pull.get("mergeable")
		mergeable_text = str(mergeable).lower()
		logger.info(
			f"PR #{pull_number} readiness: mergeable={mergeable_text}, "
			f"has_conflicts={has_conflicts}, draft={is_draft}"
		)

		if has_conflicts:
			raise PipelineError(f"PR #{pull_number} has conflicts and cannot be merged")
		if is_draft:
			raise PipelineError(f"PR #{pull_number} is draft and cannot be merged")
		if mergeable is True:
			logger.success(f"PR #{pull_number} is ready to merge")
			return

		elapsed = time.time() - started
		if elapsed >= timeout_seconds:
			raise PipelineError(f"Timeout waiting PR #{pull_number} to become mergeable")
		time.sleep(poll_interval)


def ensure_pull_request(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	branch: str,
	target_branch: str,
	title_prefix: str,
	allow_create: bool,
) -> dict[str, Any] | None:
	"""Return open PR if exists, otherwise create a new one."""
	existing = find_open_pull_request(gitea_url, owner, repo, token, branch, target_branch)
	if existing is not None:
		logger.info(f"Using existing PR #{existing.get('number')}")
		return existing
	if not allow_create:
		logger.warning("No open PR found and create is disabled for dry-run")
		return None

	logger.info("Creating pull request...")
	created = create_pull_request(
		gitea_url,
		owner,
		repo,
		token,
		branch,
		target_branch,
		title_prefix,
	)
	logger.info(f"Created PR #{created.get('number')}")
	return created


def merge_pull_request(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	pull_number: int,
	merge_style: str,
	repo_path: Path,
	remote: str,
	source_branch: str,
	poll_interval: int,
	timeout_seconds: int,
	show_check_details: bool,
	strict_status: bool,
	detailed_logging: bool,
	merge_405_retry_threshold: int,
	merge_refresh_cycles: int,
) -> None:
	"""Merge PR by number with selected strategy."""
	do_map = {
		"merge": "merge",
		"rebase": "rebase",
		"rebase-merge": "rebase-merge",
		"squash": "squash",
		"fast-forward-only": "fast-forward-only",
	}
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/pulls/{pull_number}/merge"
	payload = {"Do": do_map[merge_style]}
	logger.info(f"Merging PR #{pull_number} with strategy '{merge_style}'...")
	started = time.time()
	attempt = 0
	consecutive_405_attempts = 0
	recovery_step = 0
	while True:
		attempt += 1
		try:
			if detailed_logging:
				logger.debug(
				"Sending merge request with payload "
				f"{{\"Do\": \"{payload['Do']}\"}} to {url}"
			)
			api_request("POST", url, token, payload)
			break
		except ApiRequestError as error:
			error_body_text = (error.body or "").lower()
			is_transient_405 = error.status_code == 405 and "try again later" in error_body_text
			if is_transient_405:
				consecutive_405_attempts += 1
				if detailed_logging:
					body_summary = summarize_http_error_body(error.body)
					logger.warning(f"Merge 405 body: {body_summary!r}")
				else:
					logger.warning("Merge endpoint returned transient 405")
				log_pull_request_diagnostics(
					gitea_url,
					owner,
					repo,
					token,
					pull_number,
					reason=f"merge endpoint returned 405 on attempt {attempt}",
					merge_style=payload["Do"],
					status_code=error.status_code,
					error_body=error.body,
					include_details=detailed_logging,
				)
				pull = get_pull_request(gitea_url, owner, repo, token, pull_number)
				if bool(pull.get("merged", False)):
					logger.success(f"PR #{pull_number} is already merged")
					break

				if consecutive_405_attempts >= merge_405_retry_threshold:
					if recovery_step >= merge_refresh_cycles:
						raise PipelineError(
							"Merge still returns transient 405 after all recovery steps: "
							"1) PR refresh/readiness check, "
							"2) PR update endpoint, "
							"3) empty commit"
						) from error

					recovery_step += 1
					consecutive_405_attempts = 0
					logger.warning(
						f"Repeated transient 405 threshold reached ({merge_405_retry_threshold}); "
						f"running recovery step {recovery_step}/{merge_refresh_cycles}"
					)

					if recovery_step == 1:
						logger.warning(
							"Recovery step 1: refresh PR data via GET and re-check readiness"
						)
						get_pull_request(gitea_url, owner, repo, token, pull_number)
						wait_for_pull_request_ready(
							gitea_url=gitea_url,
							owner=owner,
							repo=repo,
							token=token,
							pull_number=pull_number,
							poll_interval=poll_interval,
							timeout_seconds=timeout_seconds,
							initial_delay_seconds=0,
						)
						logger.info("Retrying merge after recovery step 1")
						continue

					if recovery_step == 2:
						logger.warning(
							"Recovery step 2: trigger PR update endpoint and re-check readiness"
						)
						try:
							update_pull_request_branch(
								gitea_url,
								owner,
								repo,
								token,
								pull_number,
							)
						except ApiRequestError as update_error:
							if detailed_logging:
								update_body_summary = summarize_http_error_body(update_error.body)
								logger.warning(
									"PR update endpoint failed: "
									f"status={update_error.status_code or 'n/a'}, "
									f"body={update_body_summary!r}"
								)
							else:
								logger.warning("PR update endpoint failed")
						wait_for_pull_request_ready(
							gitea_url=gitea_url,
							owner=owner,
							repo=repo,
							token=token,
							pull_number=pull_number,
							poll_interval=poll_interval,
							timeout_seconds=timeout_seconds,
							initial_delay_seconds=0,
						)
						logger.info("Retrying merge after recovery step 2")
						continue

					if recovery_step == 3:
						logger.warning(
							"Recovery step 3: create empty commit, push and wait for new checks"
						)
						commit_message = (
							"chore: refresh PR head after repeated merge 405 "
							"(recovery step 3/3)"
						)
						run_git(
							repo_path,
							"commit",
							"--allow-empty",
							"-m",
							commit_message,
						)
						push_branch(repo_path, remote, source_branch)
						refreshed_sha = get_branch_head_sha(repo_path, source_branch)
						logger.info(f"Recovery step 3: new source SHA {refreshed_sha}")
						wait_for_pipeline(
							gitea_url=gitea_url,
							owner=owner,
							repo=repo,
							sha=refreshed_sha,
							token=token,
							poll_interval=poll_interval,
							timeout_seconds=timeout_seconds,
							stage_name="feature branch recovery step 3/3",
							show_check_details=show_check_details,
							strict_status=strict_status,
						)
						wait_for_pull_request_ready(
							gitea_url=gitea_url,
							owner=owner,
							repo=repo,
							token=token,
							pull_number=pull_number,
							poll_interval=poll_interval,
							timeout_seconds=timeout_seconds,
							initial_delay_seconds=0,
						)
						logger.info("Retrying merge after recovery step 3")
						continue

					continue
					continue

				elapsed = time.time() - started
				if elapsed >= timeout_seconds:
					log_pull_request_diagnostics(
						gitea_url,
						owner,
						repo,
						token,
						pull_number,
						reason=(
							f"merge endpoint timeout after {attempt} attempts "
							f"and {int(elapsed)}s"
						),
						merge_style=payload["Do"],
						status_code=error.status_code,
						error_body=error.body,
						include_details=detailed_logging,
					)
					raise PipelineError(
						f"Timeout waiting merge endpoint for PR #{pull_number} "
						f"after {int(elapsed)}s"
					) from error
				logger.warning(
					f"Merge endpoint temporary unavailable (attempt {attempt}); "
					f"retrying in {poll_interval}s"
				)
				time.sleep(poll_interval)
				continue
			if error.status_code == 405:
				if detailed_logging:
					body_summary = summarize_http_error_body(error.body)
					raise PipelineError(
						f"Merge rejected with non-transient 405: {body_summary}"
					) from error
				raise PipelineError("Merge rejected with non-transient 405") from error
			consecutive_405_attempts = 0
			log_pull_request_diagnostics(
				gitea_url,
				owner,
				repo,
				token,
				pull_number,
				reason=(
					f"merge request failed with status={error.status_code or 'n/a'} "
					f"on attempt {attempt}"
				),
				merge_style=payload["Do"],
				status_code=error.status_code,
				error_body=error.body,
				include_details=detailed_logging,
			)
			raise
	logger.success("PR merge completed")


def get_branch_remote_sha(
	gitea_url: str,
	owner: str,
	repo: str,
	token: str,
	branch: str,
) -> str:
	"""Get branch head SHA from Gitea branch endpoint."""
	url = f"{gitea_url}/api/v1/repos/{owner}/{repo}/branches/{branch}"
	logger.info(f"Fetching remote SHA for branch '{branch}'")
	branch_data = api_request("GET", url, token)
	if not isinstance(branch_data, dict):
		raise ApiRequestError(f"Branch response is not a JSON object: {branch}")
	commit = branch_data.get("commit", {})
	sha = commit.get("id", "")
	if not sha:
		raise ApiRequestError(f"Cannot read SHA for remote branch: {branch}")
	logger.debug(f"Remote SHA for branch '{branch}': {sha}")
	return str(sha)


def run_flow_for_branch(
	provider: ScmProvider,
	config: AppConfig,
	token: str,
	branch: str,
) -> None:
	"""Run full push -> CI -> merge -> CI flow for a single branch."""
	if branch == config.main_branch:
		raise GiteaAutomationError(
			f"Branch '{branch}' equals main branch '{config.main_branch}'. "
			"Choose a feature branch."
		)

	logger.info(f"Starting workflow for branch: {branch}")
	pull_request = provider.find_open_pull_request(
		str(config.base_url),
		config.owner,
		config.repo,
		token,
		branch,
		config.main_branch,
	)
	if pull_request is not None:
		logger.info(f"Existing PR detected before pipeline: #{pull_request.get('number')}")
	else:
		logger.info("No PR found before pipeline; it will be created after CI success")
	had_existing_pr = pull_request is not None

	push_branch(config.repo_path, config.remote, branch)
	logger.success("Push stage completed")
	pushed_sha = get_branch_head_sha(config.repo_path, branch)
	logger.info(f"Pushed commit SHA: {pushed_sha}")

	wait_for_pipeline_provider(provider, config, pushed_sha, token, "feature branch")

	if pull_request is None:
		pull_request = ensure_pull_request_provider(
			provider,
			config,
			token,
			branch,
			allow_create=not config.dry_run,
		)

	if config.dry_run:
		if pull_request is None:
			logger.success("Dry-run complete: push and pipeline check passed, PR not created")
		else:
			logger.success(
				"Dry-run complete: push and pipeline check passed, existing PR found"
			)
		return

	if pull_request is None:
		raise PipelineError("Failed to obtain pull request for merge")

	pull_number = int(pull_request.get("number", 0))
	if pull_number <= 0:
		raise ApiRequestError("Invalid pull request number received from provider")

	created_now = not had_existing_pr
	initial_delay_seconds = max(3, config.poll_interval) if created_now else 0
	wait_for_pull_request_ready_provider(
		provider,
		config,
		token,
		pull_number,
		initial_delay_seconds,
	)

	merge_pull_request_provider(
		provider,
		config,
		token,
		pull_number,
		config.repo_path,
		config.remote,
		branch,
	)

	target_sha = provider.get_branch_remote_sha(
		str(config.base_url),
		config.owner,
		config.repo,
		token,
		config.main_branch,
	)
	logger.info(f"Main branch '{config.main_branch}' SHA after merge: {target_sha}")

	wait_for_pipeline_provider(provider, config, target_sha, token, "main branch")

	logger.success(f"Workflow finished successfully for branch: {branch}")


def run_watch_loop(provider: ScmProvider, config: AppConfig, token: str) -> None:
	"""Run continuously and process newly created local branches until Ctrl+C."""
	processed_branches: set[str] = set()
	current_branch = get_current_branch(config.repo_path)
	logger.info(f"Current local branch: {current_branch}")

	if current_branch != config.main_branch:
		run_flow_for_branch(provider, config, token, current_branch)
		processed_branches.add(current_branch)
	else:
		logger.info(
			f"Current branch is main '{config.main_branch}'. Waiting for new feature branches..."
		)

	seen_branches = {
		branch
		for branch in get_local_branches(config.repo_path)
		if branch != config.main_branch
	}
	seen_branches.update(processed_branches)

	logger.info(
		f"Watch mode enabled. Monitoring local branches every {config.branch_scan_interval}s. "
		"Press Ctrl+C to stop."
	)
	while True:
		current_branches = {
			branch
			for branch in get_local_branches(config.repo_path)
			if branch != config.main_branch
		}
		new_branches = sorted(current_branches - seen_branches)
		if not new_branches:
			time.sleep(config.branch_scan_interval)
			continue

		for branch in new_branches:
			logger.info(f"Detected new branch: {branch}")
			try:
				run_flow_for_branch(provider, config, token, branch)
			except (
				GiteaAutomationError,
				ApiRequestError,
				ProviderApiError,
				PipelineError,
				CommandExecutionError,
				ValueError,
				json.JSONDecodeError,
			) as error:
				logger.error(f"Branch '{branch}' failed: {error}")
				logger.info("Watcher will continue monitoring for next branches")
			processed_branches.add(branch)

		seen_branches = current_branches | processed_branches


def run_flow(config: AppConfig, token: str) -> None:
	"""Run one-shot or watch mode flow depending on configuration."""
	provider = create_provider(config.provider_name)
	logger.info("Starting automation flow")
	if config.watch_branches:
		run_watch_loop(provider, config, token)
		return

	branch = get_current_branch(config.repo_path)
	logger.info(f"Current local branch: {branch}")
	run_flow_for_branch(provider, config, token, branch)


def main() -> int:
	"""Application entry point.

	Returns:
		Process exit code.
	"""
	parser = build_parser()
	args = parser.parse_args()
	effective_log_mode = resolve_log_mode(args)
	setup_logger(Path(args.log_file), log_mode=effective_log_mode)
	log_project_metadata()
	load_dotenv_file(Path(".env"))

	try:
		logger.debug("CLI arguments parsed successfully")
		config, token = resolve_config(args)
		if config.log_mode != effective_log_mode:
			setup_logger(Path(args.log_file), log_mode=config.log_mode)
		run_flow(config, token)
		return 0
	except KeyboardInterrupt:
		logger.warning("Execution interrupted by user")
		return 130
	except InvalidConfigError as error:
		logger.error(f"Invalid configuration: {error}")
		return 2
	except CommandExecutionError as error:
		logger.error(f"Command execution failed: {error}")
		return 3
	except ApiRequestError as error:
		logger.error(f"Provider API error: {error}")
		return 4
	except ProviderApiError as error:
		logger.error(f"Provider API error: {error}")
		return 4
	except PipelineError as error:
		logger.error(f"Pipeline stage failed: {error}")
		return 5
	except (GiteaAutomationError, ValueError, json.JSONDecodeError) as error:
		logger.error(str(error))
		return 1
	except Exception:
		logger.exception("Unexpected unhandled exception")
		return 99


if __name__ == "__main__":
	raise SystemExit(main())
