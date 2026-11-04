"""Exercise publication against real disposable bare Git remotes."""
import subprocess

import pytest

from tools import git_ops


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(git_ops, 'GITHUB_TOKEN', '')
    remote = tmp_path / 'origin.git'
    seed = tmp_path / 'seed'
    work = tmp_path / 'work'
    seed.mkdir()
    git(tmp_path, 'init', '--bare', str(remote))
    git(seed, 'init', '-b', 'main')
    git(seed, 'config', 'user.name', 'Fixture')
    git(seed, 'config', 'user.email', 'fixture@example.invalid')
    (seed / 'source.py').write_text('value = 1\n')
    git(seed, 'add', 'source.py')
    git(seed, 'commit', '-m', 'Original source')
    git(seed, 'remote', 'add', 'origin', str(remote))
    git(seed, 'push', 'origin', 'main')
    git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    git(tmp_path, 'clone', str(remote), str(work))
    return work, remote, git(remote, 'rev-parse', 'main')


def test_push_creates_new_remote_branch_with_verified_fix_and_keeps_main(repository):
    work, remote, original = repository
    (work / 'source.py').write_text('value = 2\n')
    (work / '.venv').mkdir()
    (work / '.venv' / 'generated.txt').write_text('not source')
    branch = git_ops.generate_branch_name('Example', 'Owner')
    result = git_ops.commit_and_push_all(str(work), 'Correct value', branch)
    assert result['status'] == 'success'
    assert result['verified'] is True
    assert result['commit_sha'] == git(remote, 'rev-parse', branch)
    assert git(remote, 'show', f'{branch}:source.py') == 'value = 2'
    assert git(remote, 'ls-tree', '-r', '--name-only', branch) == 'source.py'
    assert git(remote, 'rev-parse', 'main') == original
    assert git(work, 'branch', '--show-current') == branch


def test_existing_remote_branch_is_never_updated(repository):
    work, remote, original = repository
    git(remote, 'branch', 'existing-fix', 'main')
    (work / 'source.py').write_text('value = 2\n')
    result = git_ops.commit_and_push_all(str(work), 'Correct value', 'existing-fix')
    assert result['error_type'] == 'BRANCH_EXISTS'
    assert git(remote, 'rev-parse', 'existing-fix') == original
    assert git(remote, 'rev-parse', 'main') == original


def test_branch_creation_failure_stops_before_commit(repository):
    work, remote, original = repository
    git(work, 'branch', 'existing-local')
    (work / 'source.py').write_text('value = 2\n')
    result = git_ops.commit_and_push_all(str(work), 'Correct value', 'existing-local')
    assert result['error_type'] == 'BRANCH_ERROR'
    assert git(work, 'rev-parse', 'HEAD') == original
    assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/heads') == 'refs/heads/main'


def test_lost_push_response_verifies_same_branch_without_duplicate_commit(repository, monkeypatch):
    work, remote, original = repository
    (work / 'source.py').write_text('value = 2\n')
    real_command = git_ops.run_git_command
    pushes = []

    def command(path, args, **kwargs):
        result = real_command(path, args, **kwargs)
        if args[:2] == ['git', 'push']:
            pushes.append(args)
            assert result[0]
            return False, '', 'Connection lost after server accepted the push'
        return result

    monkeypatch.setattr(git_ops, 'run_git_command', command)
    monkeypatch.setattr(git_ops.time, 'sleep', lambda delay: None)
    result = git_ops.commit_and_push_all(str(work), 'Correct value', 'retry-fix')
    assert result['status'] == 'success' and result['verified']
    assert result['branch'] == 'retry-fix'
    assert len(pushes) == 1
    assert git(remote, 'rev-parse', 'main') == original
    assert git(remote, 'rev-list', '--count', 'main..retry-fix') == '1'


def test_generated_branch_names_are_unique_within_same_second(monkeypatch):
    monkeypatch.setattr(git_ops.time, 'time', lambda: 123)
    assert git_ops.generate_branch_name() != git_ops.generate_branch_name()


def test_github_publication_links_use_credential_free_urls():
    result = git_ops._push_success('https://github.com/owner/repo.git', 'new-fix', 'abc123', 1)
    assert result['branch_url'] == 'https://github.com/owner/repo/tree/new-fix'
    assert result['commit_url'] == 'https://github.com/owner/repo/commit/abc123'


def test_workflow_runs_real_checks_pushes_fix_and_persists_branch(repository, tmp_path, monkeypatch):
    from unittest.mock import Mock
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    import agents.orchestrator as orchestration
    from db import models

    work, remote, _ = repository
    (work / 'source.py').write_text('value = missing_name\n')
    (work / 'test_source.py').write_text('from source import value\n\n\ndef test_value() -> None:\n    assert value == 2\n')
    git(work, 'add', 'source.py', 'test_source.py')
    git(work, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Broken fixture')
    git(work, 'push', 'origin', 'main')
    original = git(remote, 'rev-parse', 'main')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(orchestration.config, 'GROQ_API_KEY', '')
    monkeypatch.setattr(orchestration.config, 'GEMINI_API_KEY', '')
    engine = create_engine('sqlite:///:memory:')
    models.Run.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Run(id='publish-workflow', repo_url=str(remote), team_name='test', leader_name='test', branch_name='main'))
        db.commit()
        agent = orchestration.OrchestratorAgent(db=db)
        agent.ai_status = {'ready': True, 'provider': 'fixture', 'reason': ''}
        fixer = Mock()
        fixer.specialty = 'Python'
        fixer.fix.return_value = {'fixed_code': 'value = 2\n', 'explanation': 'Fix undefined name'}
        agent.router = Mock()
        agent.router.route.return_value = Mock(return_value=fixer)
        result = agent.run('publish-workflow', str(remote), str(work))['results']
        assert result['status'] == 'passed'
        assert {check['name'] for check in result['analysis']['checks']} == {'flake8', 'mypy', 'pytest'}
        assert all(check['status'] == 'passed' for check in result['analysis']['checks'])
        assert result['push']['status'] == 'success' and result['push']['verified']
        row = db.get(models.Run, 'publish-workflow')
        assert row.branch_name == result['branch'] == result['push']['branch']
        assert row.memory['push']['commit_sha'] == git(remote, 'rev-parse', row.branch_name)
        assert git(remote, 'show', f'{row.branch_name}:source.py') == 'value = 2'
        assert git(remote, 'rev-parse', 'main') == original
    engine.dispose()
