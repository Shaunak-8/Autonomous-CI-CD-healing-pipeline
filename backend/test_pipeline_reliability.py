import asyncio
import json
from pathlib import Path
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


def test_fast_dependency_setup_skips_heavy_direct_requirements(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / 'requirements.txt').write_text('httpx==0.28.0\ntorch>=2.2.0\n')
    installed_requirements, progress = [], []

    def command(cmd, *_args, **_kwargs):
        if '-r' in cmd:
            installed_requirements.append(Path(cmd[cmd.index('-r') + 1]).read_text())
        return {'exit_code': 0, 'complete': True, 'output': ''}

    monkeypatch.setattr(environment, 'run_command', command)
    environment.prepare_environment(RepositoryInventory(tmp_path), progress.append)
    assert installed_requirements and 'httpx==0.28.0' in installed_requirements[0]
    assert 'torch>=2.2.0' not in installed_requirements[0]
    assert any('ALLOW_HEAVY_DEPENDENCIES=true' in item for item in progress)


def test_full_dependency_setup_includes_heavy_requirements_when_enabled(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('value = 1\n')
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('torch>=2.2.0\n')
    installed_requirements = []

    def command(cmd, *_args, **_kwargs):
        if '-r' in cmd:
            installed_requirements.append(Path(cmd[cmd.index('-r') + 1]))
        return {'exit_code': 0, 'complete': True, 'output': ''}

    monkeypatch.setenv('ALLOW_HEAVY_DEPENDENCIES', 'true')
    monkeypatch.setattr(environment, 'run_command', command)
    environment.prepare_environment(RepositoryInventory(tmp_path))
    assert Path('requirements.txt') in installed_requirements


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


def test_reference_edits_do_not_discard_valid_primary_edits(tmp_path, monkeypatch):
    from agents.fixer import PythonFixerAgent
    from core.context_builder import ContextBuilder
    (tmp_path / 'app.py').write_text('from model import value\nresult = value - 1\n')
    (tmp_path / 'model.py').write_text('value = 10\n')
    agent = PythonFixerAgent(object(), ContextBuilder())
    monkeypatch.setattr(agent, '_generate', lambda _: (json.dumps({'edits': [
        {'old': 'result = value - 1', 'new': 'result = value + 1'},
        {'old': 'value = 10', 'new': 'value = 20'}]}), 'groq'))
    result = agent.fix({'file': 'app.py', 'bug_type': 'LOGIC'}, str(tmp_path))
    assert 'result = value + 1' in result['fixed_code']
    assert 'value = 20' not in result['fixed_code']
    assert (tmp_path / 'model.py').read_text() == 'value = 10\n'


def test_multi_module_test_failures_are_healed_with_real_validation(tmp_path, monkeypatch):
    from agents.orchestrator import OrchestratorAgent
    from agents.fixer import BaseFixerAgent
    (tmp_path / 'models.py').write_text(
        'class Task:\n    def __init__(self) -> None:\n        self.completed = False\n'
        '    def complete(self) -> None:\n        self.completed = False\n')
    (tmp_path / 'services.py').write_text(
        'from models import Task\nclass TaskService:\n'
        '    def complete(self) -> Task:\n        task = Task()\n        task.complete()\n        return task\n'
        '    def next_id(self, value: int) -> int:\n        return value + 2\n')
    (tmp_path / 'utils.py').write_text('def parse_bool(value: str) -> bool:\n    return value == "yes"\n')
    (tmp_path / 'tests').mkdir()
    test_source = (
        'from models import Task\nfrom services import TaskService\nfrom utils import parse_bool\n'
        'def test_task() -> None:\n    task = Task()\n    task.complete()\n    assert task.completed\n'
        'def test_service() -> None:\n    assert TaskService().next_id(0) == 1\n'
        'def test_complete() -> None:\n    assert TaskService().complete().completed\n'
        'def test_helper() -> None:\n    assert parse_bool("true")\n')
    (tmp_path / 'tests/test_app.py').write_text(test_source)
    agent = OrchestratorAgent(publish=False)
    agent.ai_status = {'ready': True, 'provider': 'fixture', 'model': 'fixture'}
    agent.groq_client = object()
    agent.gemini_model = None
    def generate(_self, prompt):
        if 'File: models.py' in prompt:
            edits = [{'old': '    def complete(self) -> None:\n        self.completed = False',
                      'new': '    def complete(self) -> None:\n        self.completed = True'}]
        elif 'File: services.py' in prompt:
            edits = [{'old': 'return value + 2', 'new': 'return value + 1'}]
        else:
            edits = [{'old': 'value == "yes"', 'new': 'value in ("yes", "true")'}]
        return json.dumps({'edits': edits}), 'fixture'
    monkeypatch.setattr(BaseFixerAgent, '_generate', generate)
    report = agent.run('cafef001', 'fixture', str(tmp_path))['results']
    assert report['status'] == 'passed'
    assert report['total_failures'] == 0
    assert {fix['file'] for fix in report['fixes']} == {'models.py', 'services.py', 'utils.py'}
    assert (tmp_path / 'tests/test_app.py').read_text() == test_source


def test_rejected_edit_is_retried_with_feedback_and_original_source(tmp_path, monkeypatch):
    from agents.fixer import PythonFixerAgent
    from core.context_builder import ContextBuilder
    source = tmp_path / 'app.py'
    source.write_text('value = 1\n')
    agent = PythonFixerAgent(object(), ContextBuilder())
    prompts = []
    def generate(prompt):
        prompts.append(prompt)
        return (json.dumps({'edits': [{'old': 'wrong source' if len(prompts) == 1 else 'value = 1', 'new': 'value = 2'}]}), 'groq')
    monkeypatch.setattr(agent, '_generate', generate)
    result = agent.fix({'file': 'app.py', 'bug_type': 'LOGIC'}, str(tmp_path))
    assert result['fixed_code'] == 'value = 2\n'
    assert source.read_text() == 'value = 1\n'
    assert len(prompts) == 2
    assert 'No edits target the requested source file' in prompts[1]


def test_pytest_multi_import_failure_maps_only_symbols_used_by_failing_test(tmp_path):
    for name in ('models', 'services', 'utils'):
        (tmp_path / f'{name}.py').write_text('value = 1\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_app.py').write_text(
        'from models import Task\nfrom services import TaskService\nfrom utils import parse_bool\n'
        'def test_service():\n    assert TaskService().pending() == []\n'
        'def test_helper():\n    assert parse_bool("true") is True\n')
    inventory = RepositoryInventory(tmp_path)
    findings = checks._findings({'name': 'pytest', 'root': '.', 'output':
        'FAILED tests/test_app.py::test_service - AssertionError\n'
        'FAILED tests/test_app.py::test_helper - AssertionError'}, inventory)
    assert {f['file'] for f in findings} == {'services.py', 'utils.py'}
    assert all(f['test_file'] == 'tests/test_app.py' for f in findings)
    assert 'TaskService().pending()' in findings[0]['error_message']


def test_pytest_failure_maps_to_single_imported_source(tmp_path):
    (tmp_path / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (tmp_path / 'test_calculator.py').write_text('from calculator import add\n')
    findings = checks._findings({'name': 'pytest', 'root': '.',
                                'output': 'FAILED test_calculator.py::test_add - assert -1 == 3'}, RepositoryInventory(tmp_path))
    assert findings[0]['file'] == 'calculator.py'


def test_pytest_traceback_preserves_real_source_frames(tmp_path):
    (tmp_path / 'app').mkdir()
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'app' / 'main.py').write_text('from .services import UserService\n')
    (tmp_path / 'app' / 'services.py').write_text('from .database import get_connection\n')
    (tmp_path / 'app' / 'database.py').write_text('def get_connection(): pass\n')
    (tmp_path / 'tests' / 'test_main.py').write_text('from app.main import UserService\n')
    output = (
        'FAILED tests/test_main.py::test_create_user - assert 500 == 200\n'
        f'File "{tmp_path / "app" / "main.py"}", line 21, in create_user\n'
        f'File "{tmp_path / "app" / "services.py"}", line 14, in create_user\n'
        f'File "{tmp_path / "app" / "database.py"}", line 9, in get_connection\n'
    )
    findings = checks._findings({'name': 'pytest', 'root': '.', 'output': output}, RepositoryInventory(tmp_path))
    assert {'app/main.py', 'app/services.py', 'app/database.py'} <= {finding['file'] for finding in findings}


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


def test_batches_process_more_than_twenty_five_files_without_hiding_findings(tmp_path, monkeypatch):
    from collections import Counter
    from agents.orchestrator import OrchestratorAgent
    from agents.fixer import BaseFixerAgent
    monkeypatch.setenv('MAX_FIXES_PER_RUN', '0')
    monkeypatch.setenv('MAX_RETRIES', '2')
    for index in range(31):
        (tmp_path / f'source_{index}.py').write_text('value = 0\n')
    agent = OrchestratorAgent(publish=False)
    agent.ai_status = {'ready': True, 'provider': 'fixture', 'model': 'fixture'}
    agent.groq_client = object()
    agent.gemini_model = None
    def report():
        failures = [{'file': p.name, 'line': 1, 'bug_type': 'LOGIC', 'error_message': 'Expected value = 1'}
                    for p in sorted(tmp_path.glob('*.py')) if p.read_text() == 'value = 0\n']
        return {'complete': True, 'passed': not failures, 'failures': failures,
                'checks': [{'root': '.', 'name': 'fixture', 'status': 'failed' if failures else 'passed'}]}
    def analyze(repo_path, run_id=None):
        agent.analyzer.inventory = RepositoryInventory(repo_path)
        agent.analyzer.check_report = report()
        return agent.analyzer.check_report['failures']
    monkeypatch.setattr(agent.analyzer, 'run', analyze)
    monkeypatch.setattr(agent.validator, 'validate', lambda _: {
        'status': 'passed' if report()['passed'] else 'failed', 'check_report': report()})
    monkeypatch.setattr(BaseFixerAgent, '_generate', lambda *_: (
        json.dumps({'edits': [{'old': 'value = 0', 'new': 'value = 1'}]}), 'fixture'))
    result = agent.run('ba7c0031', 'fixture', str(tmp_path))['results']
    assert result['status'] == 'passed'
    assert result['total_fixes'] == 31
    assert result['total_failures'] == 0
    assert result['iterations'] == 7
    assert max(Counter(f['iteration'] for f in result['fixes']).values()) <= 5
    assert all(p.read_text() == 'value = 1\n' for p in tmp_path.glob('*.py'))


def test_cpp_checks_with_same_name_do_not_create_false_regressions(tmp_path, monkeypatch):
    import agents.orchestrator as orchestration
    agent = orchestration.OrchestratorAgent.__new__(orchestration.OrchestratorAgent)
    agent.db = None
    agent.ledger = Mock()
    agent._batch_backups = {'source.cpp': 'original'}
    entry = {'status': 'applied'}
    agent._batch_entries = [entry]
    checks_report = [
        {'root': '.', 'name': 'cpp:syntax', 'command': ['g++', 'a.cpp'], 'status': 'passed'},
        {'root': '.', 'name': 'cpp:syntax', 'command': ['g++', 'b.cpp'], 'status': 'failed'}]
    agent.validator = Mock()
    agent.validator.validate.return_value = {'status': 'failed', 'check_report': {
        'checks': checks_report, 'failures': [], 'passed': False, 'complete': True}}
    agent._publish_validated_batch = Mock()
    monkeypatch.setattr(orchestration, 'emit', Mock())
    state = {'run_id': 'identity', 'repo_path': str(tmp_path), 'iteration': 1,
             'check_report': {'checks': checks_report}}
    agent.validate_node(state)
    assert entry['status'] == 'applied'
    agent._publish_validated_batch.assert_called_once()
    agent.validator.validate.assert_called_once()


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


def test_validated_batches_publish_to_one_verified_branch(monkeypatch):
    import agents.orchestrator as orchestration
    agent = orchestration.OrchestratorAgent.__new__(orchestration.OrchestratorAgent)
    agent.publish = True
    agent._batch_entries = [{'file': 'first.py', 'status': 'applied'}]
    published = Mock(side_effect=[
        {'status': 'success', 'branch': 'TEAM_AI_FIX', 'commit_sha': 'first-sha', 'verified': True},
        {'status': 'success', 'branch': 'TEAM_AI_FIX', 'commit_sha': 'second-sha', 'verified': True},
    ])
    monkeypatch.setattr(orchestration.git_ops, 'commit_and_push_batch', published)
    monkeypatch.setattr(orchestration, 'emit', Mock())
    state = {'run_id': 'partial', 'repo_path': '.', 'token': None, 'branch_name': '',
             'push_result': None, 'published_commit_sha': None, 'published_batches': 0}

    agent._publish_validated_batch(state)
    agent._batch_entries = [{'file': 'second.py', 'status': 'applied'}]
    agent._publish_validated_batch(state)

    assert state['branch_name'] == 'TEAM_AI_FIX'
    assert state['published_commit_sha'] == 'second-sha'
    assert state['published_batches'] == 2
    assert published.call_args_list[1].kwargs['branch_name'] == 'TEAM_AI_FIX'
    assert published.call_args_list[1].kwargs['expected_remote_sha'] == 'first-sha'


def test_grouped_findings_count_toward_five_issue_batch_limit(tmp_path, monkeypatch):
    import agents.orchestrator as orchestration
    from core.failure_ledger import FailureLedger
    agent = orchestration.OrchestratorAgent.__new__(orchestration.OrchestratorAgent)
    agent.db = None
    keys = [object(), object()]
    agent.groq_clients = keys
    agent.groq_client = keys[0]
    agent.gemini_models = []
    agent.gemini_model = None
    agent.ai_status = {'ready': True, 'provider': 'groq'}
    agent.context_builder = Mock()
    agent.dep_graph = Mock()
    agent.dep_graph.get_root_nodes.return_value = []
    agent.memory = Mock()
    agent.ledger = FailureLedger()
    (tmp_path / 'source.py').write_text('value = 1\n')
    assignments, counts = [], []
    def build(client, *_args, **_kwargs):
        assignments.append(client)
        fixer = Mock()
        def fix(failure, *_):
            counts.append(len(failure['error_message'].splitlines()))
            return {}
        fixer.fix.side_effect = fix
        return fixer
    agent.router = Mock()
    agent.router.route.return_value = build
    monkeypatch.setattr(orchestration, 'emit', Mock())
    state = {'run_id': 'grouped', 'repo_path': str(tmp_path), 'iteration': 1, 'fixes_applied': [],
             'repository_profile': {'policy': {'fix_batch_size': 5}},
             'failures': [{'file': 'source.py', 'line': i, 'bug_type': 'TYPE_ERROR', 'error_message': f'Error {i}'} for i in range(7)]}
    agent.fix_node(state)
    assert state['deferred_failures'] == 2
    agent.fix_node(state)
    assert state['deferred_failures'] == 0
    assert assignments == keys
    assert counts == [5, 2]


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
