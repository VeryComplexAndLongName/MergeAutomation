# SCM Merge Automation CLI

Unified CLI tool for automating merge workflows across Git platforms.

The product solves the same workflow for different providers: Gitea, GitHub, and GitLab, with the ability to add any other providers. The connection process is described in the section "Add A New Provider In Code (Simple)".

Main flow:

1. Push the current feature branch to remote.
2. Wait for CI/checks for that branch SHA.
3. Find an existing PR/MR or create a new one into the target branch.
4. Wait until the PR/MR is ready for merge.
5. Merge PR/MR.
6. Wait for CI/checks on the main branch after merge.
7. In watch mode, wait for a new local branch (except main) and repeat the cycle.

If a stage fails for a specific branch, the error is logged, and in watch mode the app continues monitoring next branches. Stop the app with Ctrl+C.

In case of errors persist in flow, recommended usge extended logging for better understanding situation (parameter `"log_mode": "extended"` in `config.json`)

## What The Product Does

- Unified CLI interface for `gitea`, `github`, `gitlab`.
- Unified model for CI/check status processing.
- Continuous watch mode: process new branches until Ctrl+C.
- Dry-run mode for safe flow verification without merge.
- Strict-status mode where success requires explicit CI checks.
- Colored console logs and file logs in `logs/app.log`.

## Requirements

- Python 3.12+
- `uv` for dependency and environment management
- Local git repository with a valid `remote`
- Provider token with permissions to read statuses and merge PR/MR

Install dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install uv[all] --upgrade
uv sync
```

## Configuration

By default, runtime configuration is loaded from `config.json` in the repository root.

Example configuration:

```json
{
  "provider": "gitea",
  "base_url": "http://hppiigit:3000",
  "repo_path": "C:/Prog/hyper",
  "remote": "origin",
  "main_branch": "main",
  "poll_interval": 10,
  "branch_scan_interval": 5,
  "timeout_seconds": 1800,
  "merge_style": "merge",
  "pr_title_prefix": "Auto merge",
  "log_mode": "extended",
  "dry_run": false,
  "watch_branches": true,
  "show_check_details": true,
  "strict_status": false
}
```

Fields:

- `provider`: `gitea`, `github`, `gitlab`
- `base_url`: provider API base URL
- `owner`, `repo`: repository coordinates (optional)
: if not set, the app will try to auto-detect them from `git remote` inside `repo_path`.
- `repo_path`: local repository path
- `main_branch`: name of the main branch (not required to be `main`)
- `merge_style`: merge strategy (provider-dependent)
- `log_mode`: logging mode (`basic` or `extended`)
- `watch_branches`: continuous mode until Ctrl+C
- `branch_scan_interval`: local branch scan interval

## Tokens

You can pass `--token` directly or use an environment variable.

- Gitea: `GITEA_TOKEN`
- GitHub: `GITHUB_TOKEN`
- GitLab: `GITLAB_TOKEN`

Examples:

```powershell
$env:GITEA_TOKEN = "your-token"
uv run python gitea.py --provider gitea --base-url http://hppiigit:3000
```

```powershell
$env:GITHUB_TOKEN = "your-token"
uv run python gitea.py --provider github --base-url https://api.github.com
```

```powershell
$env:GITLAB_TOKEN = "your-token"
uv run python gitea.py --provider gitlab --base-url https://gitlab.com
```

## Connect A Provider In 3 Steps

Yes, you can connect different providers.

For already supported providers (Gitea, GitHub, GitLab), the process is simple:

1. Select provider in `config.json`:

```json
{
  "provider": "github",
  "base_url": "https://api.github.com"
}
```

1. Set token in an environment variable:

```powershell
$env:GITHUB_TOKEN = "your-token"
```

1. Run the CLI:

```powershell
uv run python gitea.py
```

If needed, pass the same parameters via CLI:

```powershell
uv run python gitea.py --provider github --base-url https://api.github.com --token your-token
```

Minimum required parameters:

- `provider`
- `base_url`
- token (`--token` or provider env variable)

## CLI

Show all options:

```powershell
uv run python gitea.py --help
```

Important:

- `--provider` selects provider API implementation.
- `--base-url` sets the provider endpoint.
- `--gitea-url` is a compatibility alias for `--base-url`.
- `--main-branch` sets the main branch.
- `--log-mode` switches logging mode (`basic`/`extended`).
- `--watch-branches` enables continuous branch monitoring.
- `--no-watch-branches` runs a one-shot cycle for current branch.

## Add A New Provider In Code (Simple)

If you need a provider that is not supported yet, add it in 4 short steps.

1. Create a new adapter class in [src/gitea_automation/providers.py](src/gitea_automation/providers.py) implementing `ScmProvider`.
2. Implement 7 methods: commit statuses, PR/MR search, PR/MR create, PR/MR get, readiness, merge, branch SHA.
3. Register the adapter in `create_provider` factory in [src/gitea_automation/providers.py](src/gitea_automation/providers.py).
4. Add provider name to `--provider` choices in [src/gitea_automation/cli.py](src/gitea_automation/cli.py) and document token env var in README.

After that, user flow stays the same: `provider + base_url + token`.

## Dry-run Mode

Dry-run does not merge and does not create a new PR/MR.

In this mode:

1. Push feature branch.
2. Check CI/checks for pushed SHA.
3. Search for existing open PR/MR.

Run:

```powershell
uv run python gitea.py --dry-run
```

## Watch Mode

By default, the app runs in watch mode:

1. Processes current branch if it is not main.
1. Switches to local branch monitoring.
1. Automatically starts full workflow when a new branch appears (except main).
1. Does not exit after PR/MR and keeps running until Ctrl+C.

Examples:

```powershell
uv run python gitea.py
```

```powershell
uv run python gitea.py --main-branch develop --branch-scan-interval 3
```

One-shot run (without watching):

```powershell
uv run python gitea.py --no-watch-branches
```

## Strict Status Mode

`--strict-status` requires at least one explicit check/status.
If provider returns `success` but checks are missing, execution fails.

Run:

```powershell
uv run python gitea.py --strict-status
```

Disable:

```powershell
uv run python gitea.py --no-strict-status
```

## Tests And Quality

```powershell
uv run pytest
uv run ruff check .
uv run mypy .
```

## Logging

- `extended` (default): full diagnostics with detailed technical information.
- `basic`: only key events and errors without detailed HTTP diagnostics.
- Configure via `log_mode` field in `config.json` or CLI `--log-mode`.

Examples:

```json
{
  "log_mode": "basic"
}
```

```powershell
uv run python gitea.py --log-mode basic
```
