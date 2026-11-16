import os
import subprocess
import logging
import re
import time
import random
import uuid
from urllib.parse import quote
from typing import Optional
from config import GITHUB_TOKEN

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 120  # seconds


def normalize_repo_url(url: str) -> str:
    """Convert browser URLs like /tree/main into a valid git clone URL."""
    if not url:
        return url

    cleaned = url.strip()
    cleaned = cleaned.replace("\\", "/")

    # Strip common GitHub UI path suffixes like /tree/main, /blob/main, /issues, etc.
    cleaned = re.sub(r"/tree/.*$", "", cleaned)
    cleaned = re.sub(r"/blob/.*$", "", cleaned)
    cleaned = re.sub(r"/pull/.*$", "", cleaned)
    cleaned = re.sub(r"/issues/.*$", "", cleaned)
    cleaned = re.sub(r"/commit/.*$", "", cleaned)

    cleaned = re.sub(r"https://github.com/", "https://github.com/", cleaned)
    cleaned = cleaned.rstrip("/")

    if "github.com" in cleaned and not cleaned.endswith(".git"):
        cleaned = f"{cleaned}.git"

    return cleaned


def run_git_command(repo_path: str, cmd: list, mask_token: str = None, timeout: int = GIT_TIMEOUT) -> tuple:
    """Helper to run git subprocesses with timeout and masked logging."""
    log_cmd = " ".join(cmd)
    if mask_token:
        log_cmd = log_cmd.replace(mask_token, "****")
    
    logger.debug(f"[GIT EXEC] {log_cmd}")
    
    # Sanitized environment that blocks macOS Keychain
    sanitized_env = os.environ.copy()
    sanitized_env["GIT_CONFIG_NOSYSTEM"] = "1"
    sanitized_env["GIT_TERMINAL_PROMPT"] = "0"
    sanitized_env["GIT_ASKPASS"] = ""
    
    try:
        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=True, env=sanitized_env, timeout=timeout)
        return True, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        logger.warning(f"[GIT TIMEOUT] CMD={log_cmd} | timeout={timeout}s")
        return False, "", f"Command timed out after {timeout}s"
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or '').replace(mask_token, '***') if mask_token else (e.stderr or '')
        logger.warning(f"[GIT ERROR] CMD={log_cmd} | EXIT={e.returncode} | STDERR={stderr.strip()}")
        return False, e.stdout or '', stderr
    except OSError:
        return False, '', 'Git could not be executed. Check that Git is installed.'


def generate_branch_name(team: str = "HOGRIDERS", leader: str = "AI") -> str:
    """Requirement 4: Generate clean, uppercase branch name securely."""
    clean_team = re.sub(r'[^A-Z0-9]', '_', team.upper()).strip('_') or "TEAM"
    clean_leader = re.sub(r'[^A-Z0-9]', '_', leader.upper()).strip('_') or "LEADER"
    timestamp = int(time.time())
    return f"{clean_team}_{clean_leader}_AI_FIX_{timestamp}_{uuid.uuid4().hex[:8]}"

def prepare_repo(repo_path: str):
    """
    Requirement 3: Disable Credential Caching.
    Ensures safe, clean state before pushing.
    """
    logger.info("[V3] Preparing repository and unsetting credentials...")
    
    # Scope credential.helper override to this repo only (don't mutate global config)
    run_git_command(repo_path, ["git", "config", "--local", "credential.helper", ""])
    
    logger.info("[V3] Credential helpers disabled.")

def commit_changes(repo_path: str, message: str):
    """Requirement 5: Implement clean Commit Logic.
    Returns: True on successful commit, None if tree is clean, False on error.
    """
    logger.info("[V3] Staging and committing changes...")
    
    # Stage tracked source edits only; generated environments must never be committed.
    add_ok, _, add_err = run_git_command(repo_path, ["git", "add", "--update"])
    if not add_ok:
        logger.error(f"[V3] git add failed: {add_err}")
        return False
    
    # Check if dirty
    status_ok, stdout, status_err = run_git_command(repo_path, ["git", "diff", "--cached", "--name-only"])
    if not status_ok:
        logger.error(f"[V3] git status failed: {status_err}")
        return False
    if not stdout.strip():
        logger.info("[V3] Nothing to commit. Tree is clean.")
        return None
        
    full_message = f"[AI-AGENT] Fix applied: {message}"
    commit_ok, _, commit_err = run_git_command(repo_path, [
        "git", "-c", "user.name=CI Healing Agent", "-c", "user.email=ci-healing@users.noreply.github.com",
        "commit", "-m", full_message])
    
    if commit_ok:
        logger.info(f"[V3] Committed changes successfully. MSG: {full_message}")
        return True
    else:
        logger.error(f"[V3] Commit failed: {commit_err}")
        return False

def classify_error(stderr: str) -> str:
    """Requirement 8: Error Classification."""
    stderr_lower = stderr.lower()
    if "403" in stderr_lower: return "AUTH_ERROR (Forbidden)"
    if "401" in stderr_lower: return "INVALID_TOKEN (Unauthorized)"
    if "404" in stderr_lower: return "REPO_NOT_FOUND"
    
    network_indicators = ["network", "connection", "resolve host", "timed out", "unreachable"]
    if any(ind in stderr_lower for ind in network_indicators):
        return "NETWORK_ERROR"
        
    return "UNKNOWN_GIT_ERROR"

def push_changes(repo_path: str, token: str, branch_name: str, *, allow_existing_branch: bool = False,
                 expected_remote_sha: str = None) -> dict:
    """
    Requirement 1, 2, 4, 6, 7: Core Fault-Tolerant Push Engine.
    100% Subprocess. Direct URL forcing. 3-stage retries.
    Prefer the authenticated user's GitHub OAuth token or personal access token.
    Repository push access depends on the token's permissions.
    """
    # The explicitly supplied user token takes precedence over the server default.
    token = token or GITHUB_TOKEN
    
    # Extract original URL safely
    success, origin_url, err = run_git_command(repo_path, ["git", "remote", "get-url", "origin"])
    origin_url = normalize_repo_url(origin_url.strip())
    
    if not success or not origin_url:
        logger.error("[V3] Could not determine origin URL. Cannot construct Auth URL.")
        return {"status": "failed", "branch": branch_name, "attempts": 0, "error_type": "LOCAL_ERROR", "error": "No origin URL"}

    # Requirement 2: Clean and Construct Auth URL
    clean_url = re.sub(r"https://[^@]+@github.com/", "https://github.com/", origin_url)
    if not clean_url.endswith(".git") and "github.com" in clean_url:
        clean_url = clean_url.rstrip('/') + ".git"
    
    auth_url = clean_url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
    if clean_url.startswith('https://github.com/') and not token:
        return {"status": "failed", "branch": branch_name, "attempts": 0, "error_type": "NO_TOKEN", "error": "Connect a GitHub token with write access to repository contents."}
    current_ok, current, _ = run_git_command(repo_path, ['git', 'branch', '--show-current'])
    if not current_ok or current.strip() != branch_name:
        return {'status': 'failed', 'branch': branch_name, 'attempts': 0, 'error_type': 'BRANCH_ERROR',
                'error': 'The working copy is not on the new fix branch. Push was stopped.'}
    head_ok, head, _ = run_git_command(repo_path, ['git', 'rev-parse', 'HEAD'])
    if not head_ok:
        return {'status': 'failed', 'branch': branch_name, 'attempts': 0, 'error_type': 'LOCAL_ERROR', 'error': 'Cannot read the fix commit.'}
    commit_sha = head.strip()
    remote_ref = f'refs/heads/{branch_name}'
    masked_url = re.sub(r"https://[^@]+@", "https://***@", auth_url)

    for attempt in range(1, 4):
        logger.info(f"--- [V3] Git Push Attempt {attempt}/3 ---")
        
        # Requirement 6: Mandatory Logging
        logger.info(f"[V3] PUSHING TO: {masked_url}")
        
        # Check before every attempt: never update a pre-existing branch. A matching
        # SHA on retry means the earlier push reached the remote despite a lost reply.
        listed, refs, stderr = run_git_command(repo_path, ['git', 'ls-remote', '--heads', auth_url, remote_ref], mask_token=token)
        if listed and refs.strip():
            remote_sha = refs.split()[0]
            if remote_sha == commit_sha:
                return _push_success(clean_url, branch_name, commit_sha, attempt)
            if allow_existing_branch and expected_remote_sha and remote_sha == expected_remote_sha:
                # This branch was created and verified earlier in this run. A normal
                # (non-force) push below can only fast-forward it from that commit.
                pass
            elif allow_existing_branch:
                return {'status': 'failed', 'branch': branch_name, 'attempts': attempt,
                        'error_type': 'REMOTE_BRANCH_CHANGED',
                        'error': 'The remote fix branch changed after the last verified push. Refusing to overwrite it.'}
            elif attempt > 1 and remote_sha == commit_sha:
                return _push_success(clean_url, branch_name, commit_sha, attempt)
            else:
                return {'status': 'failed', 'branch': branch_name, 'attempts': attempt, 'error_type': 'BRANCH_EXISTS',
                        'error': 'The remote fix branch already exists. Start a new run to create a fresh branch.'}
        cmd = ["git", "push", auth_url, f"HEAD:{remote_ref}"]
        success, stdout, stderr = run_git_command(repo_path, cmd, mask_token=token) if listed else (False, '', stderr)
        
        if success:
            verified, refs, stderr = run_git_command(repo_path, ['git', 'ls-remote', '--heads', auth_url, remote_ref], mask_token=token)
            if verified and refs.strip() and refs.split()[0] == commit_sha:
                logger.info(f"[V3] [SUCCESS] Verified remote commit on attempt {attempt}")
                return _push_success(clean_url, branch_name, commit_sha, attempt)
            if verified:
                stderr = 'The remote branch does not match the local fix commit.'
            error_type = 'VERIFICATION_ERROR'
            
        else:
            error_type = classify_error(stderr)
            logger.error(f"[V3] [FAILURE] Attempt {attempt} failed: {error_type} | Output: {stderr}")
            
        if attempt < 3 and error_type not in {'AUTH_ERROR (Forbidden)', 'INVALID_TOKEN (Unauthorized)', 'REPO_NOT_FOUND'}:
            logger.info("[V3] [RECOVERY] Retrying the same fix branch...")
            prepare_repo(repo_path)
            time.sleep((2 ** attempt) + random.uniform(0.1, 0.5))
        else:
            return {
                    "status": "failed",
                    "branch": branch_name,
                    "attempts": attempt,
                    "error_type": error_type,
                    "error": stderr
            }


def _push_success(repo_url, branch, commit_sha, attempts):
    result = {'status': 'success', 'branch': branch, 'commit_sha': commit_sha,
              'verified': True, 'attempts': attempts}
    if repo_url.startswith('https://github.com/'):
        base = repo_url.removesuffix('.git')
        result.update(branch_url=f'{base}/tree/{quote(branch, safe="")}', commit_url=f'{base}/commit/{commit_sha}')
    return result

def commit_and_push_all(repo_path: str, message: str, branch_name: str = None, token: str = None) -> dict:
    """Create a fresh branch, commit validated edits, push, and verify the remote SHA."""
    try:
        logger.info("[V3] Initiating Senior DevOps Push Pipeline...")
        # Prefer the connected user's token over the server default.
        token = token or GITHUB_TOKEN
        
        if not branch_name:
            branch_name = generate_branch_name()
            
        prepare_repo(repo_path)
        valid, _, _ = run_git_command(repo_path, ['git', 'check-ref-format', '--branch', branch_name])
        if not valid or branch_name.lower() in {'main', 'master', 'develop', 'development'}:
            return {'status': 'failed', 'branch': branch_name, 'attempts': 0, 'error_type': 'BRANCH_ERROR',
                    'error': 'Publishing requires a new fix branch.'}
        created, _, _ = run_git_command(repo_path, ['git', 'checkout', '-b', branch_name])
        if not created:
            return {'status': 'failed', 'branch': branch_name, 'attempts': 0, 'error_type': 'BRANCH_ERROR',
                    'error': 'Could not create a fresh fix branch. No commit or push was attempted.'}
        commit_result = commit_changes(repo_path, message)
        
        if commit_result is False:
            return {"status": "failed", "branch": branch_name, "attempts": 0, "error_type": "COMMIT_ERROR", "error": "Local git commit failed"}
        if commit_result is None:
            return {'status': 'nothing_to_commit', 'branch': branch_name, 'attempts': 0}
        
        return push_changes(repo_path, token, branch_name)

    except Exception as e:
        logger.exception(f"[V3] CRITICAL_SYSTEM_ERROR in push pipeline: {e}")
        return {
            "status": "failed",
            "branch": branch_name or "unknown",
            "attempts": 0,
            "error_type": "CRITICAL_SYSTEM_ERROR",
            "error": str(e)
        }


def commit_and_push_batch(repo_path: str, message: str, branch_name: str = None, token: str = None,
                          expected_remote_sha: str = None) -> dict:
    """Publish one validated batch without ever force-updating a remote branch.

    The first batch receives a new branch. Later batches may extend that branch only
    when it still points at the commit verified by the preceding batch in this run.
    """
    if not branch_name:
        return commit_and_push_all(repo_path, message, token=token)

    try:
        prepare_repo(repo_path)
        current_ok, current, _ = run_git_command(repo_path, ['git', 'branch', '--show-current'])
        if not current_ok or current.strip() != branch_name:
            return {'status': 'failed', 'branch': branch_name, 'attempts': 0, 'error_type': 'BRANCH_ERROR',
                    'error': 'The working copy is not on the validated fix branch. Push was stopped.'}
        if not expected_remote_sha:
            return {'status': 'failed', 'branch': branch_name, 'attempts': 0, 'error_type': 'PUBLISH_STATE_ERROR',
                    'error': 'Missing the previously verified commit SHA; refusing to update the branch.'}
        commit_result = commit_changes(repo_path, message)
        if commit_result is False:
            return {'status': 'failed', 'branch': branch_name, 'attempts': 0, 'error_type': 'COMMIT_ERROR',
                    'error': 'Local git commit failed'}
        if commit_result is None:
            return {'status': 'nothing_to_commit', 'branch': branch_name, 'attempts': 0,
                    'commit_sha': expected_remote_sha, 'verified': True}
        return push_changes(repo_path, token or GITHUB_TOKEN, branch_name, allow_existing_branch=True,
                            expected_remote_sha=expected_remote_sha)
    except Exception as exc:
        logger.exception('[V3] Batch publish failed')
        return {'status': 'failed', 'branch': branch_name, 'attempts': 0,
                'error_type': 'CRITICAL_SYSTEM_ERROR', 'error': str(exc)}

def clone_repo(url: str, path: str, token: str = None):
    """Clone one working copy and remove credentials from its saved origin URL."""
    effective_token = token or GITHUB_TOKEN
    clean_url = normalize_repo_url(url)
    clean_url = re.sub(r"https://[^@]+@github.com/", "https://github.com/", clean_url)
    auth_url = clean_url
    if effective_token and clean_url.startswith('https://github.com/'):
        auth_url = clean_url.replace('https://github.com/', f'https://x-access-token:{effective_token}@github.com/')
    sanitized_env = os.environ.copy()
    sanitized_env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '',
                          'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': ''})
    if os.path.isdir(os.path.join(path, '.git')):
        ok, _, _ = run_git_command(path, ['git', 'remote', 'set-url', 'origin', clean_url])
        if not ok:
            raise RuntimeError('Could not update the saved repository origin')
        return True
    if os.path.exists(path) and os.listdir(path):
        raise RuntimeError('The clone directory is incomplete. Start a new run with a fresh workspace.')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        subprocess.run(['git', 'clone', '--depth', '1', auth_url, path],
                       check=True, capture_output=True, text=True, env=sanitized_env, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'Cloning exceeded {GIT_TIMEOUT}s. Check network access or repository size.') from None
    except subprocess.CalledProcessError as exc:
        message = exc.stderr or 'Git clone failed'
        if effective_token:
            message = message.replace(effective_token, '***')
        raise RuntimeError(message[-2000:]) from None
    except OSError:
        raise RuntimeError('Git could not be executed. Check that Git is installed.') from None
    ok, _, _ = run_git_command(path, ['git', 'remote', 'set-url', 'origin', clean_url])
    if not ok:
        raise RuntimeError('Could not save a credential-free repository origin')
    return True
