#!/usr/bin/env python3
"""Opt-in real managed profile: harness only supplies inputs, faults and observations."""
import argparse
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_live_slice import Origin, reserve_port, run, wait, read_lines
from tap_core.runtime import MacOS, Profile, Lifecycle
from tap_core.components import Job, secret
from tap_core.capture import Writer

class ManagedOrigin(Origin):
    def end_headers(self):
        if self.path == '/record':
            self.send_header('X-Fixture-Value', self.server.fixture_value)
        super().end_headers()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('backend','bun','node','playwright','chrome','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args = parser.parse_args()
    for name in ('backend','bun','node','playwright','chrome'):
        setattr(args,name,getattr(args,name).resolve(strict=True))
    adapter = MacOS()
    before = adapter.network_state()
    report = {'scope':'managed development profile, synthetic loopback HTTP/WS; not clean-Mac or installed pack',
              'source_commit':run(['git','-C',ROOT,'rev-parse','HEAD']).strip(),
              'python':platform.python_version(), 'macOS':platform.mac_ver()[0],
              'bun':run([args.bun,'--version']).strip(), 'node':run([args.node,'--version']).strip(),
              'backend':run([args.backend,'--version']).splitlines()[0],
              'playwright':json.loads((args.playwright/'package.json').read_text())['version']}
    with tempfile.TemporaryDirectory(prefix='tap-managed-') as directory, reserve_port() as proxy, reserve_port() as hub:
        root = Path(directory)
        servers=[]; profile=None
        try:
            for _ in range(3):
                server=ThreadingHTTPServer(('127.0.0.1',0),ManagedOrigin)
                server.fixture_value='probe'; servers.append(server)
            origins=['http://127.0.0.1:'+str(server.server_port) for server in servers]
            for server in servers:
                server.foreign_base=origins[2]; server.nonce_attribute='nonce = "dGFwLWZpeHR1cmU"'
                threading.Thread(target=server.serve_forever,daemon=True).start()
            profile_root=root/'profile'
            bridge={'version':1,'enabled':True,'hub_port':hub.getsockname()[1], 'allow_origins':origins,
                    'exclude_origins':[origins[2]], 'page_scripts':[str(ROOT/'fixtures/managed/page.js')]}
            components={'version':1,'python':sys.executable,'bun':str(args.bun),
                'readers':{'projection':{'version':1,'revision':'managed-1',
                    'command':[sys.executable,str(ROOT/'fixtures/live-slice/reader.py')], 'config':{'url':origins[0]+'/record'}}},
                'handlers':{'projection':{'command':[sys.executable,str(ROOT/'fixtures/managed/handler.py')],
                    'config':{'projection':str(profile_root/'data/readers/projection/result.json')},'origins':[origins[0]]},
                    'echo':{'command':[sys.executable,str(ROOT/'fixtures/managed/handler.py')], 'config':{'echo':True},'origins':origins[:2]}}}
            profile=Profile(profile_root,str(args.backend),proxy.getsockname()[1],'explicit',origins[0]+'/record',[],bridge=bridge,components=components)
            for name,value in [('bridge',bridge),('components',components)]:
                (root/(name+'.json')).write_text(json.dumps(value))
            # Seed retained input before install, without running a reader.
            for name in ('data','state'):
                (profile_root/name).mkdir(parents=True,mode=0o700)
            writer=Writer(profile_root/'data',profile_root/'state')
            seed=json.loads((ROOT/'fixtures/capture/v1.jsonl').read_text().splitlines()[0])
            seed.update(record_id=str(uuid.uuid4()),url=origins[0]+'/record',status=200,body=json.dumps({'value':'backlog'}))
            writer.submit(seed); writer.close()
            assert writer.written==1
            prefix=[sys.executable,ROOT/'tap','--profile',profile_root]
            projection=profile_root/'data/readers/projection/result.json'
            checkpoint=profile_root/'state/readers/projection/checkpoint.json'
            proxy.close(); hub.close()
            run(prefix+['install','--backend',args.backend,'--port',profile.port,'--routing','explicit','--probe-url',profile.probe_url,
                        '--bridge-config',root/'bridge.json','--components-config',root/'components.json'])
            wait(lambda: projection.exists(),'automatic backlog projection')
            assert json.loads(projection.read_text())['value']=='backlog'
            report['automatic_backlog']=True
            run(prefix+['on'])
            state=json.loads(run(prefix+['doctor']))
            pid=state['components']['pid']
            run(prefix+['on'])
            assert json.loads(run(prefix+['doctor']))['components']['pid']==pid
            report['repeat_on_no_duplicate']=True
            # Knowing page authority and forging old forwarded headers is insufficient on direct Hub.
            page_token=(profile_root/'state/bridge-token').read_text().strip()
            req=Request('http://127.0.0.1:%s/__tap/probe/runtime.js'%bridge['hub_port'],headers={
                'X-Tap-Probe-Token':page_token,'X-Tap-Probe-Origin':origins[0]})
            try:
                build_opener(ProxyHandler({})).open(req,timeout=2)
                raise AssertionError('direct listener accepted forged authority')
            except HTTPError as error:
                assert error.code==403
            report['direct_listener_forgery_denied']=True
            def browser_round():
                value=secrets.token_hex(12); servers[0].fixture_value=value
                config={'proxy_port':profile.port,'origins':origins,'playwright':str(args.playwright),'chrome':str(args.chrome),'output':str(root/'browser.json')}
                path=root/'browser-config.json'; path.write_text(json.dumps(config))
                run([args.node,ROOT/'fixtures/managed/browser.cjs',path],timeout=60)
                result=json.loads((root/'browser.json').read_text())
                record=next(r for r in read_lines(profile_root/'data/stream.jsonl') if r.get('record_id')==result['displayed']['record_id'])
                assert json.loads(record['body'])['value']==value==result['displayed']['value']
                return result
            report['browser']=browser_round()
            progress=json.loads(checkpoint.read_text())['processed']
            run(prefix+['off']); run(prefix+['on'])
            assert json.loads(checkpoint.read_text())['processed']>=progress
            report['restart_resume']=True
            report['browser_after_restart']=browser_round()
            state=json.loads(run(prefix+['doctor']))
            os.kill(state['components']['pid'],signal.SIGKILL)
            def recovered():
                value=json.loads(run(prefix+['status']))['components']
                return value['healthy'] and value['pid']!=state['components']['pid']
            wait(recovered,'controller crash recovery',seconds=20)
            report['controller_crash_recovered']=True
            state=json.loads(run(prefix+['doctor']))
            os.kill(state['components']['hub_pid'],signal.SIGKILL)
            wait(lambda: not json.loads(run(prefix+['status']))['components']['healthy'],'dead Hub observed')
            result=subprocess.run(list(map(str,prefix+['doctor'])),capture_output=True,text=True)
            assert result.returncode==1
            report['hub_failure_observed']=True
            run(prefix+['off']); run(prefix+['on'])
            assert json.loads(run(prefix+['doctor']))['healthy']
            report['manual_recovery']=True
            # A hung live Hub must also fail health and be reclaimed.
            state=json.loads(run(prefix+['doctor']))
            os.kill(state['components']['hub_pid'],signal.SIGSTOP)
            wait(lambda: not json.loads(run(prefix+['status']))['components']['healthy'],'hung Hub observed')
            run(prefix+['off']); run(prefix+['on'])
            report['hub_hang_observed_and_recovered']=True
            # Three abrupt controller exits exhaust the automatic restart budget.
            for attempt in range(3):
                old_pid=json.loads(run(prefix+['doctor']))['components']['pid']
                os.kill(old_pid,signal.SIGKILL)
                if attempt < 2:
                    def restarted():
                        value=json.loads(run(prefix+['status']))['components']
                        return value['healthy'] and value['pid']!=old_pid
                    wait(restarted,'bounded controller restart',seconds=20)
                else:
                    def exhausted():
                        value=json.loads(run(prefix+['status']))['components']
                        return value.get('phase')=='failed' and 'budget exhausted' in (value.get('error') or '')
                    wait(exhausted,'controller restart budget exhausted',seconds=20)
            # Observe the loaded job remain without a PID beyond two throttle
            # intervals; a failure message alone does not prove retries stopped.
            wait(lambda: adapter.service_pid(Job(profile)) is None,'controller parked after exhausted budget')
            starts=(profile_root/'state/component-starts.json').read_bytes()
            deadline=time.monotonic()+7
            while time.monotonic()<deadline:
                assert adapter.service_loaded(Job(profile))
                assert adapter.service_pid(Job(profile)) is None
                assert (profile_root/'state/component-starts.json').read_bytes()==starts
                time.sleep(0.5)
            report['controller_budget_no_restart_observation_seconds']=7
            run(prefix+['off'])
            report['controller_restart_budget_exhausted']=True
            # A foreign listener must survive failed on; the proxy must unwind.
            with reserve_port() as foreign:
                occupied=foreign.getsockname()[1]
                altered=dict(bridge,hub_port=occupied)
                (root/'occupied.json').write_text(json.dumps(altered))
                run(prefix+['bridge','configure','--config',root/'occupied.json'])
                failed=subprocess.run(list(map(str,prefix+['on'])),capture_output=True,text=True)
                assert failed.returncode==1 and 'occupied' in failed.stderr
                assert foreign.getsockname()[1]==occupied
                assert not adapter.service_loaded(profile)
            run(prefix+['bridge','configure','--config',root/'bridge.json'])
            report['occupied_hub_preserved_and_proxy_cleaned']=True
            altered=dict(components,bun=str(root/'missing-bun'))
            (root/'bad-components.json').write_text(json.dumps(altered))
            run(prefix+['components','configure','--config',root/'bad-components.json'])
            failed=subprocess.run(list(map(str,prefix+['on'])),capture_output=True,text=True)
            assert failed.returncode==1 and 'not executable' in failed.stderr
            assert not adapter.service_loaded(profile)
            report['bad_dependency_cleaned']=True
            # A new reader binding really launches a failing worker over backlog.
            failing=dict(components,readers={'failure':{'version':1,'revision':'failure-1',
                'command':[sys.executable,'-c','raise SystemExit(2)'],'config':{}}})
            (root/'failing-components.json').write_text(json.dumps(failing))
            run(prefix+['components','configure','--config',root/'failing-components.json'])
            subprocess.run(list(map(str,prefix+['on'])),capture_output=True,text=True,timeout=30)
            def reader_failed():
                value=json.loads(run(prefix+['status']))['components']
                return value.get('readers',{}).get('failure',{}).get('phase')=='failed'
            wait(reader_failed,'reader bounded retry failure',seconds=10)
            value=json.loads(run(prefix+['status']))['components']
            assert not value['healthy'] and value['readers']['failure']['failures']==3
            progress=json.loads((profile_root/'state/readers/failure/checkpoint.json').read_text())
            assert progress['processed']==0 and progress['phase']=='failed'
            run(prefix+['off'])
            report['reader_worker_failure_bounded_no_checkpoint_advance']=True
            run(prefix+['components','configure','--config',root/'components.json'])
            run(prefix+['on'])
            assert json.loads(run(prefix+['doctor']))['healthy']
        except Exception as error:
            if profile is not None:
                for path in (profile.root/'logs/capture.log',profile.root/'logs/components.log'):
                    if path.exists():
                        detail=path.read_text(errors='replace')[-12000:]
                        for token_name in ('bridge-token','component-token'):
                            token=profile.root/'state'/token_name
                            if token.exists(): detail=detail.replace(token.read_text().strip(),'[fixture token]')
                        print(str(path)+':\n'+detail,file=sys.stderr)
            detail=str(error)
            if profile is not None:
                for name in ('bridge-token','component-token'):
                    token=profile.root/'state'/name
                    if token.exists(): detail=detail.replace(token.read_text().strip(),'[fixture token]')
            raise RuntimeError(detail) from None
        finally:
            if profile is not None:
                Lifecycle(profile,adapter).off()
                assert not adapter.service_loaded(profile) and not adapter.service_loaded(Job(profile))
            for server in servers:
                server.shutdown(); server.server_close()
        assert adapter.network_state()==before
    report.update(no_private_checkout=True,no_harness_reader_or_handler_controller=True,cleanup=True,system_settings_unchanged=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
