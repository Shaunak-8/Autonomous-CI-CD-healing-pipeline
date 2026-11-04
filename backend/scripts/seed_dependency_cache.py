"""Recover downloaded wheels from pip's local HTTP cache for offline setup."""
import argparse
from email.parser import Parser
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
import csv
import base64
import hashlib
import io


def seed(cache, output):
    output.mkdir(parents=True, exist_ok=True)
    added = 0
    for body in cache.rglob('*.body'):
        with body.open('rb') as stream:
            if stream.read(4) != b'PK\x03\x04':
                continue
        try:
            with zipfile.ZipFile(body) as wheel:
                paths = [name for name in wheel.namelist() if name.endswith('.dist-info/WHEEL')]
                if len(paths) != 1:
                    continue
                info = Parser().parsestr(wheel.read(paths[0]).decode('utf-8'))
                tags = [tag.split('-') for tag in info.get_all('Tag', [])]
                if not tags or any(len(tag) != 3 for tag in tags):
                    continue
                stem = paths[0].split('/')[0].removesuffix('.dist-info')
                build = f"-{info['Build']}" if info.get('Build') else ''
                tag = '-'.join('.'.join(sorted({parts[index] for parts in tags})) for index in range(3))
                filename = f'{stem}{build}-{tag}.whl'
                if Path(filename).name != filename:
                    continue
                target = output / filename
                if target.exists():
                    continue
                try:
                    os.link(body, target)
                except OSError:
                    shutil.copy2(body, target)
                added += 1
        except (zipfile.BadZipFile, UnicodeDecodeError):
            continue
    return added


def seed_installed(site_packages, name, output):
    """Repack a locally installed pure Python distribution, verifying RECORD hashes."""
    paths = list(site_packages.glob(f'{name}-*.dist-info'))
    if len(paths) != 1:
        raise ValueError('Choose one installed distribution with matching metadata')
    metadata = paths[0]
    info = Parser().parsestr((metadata / 'WHEEL').read_text(encoding='utf-8'))
    if info.get('Root-Is-Purelib', '').lower() != 'true' or info.get_all('Tag') != ['py3-none-any']:
        raise ValueError('Only pure Python distributions can be recovered from an installed environment')
    target = output / f'{metadata.name.removesuffix(".dist-info")}-py3-none-any.whl'
    output.mkdir(parents=True, exist_ok=True)
    records = []
    root = site_packages.resolve()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as wheel:
        for relative, digest, _ in csv.reader((metadata / 'RECORD').read_text(encoding='utf-8').splitlines()):
            source = (root / relative).resolve()
            if not source.is_relative_to(root) or relative.endswith(('.pyc', '/RECORD', '/INSTALLER', '/REQUESTED')):
                continue
            content = source.read_bytes()
            if digest:
                algorithm, expected = digest.split('=', 1)
                actual = base64.urlsafe_b64encode(hashlib.new(algorithm, content).digest()).decode().rstrip('=')
                if actual != expected:
                    raise ValueError(f'Installed package file does not match its recorded hash: {relative}')
            wheel.writestr(relative, content)
            fingerprint = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=')
            records.append((relative, f'sha256={fingerprint}', len(content)))
        record = f'{metadata.name}/RECORD'
        records.append((record, '', ''))
        buffer = io.StringIO()
        csv.writer(buffer).writerows(records)
        wheel.writestr(record, buffer.getvalue())
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pip-cache', type=Path)
    parser.add_argument('--installed-site-packages', type=Path)
    parser.add_argument('--distribution', default='setuptools')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'results' / 'dependency-wheels')
    args = parser.parse_args()
    cache = args.pip_cache or Path(subprocess.run([sys.executable, '-m', 'pip', 'cache', 'dir'],
                                                check=True, capture_output=True, text=True).stdout.strip())
    count = seed(cache / 'http-v2', args.output)
    if args.installed_site_packages:
        wheel = seed_installed(args.installed_site_packages, args.distribution, args.output)
        print(f'Recovered installed distribution: {wheel.name}')
    print(f'Added {count} cached wheels to {args.output}. No packages downloaded.')
