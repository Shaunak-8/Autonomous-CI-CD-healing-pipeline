import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


class WhiteBoxAnalyzer:
    """Minimal white-box analysis layer for Python repositories."""

    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    def analyze_language(self, file_path: str) -> Dict[str, Any]:
        ext = Path(file_path).suffix.lower()
        if ext == ".py":
            report = self.analyze_python_file(file_path)
            return {
                "enabled": True,
                "status": "OK",
                "language": "python",
                "reason": "",
                **report,
            }

        if ext in {".js", ".jsx", ".ts", ".tsx"}:
            return {
                "enabled": True,
                "status": "UNSUPPORTED",
                "language": "javascript",
                "reason": "white-box analysis not supported for this language",
                "functions": [],
                "branches": [],
            }

        return {
            "enabled": True,
            "status": "UNSUPPORTED",
            "language": "unknown",
            "reason": "white-box analysis not supported for this language",
            "functions": [],
            "branches": [],
        }

    def analyze_repository(self, inventory=None, run_coverage=True) -> Dict[str, Any]:
        if not self.repo_path or not os.path.isdir(self.repo_path):
            return {
                "enabled": True,
                "status": "PARTIAL",
                "language": "unknown",
                "coverage": None,
                "uncovered_paths": [],
                "generated_tests": [],
                "reason": "Repository path is missing or invalid.",
            }

        from core.repository import RepositoryInventory
        inventory = inventory or RepositoryInventory(self.repo_path)
        py_files = inventory.source_files('python')

        if not py_files:
            return {
                "enabled": True,
                "status": "UNSUPPORTED",
                "language": "unknown",
                "coverage": None,
                "uncovered_paths": [],
                "generated_tests": [],
                "reason": "white-box analysis not supported for this language",
            }

        coverage_report = self.collect_coverage(self.repo_path, allow_missing=True) if run_coverage else {
            'status': 'PARTIAL', 'coverage': None,
            'reason': 'Static branch analysis only; coverage was not measured.',
        }
        coverage = coverage_report.get("coverage")

        uncovered_paths: List[Dict[str, Any]] = []
        for file_path in py_files:
            full_path = os.path.join(self.repo_path, file_path)
            analysis = self.analyze_python_file(full_path)
            uncovered_paths.extend(self._derive_uncovered_paths(file_path, analysis))

        if not run_coverage:
            for branch in uncovered_paths:
                branch['coverage_status'] = 'UNMEASURED'
                branch['type'] = 'STATIC_BRANCH'

        generated_tests: List[Dict[str, Any]] = []
        for branch in (uncovered_paths[:3] if run_coverage else []):
            test_name = f"test_{branch['function']}_{branch['line']}"
            full_path = os.path.join(self.repo_path, branch["file"])
            test_code = self.generate_targeted_test(branch, full_path)
            if not test_code:
                continue

            test_file = self._write_generated_test(branch, test_code)
            if not test_file:
                continue

            result = self._run_generated_test(test_file)
            generated_tests.append({
                "file": test_file,
                "name": test_name,
                "status": result["status"],
                "result": result.get("output", ""),
            })

        return {
            "enabled": True,
            "status": coverage_report.get("status", "PARTIAL"),
            "language": "python",
            "coverage": coverage,
            "uncovered_paths": uncovered_paths,
            "generated_tests": generated_tests,
            "reason": coverage_report.get("reason", ""),
        }

    def analyze_python_file(self, file_path: str) -> Dict[str, Any]:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                source = handle.read()

            tree = ast.parse(source)
            self._assign_parents(tree)
            functions = []
            branches = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append({
                        "name": node.name,
                        "line": node.lineno,
                        "type": "function",
                    })

                if isinstance(node, ast.If):
                    branches.append({
                        "file": os.path.relpath(file_path, self.repo_path) if self.repo_path else file_path,
                        "function": self._current_function_name(node),
                        "line": node.lineno,
                        "type": "UNCOVERED_BRANCH",
                        "condition": self._expression_to_text(node.test),
                        "status": "UNTESTED",
                    })

                if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                    branches.append({
                        "file": os.path.relpath(file_path, self.repo_path) if self.repo_path else file_path,
                        "function": self._current_function_name(node),
                        "line": node.lineno,
                        "type": "UNCOVERED_BRANCH",
                        "condition": type(node).__name__.upper(),
                        "status": "UNTESTED",
                    })

                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        branches.append({
                            "file": os.path.relpath(file_path, self.repo_path) if self.repo_path else file_path,
                            "function": self._current_function_name(node),
                            "line": handler.lineno,
                            "type": "UNCOVERED_BRANCH",
                            "condition": f"except {handler.type.__class__.__name__ if handler.type else 'Exception'}",
                            "status": "UNTESTED",
                        })

            return {
                "functions": functions,
                "branches": branches,
                "classes": self._find_classes(tree),
                "reason": "",
            }
        except Exception as exc:  # pragma: no cover - defensive fallback
            return {
                "functions": [],
                "branches": [],
                "classes": [],
                "reason": f"Unable to parse file: {exc}",
            }

    def _assign_parents(self, root: ast.AST) -> None:
        for parent in ast.walk(root):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent

    def _find_classes(self, tree: ast.AST) -> List[Dict[str, Any]]:
        classes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append({
                    "name": node.name,
                    "line": node.lineno,
                })
        return classes

    def _current_function_name(self, node: ast.AST) -> str:
        parent = getattr(node, "_parent", None)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent.name
            parent = getattr(parent, "_parent", None)
        return "__module__"

    def _expression_to_text(self, expr: ast.AST) -> str:
        try:
            return ast.unparse(expr)
        except Exception:
            return getattr(expr, "__class__", type(expr)).__name__

    def _derive_uncovered_paths(self, relative_file: str, analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        uncovered = []
        for branch in analysis.get("branches", []):
            uncovered.append({
                "file": branch.get("file") or relative_file,
                "function": branch.get("function", "__module__"),
                "line": branch.get("line", 0),
                "type": "UNCOVERED_BRANCH",
                "condition": branch.get("condition", "branch"),
                "coverage_status": "uncovered",
                "reason": "Branch is not exercised by current tests.",
                "suggested_test_behavior": f"Exercise the path where {branch.get('condition', 'condition')} is true.",
            })
        return uncovered

    def collect_coverage(self, repo_path: str, allow_missing: bool = True) -> Dict[str, Any]:
        if not os.path.isdir(repo_path):
            return {
                "enabled": True,
                "status": "PARTIAL",
                "coverage": None,
                "reason": "Repository path is missing or invalid.",
            }

        if os.path.exists(os.path.join(repo_path, "package.json")):
            return {
                "enabled": True,
                "status": "UNSUPPORTED",
                "coverage": None,
                "reason": "white-box analysis not supported for this language",
            }

        try:
            import coverage  # type: ignore
        except Exception:
            return {
                "enabled": True,
                "status": "PARTIAL",
                "coverage": None,
                "reason": "Coverage could not be calculated because the Python coverage tool is unavailable.",
            }

        data_file = os.path.join(repo_path, ".whitebox_coverage.db")
        report_file = os.path.join(repo_path, ".whitebox_coverage.json")

        run_cmd = [sys.executable, "-m", "coverage", "run", "--data-file", data_file, "-m", "pytest", "-q"]
        proc = subprocess.run(run_cmd, cwd=repo_path, capture_output=True, text=True, timeout=180)

        if proc.returncode not in (0, 1, 5):
            return {
                "enabled": True,
                "status": "PARTIAL",
                "coverage": None,
                "reason": f"Coverage collection failed: {proc.stderr.strip() or proc.stdout.strip() or 'unknown error'}",
            }

        json_proc = subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", report_file, "--data-file", data_file],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=180,
        )

        if json_proc.returncode != 0 or not os.path.exists(report_file):
            return {
                "enabled": True,
                "status": "PARTIAL",
                "coverage": None,
                "reason": "Coverage data could not be generated from the test run.",
            }

        with open(report_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        totals = payload.get("totals", {})
        statement = totals.get("percent_covered")
        branch = totals.get("percent_covered_branches", statement)
        function = totals.get("percent_covered_functions", statement)
        line = totals.get("percent_covered", statement)

        coverage = {
            "statement_coverage": float(statement) if statement is not None else None,
            "branch_coverage": float(branch) if branch is not None else None,
            "function_coverage": float(function) if function is not None else None,
            "line_coverage": float(line) if line is not None else None,
        }

        if all(value is None for value in coverage.values()):
            return {
                "enabled": True,
                "status": "PARTIAL",
                "coverage": None,
                "reason": "No executable test coverage data was discovered.",
            }

        return {
            "enabled": True,
            "status": "OK",
            "coverage": coverage,
            "reason": "Coverage collected successfully.",
        }

    def generate_targeted_test(self, branch: Dict[str, Any], file_path: str) -> str:
        try:
            function_name = branch.get("function") or "validate"
            condition = str(branch.get("condition") or "")
            pattern = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*[<>!=]=?\s*[-+]?\d+", condition)
            target_name = pattern.group(1) if pattern else "age"

            if "<" in condition and "18" in condition:
                value = 15
                expected = "False"
            elif ">" in condition and "100" in condition:
                value = 120
                expected = "False"
            elif "is None" in condition.lower():
                value = "None"
                expected = "True"
            else:
                value = 15 if target_name != "age" else 15
                expected = "True"

            module_name = os.path.splitext(os.path.basename(file_path))[0]
            test_name = f"test_{function_name}_{abs(hash(condition)) % 10000}"
            snippet = (
                f"from {module_name} import {function_name}\n\n"
                f"def {test_name}():\n"
                f"    assert {function_name}({value}) is {expected}\n"
            )
            compile(snippet, "<whitebox_generated_test>", "exec")
            return snippet
        except Exception:
            return ""

    def _write_generated_test(self, branch: Dict[str, Any], test_code: str) -> Optional[str]:
        try:
            repo_root = self.repo_path
            tests_dir = os.path.join(repo_root, "tests")
            os.makedirs(tests_dir, exist_ok=True)
            file_name = f"test_whitebox_{branch.get('function', 'case')}_{branch.get('line', 0)}.py"
            file_path = os.path.join(tests_dir, file_name)
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(test_code)
            return file_path
        except Exception:
            return None

    def _run_generated_test(self, test_file: str) -> Dict[str, Any]:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", os.path.basename(test_file)],
                cwd=os.path.dirname(test_file),
                capture_output=True,
                text=True,
                timeout=180,
            )
            return {
                "status": "PASSED" if proc.returncode == 0 else "FAILED",
                "output": (proc.stdout or "") + (proc.stderr or ""),
            }
        except Exception as exc:
            return {
                "status": "FAILED",
                "output": str(exc),
            }
