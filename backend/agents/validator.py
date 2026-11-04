from tools.sandbox import E2BSandbox
import tempfile
import subprocess
import os
import sys
from api.enums import IterationStatusEnum
from core.repository import RepositoryInventory
from tools.checks import run_repository_checks

class ValidatorAgent:
    def __init__(self):
        self.sandbox = E2BSandbox()

    def validate(self, repo_path: str, bug_type: str = None) -> dict:
        """Re-run tests to see if the fix actually worked."""
        
        res = run_repository_checks(RepositoryInventory(repo_path))
        status = IterationStatusEnum.passed if res['passed'] else IterationStatusEnum.failed
        return {
            "status": status.value,
            "remaining_failures": len(res['failures']),
            "output": '\n'.join(f"{c['name']}: {c['status']}" for c in res['checks']),
            "check_report": res,
        }

def validate_fix(fix: str, failure) -> bool:
    """Validate a generated fix by checking syntax and basic correctness."""
    # 1. Non-empty check
    if not fix or fix.strip() == "" or fix.strip() == "pass":
        return False

    # 2. Syntax check — write to temp file and py_compile
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write(fix)
        
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", tmp_path],
            capture_output=True, text=True, timeout=10
        )
        os.unlink(tmp_path)
        
        if result.returncode != 0:
            print(f"Syntax validation failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"Validation error: {e}")
        # Clean up temp file if it exists
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False

    # 3. Basic relevance check — fix should not be identical to error message
    if hasattr(failure, 'message') and fix.strip() == failure.message.strip():
        return False

    return True

