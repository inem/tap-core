"""Keep one owned process group bounded by its controller's lifetime.

Launched as a new session. Trusted children must not daemonize/escape the group.
The guardian retains inherited reader locks until group cleanup.
"""
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import threading
import time


def _in_process_python_target(argv):
    if not argv:
        return None
    exe = argv[0]
    if exe not in (sys.executable, '/usr/bin/python3') and not Path(exe).name.startswith('python'):
        return None
    rest = list(argv[1:])
    while rest and rest[0] == '-B':
        rest.pop(0)
    if not rest or rest[0].startswith('-') or not rest[0].endswith('.py'):
        return None
    script = Path(rest[0])
    if not script.is_file():
        return None
    return str(script), rest[1:]


def main():
    parent = int(sys.argv[1])
    if os.getpid() != os.getpgrp() or os.getppid() != parent:
        return 1
    def stop(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    target = _in_process_python_target(sys.argv[2:])
    if target is not None:
        script, args = target
        def watch():
            while True:
                if os.getppid() != parent:
                    stop()
                time.sleep(0.05)
        threading.Thread(target=watch, daemon=True).start()
        sys.argv = [script, *args]
        sys.path[0] = str(Path(script).resolve().parent)
        runpy.run_path(script, run_name='__main__')
        return 0
    cmd = list(sys.argv[2:])
    if cmd and cmd[0] == '/usr/bin/python3':
        cmd[0] = sys.executable
    lock = os.environ.get('TAP_READER_LOCK_FD')
    process = subprocess.Popen(cmd, pass_fds=(int(lock),) if lock else ())
    while process.poll() is None:
        if os.getppid() != parent:
            stop()
        time.sleep(0.05)
    # The controller cleans remaining group members after reaping this guard.
    return process.returncode


if __name__ == '__main__':
    sys.exit(main())
