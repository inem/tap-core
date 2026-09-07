"""Synthetic marker and private file control for the Local Capture experiment.

No production capture or general-purpose controller. Backend state and accepted
configuration are observations, not proof that the OS has applied a selector.
"""
import asyncio
import json
import os
from pathlib import Path

from mitmproxy import ctx, http


class Probe:
    def __init__(self):
        self.config = json.loads(Path(os.environ['TAP_LOCAL_FIXTURE_CONFIG']).read_text())
        self.root = Path(self.config['root'])
        self.task = None

    def emit(self, value):
        with (self.root / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(value) + '\n')

    def request(self, flow):
        if (flow.request.host != '127.0.0.1' or
                flow.request.port != self.config['origin_port'] or
                flow.request.path != self.config['path']):
            flow.response = http.Response.make(403, b'fixture-only\n')
            return
        self.emit({'event': 'intercepted',
                   'pid_available': getattr(flow.client_conn, 'pid', None) is not None,
                   'process_name_available': getattr(flow.client_conn, 'process_name', None) is not None})
        flow.response = http.Response.make(200, b'tap-core-intercepted\n',
                                           {'Content-Type': 'text/plain', 'Connection': 'close'})

    async def watch(self):
        previous = None
        while True:
            control = self.root / 'control.json'
            if control.exists():
                request = json.loads(control.read_text())
                if request != previous:
                    pid = request['pid']
                    if type(pid) is not int or pid not in self.config['client_pids']:
                        raise ValueError('Only the predeclared fixture PIDs may be selected')
                    if self.config['mode'] != 'local':
                        raise ValueError('Local rules cannot be changed in explicit control mode')
                    ctx.options.update(mode=[f'local:{pid}'])
                    previous = request
                    self.emit({'event': 'selector_requested', 'generation': request['generation']})
            await asyncio.sleep(0.1)

    def running(self):
        self.emit({'event': 'running', 'mode': self.config['mode']})
        self.task = asyncio.create_task(self.watch())
        self.task.add_done_callback(self.completed)

    def completed(self, task):
        if not task.cancelled() and task.exception():
            self.emit({'event': 'control_failed', 'error': type(task.exception()).__name__})
            ctx.master.shutdown()

    def done(self):
        if self.task:
            self.task.cancel()


addons = [Probe()]
