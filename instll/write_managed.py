"""Write installer-owned bridge/components bindings under TAP_ROOT.

Paths stay inside the installation: portable Python/Bun and the downloaded
checkout. No developer machine paths are accepted or written.
"""
import json
import sys
from pathlib import Path


ORIGIN = 'http://127.0.0.1:18998'


def main(root, checkout, python, bun, hub_port, profile):
    root, checkout, profile = Path(root), Path(checkout), Path(profile)
    python, bun = Path(python), Path(bun)
    for path in (python, bun, checkout / 'tap', checkout / 'fixtures/managed/page.js',
                 checkout / 'fixtures/managed/handler.py', checkout / 'fixtures/live-slice/reader.py'):
        if not path.is_file():
            raise SystemExit(f'tap-core: managed runtime missing required file: {path}')
    managed = root / 'managed'
    managed.mkdir(mode=0o700, exist_ok=True)
    bridge = {
        'version': 1,
        'enabled': True,
        'hub_port': int(hub_port),
        'allow_origins': [ORIGIN],
        'exclude_origins': [],
        'page_scripts': [str(checkout / 'fixtures/managed/page.js')],
    }
    components = {
        'version': 1,
        'python': str(python),
        'bun': str(bun),
        'readers': {
            'projection': {
                'version': 1,
                'revision': 'installer-managed-1',
                'command': [str(python), str(checkout / 'fixtures/live-slice/reader.py')],
                'config': {'url': ORIGIN + '/record'},
            }
        },
        'handlers': {
            'echo': {
                'command': [str(python), str(checkout / 'fixtures/managed/handler.py')],
                'config': {'echo': True},
                'origins': [ORIGIN],
            },
            'projection': {
                'command': [str(python), str(checkout / 'fixtures/managed/handler.py')],
                'config': {'projection': str(profile / 'data/readers/projection/result.json')},
                'origins': [ORIGIN],
            },
        },
    }
    for name, value in (('bridge.json', bridge), ('components.json', components)):
        path = managed / name
        path.write_text(json.dumps(value, indent=2) + '\n')
        path.chmod(0o600)
    print(managed)


if __name__ == '__main__':
    main(*sys.argv[1:])
