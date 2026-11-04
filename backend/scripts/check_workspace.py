"""Prepare and validate an existing clone without editing or pushing source code."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.repository import RepositoryInventory
from tools.environment import prepare_environment
from agents.analyzer import AnalyzerAgent


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    args = parser.parse_args()
    path = Path(args.path).resolve()
    workspace_root = Path(__file__).resolve().parents[1] / 'workspaces'
    if not path.is_relative_to(workspace_root.resolve()) or path == workspace_root.resolve():
        raise SystemExit('Choose one existing clone inside backend/workspaces')
    inventory = RepositoryInventory(path)
    prepare_environment(inventory, lambda message: print(message, flush=True))
    agent = AnalyzerAgent()
    agent.run(str(path))
    report = agent.check_report
    print(json.dumps({
        'complete': report['complete'], 'passed': report['passed'],
        'findings': len(report['failures']),
        'checks': [{'name': c['name'], 'root': c['root'], 'status': c['status'], 'output': c['output'][-1800:]}
                   for c in report['checks']],
    }, indent=2))
