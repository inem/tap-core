#!/usr/bin/env python3
"""Real isolated mitmproxy -> authenticated Hub -> SSE subscriber; synthetic data.

No system routing, existing profiles, CA trust or provider account changes.
Upstreams wait after their first fragment/frame so completion cannot explain a
passing check. Run with the declared backend and Bun paths, not a live profile.
"""
import argparse
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.runtime import Profile
from tap_core.capture import DEFAULT_CAPTURE

RELEASE = threading.Event()
MARKER = 'synthetic-live-delta'


def frame(body, masked=False):
    assert len(body) < 126
    if not masked:
        return bytes([0x81, len(body)]) + body
    mask = b'abcd'
    return bytes([0x81, 0x80 | len(body)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(body))


class Origin(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_): pass

    def do_GET(self):
        if self.path in ('/sse', '/empty-type'):
            self.send_response(200)
            if self.path == '/sse': self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            first = f'data: {{"type":"text_delta","text":"{MARKER}"}}\n\n'.encode()
            for data in [first, b'data: {"type":"done"}\n\n']:
                self.wfile.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n'); self.wfile.flush()
                if data == first: assert RELEASE.wait(8), 'upstream not released'
            self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
        elif self.path == '/ws':
            key = self.headers['Sec-WebSocket-Key']
            digest = hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket'); self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(digest).decode()); self.end_headers()
            self.wfile.write(frame(MARKER.encode())); self.wfile.flush()
            assert RELEASE.wait(8), 'upstream not released'
            self.wfile.write(b'\x88\x00'); self.wfile.flush(); self.close_connection = True
        elif self.path == '/probe':
            self.send_response(200); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'{}')
        else:
            self.send_error(404)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); return sock.getsockname()[1]


def until(check, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result: return result
        time.sleep(.02)
    raise TimeoutError('live capture fixture condition timed out')


class Subscriber:
    def __init__(self, hub_port, token):
        self.records, self.errors = [], []
        self.connection = http.client.HTTPConnection('127.0.0.1', hub_port, timeout=10)
        self.connection.request('GET', '/v1/capture/live', headers={'Authorization':'Bearer '+token, 'Accept':'text/event-stream'})
        self.response = self.connection.getresponse()
        assert self.response.status == 200
        self.thread = threading.Thread(target=self.read, daemon=True); self.thread.start()

    def read(self):
        try:
            while True:
                line = self.response.readline()
                if not line: break
                if line.startswith(b'data: '): self.records.append(json.loads(line[6:]))
        except (OSError, ValueError) as error:
            self.errors.append(type(error).__name__)

    def close(self):
        self.connection.close()


def api(hub_port, path, token, method='GET', body=None):
    connection = http.client.HTTPConnection('127.0.0.1', hub_port, timeout=3)
    try:
        connection.request(method, path, body=body, headers={'Authorization':'Bearer '+token})
        response = connection.getresponse(); data = response.read()
        return response.status, data
    finally: connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', required=True); parser.add_argument('--bun', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    origin_url = f'http://127.0.0.1:{origin.server_port}'
    report = {'scope':'isolated real backend/Hub; no user data or system routing mutation','checks':[]}
    children = []; subscriber = None
    with tempfile.TemporaryDirectory(prefix='tap-live-capture-') as directory:
        root = Path(directory)
        profile = Profile(root, args.backend, port(), 'explicit', origin_url+'/sse', [],
            bridge={'version':1,'enabled':True,'hub_port':port(),'allow_origins':[], 'exclude_origins':[], 'page_scripts':[]},
            components={'version':1,'python':sys.executable,'bun':args.bun,'readers':{},'handlers':{},'services':{}},
            capture={**DEFAULT_CAPTURE,'live_routes':[{'origin':origin_url,'path_prefix':'/', 'metadata_headers':['x-fixture-thread']}]})
        profile.save()
        token = (root/'state/component-token').read_text().strip()
        hub_port = profile.bridge['hub_port']
        with (root/'fixture.log').open('w') as log:
            try:
                hub = subprocess.Popen([args.bun,str(ROOT/'tap_core/hub.mjs'),str(root)],stdout=log,stderr=log); children.append(hub)
                def ready():
                    try: return api(hub_port,'/health',token)[0] == 200
                    except OSError: return False
                until(ready)
                assert api(hub_port,'/v1/capture/live','wrong')[0] == 403
                assert api(hub_port,'/v1/capture/live',(root/'state/bridge-token').read_text().strip())[0] == 403
                assert api(hub_port,'/v1/capture/live',token,'POST','{}')[0] == 400
                assert api(hub_port,'/v1/capture/live',token,'POST','x'*140000)[0] == 400
                report['checks'].append('controller authentication, page-token denial and bounded malformed input')
                subscriber = Subscriber(hub_port,token)
                until(lambda: subscriber.records)
                env = {**os.environ,'TAP_CORE_PROFILE':str(root),'TAP_CORE_DATA':str(root/'data'),
                       'TAP_CORE_STATE':str(root/'state'),'TAP_CORE_CAPTURE':json.dumps(profile.capture)}
                proxy = subprocess.Popen([args.backend,'--listen-host','127.0.0.1','--listen-port',str(profile.port),
                    '--set','connection_strategy=lazy','-s',str(ROOT/'tap_core/capture.py')],env=env,stdout=log,stderr=log); children.append(proxy)
                def proxy_ready():
                    if proxy.poll() is not None: raise RuntimeError((root/'fixture.log').read_text()[-2000:])
                    try:
                        with socket.create_connection(('127.0.0.1',profile.port),timeout=.1):return True
                    except OSError:return False
                until(proxy_ready,8)
                for path in ('/sse','/empty-type'):
                    RELEASE.clear(); offset=len(subscriber.records)
                    conn=http.client.HTTPConnection('127.0.0.1',profile.port,timeout=5)
                    conn.request('GET',origin_url+path,headers={'Authorization':'private-fixture-credential',
                        'Cookie':'private-fixture-cookie','X-Fixture-Thread':'synthetic-thread'})
                    response=conn.getresponse(); first=response.readline()
                    assert MARKER.encode() in first and not RELEASE.is_set()
                    def captured():
                        return next((r for r in subscriber.records[offset:] if r.get('phase')=='data'
                            and r.get('direction')=='response' and MARKER.encode() in base64.b64decode(r['data'])),None)
                    record=until(captured)
                    assert not RELEASE.is_set(), 'delivery depended on HTTP completion'
                    flow_id=record['flow_id']
                    RELEASE.set(); rest=response.read(); conn.close()
                    until(lambda:any(r.get('flow_id')==flow_id and r.get('direction')=='response' and r.get('phase')=='end' for r in subscriber.records))
                    seen=[r for r in subscriber.records if r.get('flow_id')==flow_id]
                    raw=b''.join(base64.b64decode(r['data']) for r in seen if r.get('phase')=='data' and r.get('direction')=='response')
                    assert raw == first+rest
                    assert [r['sequence'] for r in seen] == list(range(1,len(seen)+1))
                    report['checks'].append(path+': fragment arrives before completion; identical bytes and contiguous flow sequence')
                RELEASE.clear(); offset=len(subscriber.records)
                with socket.create_connection(('127.0.0.1',profile.port),timeout=6) as sock:
                    key=base64.b64encode(b'synthetic-ws-key').decode()
                    sock.sendall((f'GET {origin_url}/ws HTTP/1.1\r\nHost: 127.0.0.1:{origin.server_port}\r\n'
                        f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
                    received=b''
                    while MARKER.encode() not in received: received+=sock.recv(4096)
                    assert b'101 Switching Protocols' in received
                    sock.sendall(frame(b'synthetic-client-message',masked=True))
                    def captured_ws(direction,text):
                        return any(r.get('transport')=='websocket' and r.get('phase')=='data' and r.get('direction')==direction
                            and base64.b64decode(r['data'])==text for r in subscriber.records[offset:])
                    until(lambda:captured_ws('response',MARKER.encode()))
                    until(lambda:captured_ws('request',b'synthetic-client-message'))
                    assert not RELEASE.is_set()
                    RELEASE.set(); sock.recv(4096)
                report['checks'].append('WebSocket frames in both directions arrive before close, unchanged bytes at client')
                assert 'private-fixture' not in json.dumps(subscriber.records)
                report['checks'].append('upstream auth/cookie headers absent from feed')
                # A missing Hub must not stall the upstream or client.
                hub.terminate(); hub.wait(timeout=4)
                RELEASE.set()
                conn=http.client.HTTPConnection('127.0.0.1',profile.port,timeout=3)
                start=time.monotonic(); conn.request('GET',origin_url+'/sse')
                response=conn.getresponse(); assert MARKER.encode() in response.read(); conn.close()
                report['hub_unavailable_client_seconds']=round(time.monotonic()-start,3)
                def failed():
                    try: return json.loads((root/'state/live-capture.json').read_text()).get('publication_failed',0)>0
                    except (OSError,ValueError):return False
                until(failed)
                report['checks'].append('Hub failure does not stall client; failure exposed in producer health')
                hub = subprocess.Popen([args.bun,str(ROOT/'tap_core/hub.mjs'),str(root)],stdout=log,stderr=log); children.append(hub)
                until(ready)
                subscriber.close(); subscriber = Subscriber(hub_port,token)
                until(lambda:subscriber.records)
                conn=http.client.HTTPConnection('127.0.0.1',profile.port,timeout=3)
                conn.request('GET',origin_url+'/sse'); response=conn.getresponse(); response.read(); conn.close()
                until(lambda:any(r.get('reason')=='publisher_loss' for r in subscriber.records))
                report['checks'].append('next successful publication reports lost observations after Hub recovery')
            finally:
                RELEASE.set()
                for child in reversed(children):
                    if child.poll() is None:
                        child.terminate()
                        try: child.wait(timeout=4)
                        except subprocess.TimeoutExpired:child.kill();child.wait()
                if subscriber:subscriber.close()
    origin.shutdown()
    if args.output:args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
