#!/usr/bin/env python3
"""Start the local service and native Qt UI without a browser or external host."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def launch(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--tab', choices=('detection', 'wifi'), default='detection')
    parser.add_argument('--smoke-seconds', type=int, default=0)
    parser.add_argument('--auto-demo', action='store_true', help='verification-only automatic replay start')
    parser.add_argument('--verify-keyboard', action='store_true', help='verification-only show the included touch keyboard')
    parser.add_argument('--verify-scan', action='store_true', help='verification-only scan WiFi without connecting')
    args = parser.parse_args(argv)
    if args.smoke_seconds < 0 or args.smoke_seconds > 3600:
        parser.error('smoke duration must be 0..3600')
    root = Path(args.root).resolve()
    app = Path(__file__).resolve().parent
    runtime = root / 'app-state'
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    qmlscene = shutil.which('qmlscene')
    if qmlscene is None:
        raise RuntimeError('qmlscene is required on the board')
    lock = (runtime / 'launcher.lock').open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Helmet application is already running', file=sys.stderr)
        return 1
    workspace = Path(tempfile.mkdtemp(prefix='session-', dir=runtime))
    os.chmod(workspace, 0o700)
    service = ui = None
    closed = False
    service_log = (workspace / 'service.log').open('wb')
    ui_log = (workspace / 'ui.log').open('wb')
    stopped = False
    ui_code = 1
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        service = subprocess.Popen([
            sys.executable, str(app / 'service.py'), '--root', str(root), '--port', '0',
            '--token-file', str(workspace / 'token'), '--ready-file', str(workspace / 'ready.json'),
        ], stdout=service_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while not (workspace / 'ready.json').is_file():
            if service.poll() is not None or time.monotonic() > deadline or stopped:
                raise RuntimeError('local service failed to become ready')
            time.sleep(.05)
        ready = json.loads((workspace / 'ready.json').read_text())
        token = (workspace / 'token').read_text().strip()
        qml_url = (app / 'qml' / 'Main.qml').as_uri()
        properties = {'apiBase': 'http://127.0.0.1:' + str(ready['port']), 'apiToken': token,
                      'initialTab': args.tab, 'automaticDemo': args.auto_demo,
                      'smokeSeconds': args.smoke_seconds, 'verificationDir': str(workspace) if args.smoke_seconds else '',
                      'verificationKeyboard': args.verify_keyboard, 'verificationScan': args.verify_scan}
        # Token is private file content, never argv/env/console output.
        wrapper = workspace / 'App.qml'
        wrapper.write_text('import QtQuick 2.15\nimport QtQuick.Window 2.15\n'
                           'Loader { Component.onCompleted: setSource(' + json.dumps(qml_url) + ', '
                           + json.dumps(properties, ensure_ascii=False) + ') }\n', encoding='utf-8')
        os.chmod(wrapper, 0o600)
        env = os.environ.copy()
        env.setdefault('XDG_RUNTIME_DIR', '/var/run')
        env.setdefault('WAYLAND_DISPLAY', 'wayland-0')
        env.setdefault('QT_QPA_PLATFORM', 'wayland')
        # The included touch keyboard works on Wayland without compositor IME.
        env.pop('QT_IM_MODULE', None)
        env['QT_QUICK_CONTROLS_STYLE'] = 'Material'
        env['QT_QUICK_CONTROLS_MATERIAL_THEME'] = 'Dark'
        ui = subprocess.Popen([qmlscene, str(wrapper)], env=env, stdout=ui_log, stderr=subprocess.STDOUT)
        (workspace / 'processes.json').write_text(json.dumps({'launcher': os.getpid(),
            'service': service.pid, 'ui': ui.pid, 'tab': args.tab, 'offline_localhost': True}), encoding='utf-8')
        while ui.poll() is None and not stopped:
            if service.poll() is not None:
                raise RuntimeError('local service exited while application was open')
            time.sleep(.1)
        if ui.poll() is None:
            ui.terminate()
        ui.wait(timeout=5)
        ui_code = 0 if ui.returncode == 0 else 1
    finally:
        if ui is not None and ui.poll() is None:
            ui.terminate()
            try:
                ui.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print('Qt application is closing; retaining application lock', file=sys.stderr)
                while ui.poll() is None:
                    time.sleep(.5)
        if service is not None and service.poll() is None:
            service.terminate()
            try:
                service.wait(timeout=45)
            except subprocess.TimeoutExpired:
                # Do not externally kill a live SDK or WiFi transaction.
                print('Service cleanup still in progress; retaining application lock', file=sys.stderr)
                while service.poll() is None:
                    time.sleep(.5)
        closed = (service is None or service.poll() is not None) and (ui is None or ui.poll() is not None)
        service_log.close()
        ui_log.close()
        if closed:
            for sensitive in ('token', 'App.qml'):
                try:
                    (workspace / sensitive).unlink()
                except FileNotFoundError:
                    pass
        (workspace / 'exit.json').write_text(json.dumps({'service_exit': service.returncode if service else None,
            'ui_exit': ui.returncode if ui else None, 'cleanup_completed': closed}), encoding='utf-8')
        lock.close()
    return 0 if ui_code == 0 and service is not None and service.returncode == 0 and closed else 1


if __name__ == '__main__':
    try:
        raise SystemExit(launch())
    except (ValueError, RuntimeError, OSError) as error:
        print('Application startup failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
