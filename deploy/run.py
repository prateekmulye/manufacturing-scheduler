"""One app and one bounded HTTP proxy; stop both when either fails."""
import os
import signal
import subprocess
import sys
import time

children = []
stopping = False


def stop(_signal=None, _frame=None):
    global stopping
    stopping = True


for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, stop)

try:
    env = dict(os.environ, PORT="8081")
    children.append(subprocess.Popen(sys.argv[1:], env=env, start_new_session=True))
    children.append(subprocess.Popen(
        ["/usr/local/sbin/haproxy", "-db", "-f", "/app/deploy/haproxy.cfg"],
        start_new_session=True))
    while not stopping and all(child.poll() is None for child in children):
        time.sleep(0.1)
    code = 0 if stopping else 1
finally:
    # Kill owned process groups, including a running solver or Python agent.
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    for child in children:
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
sys.exit(code)
