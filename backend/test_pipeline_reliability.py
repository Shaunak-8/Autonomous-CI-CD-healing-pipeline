import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from groq import AuthenticationError

from api import sse
from core.llm_status import check_groq
from core.repository import RepositoryInventory
from tools import checks, environment


def test_provider_preflight_handles_expired_credentials_without_exposing_them():
    client = Mock()
    client.models.list.side_effect = AuthenticationError(
        'secret provider response', response=httpx.Response(401, request=httpx.Request('GET', 'https://example.invalid')), body={})
    result = check_groq(client, 'configured-model')
    assert not result['ready']
    assert 'invalid or expired' in result['reason']
    assert 'secret provider response' not in result['reason']


def test_provider_preflight_detects_retired_model():
    client = Mock()
    client.models.list.return_value.data = [SimpleNamespace(id='enabled-model')]
    assert check_groq(client, 'enabled-model')['ready']
    result = check_groq(client, 'retired-model')
    assert not result['ready']
    assert result['available_models'] == ['enabled-model']


def test_dependency_setup_uses_workspace_virtualenv_and_lockfile(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / 'requirements.txt').write_text('example-package\n')
    (tmp_path / 'web').mkdir()
    (tmp_path / 'web' / 'app.js').write_text('const value = 1;\n')
    (tmp_path / 'web' / 'package.json').write_text('{}')
    (tmp_path / 'web' / 'package-lock.json').write_text('{}')
    commands = []

    def command(cmd, cwd, timeout, **kwargs):
        commands.append(cmd)
        return {'exit_code': 0, 'complete': True, 'output': ''}

    monkeypatch.setattr(environment, 'run_command', command)
    environment.prepare_environment(RepositoryInventory(tmp_path))
    pip_commands = [cmd for cmd in commands if 'pip' in cmd]
    assert pip_commands and all('.venv' in cmd[0] for cmd in pip_commands)
    assert any('ci' in cmd for cmd in commands)


def test_dependency_failure_stops_with_actionable_error(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('value = 1\n')
    monkeypatch.setenv('REPO_PYTHON_EXECUTABLE', 'python')
    monkeypatch.setattr(environment, 'run_command', lambda *args, **kwargs: {'exit_code': 1, 'complete': True, 'output': 'package resolution failed'})
    with pytest.raises(RuntimeError, match='Dependency setup failed'):
        environment.prepare_environment(RepositoryInventory(tmp_path))


def test_install_progress_streams_output_without_corrupting_capture(tmp_path):
    import sys
    updates = []
    result = checks.run_command([sys.executable, '-u', '-c',
        "import time; print('Downloading dependency'); time.sleep(.4); print('Installed dependency')"],
        tmp_path, 5, progress=updates.append, progress_interval=.05)
    assert result['complete'] and result['exit_code'] == 0
    assert any('Downloading dependency' in update for update in updates)
    assert result['output'].splitlines()[:2] == ['Downloading dependency', 'Installed dependency']


@pytest.mark.parametrize('cached_success', [True, False])
def test_dependency_cache_avoids_network_when_sufficient(tmp_path, monkeypatch, cached_success):
    cache = tmp_path / 'wheels'
    cache.mkdir()
    (cache / 'example.whl').touch()
    monkeypatch.setenv('DEPENDENCY_WHEELHOUSE', str(cache))
    source = tmp_path / 'repo'
    source.mkdir()
    (source / 'app.py').write_text('value = 1\n')
    (source / 'requirements.txt').write_text('example-package\n')
    commands = []

    def command(cmd, *args, **kwargs):
        commands.append(cmd)
        failed = '--no-index' in cmd and not cached_success
        return {'exit_code': 1 if failed else 0, 'complete': True, 'output': 'missing cached package' if failed else ''}

    monkeypatch.setattr(environment, 'run_command', command)
    environment.prepare_environment(RepositoryInventory(source))
    installs = [cmd for cmd in commands if 'install' in cmd]
    assert any('--no-index' in cmd for cmd in installs)
    online = [cmd for cmd in installs if '--no-index' not in cmd]
    assert bool(online) is not cached_success
    assert all('--retries' in cmd and '--timeout' in cmd for cmd in online)


def test_install_progress_preserves_timeout_and_redacts_url_credentials(tmp_path):
    import sys
    updates = []
    result = checks.run_command([sys.executable, '-u', '-c',
        "import time; print('https://user:example-secret@example.invalid/package'); time.sleep(10)"],
        tmp_path, .5, progress=updates.append, progress_interval=.05)
    assert not result['complete']
    assert 'timed out' in result['output']
    assert any('https://***@example.invalid' in update for update in updates)
    assert all('example-secret' not in update for update in updates)


def test_sse_replays_terminal_event_for_late_client():
    async def check():
        run_id = 'late-client'
        sse.emit(run_id, 'ReporterAgent', '{"status":"passed"}', 'RUN_COMPLETED')
        response = await sse.stream(run_id, SimpleNamespace(headers={}))
        event = await anext(response.body_iterator)
        assert json.loads(event['data'])['type'] == 'RUN_COMPLETED'
        await response.body_iterator.aclose()
        assert not sse.subscribers[run_id]

    asyncio.run(check())


def test_sse_broadcasts_to_each_client_without_consuming_each_others_events():
    async def check():
        run_id = 'two-clients'
        a = await sse.stream(run_id, SimpleNamespace(headers={}))
        b = await sse.stream(run_id, SimpleNamespace(headers={}))
        await asyncio.to_thread(sse.emit, run_id, 'AnalyzerAgent', 'Started', 'ANALYSIS_STARTED')
        first = await asyncio.wait_for(anext(a.body_iterator), 1)
        second = await asyncio.wait_for(anext(b.body_iterator), 1)
        assert first['id'] == second['id']
        await a.body_iterator.aclose()
        await b.body_iterator.aclose()

    asyncio.run(check())


def test_pytest_failure_maps_to_single_imported_source(tmp_path):
    (tmp_path / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (tmp_path / 'test_calculator.py').write_text('from calculator import add\n')
    findings = checks._findings({'name': 'pytest', 'root': '.',
                                'output': 'FAILED test_calculator.py::test_add - assert -1 == 3'}, RepositoryInventory(tmp_path))
    assert findings[0]['file'] == 'calculator.py'


def test_commit_does_not_include_generated_environments(tmp_path):
    import subprocess
    from tools.git_ops import commit_changes
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    git('init')
    git('config', 'user.name', 'Pipeline Test')
    git('config', 'user.email', 'pipeline@example.invalid')
    (tmp_path / 'source.py').write_text('value = 1\n')
    git('add', 'source.py')
    git('commit', '-m', 'Initial fixture')
    (tmp_path / '.venv').mkdir()
    (tmp_path / '.venv' / 'generated.txt').write_text('Generated dependency fixture')
    (tmp_path / 'source.py').write_text('value = 2\n')
    assert commit_changes(str(tmp_path), 'Fix source') is True
    assert git('ls-files').strip() == 'source.py'
    assert commit_changes(str(tmp_path), 'No changes') is None


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_minimal_ai_edits_preserve_untouched_code_and_line_endings(tmp_path, newline):
    from agents.fixer import BaseFixerAgent
    from core.context_builder import ContextBuilder
    original = f'def add(a: int, b: int) -> int:{newline}    return a - b{newline}'
    (tmp_path / 'calculator.py').write_bytes(original.encode())
    client = Mock()
    client.chat.completions.create.return_value.choices = [SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=json.dumps({
            'edits': [{'old': '    return a - b', 'new': '    return a + b'}]})))]
    result = BaseFixerAgent(client, ContextBuilder()).fix(
        {'file': 'calculator.py', 'line': 2, 'bug_type': 'LOGIC'}, str(tmp_path))
    assert result['fixed_code'] == original.replace('return a - b', 'return a + b')


@pytest.mark.parametrize('directive', ['# type: ignore[assignment]', '# noqa: F821', '# NOQA'])
def test_generated_fixes_cannot_silence_validation(tmp_path, directive):
    from agents.fixer import BaseFixerAgent
    from core.context_builder import ContextBuilder
    source = tmp_path / 'source.py'
    source.write_text('value = missing\n')
    client = Mock()
    client.chat.completions.create.return_value.choices = [SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=json.dumps({'edits': [
            {'old': 'value = missing', 'new': f'value = missing  {directive}'}]})))]
    agent = BaseFixerAgent(client, ContextBuilder())
    agent.primary_provider = 'groq'
    result = agent.fix({'file': 'source.py', 'line': 1, 'bug_type': 'TYPE_ERROR'}, str(tmp_path))
    assert 'suppression directives' in result['error']
    assert 'fixed_code' not in result
    assert source.read_text() == 'value = missing\n'


def test_repair_ledger_allows_retry_only_after_source_changes(tmp_path):
    from agents.orchestrator import OrchestratorAgent
    from core.failure_ledger import FailureLedger
    agent = OrchestratorAgent.__new__(OrchestratorAgent)
    ledger = FailureLedger()
    source = tmp_path / 'source.py'
    source.write_text('value = missing\n')
    finding = {'file': 'source.py', 'line': 1, 'bug_type': 'TYPE_ERROR', 'error_message': 'same error'}
    first = agent._finding_strategy(finding, str(tmp_path))
    ledger.add_attempt('source.py', first, 'applied')
    assert ledger.was_tried('source.py', agent._finding_strategy(finding, str(tmp_path)))
    source.write_text('value = missing\n# partial repair\n')
    assert not ledger.was_tried('source.py', agent._finding_strategy(finding, str(tmp_path)))


def test_provider_rate_limit_waits_then_retries(monkeypatch):
    from groq import RateLimitError
    from agents import fixer
    client = Mock()
    rate_limit = RateLimitError('quota temporarily exhausted', body={}, response=httpx.Response(
        429, headers={'retry-after': '1'}, request=httpx.Request('POST', 'https://example.invalid')))
    response = SimpleNamespace(choices=[])
    client.chat.completions.create.side_effect = [rate_limit, response]
    sleep = Mock()
    monkeypatch.setattr(fixer.time, 'sleep', sleep)
    agent = fixer.BaseFixerAgent(client, None)
    agent.on_progress = Mock()
    assert agent._groq_response('Return JSON') is response
    sleep.assert_called_once_with(1)
    assert client.chat.completions.create.call_count == 2
    agent.on_progress.assert_called_once()


def test_failed_validation_reverts_a_batch_that_breaks_a_passing_check(tmp_path, monkeypatch):
    import agents.orchestrator as orchestration
    agent = orchestration.OrchestratorAgent.__new__(orchestration.OrchestratorAgent)
    agent.db = None
    source = tmp_path / 'source.py'
    source.write_bytes(b'value = 2\r\n')
    agent._batch_backups = {'source.py': 'value = 1\r\n'}
    entry = {'file': 'source.py', 'status': 'applied', 'iteration': 1}
    agent._batch_entries = [entry]
    agent.ledger = Mock()
    failed = {'status': 'failed', 'check_report': {'complete': True, 'passed': False, 'failures': [],
              'checks': [{'root': '.', 'name': 'pytest', 'status': 'failed'}]}}
    restored = {'status': 'passed', 'check_report': {'complete': True, 'passed': True, 'failures': [],
                'checks': [{'root': '.', 'name': 'pytest', 'status': 'passed'}]}}
    agent.validator = Mock()
    agent.validator.validate.side_effect = [failed, restored]
    monkeypatch.setattr(orchestration, 'emit', Mock())
    state = {'run_id': 'rollback', 'repo_path': str(tmp_path), 'iteration': 1,
             'check_report': {'checks': [{'root': '.', 'name': 'pytest', 'status': 'passed'}]}}
    agent.validate_node(state)
    assert source.read_bytes() == b'value = 1\r\n'
    assert entry['status'] == 'reverted'
    assert agent.validator.validate.call_count == 2


def test_provider_outage_does_not_skip_validation_of_already_applied_edits(tmp_path, monkeypatch):
    import agents.orchestrator as orchestration
    agent = orchestration.OrchestratorAgent.__new__(orchestration.OrchestratorAgent)
    agent.db = None
    agent._batch_backups = {'source.py': 'value = 1\n'}
    agent.ledger = Mock()
    agent.validator = Mock()
    report = {'complete': True, 'passed': False, 'failures': [],
              'checks': [{'root': '.', 'name': 'pytest', 'status': 'passed'},
                         {'root': '.', 'name': 'mypy', 'status': 'failed'}]}
    agent.validator.validate.return_value = {'status': 'failed', 'check_report': report}
    monkeypatch.setattr(orchestration, 'emit', Mock())
    state = {'run_id': 'outage', 'repo_path': str(tmp_path), 'iteration': 1,
             'ai': {'ready': False, 'reason': 'Provider outage'},
             'check_report': {'checks': [{'root': '.', 'name': 'pytest', 'status': 'passed'}]}}
    agent.validate_node(state)
    agent.validator.validate.assert_called_once_with(str(tmp_path))
    assert state['check_report'] is report


def test_provider_failure_keeps_source_findings_pending(monkeypatch):
    import agents.orchestrator as orchestration
    from core.failure_ledger import FailureLedger
    agent = orchestration.OrchestratorAgent.__new__(orchestration.OrchestratorAgent)
    agent.db = None
    agent.groq_client = None
    agent.gemini_model = None
    agent.ai_status = {'ready': True, 'provider': 'groq'}
    agent.context_builder = Mock()
    agent.dep_graph = Mock()
    agent.dep_graph.get_root_nodes.return_value = []
    agent.memory = Mock()
    agent.ledger = FailureLedger()
    fixer = Mock()
    fixer.fix.return_value = {'error': 'Both providers unavailable', 'provider_failure': True, 'retryable': False}
    agent.router = Mock()
    agent.router.route.return_value = Mock(return_value=fixer)
    monkeypatch.setattr(orchestration, 'emit', Mock())
    state = {'run_id': 'outage', 'repo_path': '.', 'iteration': 1, 'fixes_applied': [],
             'repository_profile': {'policy': {'fix_batch_size': 2}},
             'failures': [{'file': 'source.py', 'line': 1, 'bug_type': 'TYPE_ERROR', 'error_message': 'wrong type'}]}
    agent.fix_node(state)
    assert not state['ai']['ready']
    assert state['deferred_failures'] == 1
    assert agent.ledger.to_dict() == {}


def test_runtime_health_reports_provider_configuration_without_keys(monkeypatch):
    import main
    monkeypatch.setattr(main.config, 'GROQ_API_KEY', 'example-groq-secret')
    monkeypatch.setattr(main.config, 'GEMINI_API_KEY', 'example-gemini-secret')
    result = main.health_check()
    assert result['ai']['groq']['configured']
    assert result['ai']['gemini']['configured']
    assert result['pipeline_revision']
    assert 'example-groq-secret' not in str(result)
    assert 'example-gemini-secret' not in str(result)
