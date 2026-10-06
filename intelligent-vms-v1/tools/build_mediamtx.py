"""Build the checksum-pinned MediaMTX runtime with confined RTSP/RTSPS sources."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REDIRECT_CALLBACK = '\t\tOnResponse: func(res *base.Response) {\n'
REDIRECT_GUARD = '''\t\t\t// VMS camera targets are pinned; never follow a camera-provided redirect.
\t\t\tif res.StatusCode >= 300 && res.StatusCode < 400 {
\t\t\t\tdelete(res.Header, "Location")
\t\t\t}
'''


def prepare_source(work: Path) -> Path:
    """Fetch verified upstream source and apply the minimal redirect policy patch.

    Args:
        work: Build-owned directory used for immutable inputs and extracted source.

    Returns:
        Patched upstream source directory.

    Raises:
        RuntimeError: If source integrity or patch context differs from the pinned input.
    """
    manifest = json.loads((ROOT / 'infra/mediamtx/runtime.json').read_text())
    work.mkdir(parents=True, exist_ok=True)
    archive = work / 'source.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(manifest['source_url'], timeout=120) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest['source_sha256']:
        raise RuntimeError('MediaMTX source checksum mismatch')
    source = work / ('mediamtx-' + manifest['upstream_version'])
    with tarfile.open(archive) as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != source.name or '..' in parts or any('\\' in p or ':' in p for p in parts):
                raise RuntimeError('Unsafe MediaMTX archive path')
            target = work.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
            else:
                raise RuntimeError('Unsupported MediaMTX archive member')
    target = source / 'internal/staticsources/rtsp/source.go'
    code = target.read_text()
    if REDIRECT_GUARD not in code:
        if code.count(REDIRECT_CALLBACK) != 1:
            raise RuntimeError('MediaMTX redirect patch context mismatch')
        target.write_text(code.replace(REDIRECT_CALLBACK, REDIRECT_CALLBACK + REDIRECT_GUARD))
    return source


def build(work: Path, output: Path, target_os: str) -> None:
    """Build a static runtime and checksummed Windows package from verified source.

    Args:
        work: Build-owned source directory.
        output: Output executable, or Windows ZIP package path.
        target_os: Explicit linux/windows Go target.

    Raises:
        RuntimeError: If the compiler version differs from the pinned version.
        subprocess.CalledProcessError: If generation or compilation fails.
    """
    manifest = json.loads((ROOT / 'infra/mediamtx/runtime.json').read_text())
    version = subprocess.check_output(['go', 'version'], text=True)
    if f"go{manifest['go_version']} " not in version:
        raise RuntimeError('MediaMTX requires the pinned Go compiler')
    source = prepare_source(work)
    env = {**os.environ, 'CGO_ENABLED': '0', 'GOTOOLCHAIN': 'local'}
    subprocess.run(['go', 'generate', './...'], cwd=source, env=env, check=True, timeout=600)
    hls_asset = source / 'internal/servers/hls/hls.min.js'
    if hashlib.sha256(hls_asset.read_bytes()).hexdigest() != manifest['hls_asset_sha256']:
        raise RuntimeError('MediaMTX generated HLS asset checksum mismatch')
    (source / 'internal/core/VERSION').write_text('v' + manifest['runtime_version'])
    output.parent.mkdir(parents=True, exist_ok=True)
    executable = output.with_suffix('.exe') if target_os == 'windows' else output
    subprocess.run(['go', 'build', '-trimpath', '-buildvcs=false', '-o', str(executable)],
                   cwd=source, env={**env, 'GOOS': target_os, 'GOARCH': 'amd64'}, check=True, timeout=600)
    if target_os == 'windows':
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, path in [('mediamtx.exe', executable), ('LICENSE', source / 'LICENSE')]:
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_STORED
                entry.create_system = 3
                entry.external_attr = 0o644 << 16
                archive.writestr(entry, path.read_bytes())
        output.with_suffix(output.suffix + '.sha256').write_text(hashlib.sha256(output.read_bytes()).hexdigest())
        if hashlib.sha256(output.read_bytes()).hexdigest() != manifest['windows_zip_sha256']:
            raise RuntimeError('MediaMTX reproducible Windows package checksum mismatch')
    print('mediamtx_build_ok version=' + manifest['runtime_version'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target-os', choices=('linux', 'windows'), required=True)
    args = parser.parse_args()
    build(args.work.resolve(), args.output.resolve(), args.target_os)
