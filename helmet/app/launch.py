#!/usr/bin/env python3
"""Open the native resident App, or run an explicitly bounded one-shot check."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

try:
    from . import resident
except ImportError:
    import resident


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--tab', choices=resident.TABS, default='detection')
    parser.add_argument('--control', choices=('show', 'status', 'hide', 'quit'),
                        help='bounded local resident control; default is show')
    parser.add_argument('--background', action='store_true',
                        help='ensure the resident service exists without changing window visibility')
    parser.add_argument('--smoke-seconds', type=int, default=0)
    parser.add_argument('--auto-demo', action='store_true', help='verification-only automatic replay start')
    parser.add_argument('--verify-keyboard', action='store_true', help='verification-only included touch keyboard')
    parser.add_argument('--verify-scan', action='store_true', help='verification-only scan without connecting')
    parser.add_argument('--verify-resident-ui', action='store_true',
                        help='native private capture per show on the normal resident path; no automatic start')
    args = parser.parse_args(argv)
    if args.background and (args.control is not None or args.smoke_seconds or args.verify_resident_ui):
        parser.error('background bootstrap cannot be mixed with controls or one-shot verification')
    if not 0 <= args.smoke_seconds <= 3600:
        parser.error('smoke duration must be 0..3600')
    automatic = args.auto_demo or args.verify_keyboard or args.verify_scan
    if automatic and args.smoke_seconds == 0:
        parser.error('one-shot verification flags require a bounded --smoke-seconds duration')
    if args.smoke_seconds and (args.control is not None or args.verify_resident_ui):
        parser.error('one-shot verification cannot be mixed with resident controls/captures')
    if args.verify_resident_ui and args.control not in (None, 'show'):
        parser.error('native resident capture is enabled only when opening the application')
    args.mode = 'one-shot' if args.smoke_seconds else 'resident'
    args.alert_port = 0 if args.mode == 'one-shot' else 8092
    args.control = args.control or 'show'
    return args


def one_shot(args):
    """Compatibility smoke, with the same cross-root inherited resource lease."""
    app = Path(__file__).resolve().parent
    root = resident.validate_install(Path(args.root), app)
    qmlscene = shutil.which('qmlscene')
    if qmlscene is None:
        raise RuntimeError('qmlscene is required on the board')
    os.umask(0o077)
    coordinator = resident.Coordinator().prepare()
    workspace = None
    manager_lease = resource_lease = None
    service = ui = None
    service_log = ui_log = None
    service_id = ui_id = None
    port = token = None
    stopped = False
    phase = 'starting'
    session = uuid.uuid4().hex
    fingerprint = resident.code_identity(app)
    manager_id = resident.process_identity(os.getpid())
    success = False

    def stop(number, frame):
        nonlocal stopped
        stopped = True

    def record():
        row = os.fstat(resource_lease.fileno())
        return {'protocol': resident.PROTOCOL, 'version': resident.APP_VERSION, 'code_id': fingerprint,
                'root': str(root), 'session': session, 'workspace': str(workspace.directory),
                'mode': 'one-shot', 'phase': phase, 'manager': manager_id,
                'service': service_id, 'ui': ui_id,
                'lease': {'device': row.st_dev, 'inode': row.st_ino,
                          'service_inherited': service is not None}, 'recorded_at': time.monotonic()}

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with coordinator.acquire('startup.lock', resident.STARTUP_TIMEOUT):
            coordinator.assert_no_live_owners(coordinator.read_record())
            manager_lease = coordinator.acquire('manager.lock')
            resource_lease = coordinator.acquire('resource.lock')
            runtime = resident.private_directory(root / 'app-state')
            workspace = resident.Coordinator(runtime / ('session-' + session)).prepare()
            coordinator.remove_socket()
            coordinator.write_record(record())
        service_log = os.fdopen(workspace.open_file('service.log', os.O_WRONLY, exclusive=True), 'wb')
        ui_log = os.fdopen(workspace.open_file('ui.log', os.O_WRONLY, exclusive=True), 'wb')
        service = subprocess.Popen(resident.service_arguments(
            app, root, workspace.directory, resource_lease.fileno(),
            alert_port=args.alert_port, lan_port=0,
        ), pass_fds=(resource_lease.fileno(),), stdin=subprocess.DEVNULL,
            stdout=service_log, stderr=subprocess.STDOUT)
        service_id = resident.process_identity(service.pid)
        coordinator.write_record(record())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if service.poll() is not None or stopped:
                raise RuntimeError('local service failed to become ready')
            ready = resident.service_ready(
                workspace, service, expected_alert_port=args.alert_port)
            if ready is not None:
                port, token = ready['port'], resident.service_token(workspace)
                break
            time.sleep(.05)
        else:
            raise RuntimeError('local service readiness budget elapsed')
        wrapper = resident.write_qml_wrapper(workspace, 'App.qml', app,
            {'apiBase': 'http://127.0.0.1:' + str(port), 'apiToken': token,
             'initialTab': args.tab, 'residentMode': False, 'automaticDemo': args.auto_demo,
             'smokeSeconds': args.smoke_seconds, 'verificationDir': str(workspace.directory),
             'verificationKeyboard': args.verify_keyboard, 'verificationScan': args.verify_scan})
        ui = subprocess.Popen([qmlscene, str(wrapper)], env=resident.qml_environment(),
                              pass_fds=(resource_lease.fileno(),), stdin=subprocess.DEVNULL,
                              stdout=ui_log, stderr=subprocess.STDOUT)
        ui_id = resident.process_identity(ui.pid)
        # Normal cleanup must begin even if Qt's own smoke Timer never runs.
        smoke_deadline = time.monotonic() + args.smoke_seconds
        phase = 'running'
        coordinator.write_record(record())
        workspace.write_json('processes.json', {'launcher': os.getpid(), 'service': service.pid,
            'ui': ui.pid, 'tab': args.tab, 'offline_localhost': True, 'mode': 'one-shot',
            'session': session, 'identities': {'launcher': manager_id, 'service': service_id, 'ui': ui_id}})
        while ui.poll() is None and not stopped and time.monotonic() < smoke_deadline:
            if service.poll() is not None:
                raise RuntimeError('local service exited while application was open')
            time.sleep(.1)
    finally:
        closed = False
        journal_failed = False
        if workspace is not None and resource_lease is not None:
            phase = 'stopping'
            try:
                coordinator.write_record(record())
            except (OSError, ValueError):
                journal_failed = True
            # A receipt failure never skips the real owner's normal cleanup.
            if service is not None and service.poll() is None:
                resident.request_service_shutdown(service, service_id, port, token)
                while service.poll() is None:
                    time.sleep(.2)
            if ui is not None and ui.poll() is None:
                ui.terminate()
                while ui.poll() is None:
                    time.sleep(.2)
            closed = ((service is None or service.poll() is not None)
                      and (ui is None or ui.poll() is not None))
            if closed:
                for name in ('token', 'App.qml'):
                    try:
                        if workspace._stat(name) is not None:
                            os.unlink(name, dir_fd=workspace.descriptor)
                    except (OSError, ValueError):
                        journal_failed = True
            success = (closed and service is not None and service.returncode == 0
                       and ui is not None and ui.returncode == 0 and not journal_failed)
            phase = 'completed' if success else 'failed'
            try:
                coordinator.write_record(record())
            except (OSError, ValueError):
                journal_failed, success = True, False
            if closed:
                resource_lease.close()
                resource_lease = None
            try:
                workspace.write_json('exit.json', {'service_exit': service.returncode if service else None,
                    'ui_exit': ui.returncode if ui else None, 'cleanup_completed': closed,
                    'mode': 'one-shot', 'session': session, 'journal_failed': journal_failed,
                    'resource_lease_released': resource_lease is None})
            except (OSError, ValueError):
                success = False
        for stream in (service_log, ui_log):
            if stream is not None:
                stream.close()
        if resource_lease is not None:
            resource_lease.close()
        if manager_lease is not None:
            manager_lease.close()
        if workspace is not None:
            workspace.close()
        coordinator.close()
    return 0 if success else 1


def launch(argv=None):
    args = parse_arguments(argv)
    if args.mode == 'one-shot':
        return one_shot(args)
    reply = resident.client(args.root, args.tab, args.control, args.verify_resident_ui,
                            background=args.background)
    # Accepted is a lifecycle intent; the separate Qt ACK records actual application.
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(launch())
    except (ValueError, RuntimeError, OSError):
        print('Application request failed; inspect the private resident ownership and lifecycle receipts', file=sys.stderr)
        raise SystemExit(1)
