"""
System-Wide Upgrade Manager (``src/updater.py``).

Upgrades the globally installed Research Agent CLI and MCP servers (`ra-cli`, `ra-mcp-*`)
from the official GitHub repository (`https://github.com/ahmeterdem1/research-agent`) or
Homebrew without modifying any local development checkout.
"""

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Literal, Optional

import requests

from config import settings
from config.settings import get_logger

logger = get_logger(__name__)

DEFAULT_GITHUB_REPO = os.getenv("RA_GITHUB_REPO", "ahmeterdem1/research-agent")
DEFAULT_GITHUB_URL = f"https://github.com/{DEFAULT_GITHUB_REPO}"
HOMEBREW_FORMULA = "research-agent"


@dataclass
class UpgradeResult:
    upgraded: bool
    method: Literal["uv", "homebrew"]
    local_version: str
    remote_version: Optional[str]
    local_sha: Optional[str]
    remote_sha: Optional[str]
    message: str


def _parse_version(version_str: str) -> tuple[int, ...]:
    cleaned = version_str.strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    return tuple(int(p) for p in parts) if parts else (0, 0, 0)


def get_local_version() -> str:
    """Return the currently running version of `research-agent`."""
    pyproject = settings.PROJECT_ROOT / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            ver = data.get("project", {}).get("version")
            if ver:
                return str(ver).strip()
        except Exception:
            pass
    try:
        return importlib.metadata.version("research-agent")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def get_install_receipt_path() -> Path:
    return settings.DATA_DIR / "install_receipt.json"


def load_install_receipt() -> dict:
    path = get_install_receipt_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Fallback: inspect PEP 610 direct_url.json written by `uv tool install git+...`
    try:
        dist = importlib.metadata.distribution("research-agent")
        raw_direct = dist.read_text("direct_url.json")
        if raw_direct:
            direct_data = json.loads(raw_direct)
            commit_id = (direct_data.get("vcs_info") or {}).get("commit_id")
            if commit_id:
                return {"sha": commit_id, "source": "direct_url"}
    except Exception:
        pass
    return {}


def save_install_receipt(receipt: dict) -> None:
    path = get_install_receipt_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write install receipt to %s: %s", path, e)


def detect_install_method(explicit_method: Optional[str] = None) -> Literal["uv", "homebrew"]:
    """
    Detect whether the system-wide installation is managed by Homebrew or `uv tool`.
    Supports future Homebrew formula distribution automatically.
    """
    override = (explicit_method or os.getenv("RA_INSTALL_METHOD", "")).strip().lower()
    if override in ("homebrew", "brew"):
        return "homebrew"
    if override == "uv":
        return "uv"

    ra_bin = shutil.which("ra-cli") or sys.executable
    resolved_bin = str(Path(ra_bin).resolve()) if ra_bin else ""
    if "/Cellar/research-agent" in resolved_bin or "/opt/homebrew/opt/research-agent" in resolved_bin:
        return "homebrew"
    return "uv"


def _parse_version_from_pyproject_bytes(raw_bytes: bytes) -> Optional[str]:
    try:
        parsed = tomllib.loads(raw_bytes.decode("utf-8"))
        v = parsed.get("project", {}).get("version")
        return str(v).strip() if v else None
    except Exception:
        return None


def _fetch_via_gh_cli(repo: str, ref: str) -> dict:
    """Use `gh api` if the user is authenticated with GitHub CLI (supports private repos)."""
    if not shutil.which("gh"):
        return {}
    try:
        # 1. Fetch remote pyproject.toml
        cmd_contents = ["gh", "api", f"repos/{repo}/contents/pyproject.toml?ref={ref}"]
        res_contents = subprocess.run(cmd_contents, capture_output=True, text=True, timeout=10)
        remote_version: Optional[str] = None
        if res_contents.returncode == 0:
            payload = json.loads(res_contents.stdout)
            b64_content = payload.get("content", "")
            if b64_content:
                remote_version = _parse_version_from_pyproject_bytes(base64.b64decode(b64_content))

        # 2. Fetch remote commit SHA
        cmd_sha = ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"]
        res_sha = subprocess.run(cmd_sha, capture_output=True, text=True, timeout=10)
        remote_sha = res_sha.stdout.strip() if res_sha.returncode == 0 else None

        if remote_version or remote_sha:
            return {"version": remote_version, "sha": remote_sha, "source": "gh_cli"}
    except Exception as e:
        logger.debug("gh api check failed for %s: %s", repo, e)
    return {}


def _fetch_via_github_rest(repo: str, ref: str) -> dict:
    """Query GitHub REST API (uses GITHUB_TOKEN / GH_TOKEN if set for private repos)."""
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "research-agent-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    remote_version: Optional[str] = None
    remote_sha: Optional[str] = None
    try:
        r_file = requests.get(
            f"https://api.github.com/repos/{repo}/contents/pyproject.toml",
            params={"ref": ref},
            headers=headers,
            timeout=10,
        )
        if r_file.status_code == 200:
            b64 = r_file.json().get("content", "")
            if b64:
                remote_version = _parse_version_from_pyproject_bytes(base64.b64decode(b64))

        r_sha = requests.get(
            f"https://api.github.com/repos/{repo}/commits/{ref}",
            headers=headers,
            timeout=10,
        )
        if r_sha.status_code == 200:
            remote_sha = r_sha.json().get("sha")
    except Exception as e:
        logger.debug("GitHub REST check failed for %s: %s", repo, e)

    if remote_version or remote_sha:
        return {"version": remote_version, "sha": remote_sha, "source": "github_rest"}
    return {}


def _fetch_via_git_ls_remote(repo: str, ref: str) -> dict:
    """Fallback using `git ls-remote` over HTTPS or SSH (works with private repo SSH keys/cred helpers)."""
    if not shutil.which("git"):
        return {}
    urls = [
        f"https://github.com/{repo}.git",
        f"git@github.com:{repo}.git",
    ]
    for url in urls:
        try:
            res = subprocess.run(
                ["git", "ls-remote", url, ref, "refs/tags/v*"],
                capture_output=True,
                text=True,
                timeout=12,
            )
            if res.returncode != 0 or not res.stdout.strip():
                continue

            remote_sha: Optional[str] = None
            highest_tag_ver: Optional[str] = None
            for line in res.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                sha, refname = parts[0], parts[1]
                if refname == ref or refname.endswith(f"/{ref}") or refname == "HEAD":
                    remote_sha = sha
                elif refname.startswith("refs/tags/v") and not refname.endswith("^{}"):
                    tag_v = refname.split("refs/tags/v")[-1]
                    if highest_tag_ver is None or _parse_version(tag_v) > _parse_version(highest_tag_ver):
                        highest_tag_ver = tag_v

            if remote_sha or highest_tag_ver:
                return {"version": highest_tag_ver, "sha": remote_sha, "source": "git_ls_remote"}
        except Exception as e:
            logger.debug("git ls-remote failed for %s: %s", url, e)
    return {}


def fetch_remote_release_info(repo: str = DEFAULT_GITHUB_REPO, ref: str = "HEAD") -> dict:
    """
    Query GitHub for the latest version and commit SHA of `repo` at `ref`.
    Tries `gh api` -> GitHub REST API -> `git ls-remote`.
    """
    for fetcher in (_fetch_via_gh_cli, _fetch_via_github_rest, _fetch_via_git_ls_remote):
        info = fetcher(repo, ref)
        if info.get("version") or info.get("sha"):
            return info
    return {"version": None, "sha": None, "source": "unreachable"}


def is_upgrade_needed(
    local_version: str,
    remote_version: Optional[str],
    local_sha: Optional[str] = None,
    remote_sha: Optional[str] = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Determine whether the system-wide installation should be upgraded."""
    if force:
        return True, "Forced upgrade requested (--force)."

    if remote_version and _parse_version(remote_version) != _parse_version(local_version):
        return True, f"Remote version ({remote_version}) differs from installed version ({local_version})."

    if remote_sha and local_sha and remote_sha != local_sha:
        return True, f"New commit available on GitHub ({remote_sha[:8]} vs installed {local_sha[:8]})."

    if remote_sha and local_sha is None:
        return True, f"No prior install receipt found; installing remote commit {remote_sha[:8]}."

    if remote_version is None and remote_sha is None:
        return False, "Could not reach GitHub to verify remote version (use --force to install anyway)."

    return False, f"Already up to date (version {local_version})."


def _run_uv_global_install(repo: str, ref: str) -> tuple[bool, str]:
    """
    Install the package globally via `uv tool install --force --reinstall` directly from GitHub.
    Never modifies any local development directory.
    """
    ref_suffix = f"@{ref}" if ref and ref != "HEAD" else ""
    candidates = [
        f"git+https://github.com/{repo}.git{ref_suffix}",
        f"git+ssh://git@github.com/{repo}.git{ref_suffix}",
    ]
    last_err = ""
    for git_target in candidates:
        cmd = ["uv", "tool", "install", "--force", "--reinstall", git_target]
        logger.info("Running system-wide upgrade: %s", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True, f"Installed system-wide via `{' '.join(cmd)}`"
        last_err = (res.stderr or res.stdout or "").strip()
        logger.debug("uv tool install failed for %s: %s", git_target, last_err)

    return False, f"`uv tool install` failed: {last_err}"


def _run_homebrew_upgrade(formula: str = HOMEBREW_FORMULA) -> tuple[bool, str]:
    """Upgrade via Homebrew when distributed as a brew formula."""
    logger.info("Running Homebrew upgrade for %s", formula)
    subprocess.run(["brew", "update"], capture_output=True, text=True)
    res = subprocess.run(["brew", "upgrade", formula], capture_output=True, text=True)
    if res.returncode == 0:
        return True, f"Upgraded `{formula}` via Homebrew."
    err = (res.stderr or res.stdout or "").strip()
    return False, f"`brew upgrade {formula}` failed: {err}"


def _refresh_opencode_integration() -> None:
    """Refresh `~/.config/opencode/` using the newly installed `ra-cli` binary."""
    ra_bin = shutil.which("ra-cli")
    if ra_bin:
        try:
            subprocess.run([ra_bin, "install-opencode", "--no-migrate"], check=False, capture_output=True, text=True)
            return
        except Exception as e:
            logger.debug("Subprocess `ra-cli install-opencode` failed: %s", e)

    from src.cli import install_opencode

    install_opencode(migrate_repo_data=False)


def run_upgrade(
    repo: str = DEFAULT_GITHUB_REPO,
    ref: str = "HEAD",
    method: Optional[str] = None,
    check_only: bool = False,
    force: bool = False,
) -> UpgradeResult:
    """
    Check the remote GitHub repository (`ahmeterdem1/research-agent`) and upgrade the
    system-wide installation (`uv tool` or `homebrew`) if a newer version/commit exists.
    Never modifies the local coding workspace.
    """
    install_method = detect_install_method(method)
    local_ver = get_local_version()
    receipt = load_install_receipt()
    local_sha = receipt.get("sha")

    remote_info = fetch_remote_release_info(repo=repo, ref=ref)
    remote_ver = remote_info.get("version")
    remote_sha = remote_info.get("sha")

    needs_up, reason = is_upgrade_needed(
        local_version=local_ver,
        remote_version=remote_ver,
        local_sha=local_sha,
        remote_sha=remote_sha,
        force=force,
    )

    if check_only:
        status_msg = f"Upgrade available: {reason}" if needs_up else reason
        return UpgradeResult(
            upgraded=False,
            method=install_method,
            local_version=local_ver,
            remote_version=remote_ver,
            local_sha=local_sha,
            remote_sha=remote_sha,
            message=status_msg,
        )

    if not needs_up:
        return UpgradeResult(
            upgraded=False,
            method=install_method,
            local_version=local_ver,
            remote_version=remote_ver,
            local_sha=local_sha,
            remote_sha=remote_sha,
            message=reason,
        )

    if install_method == "homebrew":
        ok, msg = _run_homebrew_upgrade()
    else:
        ok, msg = _run_uv_global_install(repo=repo, ref=ref)

    if not ok:
        return UpgradeResult(
            upgraded=False,
            method=install_method,
            local_version=local_ver,
            remote_version=remote_ver,
            local_sha=local_sha,
            remote_sha=remote_sha,
            message=f"Upgrade failed: {msg}",
        )

    save_install_receipt(
        {
            "version": remote_ver or local_ver,
            "sha": remote_sha,
            "repo": repo,
            "method": install_method,
            "upgraded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _refresh_opencode_integration()

    target_ver_label = remote_ver or (remote_sha[:8] if remote_sha else "latest")
    return UpgradeResult(
        upgraded=True,
        method=install_method,
        local_version=local_ver,
        remote_version=remote_ver,
        local_sha=local_sha,
        remote_sha=remote_sha,
        message=(
            f"Successfully upgraded system-wide installation ({install_method}) "
            f"from {local_ver} -> {target_ver_label} and refreshed OpenCode templates."
        ),
    )

