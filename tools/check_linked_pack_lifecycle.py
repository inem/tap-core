#!/usr/bin/env python3
"""#14 lifecycle check for published example.linked-http.

Covers, without a new supervisor or browser:
  - hanging handler → Hub handler_timeout
  - reader failure visible in components status (checkpoint retained)
  - incompatible pack update refused (selected + checkpoint unchanged)
  - disable + uninstall remove code but keep pack/reader data

Explicit loopback / mocked launchd only. Exit 0 iff scenario_passed and
cleanup_verified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import contextmanager
from unittest.mock import Mock, PropertyMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.capture import Writer
from tap_core.components import Job, _start, status, stop
from tap_core.pack_store import PackStore, PackError, build_artifact
from tap_core.packs import load_manifest
from tap_core.runtime import Profile

PACK_ID = 'example.linked-http'
ORIGIN = 'http://127.0.0.1:18998'
RECORD_URL = ORIGIN + '/record'
RELEASE = ('https://github.com/inem/tap-pack-linked-http/releases/download/'
           'v0.1.0/example.linked-http-0.1.0.tap-pack')


def run(args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or str(result.returncode))
    return result.stdout


@contextmanager
def reserve_port():
    import socket
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract_artifact(artifact, dest):
    dest = Path(dest)
    dest.mkdir(parents=True)
    with tarfile.open(artifact, 'r:gz') as archive:
        archive.extractall(dest)
    return dest


def rewrite_pack(source, *, version, handler=None, reader=None):
    """Copy pack source, bump version, optionally replace entry scripts; rebuild hashes."""
    source = Path(source)
    work = source.parent / ('pack-' + version)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(source, work)
    if handler is not None:
        (work / 'handler.py').write_text(handler)
    if reader is not None:
        (work / 'reader.py').write_text(reader)
    manifest = json.loads((work / 'pack.json').read_text())
    manifest['version'] = version
    page = work / 'page.js'
    page_hash = digest(page)
    for resource in manifest.get('resources') or []:
        if resource.get('file') == 'page.js':
            resource['sha256'] = page_hash
            resource['version'] = version
            resource['source_revision'] = 'sha256:' + page_hash
    for use in manifest['entrypoints']['page']['uses']:
        use['version'] = version
    (work / 'pack.json').write_text(json.dumps(manifest, indent=2) + '\n')
    load_manifest(work)  # validate before build
    return work


def scenario_failures(steps):
    """Pure gate used by unit tests; keep claims explicit."""
    failures = []
    if not steps.get('handler_timeout'):
        failures.append('handler_timeout')
    if not steps.get('reader_error_visible'):
        failures.append('reader_error_visible')
    if not steps.get('incompatible_update_refused'):
        failures.append('incompatible_update_refused')
    if not steps.get('selected_unchanged'):
        failures.append('selected_unchanged')
    if not steps.get('checkpoint_unchanged'):
        failures.append('checkpoint_unchanged')
    if not steps.get('uninstall_removed_code'):
        failures.append('uninstall_removed_code')
    if not steps.get('data_retained'):
        failures.append('data_retained')
    return failures


HANG_HANDLER = '''import json, os, sys, time
context = json.loads(os.environ["TAP_PACK_CONTEXT"])
request = json.loads(sys.stdin.readline())
time.sleep(20)
print(json.dumps({"ok": True, "value": {"late": True}}))
'''

INCOMPAT_READER = '''import json, os, sys
from pathlib import Path
context = json.loads(os.environ["TAP_PACK_CONTEXT"])
print(json.dumps({"event": "start"}), file=sys.stderr)
for line in sys.stdin:
    record = json.loads(line)
    if record.get("url") != context["config"]["url"]:
        continue
    result = {"record_id": record["record_id"], "value": "incompatible-v2", "rev": 2}
    target = Path(context["output_dir"]) / "result.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result) + "\\n")
    temporary.replace(target)
    print(json.dumps(result), flush=True)
print(json.dumps({"event": "stop"}), file=sys.stderr)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', type=Path)
    parser.add_argument('--source', type=Path, default=ROOT / '.work/tap-pack-linked-http')
    parser.add_argument('--bun', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--release-url', default=RELEASE)
    args = parser.parse_args()
    bun = args.bun.resolve(strict=True)
    if run([bun, '--version']).strip() != '1.3.11':
        raise SystemExit('This check requires Bun 1.3.11')

    steps = {}
    report = {
        'scope': 'example.linked-http lifecycle/errors; no browser, CA, SSE, or third-party WS',
        'core_commit': run(['git', '-C', ROOT, 'rev-parse', 'HEAD']).strip(),
        'python': platform.python_version(),
        'bun': run([bun, '--version']).strip(),
        'pack_id': PACK_ID,
        'scenario_passed': False,
        'cleanup_verified': False,
    }

    with tempfile.TemporaryDirectory(prefix='tap-linked-life-') as directory:
        root = Path(directory)
        profile_root = root / 'profile'
        base_artifact = root / 'base.tap-pack'
        if args.pack:
            base_artifact.write_bytes(args.pack.resolve(strict=True).read_bytes())
            report['artifact_source'] = 'pack'
        elif args.source.is_dir():
            build_artifact(args.source.resolve(strict=True), base_artifact)
            report['artifact_source'] = 'source'
        else:
            import urllib.request
            urllib.request.urlretrieve(args.release_url, base_artifact)
            report['artifact_source'] = 'release'
        report['artifact_sha256'] = digest(base_artifact)
        pack_src = extract_artifact(base_artifact, root / 'src-0.1.0')

        hang_src = rewrite_pack(pack_src, version='0.1.1', handler=HANG_HANDLER)
        hang_artifact = root / 'hang.tap-pack'
        build_artifact(hang_src, hang_artifact)

        incompat_src = rewrite_pack(pack_src, version='0.2.0', reader=INCOMPAT_READER)
        incompat_artifact = root / 'incompat.tap-pack'
        build_artifact(incompat_src, incompat_artifact)

        with reserve_port() as hub_port:
            bridge = dict(version=1, enabled=True, hub_port=hub_port, allow_origins=[],
                          exclude_origins=[], page_scripts=[])
            components = dict(version=1, python=sys.executable, bun=str(bun),
                              readers={}, handlers={})
            Profile(profile_root, '/fixture/backend', 19241, 'explicit', RECORD_URL, [],
                    bridge=bridge, components=components).save()

        store = PackStore(profile_root)
        grants = dict(origins=[ORIGIN],
                      capabilities=['page.inject', 'capture.read', 'bridge.handle'])
        store.install(hang_artifact)
        store.enable(PACK_ID, '0.1.1', **grants)

        controller = {'proc': None}

        def spawn(cmd, **kwargs):
            if list(map(str, cmd[:2])) == ['/bin/launchctl', 'bootstrap']:
                controller['proc'] = subprocess.Popen(
                    [sys.executable, '-B', str(ROOT / 'tap_core/service.py'), str(profile_root)],
                    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return subprocess.CompletedProcess(cmd, 0, '', '')
            return subprocess.run(list(map(str, cmd)), capture_output=True, text=True, check=False)

        adapter = Mock()
        adapter.port_open.return_value = False
        adapter.service_loaded.return_value = False
        adapter.run.side_effect = spawn
        adapter.service_pid.side_effect = (
            lambda job: controller['proc'].pid
            if controller['proc'] is not None and controller['proc'].poll() is None else None)

        def wait_pred(pred, seconds=20):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if pred():
                    return True
                time.sleep(0.05)
            return pred()

        adapter.wait.side_effect = wait_pred
        plist = root / 'components.plist'

        def restart():
            if controller['proc'] is not None and controller['proc'].poll() is None:
                adapter.service_loaded.return_value = True
                stop(Profile.load(profile_root), adapter)
                try:
                    os.killpg(controller['proc'].pid, signal.SIGTERM)
                    controller['proc'].wait(timeout=5)
                except Exception:
                    try:
                        os.killpg(controller['proc'].pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                controller['proc'] = None
            adapter.service_loaded.return_value = False
            with patch.object(Job, 'plist', new_callable=PropertyMock, return_value=plist):
                _start(Profile.load(profile_root), adapter)
            assert status(Profile.load(profile_root), adapter)['healthy']

        try:
            restart()
            # 1) hanging handler → timeout
            probe = subprocess.run(
                [str(bun), str(ROOT / 'tests/linked_handler_errors.mjs'),
                 str(profile_root), 'timeout'],
                capture_output=True, text=True, timeout=30)
            assert probe.returncode == 0, probe.stdout + probe.stderr
            body = json.loads(probe.stdout)
            assert body.get('error', {}).get('code') == 'handler_timeout', body
            steps['handler_timeout'] = True
            report['handler_timeout'] = body

            # Switch to good 0.1.0, project a record, then poison the journal.
            adapter.service_loaded.return_value = True
            stop(Profile.load(profile_root), adapter)
            if controller['proc'] is not None and controller['proc'].poll() is None:
                os.killpg(controller['proc'].pid, signal.SIGTERM)
                controller['proc'].wait(timeout=5)
            controller['proc'] = None
            checkpoint = profile_root / 'state/readers' / PACK_ID / 'checkpoint.json'
            if checkpoint.is_file():
                checkpoint.unlink()
            store.install(base_artifact)
            store.enable(PACK_ID, '0.1.0', **grants)
            (profile_root / 'data').mkdir(parents=True, exist_ok=True)
            writer = Writer(profile_root / 'data', profile_root / 'state')
            value = 'life-' + uuid.uuid4().hex[:6]
            seed = json.loads((ROOT / 'fixtures/capture/v1.jsonl').read_text().splitlines()[0])
            seed.update(record_id=str(uuid.uuid4()), url=RECORD_URL, status=200,
                        body=json.dumps({'value': value}))
            writer.submit(seed)
            writer.close()
            adapter.service_loaded.return_value = False
            with patch.object(Job, 'plist', new_callable=PropertyMock, return_value=plist):
                _start(Profile.load(profile_root), adapter)
            projection = profile_root / 'data/readers' / PACK_ID / 'result.json'
            assert wait_pred(projection.is_file, 15)
            assert json.loads(projection.read_text())['value'] == value

            poison = json.loads((ROOT / 'fixtures/capture/v1.jsonl').read_text().splitlines()[0])
            poison.update(record_id=str(uuid.uuid4()), url=RECORD_URL, status=200,
                          body=json.dumps({'value': 'poison'}), record_version=99)
            writer = Writer(profile_root / 'data', profile_root / 'state')
            writer.submit(poison)
            writer.close()

            def reader_failed():
                observed = status(Profile.load(profile_root), adapter)
                row = (observed.get('readers') or {}).get(PACK_ID) or {}
                return row.get('phase') in ('failed', 'backoff') and bool(row.get('error'))
            assert wait_pred(reader_failed, 20), status(Profile.load(profile_root), adapter)
            row = status(Profile.load(profile_root), adapter)['readers'][PACK_ID]
            assert 'Unsupported capture record version' in row['error'] or 'ValueError' in row['error']
            steps['reader_error_visible'] = True
            report['reader_error'] = row

            before_checkpoint = checkpoint.read_bytes()
            before_selected = store.load()['packs'][PACK_ID]['selected']
            assert before_selected == '0.1.0'

            # incompatible update refused
            try:
                store.update(incompat_artifact)
                raise AssertionError('incompatible update should refuse')
            except PackError as error:
                report['incompatible_update_error'] = str(error)
            after = store.load()['packs'][PACK_ID]
            assert after['selected'] == '0.1.0'
            assert '0.2.0' in after['versions']
            assert checkpoint.read_bytes() == before_checkpoint
            steps['incompatible_update_refused'] = True
            steps['selected_unchanged'] = True
            steps['checkpoint_unchanged'] = True

            # Stop before uninstall so the controller does not keep deleted code mapped.
            adapter.service_loaded.return_value = True
            stop(Profile.load(profile_root), adapter)
            if controller['proc'] is not None and controller['proc'].poll() is None:
                os.killpg(controller['proc'].pid, signal.SIGTERM)
                controller['proc'].wait(timeout=5)
            controller['proc'] = None

            # Mark retained data, then disable + uninstall.
            pack_data = profile_root / 'data/packs' / PACK_ID
            pack_data.mkdir(parents=True, exist_ok=True)
            sentinel = pack_data / 'retained.txt'
            sentinel.write_text('keep-me\n')
            store.disable(PACK_ID)
            removed = store.uninstall(PACK_ID)
            code_root = profile_root / 'packs' / PACK_ID
            assert not code_root.exists() or not any(code_root.rglob('reader.py'))
            assert sentinel.is_file() and sentinel.read_text() == 'keep-me\n'
            assert projection.is_file()
            assert checkpoint.is_file()
            steps['uninstall_removed_code'] = True
            steps['data_retained'] = True
            report['uninstall'] = removed

            failures = scenario_failures(steps)
            report['scenario_failures'] = failures
            report['scenario_passed'] = not failures
        finally:
            try:
                if controller['proc'] is not None:
                    adapter.service_loaded.return_value = True
                    stop(Profile.load(profile_root), adapter)
            except Exception:
                pass
            if controller['proc'] is not None and controller['proc'].poll() is None:
                try:
                    os.killpg(controller['proc'].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                controller['proc'].wait(timeout=3)
            report['cleanup_verified'] = (
                controller['proc'] is None or controller['proc'].poll() is not None)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if not (report['scenario_passed'] and report['cleanup_verified']):
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
