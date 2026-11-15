#!/usr/bin/env python3
"""BusyBox init adapter: background readiness and evidence-confirmed normal stop."""
import argparse
import json
import math
from pathlib import Path
import sys
import time

try:
    from . import resident
except ImportError:
    import resident


def wait_for_cleanup(root, session, timeout=30):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('positive cleanup timeout required')
    root = Path(root)
    fingerprint = resident.code_identity(Path(resident.__file__).parent)
    coordinator = resident.Coordinator().prepare()
    deadline = time.monotonic() + timeout
    try:
        with coordinator.acquire('startup.lock', min(timeout, resident.STARTUP_TIMEOUT)):
            while time.monotonic() < deadline:
                record = coordinator.read_record()
                if record is None or record['session'] != session:
                    raise RuntimeError('requested owner record disappeared or changed')
                resident.match_install(record, root, fingerprint)
                try:
                    coordinator.assert_no_live_owners(record)
                except RuntimeError:
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
                    continue
                workspace = resident.Coordinator(Path(record['workspace'])).prepare()
                try:
                    receipt = workspace.read_json('exit.json')
                finally:
                    workspace.close()
                if receipt is not None:
                    if (record['phase'] != 'completed' or receipt.get('phase') != 'completed'
                            or receipt.get('session') != session or receipt.get('root') != str(root)
                            or receipt.get('version') != resident.APP_VERSION
                            or receipt.get('code_id') != fingerprint
                            or receipt.get('cleanup_completed') is not True
                            or receipt.get('resource_lease_released') is not True
                            or type(receipt.get('service_exit')) is not int or receipt['service_exit'] != 0):
                        raise RuntimeError('normal exit was not confirmed by the actual terminal receipt')
                    return {'stopped': True, 'phase': 'completed', 'session': session}
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
        raise TimeoutError('quit accepted but complete cleanup is still unconfirmed')
    finally:
        coordinator.close()


def start(root, timeout=20):
    resident.client(root, background=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application = resident.client(root, command='status')['application']
        if application.get('phase') not in ('starting', 'running'):
            raise RuntimeError('resident startup failed or is stopping')
        progress = application.get('progress', {})
        if application['phase'] == 'running' and progress.get('lan_alive') is True:
            return {'started': True, 'session': application['session'],
                    'lan_port': progress['lan_port'], 'detection': 'manual',
                    'visible': application['intent']['visible']}
        time.sleep(.1)
    raise TimeoutError('resident LAN listener did not become ready')


def stop(root, timeout=30):
    application = resident.client(root, command='quit')['application']
    if application.get('phase') == 'absent':
        # client() already checked both identities and kernel-held leases.
        return {'stopped': True, 'phase': 'absent'}
    return wait_for_cleanup(root, application['session'], timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args(argv)
    try:
        if args.action == 'start':
            result = start(args.root)
        elif args.action == 'stop':
            result = stop(args.root)
        else:
            result = resident.client(args.root, command='status')
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError):
        print('App boot action unconfirmed; inspect private readiness and cleanup receipts', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
