import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpx
from fastapi import HTTPException
from supabase_auth.errors import AuthApiError

import api.auth as auth


@pytest.mark.parametrize('header', [None, '', 'Bearer', 'Basic value', 'Bearer undefined', 'Bearer null', 'Bearer a b'])
def test_rejects_missing_and_malformed_tokens(header):
    request = SimpleNamespace(headers={'Authorization': header}, state=SimpleNamespace())
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verify_token(request))
    assert error.value.status_code == 401


def test_uses_supabase_verification_and_sets_authenticated_identity(monkeypatch):
    get_user = Mock(return_value=SimpleNamespace(user=SimpleNamespace(id='user-1', email='user@example.com')))
    monkeypatch.setattr(auth, 'supabase', SimpleNamespace(auth=SimpleNamespace(get_user=get_user)))
    request = SimpleNamespace(headers={'Authorization': 'bearer example-token'}, state=SimpleNamespace())
    result = asyncio.run(auth.verify_token(request))
    assert result == {'sub': 'user-1', 'email': 'user@example.com'}
    assert request.state.user_id == 'user-1'
    get_user.assert_called_once_with('example-token')


@pytest.mark.parametrize('provider_error,status', [
    (AuthApiError('private provider error', 401, None), 401),
    (AuthApiError('private provider error', 500, None), 503),
    (httpx.ConnectError('unreachable provider'), 503),
])
def test_provider_failures_are_safe_and_distinguish_unavailable_service(monkeypatch, provider_error, status):
    get_user = Mock(side_effect=provider_error)
    monkeypatch.setattr(auth, 'supabase', SimpleNamespace(auth=SimpleNamespace(get_user=get_user)))
    request = SimpleNamespace(headers={'Authorization': 'Bearer example-token'}, state=SimpleNamespace())
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verify_token(request))
    assert error.value.status_code == status
    assert 'private provider error' not in error.value.detail
    assert 'example-token' not in error.value.detail


def test_run_cannot_bypass_authentication_or_create_data():
    from api.routes import run_agent
    request = SimpleNamespace(headers={}, state=SimpleNamespace())
    background = Mock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(run_agent(SimpleNamespace(), request, background, Mock()))
    assert error.value.status_code == 401
    background.add_task.assert_not_called()


def test_clone_prefers_the_users_token_over_the_server_default(tmp_path, monkeypatch):
    from tools import git_ops
    monkeypatch.setattr(git_ops, 'GITHUB_TOKEN', 'example-server-token')
    command = Mock()
    monkeypatch.setattr(git_ops.subprocess, 'run', command)
    assert git_ops.clone_repo('https://github.com/example/repo', str(tmp_path / 'clone'), token='example-user-token')
    url = command.call_args_list[0].args[0][-2]
    assert 'example-user-token' in url
    assert 'example-server-token' not in url
