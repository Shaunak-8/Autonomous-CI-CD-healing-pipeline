"""Exercise real Git cloning, checks and a live AI fix in a temporary workspace."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.orchestrator import OrchestratorAgent
from api import sse
from tools.git_ops import clone_repo
from tools.environment import prepare_environment
from core.repository import RepositoryInventory
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from db import models
import config


def smoke(prepare=False, force_groq_rate_limit=False):
    with tempfile.TemporaryDirectory(prefix='healing-smoke-') as directory:
        root = Path(directory)
        source = root / 'source'
        source.mkdir()
        (source / 'calculator.py').write_text(
            'def add(a: int, b: int) -> int:\n    return a + missing_value\n', encoding='utf-8')
        (source / 'test_calculator.py').write_text(
            'from calculator import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n', encoding='utf-8')
        for args in (['init'], ['add', '.'], ['-c', 'user.name=Smoke Test', '-c', 'user.email=smoke@example.invalid', 'commit', '-m', 'Initial broken fixture']):
            subprocess.run(['git', *args], cwd=source, check=True, capture_output=True)
        cloned = root / 'clone'
        clone_repo(str(source), str(cloned), token='')
        if prepare:
            prepare_environment(RepositoryInventory(cloned), progress=lambda message: print(message, flush=True))
        engine = create_engine(f'sqlite:///{root / "smoke.db"}')
        models.Run.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(models.Run(id='c0decafe', repo_url='local-smoke-fixture', team_name='smoke', leader_name='smoke'))
            db.commit()
            agent = OrchestratorAgent(db=db, publish=False)
            if force_groq_rate_limit:
                import httpx
                from types import SimpleNamespace
                from groq import RateLimitError
                models_client = agent.groq_client.models
                def limited(**kwargs):
                    raise RateLimitError('Injected quota failure for fallback verification', body={},
                                         response=httpx.Response(429, request=httpx.Request('POST', 'https://example.invalid')))
                agent.groq_client = SimpleNamespace(models=models_client,
                    chat=SimpleNamespace(completions=SimpleNamespace(create=limited)))
            result = agent.run('c0decafe', 'local-smoke-fixture', str(cloned))
            persisted = db.get(models.Run, 'c0decafe').memory
        engine.dispose()
        report = result['results']
        summary = {
            'status': report['status'], 'fixes': report['total_fixes'],
            'checks_complete': report['analysis']['complete'],
            'checks': [{k: check[k] for k in ('name', 'status')} for check in report['analysis']['checks']],
            'ai': report['analysis'].get('ai'), 'push': report['push'],
            'fixed_source': (cloned / 'calculator.py').read_text(),
            'events': len(sse.event_history['c0decafe']),
            'persisted_status': persisted['status'],
            'injected_groq_rate_limit': force_groq_rate_limit,
        }
        print(json.dumps(summary, indent=2))
        return 0 if report['status'] == 'passed' and report['total_fixes'] > 0 else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True, help='Use the configured AI provider for this isolated fixture')
    parser.add_argument('--prepare', action='store_true', help='Also create a virtual environment and install validation tools')
    parser.add_argument('--primary', choices=['groq', 'gemini'], help='Provider to verify in this process only')
    parser.add_argument('--force-groq-rate-limit', action='store_true', help='Inject a Groq 429 to verify live Gemini failover')
    args = parser.parse_args()
    if args.primary:
        config.LLM_PRIMARY_PROVIDER = args.primary
    raise SystemExit(smoke(args.prepare, args.force_groq_rate_limit))
