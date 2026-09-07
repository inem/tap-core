"""Own-process HTTP client. Uses raw HTTPConnection, never environment proxies."""
import http.client
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    connection = http.client.HTTPConnection('127.0.0.1', request['port'], timeout=3)
    try:
        connection.request('GET', request['target'])
        response = connection.getresponse()
        print(json.dumps({'status': response.status, 'body': response.read(1024).decode()}), flush=True)
    except Exception as error:
        print(json.dumps({'error': type(error).__name__}), flush=True)
    finally:
        connection.close()
