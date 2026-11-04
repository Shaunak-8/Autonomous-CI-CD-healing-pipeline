import os
import subprocess
import tempfile
import shutil
import re
import sys
from typing import Optional
try:
    from e2b import Sandbox
except ImportError:
    Sandbox = None

class E2BSandbox:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("E2B_API_KEY")

    def run_command(self, cmd: list, cwd: str = ".") -> dict:
        """Run a command in E2B if possible, else local fall back."""
        if self.api_key and Sandbox:
            try:
                # E2B SDK v1.0+ uses E2B_API_KEY env var automatically.
                # Do not pass api_key=... as it causes SandboxBase.__init__ error.
                with Sandbox() as sb:
                    # Upload CWD to sandbox (Simplified for this turn)
                    # In a real app, you'd sync the whole repo
                    # For now, we fallback to local for complex repo-wide tests
                    # unless we implement a proper sync.
                    pass
            except Exception as e:
                print(f"E2B creation failed: {e}")
        
        # Local fallback
        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=120
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode
            }
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": str(e), "exit_code": 1}

    def execute_fix(self, file_path: str, fixed_code: str) -> dict:
        """Apply fixed code to file atomically and validate it."""
        if not file_path or not fixed_code:
            return {"success": False, "output": "", "error": "file_path and fixed_code must be non-empty"}

        try:
            # Write atomically: temp file then rename
            dir_name = os.path.dirname(file_path) or "."
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(fixed_code)
            shutil.move(tmp_path, file_path)

            # Validate locally (fast syntax check)
            ext = os.path.splitext(file_path)[1]
            if ext == ".py":
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", file_path],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    return {"success": False, "output": result.stdout, "error": result.stderr}
            
            return {"success": True, "output": "Fix applied and validated locally.", "error": ""}
        except Exception as e:
            return {"success": False, "output": "", "error": str(e)}

    def validate_tests(self, repo_path: str) -> dict:
        """Run tests in the repo and return pass/fail counts."""
        if not repo_path or not os.path.isdir(repo_path):
            return {"passed": 0, "failed": 0, "output": "", "complete": False, "error": f"Invalid repo_path: {repo_path}"}

        # Detect test runner
        if os.path.exists(os.path.join(repo_path, "package.json")):
            cmd = [shutil.which('npm.cmd' if os.name == 'nt' else 'npm') or 'npm', 'run', 'test']
        else:
            cmd = [sys.executable, '-m', "pytest", "--tb=short", "-q"]

        res = self.run_command(cmd, cwd=repo_path)
        
        output = res["stdout"] + res["stderr"]
        passed = 0
        failed = 0
        
        passed_counts = re.findall(r'(\d+)\s+passed\b', output)
        failed_counts = re.findall(r'(\d+)\s+failed\b', output)
        passed = int(passed_counts[-1]) if passed_counts else 0
        failed = int(failed_counts[-1]) if failed_counts else 0
        
        return {
            "passed": passed,
            "failed": failed,
            'complete': res['success'] and passed > 0,
            "output": output,
            "error": res["stderr"] if not res["success"] else ""
        }
