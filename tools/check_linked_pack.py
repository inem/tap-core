#!/usr/bin/env python3
"""Hermetic #14 check: installed linked pack journal → reader → Hub handler.

Uses explicit loopback only. Does not mutate the system proxy, claim CA trust,
browser UI, SSE, or third-party WS. Exit 0 only when scenario_passed and
cleanup_verified are both true.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from unittest.mock import Mock, PropertyMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.capture import Writer
from tap_core.components import Job, _start, status, stop
from tap_core.pack_store import PackStore, build_artifact
from tap_core.runtime import Profile

PACK_ID = 'example.linked-http'
ORIGIN = 'http://127.0.0.1:18998'
RECORD_URL = ORIGIN + '/record'


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', type=Path)
    parser.add_argument('--source', type=Path,
                        default=ROOT / '.work/tap-pack-linked-http',
                        help='Pack source tree used when --pack is omitted and present')
    parser.add_argument('--bun', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--release-url',
                        default='https://github.com/inem/tap-pack-linked-http/releases/download/v0.1.0/example.linked-http-0.1.0.tap-pack')
    args = parser.parse_args()
    bun = args.bun.resolve(strict=True)
    if run([bun, '--version']).strip() != '1.3.11':
        raise SystemExit('This check requires Bun 1.3.11')

    report = {
        'scope': 'installed linked pack hermetic chain; not clean-Mac, browser UI, SSE, or third-party WS',
        'core_commit': run(['git', '-C', ROOT, 'rev-parse', 'HEAD']).strip(),
        'python': platform.python_version(),
        'bun': run([bun, '--version']).strip(),
        'pack_id': PACK_ID,
        'origin': ORIGIN,
        'scenario_passed': False,
        'cleanup_verified': False,
        'transport': {
            'controlled_http_body': 'exercised',
            'own_page_hub_ws': 'handler_via_hub_protocol',
            'sse_body': 'not_claimed',
            'third_party_ws': 'not_claimed',
        },
    }

    with tempfile.TemporaryDirectory(prefix='tap-linked-pack-') as directory:
        root = Path(directory)
        profile_root = root / 'profile'
        artifact = root / 'example.linked-http-0.1.0.tap-pack'
        if args.pack:
            artifact.write_bytes(args.pack.resolve(strict=True).read_bytes())
            report['artifact_source'] = 'pack'
        elif args.source.is_dir():
            build_artifact(args.source.resolve(strict=True), artifact)
            report['artifact_source'] = 'source'
        else:
            import urllib.request
            urllib.request.urlretrieve(args.release_url, artifact)
            report['artifact_source'] = 'release'
            report['release_url'] = args.release_url
        report['artifact_sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()

        with reserve_port() as hub_port:
            bridge = dict(version=1, enabled=True, hub_port=hub_port, allow_origins=[],
                          exclude_origins=[], page_scripts=[])
            components = dict(version=1, python=sys.executable, bun=str(bun),
                              readers={}, handlers={})
            Profile(profile_root, '/fixture/backend', 19240, 'explicit', RECORD_URL, [],
                    bridge=bridge, components=components).save()

        store = PackStore(profile_root)
        store.install(artifact)
        store.enable(PACK_ID, '0.1.0', origins=[ORIGIN],
                     capabilities=['page.inject', 'capture.read', 'bridge.handle'])
        report['enable_projected'] = True

        (profile_root / 'data').mkdir(parents=True, exist_ok=True)
        writer = Writer(profile_root / 'data', profile_root / 'state')
        value = 'linked-' + uuid.uuid4().hex[:8]
        seed = json.loads((ROOT / 'fixtures/capture/v1.jsonl').read_text().splitlines()[0])
        seed.update(record_id=str(uuid.uuid4()), url=RECORD_URL, status=200,
                    body=json.dumps({'value': value}), size=len(json.dumps({'value': value})))
        writer.submit(seed)
        writer.close()

        controller = {'proc': None}

        def spawn(cmd, **kwargs):
            if list(map(str, cmd[:2])) == ['/bin/launchctl', 'bootstrap']:
                controller['proc'] = subprocess.Popen(
                    [sys.executable, '-B', str(ROOT / 'tap_core/service.py'), str(profile_root)],
                    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return subprocess.CompletedProcess(cmd, 0, '', '')
            completed = subprocess.run(list(map(str, cmd)), capture_output=True, text=True, check=False)
            return completed

        adapter = Mock()
        adapter.port_open.return_value = False
        adapter.service_loaded.return_value = False
        adapter.run.side_effect = spawn
        adapter.service_pid.side_effect = (
            lambda job: controller['proc'].pid
            if controller['proc'] is not None and controller['proc'].poll() is None else None)

        def wait_pred(pred, seconds=15):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if pred():
                    return True
                time.sleep(0.05)
            return pred()

        adapter.wait.side_effect = wait_pred
        plist = root / 'components.plist'
        try:
            with patch.object(Job, 'plist', new_callable=PropertyMock, return_value=plist):
                _start(Profile.load(profile_root), adapter)
            observed = status(Profile.load(profile_root), adapter)
            assert observed['healthy'], observed
            assert observed.get('hub_pid'), observed
            report['hub_started'] = True

            projection = profile_root / 'data/readers' / PACK_ID / 'result.json'
            assert wait_pred(projection.is_file, 15), 'reader did not write projection'
            assert json.loads(projection.read_text())['value'] == value
            report['reader_projection'] = True

            probe = subprocess.run(
                [str(bun), str(ROOT / 'tests/linked_handler_protocol.mjs'),
                 str(profile_root), value],
                capture_output=True, text=True, timeout=20)
            assert probe.returncode == 0, probe.stdout + probe.stderr
            body = json.loads(probe.stdout)
            assert body['ok'] and body['value']['value'] == value, body
            report['handler_value'] = True

            checkpoint = profile_root / 'state/readers' / PACK_ID / 'checkpoint.json'
            before = json.loads(checkpoint.read_text())
            adapter.service_loaded.return_value = True
            stop(Profile.load(profile_root), adapter)
            if controller['proc'] is not None and controller['proc'].poll() is None:
                os.killpg(controller['proc'].pid, signal.SIGTERM)
                controller['proc'].wait(timeout=5)
            controller['proc'] = None
            retained = json.loads(checkpoint.read_text())
            assert retained['processed'] == before['processed']
            assert projection.is_file()
            report['checkpoint_retained'] = True

            adapter.service_loaded.return_value = False
            with patch.object(Job, 'plist', new_callable=PropertyMock, return_value=plist):
                _start(Profile.load(profile_root), adapter)
            assert status(Profile.load(profile_root), adapter)['healthy']
            report['restart_healthy'] = True
            report['scenario_passed'] = True
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
            report['cleanup_verified'] = controller['proc'] is None or controller['proc'].poll() is not None

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
