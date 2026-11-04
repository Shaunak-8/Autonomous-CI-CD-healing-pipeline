from core.repository import RepositoryInventory
from tools.checks import run_repository_checks
import re
from typing import Optional
from agents.schema import FailureRecord
from api.sse import emit
from whitebox import WhiteBoxAnalyzer

class AnalyzerAgent:
    def __init__(self):
        self.whitebox_report = None
        self.inventory = None
        self.check_report = None

    def run(self, repo_path: str, run_id: str = None):
        """Run all linters and tests, return grouped failures."""
        self.inventory = RepositoryInventory(repo_path)
        self.whitebox_report = None
        
        def _emit(msg, status="ANALYZING"):
            if run_id:
                emit(run_id, "AnalyzerAgent", msg, status)

        summary = self.inventory.summary()
        _emit(f"Detected {summary['size']} repository: {summary['source_files']} source files, "
              f"{summary['source_bytes']} source bytes.", "REPOSITORY_PROFILE")
        if run_id:
            import json
            emit(run_id, 'AnalyzerAgent', json.dumps(summary), 'REPOSITORY_CLASSIFIED')
        self.check_report = run_repository_checks(self.inventory, _emit)
        failures = self.check_report['failures']

        _emit("Running white-box analysis...", "WHITEBOX_ANALYSIS")
        try:
            if self.inventory.size == 'large':
                raise ValueError('Repository exceeds the configured medium repository limits')
            self.whitebox_report = WhiteBoxAnalyzer(repo_path).analyze_repository(
                inventory=self.inventory, run_coverage=False)
            if self.whitebox_report.get("generated_tests"):
                _emit(f"White-box analysis generated {len(self.whitebox_report['generated_tests'])} targeted tests.", "WHITEBOX_ANALYSIS")
        except Exception as exc:
            self.whitebox_report = {
                "enabled": True,
                "status": "PARTIAL",
                "coverage": None,
                "uncovered_paths": [],
                "generated_tests": [],
                "reason": f"White-box analysis failed: {exc}",
            }
            _emit(self.whitebox_report["reason"], "WHITEBOX_ANALYSIS")
        
        # Deduplication and prioritization
        # If a line has a SyntaxError, we prioritize that over logic.
        return self._deduplicate(failures)

    def _tag(self, results, bug_type):
        for r in results:
            if "bug_type" not in r:
                r["bug_type"] = bug_type
        return results

    def _deduplicate(self, failures):
        seen = {}
        for f in failures:
            key = (f['file'], f['bug_type'], f.get('line', 0), f.get('error_message', ''))
            if key not in seen:
                seen[key] = []
            seen[key].append(f)
            
        return [g[0] for g in seen.values()]

def parse_failure(raw_output: str) -> Optional[FailureRecord]:
    file_name = "unknown_file.py"
    line_num = 0
    error_type = "LOGIC"
    message = "Unknown error"
    test_name = None

    # Extract file and line
    file_match = re.search(r'([a-zA-Z0-9_/\\]+\.py):(\d+)', raw_output)
    if file_match:
        file_name = file_match.group(1)
        line_num = int(file_match.group(2))
        
    # Extract test name
    test_match = re.search(r'::(test_[a-zA-Z0-9_]+)', raw_output)
    if test_match:
        test_name = test_match.group(1)
        
    # Classify error
    if "TypeError" in raw_output:
        error_type = "TYPE_ERROR"
    elif "SyntaxError" in raw_output or "error:" in raw_output.lower():
        error_type = "SYNTAX"
    elif "AssertionError" in raw_output:
        error_type = "LOGIC"
        
    # Grab last non-empty line as message
    lines = [line.strip() for line in raw_output.strip().split('\n') if line.strip()]
    if lines:
        message = lines[-1]

    return FailureRecord(
        file=file_name,
        line=line_num,
        error_type=error_type,
        message=message,
        test_name=test_name
    )
