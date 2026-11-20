"""Source inventory and adaptive policies shared by repository analyzers."""
import os
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

IGNORED_DIRS = {
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env',
    'dist', 'build', 'coverage', '.next', '.nuxt', '.cache', '.pytest_cache',
    '.mypy_cache', '.tox', 'vendor', 'target', 'workspaces', 'results',
}
LANGUAGES = {
    '.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
    '.ts': 'javascript', '.tsx': 'javascript', '.mjs': 'javascript',
    '.cjs': 'javascript', '.c': 'cpp', '.cc': 'cpp', '.cpp': 'cpp',
    '.h': 'cpp', '.hpp': 'cpp', '.go': 'go', '.java': 'java',
    '.rs': 'rust', '.rb': 'ruby', '.cs': 'csharp', '.php': 'php',
}


def positive_setting(name, default):
    value = int(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


def safe_path(root, relative):
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError('Source path escapes repository')
    return candidate


@dataclass(frozen=True)
class AnalysisPolicy:
    size: str
    timeout_seconds: int
    fix_batch_size: int
    max_fixes_per_run: int
    context_tokens: int
    max_file_bytes: int

    @classmethod
    def for_size(cls, size):
        medium = size != 'small'
        return cls(
            size=size,
            timeout_seconds=positive_setting(
                'MEDIUM_CHECK_TIMEOUT' if medium else 'SMALL_CHECK_TIMEOUT',
                600 if medium else 120),
            fix_batch_size=min(5, positive_setting('FIX_BATCH_SIZE', 5)),
            max_fixes_per_run=max(0, int(os.getenv('MAX_FIXES_PER_RUN', '0'))),
            context_tokens=positive_setting('LLM_CONTEXT_TOKENS', 4000),
            max_file_bytes=positive_setting('MAX_SOURCE_FILE_BYTES', 1024 * 1024),
        )


class RepositoryInventory:
    def __init__(self, repo_path):
        self.root = Path(repo_path).resolve()
        if not self.root.is_dir():
            raise ValueError('Repository directory does not exist')
        self.files = {}
        self.manifests = []
        self.skipped = []
        self.errors = []
        limit = positive_setting('MAX_SOURCE_FILE_BYTES', 1024 * 1024)
        for current, dirs, names in os.walk(self.root, onerror=lambda e: self.errors.append(str(e))):
            included_dirs = []
            for name in sorted(dirs):
                if name in IGNORED_DIRS:
                    continue
                directory = Path(current) / name
                if directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)():
                    self.skipped.append({'path': directory.relative_to(self.root).as_posix(), 'reason': 'symlink or junction'})
                else:
                    included_dirs.append(name)
            dirs[:] = included_dirs
            for name in sorted(names):
                path = Path(current) / name
                if path.is_symlink():
                    self.skipped.append({'path': path.relative_to(self.root).as_posix(), 'reason': 'symlink'})
                    continue
                rel = path.relative_to(self.root).as_posix()
                if name in {'package.json', 'pyproject.toml', 'pytest.ini', 'setup.py', 'requirements.txt'}:
                    self.manifests.append(rel)
                language = LANGUAGES.get(path.suffix.lower())
                if not language or name.endswith(('.min.js', '.min.css', '.d.ts')):
                    continue
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    self.errors.append(f'{rel}: {exc}')
                    continue
                self.files[rel] = {'bytes': size, 'language': language}
                if size > limit:
                    self.skipped.append({'path': rel, 'reason': 'oversized source'})
        self.source_bytes = sum(f['bytes'] for f in self.files.values())
        count = len(self.files)
        if count <= positive_setting('SMALL_REPO_MAX_FILES', 500) and self.source_bytes <= positive_setting('SMALL_REPO_MAX_BYTES', 5 * 1024 * 1024):
            self.size = 'small'
        elif count <= positive_setting('MEDIUM_REPO_MAX_FILES', 5000) and self.source_bytes <= positive_setting('MEDIUM_REPO_MAX_BYTES', 50 * 1024 * 1024):
            self.size = 'medium'
        else:
            self.size = 'large'
        self.policy = AnalysisPolicy.for_size(self.size)

    def source_files(self, language=None):
        blocked = {s['path'] for s in self.skipped}
        return [p for p, data in self.files.items()
                if p not in blocked and (language is None or data['language'] == language)]

    def summary(self):
        return {
            'size': self.size, 'source_files': len(self.files),
            'source_bytes': self.source_bytes,
            'languages': dict(Counter(f['language'] for f in self.files.values())),
            'skipped_files': self.skipped, 'scan_errors': self.errors,
            'policy': asdict(self.policy),
        }
