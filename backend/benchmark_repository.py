"""Generate a local medium repository and measure real analysis without an LLM."""
import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from core.context_builder import ContextBuilder
from core.project_analyzer import ProjectAnalyzer
from core.repository import RepositoryInventory
from tools.checks import run_repository_checks
from whitebox import WhiteBoxAnalyzer


def benchmark(file_count, run_checks=False, inject_error=False, functions_per_file=20):
    if not 2 <= file_count <= 5000:
        raise ValueError('Choose between 2 and 5000 source files')
    if not 1 <= functions_per_file <= 100:
        raise ValueError('Choose between 1 and 100 functions per file')
    with tempfile.TemporaryDirectory(prefix='healing-benchmark-') as directory:
        root = Path(directory)
        (root / 'base.py').write_text('def base() -> int:\n    return 1\n', encoding='utf-8')
        for index in range(file_count - 2):
            content = 'from base import base\n'
            for function in range(functions_per_file):
                content += (f'\n\ndef value_{index}_{function}() -> int:\n'
                            f'    return base() + {index + function}\n')
            (root / f'module_{index}.py').write_text(
                content, encoding='utf-8')
        (root / 'test_base.py').write_text(
            'from base import base\n\n\ndef test_base() -> None:\n    assert base() == 1\n', encoding='utf-8')
        if inject_error:
            (root / 'module_0.py').write_text('value = missing_name\n', encoding='utf-8')
        started = time.perf_counter()
        tracemalloc.start()
        inventory = RepositoryInventory(root)
        graph = ProjectAnalyzer(str(root)).analyze()
        whitebox = WhiteBoxAnalyzer(str(root)).analyze_repository(inventory=inventory, run_coverage=False)
        context = ContextBuilder(inventory.policy.context_tokens).build('module_0.py', root, 1)
        analysis_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        checks = run_repository_checks(inventory) if run_checks else None
        return {
            'repository': inventory.summary(),
            'generated_functions_per_module': functions_per_file,
            'generated_lines_approximately': (file_count - 2) * (1 + 4 * functions_per_file) + 8,
            'analysis_seconds': round(analysis_seconds, 3),
            'total_seconds': round(time.perf_counter() - started, 3),
            'parent_python_peak_mb': round(peak / 1024 / 1024, 2),
            'graph_nodes': len(graph['nodes']), 'graph_edges': len(graph['edges']),
            'whitebox_status': whitebox['status'],
            'context_estimated_tokens': context['token_count'],
            'injected_error': inject_error,
            'checks': None if checks is None else {
                'complete': checks['complete'], 'passed': checks['passed'],
                'failures': checks['failures'],
                'outcomes': [{'name': c['name'], 'status': c['status'],
                              'output': c['output'][-1000:]} for c in checks['checks']],
            },
        }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', type=int, default=1200)
    parser.add_argument('--run-checks', action='store_true')
    parser.add_argument('--inject-error', action='store_true')
    parser.add_argument('--functions-per-file', type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.files, args.run_checks, args.inject_error, args.functions_per_file), indent=2))
