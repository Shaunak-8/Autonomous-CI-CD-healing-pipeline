"""Bounded source excerpts with offsets for safe, localized replacement."""
import ast
import hashlib
import math
from pathlib import Path

from core.repository import positive_setting, safe_path


class ContextBuilder:
    def __init__(self, max_tokens=4000, max_file_bytes=None):
        if max_tokens <= 0:
            raise ValueError('max_tokens must be positive')
        self.max_tokens = max_tokens
        self.max_file_bytes = max_file_bytes or positive_setting('MAX_SOURCE_FILE_BYTES', 1024 * 1024)

    @staticmethod
    def _estimate_tokens(text):
        # An estimate, not a provider tokenizer. Budget UTF-8 bytes / 3.
        return math.ceil(len(text.encode('utf-8')) / 3)

    def _read_file_safe(self, path):
        try:
            with open(path, 'rb') as handle:
                content = handle.read(self.max_file_bytes + 1)
            if len(content) > self.max_file_bytes or b'\x00' in content:
                return ''
            return content.decode('utf-8')
        except (OSError, UnicodeError):
            return ''

    @staticmethod
    def _clip(text, byte_limit):
        return text.encode('utf-8')[:max(0, byte_limit)].decode('utf-8', errors='ignore')

    @staticmethod
    def _extract_imports(source):
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            return []
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        return list(dict.fromkeys(imports))

    def build(self, primary_file, repo_path, line=0):
        path = safe_path(repo_path, primary_file)
        source = self._read_file_safe(path)
        if not source:
            raise ValueError('Source is empty, unreadable, binary, or exceeds MAX_SOURCE_FILE_BYTES')
        budget = self.max_tokens * 3
        primary_budget = int(budget * .7)
        lines = source.splitlines(keepends=True)
        focus = max(0, min(int(line or 1) - 1, len(lines) - 1))
        left, right = focus, focus + 1
        size = len(lines[focus].encode('utf-8'))
        if size > primary_budget:
            raise ValueError('The failing source line exceeds the context budget')
        while left > 0 or right < len(lines):
            changed = False
            if left > 0 and size + len(lines[left - 1].encode('utf-8')) <= primary_budget:
                left -= 1
                size += len(lines[left].encode('utf-8'))
                changed = True
            if right < len(lines) and size + len(lines[right].encode('utf-8')) <= primary_budget:
                size += len(lines[right].encode('utf-8'))
                right += 1
                changed = True
            if not changed:
                break
        primary = ''.join(lines[left:right])
        start, end = sum(map(len, lines[:left])), sum(map(len, lines[:right]))
        remaining = budget - len(primary.encode('utf-8'))
        test_file = ''
        relative = Path(primary_file)
        for candidate in (relative.with_name(f'test_{relative.name}'),
                          Path('tests') / f'test_{relative.name}',
                          relative.with_name(f'{relative.stem}_test{relative.suffix}')):
            content = self._read_file_safe(safe_path(repo_path, candidate))
            if content:
                test_file = self._clip(content, remaining // 2)
                remaining -= len(test_file.encode('utf-8'))
                break
        imports = []
        for module in self._extract_imports(source)[:8]:
            for candidate in (Path(*module.split('.')).with_suffix('.py'),
                              Path(*module.split('.')) / '__init__.py'):
                content = self._read_file_safe(safe_path(repo_path, candidate))
                overhead = len(candidate.as_posix().encode('utf-8'))
                if content and remaining > overhead:
                    content = self._clip(content, min(remaining - overhead, budget // 10))
                    imports.append({'path': candidate.as_posix(), 'content': content})
                    remaining -= overhead + len(content.encode('utf-8'))
                    break
        text = primary + test_file + ''.join(i['path'] + i['content'] for i in imports)
        return {
            'primary': primary, 'imports': imports, 'test_file': test_file,
            'token_count': self._estimate_tokens(text),
            'primary_complete': left == 0 and right == len(lines),
            'start_offset': start, 'end_offset': end,
            'start_line': left + 1, 'end_line': right,
            'source_hash': hashlib.sha256(source.encode('utf-8')).hexdigest(),
        }

    def apply_replacement(self, context, replacement, primary_file, repo_path):
        source = self._read_file_safe(safe_path(repo_path, primary_file))
        if hashlib.sha256(source.encode('utf-8')).hexdigest() != context['source_hash']:
            raise ValueError('Source changed while the fix was being generated')
        if not replacement.strip():
            raise ValueError('Empty replacement')
        newline = '\r\n' if '\r\n' in source else '\n'
        replacement = replacement.replace('\r\n', '\n').replace('\n', newline)
        if (context['end_offset'] < len(source) or context['primary'].endswith('\n')) and not replacement.endswith('\n'):
            replacement += newline
        return source[:context['start_offset']] + replacement + source[context['end_offset']:]


def build_context(failure):
    return str(ContextBuilder().build(failure.file, '.', getattr(failure, 'line', 0)))
