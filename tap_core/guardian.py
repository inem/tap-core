"""Keep one owned process group bounded by its controller's lifetime.

Launched as a new session. Trusted children must not daemonize/escape the group.
The guardian retains inherited reader locks until group cleanup.
"""
import os
import signal
import subprocess
import sys
import time


def main():
    parent = int(sys.argv[1])
    if os.getpid() != os.getpgrp() or os.getppid() != parent:
        return 1
    def stop(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    lock = os.environ.get('TAP_READER_LOCK_FD')
    process = subprocess.Popen(sys.argv[2:], pass_fds=(int(lock),) if lock else ())
    while process.poll() is None:
        if os.getppid() != parent:
            stop()
        time.sleep(0.05)
    # Kill remaining group members even when the entrypoint leaves descendants.
    # The controlling process sees nonzero on such an uncertain completion.
    return process.returncode


if __name__ == '__main__':
    sys.exit(main())
