"""Declarative WebMCP-shaped tool descriptors in pack manifests."""
import copy
import json
from pathlib import Path
import unittest

from tap_core.packs import PackError, validate_manifest

SOURCE = Path(__file__).resolve().parents[1] / 'fixtures/packs/reader'
TOOL = {'name': 'copy_link', 'title': 'Copy link', 'description': 'Copy the current link',
        'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': {'readOnlyHint': True}}


class PackToolsTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((SOURCE / 'pack.json').read_text())

    def test_valid_tools_are_accepted(self):
        manifest = dict(self.manifest, tools=[TOOL, dict(TOOL, name='other')])
        validate_manifest(manifest, SOURCE)

    def test_invalid_tools_are_rejected(self):
        bad = [
            [TOOL, TOOL],                                         # duplicate name
            [dict(TOOL, name='')],
            [dict(TOOL, inputSchema={'type': 'string'})],
            [dict(TOOL, annotations={'unknownHint': True})],
            [dict(TOOL, annotations={'readOnlyHint': 'yes'})],
            [dict(TOOL, extra=1)],
            [TOOL] * 33,
        ]
        for tools in bad:
            with self.assertRaises(PackError, msg=str(tools)[:80]):
                validate_manifest(dict(copy.deepcopy(self.manifest), tools=tools), SOURCE)


if __name__ == '__main__':
    unittest.main()
