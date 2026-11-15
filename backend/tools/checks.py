"""Bounded tool execution with explicit outcomes, including incomplete checks."""
import json
import ast
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse
from pathlib import Path

from core.repository import IGNORED_DIRS, safe_path


def run_command(cmd, cwd, timeout, output_limit=2 * 1024 * 1024, progress=None, progress_interval=15):
    env = os.environ.copy()
    env.update({'CI': 'true', 'NO_COLOR': '1'})
    env.update({'PYTHONPATH': str(cwd), 'MYPYPATH': str(cwd), 'PYTHONIOENCODING': 'utf-8'})
    for key in ('GROQ_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GITHUB_TOKEN', 'GITHUB_CLIENT_SECRET', 'E2B_API_KEY',
                'SUPABASE_SERVICE_KEY', 'SUPABASE_JWT_SECRET', 'DATABASE_URL'):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='healing-output-') as output_dir, \
            open(Path(output_dir) / 'stdout', 'w+b') as stdout, \
            open(Path(output_dir) / 'stderr', 'w+b') as stderr:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=stdout, stderr=stderr, env=env,
                                    start_new_session=os.name != 'nt',
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        except OSError as exc:
            return {'exit_code': None, 'output': str(exc), 'complete': False}
        timed_out = False
        started = time.monotonic()
        try:
            if progress is None:
                proc.wait(timeout=timeout)
            else:
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    try:
                        proc.wait(timeout=min(max(.01, progress_interval), remaining))
                        break
                    except subprocess.TimeoutExpired:
                        elapsed = time.monotonic() - started
                        if elapsed >= timeout:
                            raise
                        tails = []
                        for name in ('stdout', 'stderr'):
                            # Separate handles preserve the child's current write offset.
                            with open(Path(output_dir) / name, 'rb') as reader:
                                reader.seek(0, os.SEEK_END)
                                reader.seek(max(0, reader.tell() - 1600))
                                tails.extend(reader.read().decode('utf-8', errors='replace').splitlines()[-2:])
                        detail = ' | '.join(line.strip() for line in tails if line.strip())[-800:]
                        detail = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1***@', detail)
                        progress(f'Still running: {elapsed:.0f}s elapsed, {max(0, timeout - elapsed):.0f}s remaining.'
                                 + (f' {detail}' if detail else ' Waiting for installer output.'))
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.kill()
            proc.wait()
        chunks = []
        truncated = False
        for stream in (stdout, stderr):
            stream.seek(0)
            chunk = stream.read(output_limit + 1)
            truncated |= len(chunk) > output_limit
            chunks.append(chunk[:output_limit].decode('utf-8', errors='replace'))
        output = '\n'.join(chunks)
        if timed_out:
            output += f'\nCheck timed out after {timeout}s.'
        if truncated:
            output += '\nCheck output exceeded the capture limit.'
        return {'exit_code': proc.returncode, 'output': output,
                'complete': not timed_out and not truncated}


def _package_roots(inventory, language, manifests):
    candidates = {str(Path(p).parent).replace('\\', '/') for p in inventory.manifests
                  if Path(p).name in manifests}
    roots = set()
    for file in inventory.source_files(language):
        parent = Path(file).parent
        roots.add(next((p.as_posix() for p in (parent, *parent.parents) if p.as_posix() in candidates), '.'))
    return sorted(roots)


def check_plan(inventory):
    plan = []
    for root in _package_roots(inventory, 'python', {'pyproject.toml', 'pytest.ini', 'setup.py', 'requirements.txt'}):
        directory = safe_path(inventory.root, root)
        local_python = directory / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        python = str(local_python) if local_python.is_file() else sys.executable
        configured_lint = any((directory / name).exists() for name in ('.flake8', 'setup.cfg', 'tox.ini'))
        for name, args in (
            ('flake8', ['-m', 'flake8', '--exclude=' + ','.join(sorted(IGNORED_DIRS)),
                        *([] if configured_lint else ['--select=E9,F63,F7,F82']), '.']),
            ('mypy', ['-m', 'mypy', '--explicit-package-bases', '--exclude', r'(^|/|\\)(' + '|'.join(re.escape(d) for d in sorted(IGNORED_DIRS)) + r')(/|\\)', '.']),
            ('pytest', ['-m', 'pytest', '--tb=short', '-q', *[
                f'--ignore-glob=*/{directory}/*' for directory in sorted(IGNORED_DIRS)]]),
        ):
            plan.append({'name': name, 'root': root, 'command': [python, *args]})
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm') or 'npm'
    for root in _package_roots(inventory, 'javascript', {'package.json'}):
        try:
            path = safe_path(inventory.root, f'{root}/package.json')
            if path.stat().st_size > inventory.policy.max_file_bytes:
                raise ValueError('Oversized package manifest')
            package = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(package, dict):
                raise ValueError('Package manifest must be an object')
            scripts = package.get('scripts', {})
            if not isinstance(scripts, dict) or not all(isinstance(v, str) for v in scripts.values()):
                raise ValueError('Package scripts must be strings')
            manifest_error = ''
        except (OSError, ValueError) as exc:
            scripts = {}
            manifest_error = str(exc)
        for name in ('lint', 'test', 'build'):
            if name == 'build' and name not in scripts:
                continue
            command = [npm, 'run', name] if name in scripts else None
            if name == 'lint' and command and re.match(r'^eslint\b', scripts[name].strip()):
                command += ['--', '--format', 'json']
            plan.append({'name': f'npm:{name}', 'root': root,
                         'command': command,
                         'reason': manifest_error or f'No {name} script is configured'})
    cpp_files = inventory.source_files('cpp')
    if cpp_files:
        compiler = shutil.which('g++')
        for source in cpp_files:
            plan.append({'name': 'cpp:syntax', 'root': '.',
                         'command': [compiler, '-fsyntax-only', source] if compiler else None,
                         'reason': 'g++ is not installed'})
        plan.append({'name': 'cpp:tests', 'root': '.', 'command': None,
                     'reason': 'Syntax checks alone cannot validate C/C++; configure a project build/test runner'})
    for language in sorted({d['language'] for d in inventory.files.values()} - {'python', 'javascript', 'cpp'}):
        plan.append({'name': language, 'root': '.', 'command': None,
                     'reason': f'No repository validation runner configured for {language}'})
    return plan


def _findings(check, inventory):
    findings = []
    known_files = set(inventory.source_files())
    root = Path(check['root'])
    if check['name'] == 'npm:lint':
        # npm prefixes its own script banner; ESLint's JSON starts afterwards.
        output = check['output']
        start = output.find('[{')
        if start >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(output[start:])
                for item in data:
                    for message in item.get('messages', []):
                        path = safe_path(inventory.root, item['filePath'])
                        relative = path.relative_to(inventory.root).as_posix()
                        if relative in known_files:
                            findings.append({'file': relative, 'line': message.get('line', 0),
                                             'bug_type': 'JS', 'error_message': message.get('message', '')[:2000],
                                             'severity': 'high' if message.get('fatal') else 'low'})
                return findings
            except (ValueError, KeyError, TypeError):
                pass

    def output_path(file):
        """Resolve tool-reported paths without accepting paths outside the repo."""
        file = unquote(file)
        if file.startswith('file://'):
            parsed = urlparse(file)
            file = parsed.path if not parsed.netloc else f'//{parsed.netloc}{parsed.path}'
        path = Path(file)
        candidate = path if path.is_absolute() else safe_path(inventory.root, root / path)
        candidate = candidate.resolve()
        if not candidate.is_relative_to(inventory.root):
            raise ValueError('Tool output path escapes repository')
        return candidate.relative_to(inventory.root).as_posix()

    for line in check['output'].splitlines():
        match = re.match(r'(.+?):(\d+)(?::\d+)?(?::\s*(.*))?$', line)
        if match:
            file, number, message = match.groups()
            message = message or ''
        else:
            match = re.match(r'FAILED\s+(\S+?\.py)(?:::|\s)(.*)', line)
            if not match:
                continue
            file, message = match.groups()
            number = 0
        try:
            relative = output_path(file)
        except (OSError, ValueError):
            continue
        if relative not in known_files:
            continue
        name = check['name']
        kind = 'LINTING' if name == 'flake8' else 'TYPE_ERROR' if name == 'mypy' else 'JS' if name.startswith('npm:') else 'LOGIC'
        if name.startswith('npm:') and re.search(r'SyntaxError|Unexpected token|Unexpected identifier', check['output'], re.I):
            kind = 'SYNTAX'
            if not message:
                message = next((error_line.strip() for error_line in check['output'].splitlines()
                                if re.search(r'SyntaxError|Unexpected token|Unexpected identifier', error_line, re.I)),
                               message)
        if name == 'flake8' and message.startswith('E9'):
            kind = 'SYNTAX'
        if name == 'cpp:syntax':
            kind = 'SYNTAX'
        if name == 'pytest' and (Path(relative).name.startswith('test_') or 'tests' in Path(relative).parts):
            try:
                tree = ast.parse(safe_path(inventory.root, relative).read_text(encoding='utf-8'))
                candidates = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        candidate = (root / (node.module.replace('.', '/') + '.py')).as_posix()
                        if candidate in known_files and not Path(candidate).name.startswith('test_'):
                            candidates.add(candidate)
                if len(candidates) == 1:
                    message = f'Test failure in {relative}: {message}\n' + check['output'][-1200:]
                    relative = candidates.pop()
                    number = 0
            except (OSError, ValueError, SyntaxError):
                pass
        findings.append({'file': relative, 'line': int(number), 'bug_type': kind,
                         'error_message': message[:2000], 'severity': 'high' if kind == 'LOGIC' else 'medium'})
    return findings


def run_repository_checks(inventory, on_progress=None):
    checks, failures = [], []
    if inventory.size == 'large':
        return {'complete': False, 'passed': False, 'checks': [], 'failures': [],
                'reason': 'Repository exceeds the configured medium repository limits'}
    plan = check_plan(inventory)
    for index, spec in enumerate(plan, 1):
        if on_progress:
            on_progress(f"Check {index}/{len(plan)}: {spec['name']} in {spec['root']}")
        if spec['command'] is None:
            result = {'exit_code': None, 'output': spec['reason'], 'complete': False}
        else:
            result = run_command(spec['command'], safe_path(inventory.root, spec['root']), inventory.policy.timeout_seconds)
        no_tests = spec['name'] == 'pytest' and result['exit_code'] == 5
        no_tests |= spec['name'] == 'npm:test' and bool(re.search(r'no test(?:s| files?)? (?:found|collected)|0 tests', result['output'], re.I))
        result['complete'] = result['complete'] and not no_tests
        if re.search(r'No module named|command not found|not recognized as|couldn.t find a configuration|Missing script:', result['output'], re.I):
            result['complete'] = False
        optional_script = (
            spec['command'] is None
            and not result['output'].startswith(('Invalid', 'Oversized', 'Package manifest'))
            and re.match(r'^No (?:lint|test) script is configured$', spec['reason'])
        )
        if optional_script:
            result['complete'] = True
        status = (
            'skipped' if optional_script
            else 'incomplete' if not result['complete']
            else 'passed' if result['exit_code'] == 0
            else 'failed'
        )
        check = {**spec, **result, 'status': status}
        checks.append(check)
        # Findings are actionable only when they reference a real source file.
        failures.extend(_findings(check, inventory))
        if on_progress:
            on_progress(f"{spec['name']} in {spec['root']}: {status}")
    complete = bool(checks) and all(c['complete'] for c in checks) and not inventory.skipped and not inventory.errors
    # A missing optional script is deliberately reported as `skipped`, not as
    # a validation failure. It is neutral only when at least one configured
    # check actually passed; a repository with no runnable checks must not be
    # reported as healthy.
    passed = (
        complete
        and any(c['status'] == 'passed' for c in checks)
        and all(c['status'] in {'passed', 'skipped'} for c in checks)
    )
    return {'complete': complete,
            'passed': passed,
            'checks': checks, 'failures': failures,
            'reason': '' if complete else 'Some source files or checks could not be fully analyzed'}
