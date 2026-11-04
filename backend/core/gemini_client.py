"""Gemini generateContent adapter with header authentication and bounded requests."""
from types import SimpleNamespace

import httpx


class GeminiProviderError(RuntimeError):
    def __init__(self, status):
        self.status = status
        self.retryable = status == 429 or status >= 500
        super().__init__(f'Gemini request failed (HTTP {status})')


class GeminiModel:
    def __init__(self, api_key, model, transport=None):
        self.api_key = api_key
        self.model = model.removeprefix('models/')
        self.transport = transport

    def _request(self, method, path, **kwargs):
        try:
            with httpx.Client(base_url='https://generativelanguage.googleapis.com/v1beta/',
                              headers={'x-goog-api-key': self.api_key}, timeout=60,
                              transport=self.transport) as client:
                response = client.request(method, path, **kwargs)
        except httpx.RequestError:
            raise GeminiProviderError(503) from None
        if response.status_code != 200:
            raise GeminiProviderError(response.status_code)
        return response.json()

    def readiness(self):
        try:
            model = self._request('GET', f'models/{self.model}')
            if 'generateContent' not in model.get('supportedGenerationMethods', []):
                raise GeminiProviderError(404)
            return {'ready': True, 'provider': 'gemini', 'model': self.model, 'reason': ''}
        except GeminiProviderError as exc:
            return {'ready': False, 'provider': 'gemini', 'model': self.model,
                    'reason': f'Gemini is unavailable (HTTP {exc.status}). Check its API key, model, and quota.'}

    def generate_content(self, prompt, json_mode=True):
        options = {'temperature': 0, 'maxOutputTokens': 2048}
        if json_mode:
            options['responseMimeType'] = 'application/json'
        result = self._request('POST', f'models/{self.model}:generateContent', json={
            'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
            'generationConfig': options,
        })
        candidates = result.get('candidates', [])
        if not candidates:
            raise RuntimeError('Gemini returned no usable completion')
        candidate = candidates[0]
        text = ''.join(p.get('text', '') for p in candidate.get('content', {}).get('parts', []))
        return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=candidate.get('finishReason'))])
