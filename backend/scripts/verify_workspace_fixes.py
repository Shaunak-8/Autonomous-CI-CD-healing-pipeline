"""Exercise live fixes in a verification clone without publishing a remote branch."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.orchestrator import OrchestratorAgent
from api import sse


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('--live', action='store_true', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'workspaces'
    path = Path(args.path).resolve()
    if not path.is_relative_to(root.resolve()) or not path.name.startswith('verification_'):
        raise SystemExit('Use an isolated verification_ clone inside backend/workspaces')
    run_id = path.name
    original = sse.emit

    def progress(run_id, agent, message, event_type='thought'):
        if event_type not in {'GRAPH_UPDATED', 'SCORE_UPDATED', 'HEALTH_UPDATED', 'FIX_APPLIED', 'RUN_COMPLETED', 'REPOSITORY_CLASSIFIED'}:
            print(f'{agent}: {message}', flush=True)
        original(run_id, agent, message, event_type)

    import agents.orchestrator as orchestration
    import agents.analyzer as analysis
    orchestration.emit = progress
    analysis.emit = progress
    result = OrchestratorAgent(publish=False).run(run_id, 'local-verification-clone', str(path))
    report = result['results']
    print(json.dumps({
        'status': report['status'], 'fixes': report['total_fixes'],
        'remaining_findings': report['total_failures'], 'deferred_findings': report['deferred_failures'],
        'analysis_complete': report['analysis']['complete'], 'push': report['push'],
        'checks': [{'name': c['name'], 'status': c['status'], 'output': c['output'][-1000:]} for c in report['analysis']['checks']],
    }, indent=2))
