"""Project only the exact synthetic response selected by the live harness."""
import json
import os
from pathlib import Path
import sys

context = json.loads(os.environ['TAP_PACK_CONTEXT'])
for line in sys.stdin:
    record = json.loads(line)
    if record.get('url') != context['config']['url'] or record.get('status') != 200:
        continue
    body = json.loads(record['body'])
    result = {'record_id': record['record_id'], 'value': body['value']}
    target = Path(context['output_dir']) / 'result.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result))
    temporary.replace(target)
