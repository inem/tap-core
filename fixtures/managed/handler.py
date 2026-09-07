"""Example-only query of a reader projection; no workflow policy in the Hub."""
import json
import os
from pathlib import Path
import sys
import time

context = json.loads(os.environ['TAP_PACK_CONTEXT'])
request = json.loads(sys.stdin.readline())
if context['config'].get('delay'):
    time.sleep(context['config']['delay'])
if context['config'].get('echo'):
    result = {'ok': True, 'value': {'args': request['args'], 'page': context['page_id'], 'session': context['session_id']}}
else:
    try:
        value = json.loads(Path(context['config']['projection']).read_text())
        result = {'ok': True, 'value': value}
    except FileNotFoundError:
        result = {'ok': False, 'error': {'code': 'not_ready', 'message': 'Reader has not produced a result yet'}}
print(json.dumps(result))
