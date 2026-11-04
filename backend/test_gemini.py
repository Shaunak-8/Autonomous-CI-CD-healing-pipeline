import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from groq import RateLimitError

from core.gemini_client import GeminiModel


def test_gemini_uses_header_authentication_and_json_generation():
    def handler(request):
        assert request.headers['x-goog-api-key'] == 'example-key'
        assert not request.url.query
        if request.method == 'GET':
            return httpx.Response(200, json={'supportedGenerationMethods': ['generateContent']})
        body = json.loads(request.content)
        assert body['generationConfig']['responseMimeType'] == 'application/json'
        return httpx.Response(200, json={'candidates': [{
            'finishReason': 'STOP', 'content': {'parts': [{'text': '{"ok":true}'}]},
        }]})
    model = GeminiModel('example-key', 'gemini-test', transport=httpx.MockTransport(handler))
    assert model.readiness()['ready']
    assert json.loads(model.generate_content('Return JSON').text) == {'ok': True}


def test_gemini_errors_do_not_disclose_credentials():
    model = GeminiModel('example-secret', 'gemini-test', transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={'error': {'message': 'example-secret'}})))
    status = model.readiness()
    assert not status['ready']
    assert '403' in status['reason']
    assert 'example-secret' not in status['reason']


def test_groq_quota_failure_switches_to_gemini(tmp_path):
    from agents.fixer import BaseFixerAgent
    from core.context_builder import ContextBuilder
    (tmp_path / 'calc.py').write_bytes(b'value = 1\n')
    groq = Mock()
    groq.chat.completions.create.side_effect = RateLimitError(
        'limited', body={}, response=httpx.Response(429, request=httpx.Request('POST', 'https://example.invalid')))
    gemini = Mock()
    gemini.generate_content.return_value = SimpleNamespace(
        text=json.dumps({'edits': [{'old': 'value = 1', 'new': 'value = 2'}]}),
        candidates=[SimpleNamespace(finish_reason='STOP')])
    agent = BaseFixerAgent(groq, ContextBuilder(), gemini)
    agent.primary_provider = 'groq'
    result = agent.fix({'file': 'calc.py', 'line': 1, 'bug_type': 'LOGIC'}, str(tmp_path))
    assert result['provider'] == 'gemini'
    assert result['fixed_code'] == 'value = 2\n'
    assert groq.chat.completions.create.call_count == 1


def test_gemini_failure_switches_back_to_groq(tmp_path, monkeypatch):
    monkeypatch.setattr('agents.fixer.time.sleep', lambda delay: None)
    from agents.fixer import BaseFixerAgent
    from core.context_builder import ContextBuilder
    from core.gemini_client import GeminiProviderError
    (tmp_path / 'calc.py').write_bytes(b'value = 1\n')
    gemini = Mock()
    gemini.generate_content.side_effect = GeminiProviderError(503)
    groq = Mock()
    groq.chat.completions.create.return_value.choices = [SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=json.dumps({
            'edits': [{'old': 'value = 1', 'new': 'value = 2'}]})))]
    agent = BaseFixerAgent(groq, ContextBuilder(), gemini)
    agent.primary_provider = 'gemini'
    assert agent.fix({'file': 'calc.py', 'line': 1, 'bug_type': 'LOGIC'}, str(tmp_path))['provider'] == 'groq'


def test_all_provider_failures_report_both_causes_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr('agents.fixer.time.sleep', lambda delay: None)
    from agents.fixer import BaseFixerAgent
    from core.context_builder import ContextBuilder
    from core.gemini_client import GeminiProviderError
    (tmp_path / 'calc.py').write_bytes(b'value = 1\n')
    groq = Mock()
    groq.chat.completions.create.side_effect = RateLimitError(
        'private provider response', body={}, response=httpx.Response(
            429, request=httpx.Request('POST', 'https://example.invalid')))
    gemini = Mock()
    gemini.generate_content.side_effect = GeminiProviderError(503)
    agent = BaseFixerAgent(groq, ContextBuilder(), gemini)
    agent.primary_provider = 'groq'
    result = agent.fix({'file': 'calc.py', 'line': 1, 'bug_type': 'LOGIC'}, str(tmp_path))
    assert result['provider_failure'] and result['retryable']
    assert 'groq: rate limited' in result['error']
    assert 'gemini: temporarily unavailable' in result['error']
    assert 'private provider response' not in str(result)


def test_gemini_retries_temporary_failure_then_recovers(monkeypatch):
    from agents.fixer import BaseFixerAgent
    from core.gemini_client import GeminiProviderError
    delays = []
    monkeypatch.setattr('agents.fixer.time.sleep', delays.append)
    gemini = Mock()
    gemini.generate_content.side_effect = [GeminiProviderError(503), SimpleNamespace(text='{}', candidates=[])]
    agent = BaseFixerAgent(None, None, gemini)
    agent.primary_provider = 'gemini'
    assert agent._generate('prompt') == ('{}', 'gemini')
    assert delays == [2]
    assert gemini.generate_content.call_count == 2


def test_gemini_does_not_retry_invalid_credentials(monkeypatch):
    import pytest
    from agents.fixer import BaseFixerAgent, ProvidersUnavailable
    from core.gemini_client import GeminiProviderError
    sleep = Mock()
    monkeypatch.setattr('agents.fixer.time.sleep', sleep)
    gemini = Mock()
    gemini.generate_content.side_effect = GeminiProviderError(403)
    with pytest.raises(ProvidersUnavailable):
        BaseFixerAgent(None, None, gemini)._generate('prompt')
    sleep.assert_not_called()
    assert gemini.generate_content.call_count == 1
