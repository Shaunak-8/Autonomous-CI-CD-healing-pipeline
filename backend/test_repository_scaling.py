"""Regression checks for adaptive analysis and truthful validation outcomes."""
import json
import sys
from unittest.mock import Mock

import pytest

from core.context_builder import ContextBuilder
from core.project_analyzer import ProjectAnalyzer
from core.repository import RepositoryInventory
from tools import checks


def test_inventory_classifies_by_count_and_bytes_and_prunes_dependencies(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'vendor.js').write_text('x' * 10000)
    assert RepositoryInventory(tmp_path).size == 'small'
    monkeypatch.setenv('SMALL_REPO_MAX_BYTES', '5')
    inventory = RepositoryInventory(tmp_path)
    assert inventory.size == 'medium'
    assert len(inventory.files) == 1
    assert inventory.policy.timeout_seconds > 120
    monkeypatch.setenv('MEDIUM_REPO_MAX_BYTES', '6')
    assert RepositoryInventory(tmp_path).size == 'large'


def test_medium_inventory_and_graph_cover_1200_files(tmp_path):
    for number in range(1200):
        (tmp_path / f'module_{number}.py').write_text(
            f'import module_{number - 1}\n' if number else 'value = 0\n')
    inventory = RepositoryInventory(tmp_path)
    assert inventory.size == 'medium'
    assert inventory.summary()['source_files'] == 1200
    graph = ProjectAnalyzer(str(tmp_path)).analyze()
    assert len(graph['nodes']) == 1200
    assert len(graph['edges']) == 1199
    assert {'source': 'module_1199.py', 'target': 'module_1198.py'} in graph['edges']


def test_graph_resolves_js_relative_imports_and_python_packages(tmp_path):
    (tmp_path / 'app').mkdir()
    (tmp_path / 'app' / 'view.tsx').write_text("import helper from './helper';\n")
    (tmp_path / 'app' / 'helper.ts').write_text('export default 1;\n')
    (tmp_path / 'app' / 'logic.py').write_text('from app.util import value\n')
    (tmp_path / 'app' / 'util.py').write_text('value = 1\n')
    graph = ProjectAnalyzer(str(tmp_path)).analyze()
    assert {'source': 'app/view.tsx', 'target': 'app/helper.ts'} in graph['edges']
    assert {'source': 'app/logic.py', 'target': 'app/util.py'} in graph['edges']


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_context_budget_and_excerpt_fix_preserve_large_file(tmp_path, newline):
    source = ''.join(f'value_{i} = {i}{newline}' for i in range(2000))
    path = tmp_path / 'large.py'
    path.write_bytes(source.encode('utf-8'))
    builder = ContextBuilder(max_tokens=200)
    context = builder.build('large.py', tmp_path, line=1501)
    assert context['token_count'] <= 200
    assert not context['primary_complete']
    assert 'value_1500 = 1500' in context['primary']
    fixed = builder.apply_replacement(
        context, context['primary'].replace('value_1500 = 1500', 'value_1500 = 9999'),
        'large.py', tmp_path)
    assert fixed == source.replace('value_1500 = 1500', 'value_1500 = 9999')
    path.write_text(source + '# changed\n')
    with pytest.raises(ValueError, match='changed'):
        builder.apply_replacement(context, 'replacement', 'large.py', tmp_path)


def test_context_handles_unicode_budget_and_rejects_escaped_paths(tmp_path):
    (tmp_path / 'unicode.py').write_text(''.join(f"value_{i} = '你好世界'\n" for i in range(100)), encoding='utf-8')
    context = ContextBuilder(100).build('unicode.py', tmp_path, 50)
    assert context['token_count'] <= 100
    with pytest.raises(ValueError, match='escapes'):
        ContextBuilder().build('../outside.py', tmp_path)


def test_check_plan_detects_nested_python_and_js_packages(tmp_path):
    (tmp_path / 'backend').mkdir()
    (tmp_path / 'frontend').mkdir()
    (tmp_path / 'backend' / 'requirements.txt').write_text('pytest\n')
    (tmp_path / 'backend' / 'app.py').write_text('value = 1\n')
    (tmp_path / 'frontend' / 'app.jsx').write_text('export default 1;\n')
    (tmp_path / 'frontend' / 'package.json').write_text(json.dumps({'scripts': {'lint': 'eslint .', 'build': 'vite build'}}))
    plan = checks.check_plan(RepositoryInventory(tmp_path))
    assert {(c['name'], c['root']) for c in plan} == {
        ('flake8', 'backend'), ('mypy', 'backend'), ('pytest', 'backend'),
        ('npm:lint', 'frontend'), ('npm:test', 'frontend'), ('npm:build', 'frontend'),
    }
    assert next(c for c in plan if c['name'] == 'npm:test')['command'] is None
    assert all(c['command'][0] == sys.executable for c in plan if c['root'] == 'backend')


def test_unconfigured_javascript_checks_are_skipped_not_incomplete(tmp_path):
    (tmp_path / 'app.js').write_text('const value = 1;\n')
    (tmp_path / 'package.json').write_text(json.dumps({}))
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert not report['passed']
    assert report['complete']
    assert {check['status'] for check in report['checks']} == {'skipped'}


def test_run_retains_all_findings_even_with_an_edit_budget(monkeypatch):
    import agents.orchestrator as orchestrator
    agent = orchestrator.OrchestratorAgent.__new__(orchestrator.OrchestratorAgent)
    monkeypatch.setattr(orchestrator, 'emit', Mock())
    state = {
        'run_id': 'limit',
        'repository_profile': {'policy': {'max_fixes_per_run': 25}},
    }
    findings = [{'file': f'source_{index}.py', 'line': 1, 'bug_type': 'LOGIC'} for index in range(30)]
    assert len(agent._limit_failures(state, findings)) == 30
    assert state['total_findings'] == 30
    assert state['capped_findings'] == 0


def test_provider_slots_rotate_across_configured_credentials():
    import agents.orchestrator as orchestrator
    agent = orchestrator.OrchestratorAgent.__new__(orchestrator.OrchestratorAgent)
    keys = [object(), object(), object()]
    agent.gemini_models = keys
    agent._provider_slots = {'groq': 0, 'gemini': 0}
    selected = [agent._next_provider_client('gemini')[0] for _ in range(5)]
    assert selected == [keys[0], keys[1], keys[2], keys[0], keys[1]]


@pytest.mark.parametrize('package', [[], {'scripts': []}, {'scripts': {'lint': 123}}])
def test_invalid_package_manifest_reports_incomplete(tmp_path, package):
    (tmp_path / 'app.js').write_text('const value = 1;\n')
    (tmp_path / 'package.json').write_text(json.dumps(package))
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert not report['complete']
    assert not report['passed']


def test_large_repository_does_not_launch_tools(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('value = 1\n')
    monkeypatch.setenv('SMALL_REPO_MAX_BYTES', '1')
    monkeypatch.setenv('MEDIUM_REPO_MAX_BYTES', '2')
    command = Mock()
    monkeypatch.setattr(checks, 'run_command', command)
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert not report['complete']
    assert 'exceeds' in report['reason']
    command.assert_not_called()


def test_javascript_checks_run_installed_npm_scripts(tmp_path):
    import os
    import shutil
    if not shutil.which('npm.cmd' if os.name == 'nt' else 'npm'):
        pytest.skip('npm is not installed')
    (tmp_path / 'package.json').write_text(json.dumps({
        'type': 'module', 'scripts': {'lint': 'node --check app.js', 'test': 'node --test app.test.js'}}))
    (tmp_path / 'app.js').write_text('export const value = 1;\n')
    (tmp_path / 'app.test.js').write_text(
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "import { value } from './app.js';\ntest('value', () => assert.equal(value, 1));\n")
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert report['passed'], report['checks']
    (tmp_path / 'app.js').write_text('export const = 1;\n')
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert not report['passed']
    assert any(c['status'] == 'failed' for c in report['checks'])


def test_optional_javascript_lint_does_not_block_a_passing_test(tmp_path):
    (tmp_path / 'package.json').write_text(json.dumps({
        'type': 'module', 'scripts': {'test': 'node --test app.test.js'}}))
    (tmp_path / 'app.js').write_text('export const value = 1;\n')
    (tmp_path / 'app.test.js').write_text(
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "import { value } from './app.js';\ntest('value', () => assert.equal(value, 1));\n")
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert report['passed'], report['checks']
    assert {check['status'] for check in report['checks']} == {'passed', 'skipped'}


def test_javascript_syntax_failures_from_file_urls_are_actionable(tmp_path):
    source = tmp_path / 'src' / 'index.js'
    source.parent.mkdir()
    source.write_text('export const = 1;\n')
    output = (
        f'file://{source}:1\n'
        'SyntaxError: Unexpected token =\n'
    )
    findings = checks._findings(
        {'name': 'npm:test', 'root': '.', 'output': output},
        RepositoryInventory(tmp_path),
    )
    assert findings == [{
        'file': 'src/index.js',
        'line': 1,
        'bug_type': 'SYNTAX',
        'error_message': 'SyntaxError: Unexpected token =',
        'severity': 'medium',
    }]


@pytest.mark.parametrize('exit_code,output,complete', [
    (5, 'no tests ran', True),
    (1, 'No module named pytest', True),
    (-1, 'Check timed out', False),
    (0, 'output exceeded capture limit', False),
])
def test_incomplete_checks_never_pass(tmp_path, monkeypatch, exit_code, output, complete):
    (tmp_path / 'app.py').write_text('value = 1\n')
    monkeypatch.setattr(checks, 'check_plan', lambda _: [{'name': 'pytest', 'root': '.', 'command': ['test']}])
    monkeypatch.setattr(checks, 'run_command', lambda *args: {
        'exit_code': exit_code, 'output': output, 'complete': complete})
    result = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert not result['passed']
    assert not result['complete']
    assert result['checks'][0]['status'] == 'incomplete'


def test_oversized_sources_prevent_complete_report(tmp_path, monkeypatch):
    monkeypatch.setenv('MAX_SOURCE_FILE_BYTES', '5')
    (tmp_path / 'app.py').write_text('value = 123456789\n')
    inventory = RepositoryInventory(tmp_path)
    assert inventory.source_files() == []
    assert inventory.summary()['skipped_files'][0]['reason'] == 'oversized source'
    assert not checks.run_repository_checks(inventory)['passed']


def test_tool_output_and_timeout_are_bounded(tmp_path):
    result = checks.run_command([sys.executable, '-c', "print('x' * 10000)"], tmp_path, 5, output_limit=100)
    assert not result['complete']
    assert len(result['output']) < 250
    result = checks.run_command([sys.executable, '-c', 'import time; time.sleep(10)'], tmp_path, .1)
    assert not result['complete']
    assert 'timed out' in result['output']


def test_failed_checks_without_parsed_findings_still_fail(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('value = 1\n')
    monkeypatch.setattr(checks, 'run_command', lambda *args: {
        'exit_code': 1, 'output': 'fatal test collection error', 'complete': True})
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert report['complete']
    assert not report['passed']
    assert report['failures'] == []


def test_pytest_summary_preserves_nested_failure_path(tmp_path):
    (tmp_path / 'backend').mkdir()
    (tmp_path / 'backend' / 'test_app.py').write_text('assert False\n')
    inventory = RepositoryInventory(tmp_path)
    findings = checks._findings({
        'name': 'pytest', 'root': 'backend',
        'output': 'FAILED test_app.py::test_value - AssertionError'}, inventory)
    assert findings[0]['file'] == 'backend/test_app.py'


def test_validator_does_not_treat_zero_failures_as_success(tmp_path, monkeypatch):
    import agents.validator as validator
    (tmp_path / 'app.py').write_text('value = 1\n')
    monkeypatch.setattr(validator, 'run_repository_checks', lambda _: {
        'passed': False, 'complete': False, 'failures': [], 'checks': []})
    assert validator.ValidatorAgent().validate(str(tmp_path))['status'] == 'failed'


@pytest.mark.parametrize('passed,complete', [(False, False), (False, True), (True, False)])
def test_report_never_pushes_unvalidated_changes(tmp_path, monkeypatch, passed, complete):
    import agents.orchestrator as orchestrator
    agent = orchestrator.OrchestratorAgent.__new__(orchestrator.OrchestratorAgent)
    agent.db = None
    agent.reporter = Mock()
    agent.reporter.build_results.return_value = {'status': 'failed'}
    push = Mock()
    monkeypatch.setattr(orchestrator.git_ops, 'commit_and_push_all', push)
    monkeypatch.setattr(orchestrator, 'emit', Mock())
    state = {'run_id': 'test', 'repo_url': 'local', 'repo_path': str(tmp_path),
             'status': 'passed', 'iteration': 1, 'failures': [],
             'fixes_applied': [{'file': 'app.py'}], 'check_report': {
                 'passed': passed, 'complete': complete, 'checks': []}}
    agent.report_node(state)
    push.assert_not_called()
    assert state['status'] == 'failed'


def test_small_fixture_checks_execute_real_pytest(tmp_path, monkeypatch):
    (tmp_path / 'test_example.py').write_text('def test_value():\n    assert 2 + 2 == 4\n')
    monkeypatch.setattr(checks, 'check_plan', lambda _: [
        {'name': 'pytest', 'root': '.', 'command': [sys.executable, '-m', 'pytest', '-q']}])
    report = checks.run_repository_checks(RepositoryInventory(tmp_path))
    assert report['passed']
    assert '1 passed' in report['checks'][0]['output']


def test_fixer_reconstructs_file_and_rejects_truncated_response(tmp_path):
    from agents.fixer import BaseFixerAgent
    (tmp_path / 'source.py').write_bytes(''.join(f'value_{i} = {i}\n' for i in range(1000)).encode())
    builder = ContextBuilder(100)
    client = Mock()
    context = builder.build('source.py', tmp_path, 501)
    response = Mock()
    response.choices = [Mock(finish_reason='stop', message=Mock(
        content=context['primary'].replace('value_500 = 500', 'value_500 = 999')))]
    client.chat.completions.create.return_value = response
    result = BaseFixerAgent(client, builder).fix(
        {'file': 'source.py', 'line': 501, 'bug_type': 'LOGIC', 'error_message': 'wrong value'}, str(tmp_path))
    assert result['fixed_code'].startswith('value_0 = 0\n')
    assert result['fixed_code'].endswith('value_999 = 999\n')
    assert 'value_500 = 999\n' in result['fixed_code']
    response.choices[0].finish_reason = 'length'
    assert BaseFixerAgent(client, builder).fix(
        {'file': 'source.py', 'line': 501, 'bug_type': 'LOGIC'}, str(tmp_path)) == {}


@pytest.mark.parametrize('fenced', [False, True])
def test_fixer_preserves_indentation_inside_function_excerpt(tmp_path, fenced):
    from agents.fixer import BaseFixerAgent
    original = 'def calculate():\n' + ''.join(f'    value_{i} = {i}\n' for i in range(1000))
    (tmp_path / 'source.py').write_bytes(original.encode())
    builder = ContextBuilder(100)
    context = builder.build('source.py', tmp_path, 502)
    assert context['primary'].startswith('    ')
    replacement = context['primary'].replace('value_500 = 500', 'value_500 = 999')
    content = f'```python\n{replacement}```' if fenced else replacement
    client = Mock()
    client.chat.completions.create.return_value.choices = [Mock(
        finish_reason='stop', message=Mock(content=content))]
    fixed = BaseFixerAgent(client, builder).fix(
        {'file': 'source.py', 'line': 502, 'bug_type': 'LOGIC'}, str(tmp_path))['fixed_code']
    assert fixed == original.replace('value_500 = 500', 'value_500 = 999')
    compile(fixed, 'source.py', 'exec')


def test_fix_batches_leave_findings_for_later_iterations(tmp_path, monkeypatch):
    import agents.orchestrator as orchestrator
    from core.failure_ledger import FailureLedger
    from core.dependency_graph import DependencyGraph
    agent = orchestrator.OrchestratorAgent.__new__(orchestrator.OrchestratorAgent)
    agent.db = None
    agent.ledger = FailureLedger()
    agent.dep_graph = DependencyGraph()
    agent.memory = Mock()
    agent.memory.get_context.return_value = []
    agent.groq_client = None
    agent.ai_status = {'ready': True, 'provider': 'mock', 'reason': ''}
    agent.gemini_model = None
    agent.context_builder = ContextBuilder()
    agent.router = Mock()
    fake_fixer = Mock()
    fake_fixer.specialty = 'Python'
    fake_fixer.fix.return_value = {'fixed_code': 'value = 2\n'}
    agent.router.route.return_value = Mock(return_value=fake_fixer)
    monkeypatch.setattr(orchestrator, 'emit', Mock())
    failures = []
    for index in range(5):
        name = f'source_{index}.py'
        (tmp_path / name).write_text('value = 1\n')
        failures.append({'file': name, 'bug_type': 'LOGIC', 'line': 1, 'error_message': 'wrong value'})
    state = {'run_id': 'batch', 'repo_path': str(tmp_path), 'failures': failures,
             'iteration': 1, 'fixes_applied': [], 'repository_profile': {'policy': {'fix_batch_size': 2}}}
    agent.fix_node(state)
    assert len(state['fixes_applied']) == 2
    assert state['deferred_failures'] == 3
    # Validation removes repaired findings before the next batch begins.
    state['failures'] = failures[2:]
    agent.fix_node(state)
    assert len(state['fixes_applied']) == 4
    assert state['deferred_failures'] == 1


def test_dependency_blast_radius_handles_deep_graph_without_recursion():
    from core.dependency_graph import DependencyGraph
    graph = DependencyGraph()
    graph.nodes = [str(i) for i in range(3000)]
    graph._adjacency = {str(i): [str(i - 1)] if i else [] for i in range(3000)}
    assert graph.get_root_nodes() == ['0']
    assert graph.get_blast_radius('0') == 2999


@pytest.mark.parametrize('tool_exit,expected', [(0, 'passed'), (2, 'failed')])
def test_workflow_reports_real_check_outcomes_without_pushing(tmp_path, monkeypatch, tool_exit, expected):
    import agents.orchestrator as orchestrator
    import agents.analyzer as analyzer
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(orchestrator.config, 'GROQ_API_KEY', '')
    monkeypatch.setattr(orchestrator.config, 'GEMINI_API_KEY', '')
    monkeypatch.setattr(orchestrator, 'emit', Mock())
    monkeypatch.setattr(analyzer, 'emit', Mock())
    monkeypatch.setattr(checks, 'run_command', lambda *args: {
        'exit_code': tool_exit, 'output': 'check completed', 'complete': True})
    push = Mock()
    monkeypatch.setattr(orchestrator.git_ops, 'commit_and_push_all', push)
    (tmp_path / 'example.py').write_text('value = 1\n')
    result = orchestrator.OrchestratorAgent().run('workflow', 'local', str(tmp_path))
    assert result['status'] == expected
    assert result['results']['status'] == expected
    assert result['results']['repository_profile']['size'] == 'small'
    assert result['results']['analysis']['passed'] is (tool_exit == 0)
    push.assert_not_called()


def test_workflow_fixes_and_validates_before_push(tmp_path, monkeypatch):
    import agents.orchestrator as orchestrator
    import agents.analyzer as analyzer
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(orchestrator.config, 'GROQ_API_KEY', '')
    monkeypatch.setattr(orchestrator.config, 'GEMINI_API_KEY', '')
    monkeypatch.setattr(orchestrator, 'emit', Mock())
    monkeypatch.setattr(analyzer, 'emit', Mock())
    source = tmp_path / 'example.py'
    source.write_text('value = missing_name\n')

    def command(*args):
        broken = 'missing_name' in source.read_text()
        return {'exit_code': 1 if broken else 0, 'complete': True,
                'output': "example.py:1:9: F821 undefined name 'missing_name'" if broken else '1 passed'}

    monkeypatch.setattr(checks, 'run_command', command)
    push = Mock(return_value={'status': 'success', 'branch': 'WORKFLOW_FIX', 'commit_sha': 'verified-sha',
                              'verified': True})
    monkeypatch.setattr(orchestrator.git_ops, 'commit_and_push_all', push)
    agent = orchestrator.OrchestratorAgent()
    agent.ai_status = {'ready': True, 'provider': 'mock', 'reason': ''}
    fixer = Mock()
    fixer.specialty = 'Python'
    fixer.fix.return_value = {'fixed_code': 'value = 1\n'}
    agent.router = Mock()
    agent.router.route.return_value = Mock(return_value=fixer)
    result = agent.run('workflow-fix', 'local', str(tmp_path))
    assert result['status'] == 'passed'
    assert result['results']['analysis']['complete']
    assert result['results']['push']['status'] == 'success'
    assert source.read_text() == 'value = 1\n'
    push.assert_called_once()
