import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from tap_core.pack_store import PackStore, build_artifact
from tap_core.session_observation import consume, SessionObservation
from tap_core.packs import validate_manifest, PackError
from tap_core.runtime import profile_lock
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
        addon.routes=((self.m['id'],"https://fixture.example","/api/"),)
        req=SimpleNamespace(scheme='https',host='fixture.example',port=443,path='/api/x',headers={'cookie':'x'*65537})
        addon.requestheaders(SimpleNamespace(request=req));self.assertTrue(addon.pending.empty())
        for i in range(30):
            req.headers={'cookie':'synthetic-%d' % i}
            addon.requestheaders(SimpleNamespace(request=req))
        self.assertEqual(addon.pending.qsize(),1)
        self.assertEqual(addon.latest[(self.m['id'],'https://fixture.example')][2]['cookie'],'synthetic-29')

    def test_slow_verification_does_not_hold_profile_mutation_lock(self):
        entered=threading.Event(); release=threading.Event()
        original=PackStore.verify

        def blocked(store,*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(store,*args,**kwargs)

        worker=threading.Thread(target=consume,args=(
            self.profile,'https://fixture.example','/api/x',{'cookie':'synthetic'}))
        with patch.object(PackStore,'verify',blocked):
            worker.start();self.assertTrue(entered.wait(2))
            with profile_lock(self.profile):
                pass
            release.set();worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_disable_between_verification_and_publish_drops_observation(self):
        target=self.profile/'state/packs/fixture.command/auth/fixture.example.cookie'
        real_lock=profile_lock

        @contextmanager
        def disable_before_publish(root,*args,**kwargs):
            self.store.disable(self.m['id'])
            with real_lock(root,*args,**kwargs) as lock:
                yield lock

        with patch('tap_core.session_observation.profile_lock',disable_before_publish):
            consume(self.profile,'https://fixture.example','/api/x',{'cookie':'synthetic'})
        self.assertFalse(target.exists())

    def test_command_observer_coexists_with_bridge_without_expanding_origins(self):
        from tap_core.bridge import configuration, effective_configuration
        base=configuration({'version':1,'enabled':True,'hub_port':19002,'allow_origins':['https://page.example'],'exclude_origins':[],'page_scripts':[]})
        result=effective_configuration(self.profile,base)
        self.assertNotIn('https://fixture.example',result['allow_origins'])
