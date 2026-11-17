#!/usr/bin/env python3
"""Exercise the final vendor wrappers and BusyBox init link on v6-r1."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path('/userdata/helmet-validation/20261117-native-app-v6-r1')
APP_LINK = Path('/opt/ui/src/apps/helmet_app')
WIFI_LINK = Path('/opt/ui/src/apps/helmet_wifi')
INIT = Path('/etc/init.d/S99zhelmet-app')
OUT = ROOT / 'evidence/entry-migration/entry-acceptance.json'

sys.path.insert(0, str(ROOT / 'src'))
from app import resident  # noqa: E402


def run(command, timeout=40):
    completed = subprocess.run(
        [str(item) for item in command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f'{command} failed: {completed.stderr.strip()} {completed.stdout.strip()}'
        )
    text = completed.stdout.strip()
    return json.loads(text) if text.startswith('{') else text


def coordinator_record():
    coordinator = resident.Coordinator().prepare()
    try:
        return coordinator.read_record()
    finally:
        coordinator.close()


def wait(predicate, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(.1)
    raise RuntimeError('timed out waiting for ' + description)


def application(record):
    return record['application']


def private(info, route, payload=None):
    connection = http.client.HTTPConnection('127.0.0.1', info['port'], timeout=5)
    try:
        body = None
        headers = {'X-Helmet-Token': info['token']}
        if payload is not None:
            body = json.dumps(payload)
            headers['Content-Type'] = 'application/json'
        connection.request('GET' if payload is None else 'POST', route, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        if response.status not in (200, 202):
            raise RuntimeError(f'{route}: HTTP {response.status}')
        return json.loads(raw)
    finally:
        connection.close()


def info_from_record(record):
    coordinator = resident.Coordinator(Path(record['workspace'])).prepare()
    try:
        ready = coordinator.read_json('ready.json')
        token = resident.service_token(coordinator)
    finally:
        coordinator.close()
    return {'port': ready['port'], 'token': token}


def lan_status(host='192.168.1.3'):
    connection = http.client.HTTPConnection(host, 8091, timeout=5)
    try:
        connection.request('GET', '/status.json')
        response = connection.getresponse()
        raw = response.read()
        return {'status': response.status, 'body': json.loads(raw)}
    finally:
        connection.close()


def stop_if_running():
    try:
        run([INIT, 'stop'], timeout=40)
    except (RuntimeError, subprocess.TimeoutExpired):
        pass
    record = coordinator_record()
    if record and record.get('manager') and resident.identity_alive(record['manager']):
        raise RuntimeError('previous resident owner did not stop')


def main():
    result = {
        'scope': 'actual final vendor wrapper paths and init symlink; command invocation, not physical finger touch',
        'root': str(ROOT),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'passes': False,
    }
    try:
        stop_if_running()
        result['init_start_1'] = run([INIT, 'start'])
        background = wait(
            lambda: (
                coordinator_record()
                if (coordinator_record() or {}).get('application', {}).get('phase') == 'running'
                and (coordinator_record() or {}).get('application', {}).get('intent', {}).get('visible') is False
                else None
            ),
            'background readiness',
        )
        result['background'] = {
            'session': background['session'],
            'identities': {
                role: background[role]['pid'] for role in ('manager', 'service')
            },
            'ui': background['application']['ui'],
            'visible_intent': background['application']['intent']['visible'],
            'lan_loopback': lan_status('127.0.0.1'),
            'lan_wlan': lan_status('192.168.1.3'),
        }
        session = background['session']

        result['open_app_link'] = run([APP_LINK])
        visible = wait(
            lambda: (
                coordinator_record()
                if (coordinator_record() or {}).get('application', {}).get('applied') is True
                and (coordinator_record() or {})['application']['ack']['visible'] is True
                else None
            ),
            'app icon visibility acknowledgement',
        )
        identities = {
            role: visible[role]['pid'] for role in ('manager', 'service', 'ui')
        }
        ui_instance = visible['application']['ui']['instance']
        info = info_from_record(visible)
        result['first_open'] = {
            'session': visible['session'],
            'identities': identities,
            'ui_instance': ui_instance,
            'tab': visible['application']['ack']['tab'],
        }
        assert visible['session'] == session
        private(info, '/api/start', {
            'model': 'helmet_416x416.rknn',
            'source': '1.jpg',
            'profile': 'responsive',
        })
        ready = wait(
            lambda: (
                private(info, '/api/status')['detection']
                if private(info, '/api/status')['detection'].get('running')
                and private(info, '/api/status')['detection'].get('valid')
                else None
            ),
            'valid final-root detection',
        )
        result['detection_ready_frame_id'] = ready['frame_id']

        result['open_wifi_link'] = run([WIFI_LINK])
        wifi = wait(
            lambda: (
                coordinator_record()
                if (coordinator_record() or {}).get('application', {}).get('applied') is True
                and (coordinator_record() or {})['application']['ack']['tab'] == 'wifi'
                else None
            ),
            'wifi tab acknowledgement',
        )
        result['wifi'] = {
            'session': wifi['session'],
            'identities': {role: wifi[role]['pid'] for role in ('manager', 'service', 'ui')},
            'ui_instance': wifi['application']['ui']['instance'],
            'tab': wifi['application']['ack']['tab'],
        }
        assert wifi['session'] == session
        assert result['wifi']['identities'] == identities
        assert result['wifi']['ui_instance'] == ui_instance

        result['open_detection_again'] = run([APP_LINK])
        detection_again = wait(
            lambda: (
                coordinator_record()
                if (coordinator_record() or {}).get('application', {}).get('applied') is True
                and (coordinator_record() or {})['application']['ack']['tab'] == 'detection'
                else None
            ),
            'detection tab acknowledgement',
        )
        time.sleep(2)
        later = private(info, '/api/status')['detection']
        result['detection_again'] = {
            'session': detection_again['session'],
            'identities': {
                role: detection_again[role]['pid'] for role in ('manager', 'service', 'ui')
            },
            'ui_instance': detection_again['application']['ui']['instance'],
            'frame_id_start': ready['frame_id'],
            'frame_id_end': later['frame_id'],
        }
        assert later['frame_id'] > ready['frame_id']

        result['init_status_1'] = run([INIT, 'status'])
        result['init_stop_1'] = run([INIT, 'stop'], timeout=40)
        terminal = wait(
            lambda: (
                coordinator_record()
                if (coordinator_record() or {}).get('phase') == 'completed'
                else None
            ),
            'first terminal record',
            timeout=35,
        )
        exit_receipt = json.loads(Path(terminal['workspace'], 'exit.json').read_text(encoding='utf-8'))
        result['terminal_1'] = {
            'session': terminal['session'],
            'phase': terminal['phase'],
            'exit': exit_receipt,
            'processes_alive': {
                role: resident.identity_alive(terminal[role]) for role in ('manager', 'service', 'ui')
            },
        }
        assert exit_receipt['cleanup_completed'] is True
        assert exit_receipt['resource_lease_released'] is True
        assert exit_receipt['service_exit'] == 0
        assert not any(result['terminal_1']['processes_alive'].values())

        result['init_start_2'] = run([INIT, 'start'])
        restarted = wait(
            lambda: (
                coordinator_record()
                if (coordinator_record() or {}).get('application', {}).get('phase') == 'running'
                else None
            ),
            'restarted background owner',
        )
        result['restart'] = {
            'session': restarted['session'],
            'new_session': restarted['session'] != session,
            'lan_wlan': lan_status('192.168.1.3'),
        }
        assert result['restart']['new_session'] is True
        result['init_status_2'] = run([INIT, 'status'])
        result['init_stop_2'] = run([INIT, 'stop'], timeout=40)
        result['passes'] = True
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        try:
            result['cleanup'] = run([INIT, 'stop'], timeout=40)
        except BaseException as cleanup_error:
            result['cleanup_error'] = f'{type(cleanup_error).__name__}: {cleanup_error}'
    finally:
        OUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        OUT.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        os.chmod(OUT, 0o600)
        OUT.with_suffix('.sha256').write_text(
            hashlib.sha256(OUT.read_bytes()).hexdigest() + '\n',
            encoding='ascii',
        )
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passes'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
