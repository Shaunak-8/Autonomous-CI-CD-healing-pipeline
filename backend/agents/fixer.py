import os
import re
import hashlib
import json
import time
from collections import Counter
from groq import RateLimitError
from groq import Groq
import config
from core.gemini_client import GeminiModel, GeminiProviderError

from agents.schema import FailureRecord


class ProvidersUnavailable(RuntimeError):
    def __init__(self, errors):
        self.providers = []
        for provider, error in errors:
            status = getattr(error, 'status', None) or getattr(error, 'status_code', None)
            retryable = isinstance(error, RateLimitError) or getattr(error, 'retryable', False) or bool(status and status >= 500)
            reason = ('rate limited' if isinstance(error, RateLimitError) or status == 429 else
                      'authentication or permission rejected' if status in (401, 403) else
                      'temporarily unavailable' if retryable else 'request failed')
            self.providers.append({'provider': provider, 'status': status, 'reason': reason, 'retryable': retryable})
        self.retryable = any(p['retryable'] for p in self.providers)
        summary = '; '.join(f"{p['provider']}: {p['reason']}" for p in self.providers)
        super().__init__(f'AI generation unavailable ({summary}). No further provider requests will be made in this run.')

class FixerRouter:
    @staticmethod
    def route(bug_type: str, file_path: str = ""):
        if file_path.endswith((".cpp", ".cc", ".h", ".hpp", ".c")):
            return CPPFixerAgent
            
        mapping = {
            "LINTING": PythonFixerAgent,
            "TYPE_ERROR": TypeFixerAgent,
            "SYNTAX": SyntaxFixerAgent,
            "LOGIC": LogicFixerAgent,
            "IMPORT": ImportFixerAgent,
            "JS": JSFixerAgent
        }
        return mapping.get(bug_type, PythonFixerAgent)

class BaseFixerAgent:
    specialty = "software"

    def __init__(self, groq_client: Groq, context_builder, gemini_model=None):
        self.groq_client = groq_client
        self.gemini_model = gemini_model
        self.context_builder = context_builder

    def _groq_response(self, prompt):
        for attempt in range(3):
            try:
                return self.groq_client.chat.completions.create(
                    model=config.GROQ_MODEL, messages=[{'role': 'user', 'content': prompt}],
                    temperature=0.0, max_completion_tokens=2048, response_format={'type': 'json_object'})
            except RateLimitError as exc:
                if self.gemini_model:
                    raise
                if attempt == 2:
                    raise
                try:
                    delay = min(90, max(1, float(exc.response.headers.get('retry-after', 15 * (attempt + 1)))))
                except (TypeError, ValueError):
                    delay = 15 * (attempt + 1)
                notify = getattr(self, 'on_progress', None)
                if notify:
                    notify(f'Provider rate limit; retrying in {delay:g}s (attempt {attempt + 2}/3).')
                while delay > 0:
                    part = min(delay, 15)
                    time.sleep(part)
                    delay -= part

    def _gemini_response(self, prompt):
        for attempt in range(3):
            try:
                return self.gemini_model.generate_content(prompt)
            except GeminiProviderError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                delay = 2 ** (attempt + 1)
                notify = getattr(self, 'on_progress', None)
                if notify:
                    notify(f'Gemini temporarily unavailable (HTTP {exc.status}); retrying in {delay}s (attempt {attempt + 2}/3).')
                time.sleep(delay)

    def _generate(self, prompt):
        primary = getattr(self, 'primary_provider', config.LLM_PRIMARY_PROVIDER)
        providers = ['gemini', 'groq'] if primary == 'gemini' else ['groq', 'gemini']
        errors = []
        for provider in providers:
            if provider == 'gemini' and self.gemini_model is None:
                continue
            if provider == 'groq' and self.groq_client is None:
                continue
            try:
                if provider == 'gemini':
                    response = self._gemini_response(prompt)
                    candidates = getattr(response, 'candidates', [])
                    if candidates and str(candidates[0].finish_reason) in {'2', 'MAX_TOKENS', 'FinishReason.MAX_TOKENS'}:
                        return None, provider
                    return response.text.strip('\r\n'), provider
                response = self._groq_response(prompt)
                if response.choices[0].finish_reason == 'length':
                    return None, provider
                return response.choices[0].message.content.strip('\r\n'), provider
            except Exception as exc:
                errors.append((provider, exc))
                notify = getattr(self, 'on_progress', None)
                if notify:
                    notify(f'{provider} could not complete the request; checking the alternate provider.')
        if errors:
            raise ProvidersUnavailable(errors)
        raise RuntimeError('No generation provider is configured')


    def fix(self, failure: dict, repo_path: str, history: list = None) -> dict:
        if not self.groq_client and not self.gemini_model:
            # No LLM configured; run without auto-fix capability.
            return {}

        # Build context
        file_path = os.path.join(repo_path, failure["file"])
        context = self.context_builder.build(failure["file"], repo_path, failure.get('line', 0))

        # Build prompt
        history_str = ""
        if history:
            history_str = "\nPrevious failed attempts:\n" + "\n".join(
                [f"- {h['fix'][:100]}... -> {h['result']}" for h in history[-3:]]
            )

        import_context = '\n'.join(f"{item['path']}:\n{item['content']}" for item in context.get('imports', []))
        prompt = f"""You are a senior {self.specialty} engineer. Fix the following code failure.

File: {failure['file']}
Bug type: {failure['bug_type']}
Error: {str(failure.get('error_message', failure.get('message', 'Unknown')))[:1500]}

Source excerpt (lines {context['start_line']}-{context['end_line']}):
{context.get('primary', '')}

Test file:
{context.get('test_file', 'N/A')}
Related imports (reference only):
{import_context}
{history_str}

Return ONLY JSON: {{"edits":[{{"old":"exact original code", "new":"corrected code"}}]}}.
Use minimal, non-overlapping edits confined to the source excerpt. Each old string must occur exactly once.
Preserve indentation. Do not change reference tests or rewrite the whole file.
No markdown, explanations, or changes to unrelated logic."""
        prompt += '\nFix the underlying types and logic. Do not introduce type: ignore, noqa, ts-ignore, lint-disable directives, or replace known types with Any to silence checks.'

        try:
            fixed_code, provider = self._generate(prompt)
            if fixed_code is None:
                return {}

            
            # Robust code extraction
            # 1. Try flexible regex (handles space before language tag and optional newlines)
            code_match = re.search(r"```[ \t]*(?:\w+)?[ \t]*\n?(.*?)\s*```", fixed_code, re.DOTALL)
            if code_match:
                fixed_code = code_match.group(1).strip('\r\n')
            else:
                # 2. Fallback: Manual block extraction
                lines = fixed_code.splitlines()
                if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
                    # Extract everything between the first and last lines
                    fixed_code = "\n".join(lines[1:-1]).strip('\r\n')
                else:
                    # 3. Last resort: just strip backticks
                    fixed_code = fixed_code.replace("```", "").strip('\r\n')

            if fixed_code.lstrip().startswith('{'):
                payload = json.loads(fixed_code)
                edits = payload.get('edits')
                if not isinstance(edits, list) or not 1 <= len(edits) <= 20:
                    raise ValueError('The provider returned an invalid edit list')
                primary = context['primary'].replace('\r\n', '\n')
                replacements = []
                for edit in edits:
                    old, new = edit.get('old'), edit.get('new')
                    if isinstance(old, str):
                        old = old.replace('\r\n', '\n')
                    if isinstance(new, str):
                        new = new.replace('\r\n', '\n')
                    if not isinstance(old, str) or not isinstance(new, str) or not old or primary.count(old) != 1:
                        raise ValueError('An edit does not uniquely match the source excerpt')
                    start = primary.index(old)
                    replacements.append((start, start + len(old), new))
                replacements.sort()
                if any(a[1] > b[0] for a, b in zip(replacements, replacements[1:])):
                    raise ValueError('Overlapping edits were rejected')
                for start, end, new in reversed(replacements):
                    primary = primary[:start] + new + primary[end:]
                fixed_code = primary
            fixed_code = self.context_builder.apply_replacement(
                context, fixed_code, failure['file'], repo_path)
            bypasses = r'#\s*(?:type:\s*ignore(?:\[[^\]]*\])?|noqa(?::[^\n]*)?|mypy:\s*ignore-errors)|//\s*@ts-(?:ignore|nocheck)|(?://|/\*)\s*eslint-disable[^\n]*'
            with open(file_path, encoding='utf-8') as handle:
                original = handle.read()
            introduced = Counter(re.findall(bypasses, fixed_code, re.I)) - Counter(re.findall(bypasses, original, re.I))
            if introduced:
                raise ValueError('Changes that add lint or type-check suppression directives are not allowed')
            if hashlib.sha256(fixed_code.encode('utf-8')).hexdigest() == context['source_hash']:
                return {}
            return {
                "fixed_code": fixed_code,
                "provider": provider,
                "explanation": f"Fixed {failure['bug_type']} in {failure['file']}",
                "lines_changed": len(fixed_code.splitlines()),
                "touches_imports": "import" in fixed_code[:200],
                "blast_radius": 1
            }
        except Exception as e:
            if isinstance(e, ProvidersUnavailable):
                return {'error': str(e), 'provider_failure': True, 'retryable': e.retryable,
                        'providers': e.providers}
            if isinstance(e, ValueError):
                return {'error': f'Generated edit was rejected: {e}. The original source was preserved.'}
            return {'error': f'Fix generation failed ({type(e).__name__}). Check model access, provider quota, and credentials.',
                    'retryable': isinstance(e, RateLimitError) or getattr(e, 'retryable', False)}

class PythonFixerAgent(BaseFixerAgent):
    specialty = "Python"

class TypeFixerAgent(BaseFixerAgent):
    specialty = "Python typing"

class SyntaxFixerAgent(BaseFixerAgent):
    specialty = "Python syntax"

class LogicFixerAgent(BaseFixerAgent):
    specialty = "Python logic"

class ImportFixerAgent(BaseFixerAgent):
    specialty = "Python imports"

class JSFixerAgent(BaseFixerAgent):
    specialty = "JavaScript"

class CPPFixerAgent(BaseFixerAgent):
    specialty = "C++"

# --- Standalone generate_fix for CLI orchestrator ---

def generate_fix(context: str, failure: FailureRecord) -> str:
    gemini_key = os.getenv("GEMINI_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")

    prompt = f"""
Fix the following python code failure.
File: {failure.file}
Line: {failure.line}
Error: {failure.error_type} - {failure.message}
Context:
{context}

Return ONLY the fixed python code snippet. No markdown blocks, no explanations.
"""

    if gemini_key and gemini_key != "your_gemini_api_key_here" and (config.LLM_PRIMARY_PROVIDER == 'gemini' or not groq_key):
        try:
            model = GeminiModel(gemini_key, config.GEMINI_MODEL)
            response = model.generate_content(prompt, json_mode=False)
            return response.text.strip()
        except Exception as e:
            print(f"Gemini error in generate_fix: {e}. Falling back to Groq.")

    if not groq_key:
        print("WARNING: No LLM API keys configured. Returning dummy fix.")
        return "# Dummy fix generated\npass"

    try:
        client = Groq(api_key=groq_key)
        response = client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error calling Groq: {e}")
        return ""
