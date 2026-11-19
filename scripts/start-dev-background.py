"""Launch the existing local backend and Electron without a console window."""
import ctypes
import json
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / 'frontend'
RUNTIME = ROOT / 'config/runtime/dev-background'

def healthy():
    try:
        with urllib.request.urlopen('http://127.0.0.1:1733/api/health', timeout=3) as response:
            result = json.load(response)
        return result.get('code') == 0 and result.get('data', {}).get('status') == 'healthy'
    except Exception:
        return False

def frontend_ready():
    try:
        for port in (5173, 5678):
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass
        return True
    except OSError:
        return False

def launch(arguments, directory, name):
    with (RUNTIME / f'{name}.stdout.log').open('w', encoding='utf-8') as output, (RUNTIME / f'{name}.stderr.log').open('w', encoding='utf-8') as errors:
        process = subprocess.Popen(arguments, cwd=directory, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                   creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    (RUNTIME / ('backend.pid' if name == 'backend' else 'frontend-launcher.pid')).write_text(str(process.pid), encoding='ascii')
    return process

def wait_ready(process, check, name):
    for _ in range(30):
        if check():
            return
        if process.poll() is not None:
            break
        time.sleep(1)
    raise RuntimeError(f'MineContext {name} failed to start. Logs: {RUNTIME}')

def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not healthy():
        python = Path(sys.executable).with_name('python.exe')
        process = launch([str(python), '-m', 'opencontext.cli', 'start', '--port', '1733', '--config', 'config/config.yaml'], ROOT, 'backend')
        wait_ready(process, healthy, 'backend')
    if not frontend_ready():
        node = shutil.which('node.exe')
        if not node:
            raise RuntimeError('Node.js is not available on PATH.')
        process = launch([node, str(FRONTEND / 'node_modules/electron-vite/bin/electron-vite.js'), 'dev'], FRONTEND, 'frontend')
        wait_ready(process, frontend_ready, 'frontend')

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        (RUNTIME / 'launcher-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        ctypes.windll.user32.MessageBoxW(None, str(error), 'MineContext startup error', 0x10)
        sys.exit(1)
