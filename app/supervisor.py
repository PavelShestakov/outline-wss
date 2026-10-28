"""Small PID 1 for the combined Outline / manager / key-delivery container.

The node is intentionally a single container so its API can HUP the daemon
without privileged Docker socket access or host PID namespaces.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

processes = []
stopping = False


def on_signal(signum, frame):
    global stopping
    stopping = True
    for child in processes:
        if child.poll() is None:
            child.terminate()


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    config = os.environ['OUTLINE_CONFIG']
    metrics = os.environ.get('OUTLINE_METRICS_URL', 'http://127.0.0.1:9091/metrics')
    # Metrics listen is always private to the node container.
    if metrics != 'http://127.0.0.1:9091/metrics':
        raise RuntimeError('Docker mode expects private metrics on 127.0.0.1:9091')
    port = int(os.environ['OUTLINE_WS_PORT'])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid WebSocket local port')
    cmd = ['/usr/local/bin/outline-ss-server', '-config=' + config,
           '-replay_history=10000', '-metrics=127.0.0.1:9091']
    outline = subprocess.Popen(cmd)
    processes.append(outline)
    pid_file = Path(os.getenv('OUTLINE_PID_FILE', '/run/outline-wss/outline.pid'))
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(outline.pid))
    os.chmod(pid_file, 0o600)
    processes.append(subprocess.Popen([sys.executable, '-m', 'app.keys_server']))
    processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                     '--host', '0.0.0.0', '--port', '18443',
                                     '--no-access-log', '--workers', '1']))
    try:
        while not stopping:
            for child in processes:
                result = child.poll()
                if result is not None:
                    print(f'outline-wss: subprocess exited ({result}); stopping node', flush=True)
                    return 1
            time.sleep(1)
        return 0
    finally:
        on_signal(signal.SIGTERM, None)
        for child in processes:
            if child.poll() is None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
        pid_file.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
