import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from tap_core.pack_store import PackStore, build_artifact
from tap_core.session_observation import consume, SessionObservation
from tap_core.packs import validate_manifest, PackError
from types import SimpleNamespace

class SessionObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root/'source'
        shutil.copytree(Path(__file__).resolve().parent.parent/'fixtures/packs/command',self.src)
        self.m = json.loads((self.src/'pack.json').read_text())
        self.m['entrypoints']['command']['session_headers'] = {'path_prefix':'/api/','headers':['cookie','authorization']}
        self.m['access']['capabilities'].append('session.observe')
        (self.src/'pack.json').write_text(json.dumps(self.m))
        artifact=self.root/'fixture.tap-pack';build_artifact(self.src,artifact)
        self.profile=self.root/'profile';self.store=PackStore(self.profile);self.store.install(artifact)
        self.store.enable(self.m['id'],self.m['version'],origins=self.m['access']['origins'],capabilities=self.m['access']['capabilities'])

    def test_exact_origin_path_private_state_and_disable(self):
        target=self.profile/'state/packs/fixture.command/auth/fixture.example.cookie'
        for origin,path in [('https://other.example','/api/x'),('https://fixture.example','/asset.js')]:
            consume(self.profile,origin,path,{'cookie':'synthetic'})
        self.assertFalse(target.exists())
        consume(self.profile,'https://fixture.example','/api/x',{'cookie':'synthetic','authorization':'fixture-token'})
        self.assertEqual(target.read_text(),'synthetic')
        self.assertEqual(target.stat().st_mode & 0o777,0o600)
        self.assertFalse((self.profile/'data').exists())
        self.store.disable(self.m['id'])
        consume(self.profile,'https://fixture.example','/api/x',{'cookie':'changed'})
        self.assertEqual(target.read_text(),'synthetic')

    def test_permission_required(self):
        self.m['access']['capabilities'].remove('session.observe')
        with self.assertRaises(PackError):validate_manifest(self.m,self.src)

    def test_queue_size_and_header_bound(self):
        addon=SessionObservation()
        addon.routes=(("https://fixture.example","/api/"),)
        req=SimpleNamespace(scheme='https',host='fixture.example',port=443,path='/api/x',headers={'cookie':'x'*65537})
        addon.requestheaders(SimpleNamespace(request=req));self.assertTrue(addon.pending.empty())
        req.headers={'cookie':'synthetic'}
        for _ in range(30):addon.requestheaders(SimpleNamespace(request=req))
        self.assertEqual(addon.pending.qsize(),16)
