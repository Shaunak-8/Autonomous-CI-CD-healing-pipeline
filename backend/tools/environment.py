"""Install project dependencies in each cloned workspace, never globally."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

from core.repository import safe_path, positive_setting
from tools.checks import _package_roots, run_command


DEFAULT_HEAVY_PACKAGES = {
    'torch', 'torchvision', 'torchaudio', 'tensorflow', 'tensorflow-cpu',
    'jax', 'jaxlib', 'paddlepaddle', 'mxnet', 'triton', 'onnxruntime',
}


def _enabled(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {'1', 'true', 'yes', 'on'}


def _heavy_packages() -> set[str]:
    configured = os.getenv('HEAVY_DEPENDENCY_PACKAGES', '')
    return DEFAULT_HEAVY_PACKAGES | {item.strip().lower() for item in configured.split(',') if item.strip()}


def _filtered_requirements(requirements: Path) -> tuple[Path | None, list[str]]:
    """Create an install list without opt-in heavyweight direct dependencies."""
    skipped, retained = [], []
    for line in requirements.read_text(encoding='utf-8').splitlines():
        candidate = line.strip()
        package = candidate.split(';', 1)[0].split('#', 1)[0].strip()
        package = package.split('[', 1)[0].split('=', 1)[0].split('<', 1)[0].split('>', 1)[0].strip().lower()
        if package in _heavy_packages():
            skipped.append(package)
        else:
            retained.append(line)
    if not skipped:
        return None, []
    handle = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', prefix='healing-requirements-', suffix='.txt', delete=False,
    )
    try:
        handle.write('\n'.join(retained) + ('\n' if retained else ''))
        return Path(handle.name), skipped
    finally:
        handle.close()


def prepare_environment(inventory, progress=lambda message: None):
    outcomes = []
    timeout = positive_setting('DEPENDENCY_INSTALL_TIMEOUT', 180)

    def execute(name, command, root, required=True):
        progress(f'Preparing {root}: {name} (timeout {timeout}s)')
        result = run_command(command, safe_path(inventory.root, root), timeout,
                             progress=lambda message: progress(f'{root}: {name}. {message}'))
        outcomes.append({'name': name, 'root': root, **result})
        if not result['complete'] or result['exit_code'] != 0:
            if not required:
                return result
            if any(marker in result['output'].lower() for marker in ('readtimeout', 'connection broken', 'resolve', 'connectionerror')):
                raise RuntimeError(f'Dependency setup failed: {name} in {root}. The package index could not be reached. '
                                   'Check network access to PyPI and retry; dependency versions have not been changed. '
                                   + result['output'][-800:])
            raise RuntimeError(f"Dependency setup failed: {name} in {root}. {result['output'][-2500:]}")
        progress(f'Completed {root}: {name}')
        return result

    def install(name, python, arguments, root):
        base = [str(python), '-u', '-m', 'pip', 'install', '--quiet', '--no-input',
                '--disable-pip-version-check', '--progress-bar', 'off', '--prefer-binary']
        default_cache = Path(__file__).resolve().parents[1] / 'results' / 'dependency-wheels'
        cache = Path(os.getenv('DEPENDENCY_WHEELHOUSE') or default_cache).resolve()
        if cache.is_dir() and any(cache.glob('*.whl')):
            result = execute(f'{name} from local cache', [*base, '--no-index', '--find-links', str(cache), *arguments], root, required=False)
            if result['complete'] and result['exit_code'] == 0:
                return
            progress(f'{root}: local cache does not satisfy all dependencies; trying the configured package index.')
        execute(name, [*base, '--timeout', '30', '--retries', '2', *arguments], root)

    for root in _package_roots(inventory, 'python', {'requirements.txt', 'pyproject.toml', 'setup.py', 'pytest.ini'}):
        directory = safe_path(inventory.root, root)
        python = directory / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.exists():
            requested = os.getenv('REPO_PYTHON_EXECUTABLE')
            command = [requested or sys.executable]
            # The supported Python 3.12 runtime is a better default for existing ML repos on Windows.
            if not requested and os.name == 'nt' and sys.version_info >= (3, 13) and shutil.which('py'):
                probe = run_command(['py', '-3.12', '-c', 'import sys; print(sys.executable)'], directory, 10)
                if probe['exit_code'] == 0:
                    command = ['py', '-3.12']
            execute('create Python virtual environment', [*command, '-m', 'venv', str(directory / '.venv')], root)
        requirements = directory / 'requirements.txt'
        if requirements.exists():
            filtered = None
            try:
                if _enabled('ALLOW_HEAVY_DEPENDENCIES'):
                    install('install Python requirements', python, ['-r', 'requirements.txt'], root)
                else:
                    filtered, skipped = _filtered_requirements(requirements)
                    if skipped:
                        progress(f'{root}: skipping heavyweight dependencies ({", ".join(skipped)}). '
                                 'Set ALLOW_HEAVY_DEPENDENCIES=true for a full install.')
                    install('install Python requirements', python,
                            ['-r', str(filtered or requirements)], root)
            finally:
                if filtered:
                    filtered.unlink(missing_ok=True)
        elif (directory / 'pyproject.toml').exists() or (directory / 'setup.py').exists():
            install('install Python package', python, ['-e', '.'], root)
        install('install validation tools', python, ['pytest', 'flake8', 'mypy'], root)
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm') or 'npm'
    for root in _package_roots(inventory, 'javascript', {'package.json'}):
        directory = safe_path(inventory.root, root)
        if not (directory / 'node_modules').is_dir():
            locked = (directory / 'package-lock.json').exists()
            execute('install JavaScript dependencies',
                    [npm, 'ci' if locked else 'install', '--prefer-offline', '--no-audit', '--no-fund', *([] if locked else ['--package-lock=false'])], root)
    return outcomes
