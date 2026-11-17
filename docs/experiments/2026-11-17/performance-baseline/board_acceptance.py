"""Collect resident, LAN and restart acceptance evidence for the v6 candidate."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path('/userdata/helmet-validation/20261117-native-app-v6-perf-7')
OUT = ROOT / 'evidence/performance/acceptance'
APP = ROOT / 'src/app'
PYTHON = sys.executable
LAN_PORT = 8091

sys.path.insert(0, str(ROOT / 'src'))
from app import resident  # noqa: E402


def json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def run_cli(*arguments, timeout=45):
    completed = subprocess.run(
        [PYTHON, *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            'command failed: '
            + ' '.join(map(str, arguments))
            + ': '
            + completed.stderr.strip()
            + ': '
            + completed.stdout.strip()
        )
    text = completed.stdout.strip()
    return json.loads(text) if text else None


def private_call(connection_info, route, payload=None, token_query=True):
    connection = http.client.HTTPConnection(
        '127.0.0.1', connection_info['port'], timeout=5
    )
    try:
        headers = {'X-Helmet-Token': connection_info['token']}
        body = None
        separator = '&' if '?' in route else '?'
        if token_query:
            route += separator + 'token=' + connection_info['token']
        if payload is not None:
            body = json.dumps(payload)
            headers['Content-Type'] = 'application/json'
        connection.request(
            'GET' if payload is None else 'POST',
            route,
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader('Content-Type', '')
        value = json.loads(raw) if 'json' in content_type else raw
        if response.status not in (200, 202):
            raise RuntimeError(
                f'private operation failed: {route} {response.status}: {value!r}'
            )
        return {
            'status': response.status,
            'content_type': content_type,
            'value': value,
            'raw': raw if isinstance(value, bytes) else None,
        }
    finally:
        connection.close()


def lan_call(route='/status.json', host='127.0.0.1'):
    connection = http.client.HTTPConnection(host, LAN_PORT, timeout=5)
    try:
        connection.request('GET', route)
        response = connection.getresponse()
        raw = response.read()
        return {
            'host': host,
            'route': route,
            'status': response.status,
            'content_type': response.getheader('Content-Type', ''),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'raw': raw,
        }
    finally:
        connection.close()


def wait_until(predicate, description, timeout=20.0, interval=0.1):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(interval)
    raise RuntimeError(f'timed out waiting for {description}; last={last!r}')


def owner_record():
    coordinator = resident.Coordinator().prepare()
    try:
        return coordinator.read_record()
    finally:
        coordinator.close()


def wait_application(expected_visible, expected_tab=None, timeout=20.0):
    def ready():
        record = owner_record()
        if not record or not record.get('manager'):
            return None
        app = record['application']
        if not app.get('applied'):
            return None
        if app['ack']['visible'] is not expected_visible:
            return None
        if expected_tab is not None and app['ack']['tab'] != expected_tab:
            return None
        return record

    return wait_until(ready, 'QML visibility/tab acknowledgement', timeout)


def connection_info():
    record = owner_record()
    coordinator = resident.Coordinator(Path(record['workspace'])).prepare()
    try:
        ready = coordinator.read_json('ready.json')
        token = resident.service_token(coordinator)
    finally:
        coordinator.close()
    return {'port': ready['port'], 'token': token}


def detection_status(info):
    return private_call(info, '/api/status')['value']['detection']


def identity_values(record):
    return {
        role: record[role]['pid']
        for role in ('manager', 'service', 'ui')
    }


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def terminate_any_owner():
    try:
        run_cli(APP / 'boot.py', 'stop', '--root', ROOT, timeout=35)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    coordinator = resident.Coordinator().prepare()
    try:
        record = coordinator.read_record()
        if record is not None:
            wait_until(
                lambda: not (
                    record.get('manager')
                    and resident.identity_alive(record['manager'])
                ),
                'existing owner cleanup',
                timeout=30,
            )
            coordinator.assert_no_live_owners(record)
    finally:
        coordinator.close()


def main():
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {
        'scope': (
            'v6-perf7 normal launch/resident control path with local 1.jpg replay; '
            'not physical USB camera, not human screen/phone observation'
        ),
        'root': str(ROOT),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'code_identity': resident.code_identity(APP),
        'actions': [],
        'passed': False,
    }
    try:
        terminate_any_owner()
        result['preclean'] = {
            'owner': owner_record(),
            'processes_before_start': subprocess.run(
                ['sh', '-c', "ps w | grep -E 'launch.py|resident.py|service.py' | grep -v grep || true"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout,
        }

        result['first_open_reply'] = run_cli(
            APP / 'launch.py', '--root', ROOT, '--tab', 'detection'
        )
        first_record = wait_application(True, 'detection')
        session_a = first_record['session']
        identities_a = identity_values(first_record)
        ui_instance_a = first_record['application']['ui']['instance']
        info = connection_info()
        result['first_open'] = {
            'session': session_a,
            'identities': identities_a,
            'ui_instance': ui_instance_a,
            'application': first_record['application'],
        }

        result['start_detection'] = private_call(
            info,
            '/api/start',
            {
                'model': 'helmet_416x416.rknn',
                'source': '1.jpg',
                'profile': 'responsive',
            },
        )['value']
        first_status = wait_until(
            lambda: (
                detection_status(info)
                if detection_status(info).get('running')
                and detection_status(info).get('valid')
                and detection_status(info).get('frame_id') is not None
                else None
            ),
            'valid replay detection',
            timeout=20,
        )
        result['detection_ready'] = first_status

        status_response = lan_call('/status.json')
        public = json.loads(status_response['raw'])
        assert status_response['status'] == 200 and public['valid'] is True
        assert isinstance(public.get('frame_token'), str)
        result['lan_status_loopback'] = {
            key: value for key, value in status_response.items() if key != 'raw'
        } | {'public': public}

        wlan_response = lan_call('/status.json', host='192.168.1.3')
        result['lan_status_wlan'] = {
            key: value for key, value in wlan_response.items() if key != 'raw'
        }
        assert wlan_response['status'] == 200
        assert json.loads(wlan_response['raw'])['frame_token'] == public['frame_token']

        token_one = public['frame_token']
        frame_response = lan_call('/frame.jpg?frame_token=' + token_one)
        assert frame_response['status'] == 200
        assert frame_response['content_type'].startswith('image/jpeg')
        assert frame_response['raw'].startswith(b'\xff\xd8')
        assert b'\xff\xd9' in frame_response['raw'][-8:]
        result['lan_exact_frame'] = {
            'frame_token': token_one,
            'status': frame_response['status'],
            'content_type': frame_response['content_type'],
            'bytes': len(frame_response['raw']),
            'sha256': frame_response['sha256'],
            'jpeg_markers': True,
        }

        invalid = lan_call('/frame.jpg?frame_token=invalid')
        result['lan_invalid_token'] = {
            'status': invalid['status'],
            'body': invalid['raw'].decode('utf-8'),
        }
        assert invalid['status'] == 400
        time.sleep(1.25)
        expired = lan_call('/frame.jpg?frame_token=' + token_one)
        result['lan_expired_token'] = {
            'status': expired['status'],
            'body': expired['raw'].decode('utf-8'),
        }
        assert expired['status'] == 503

        result['hide_reply'] = run_cli(
            APP / 'launch.py', '--root', ROOT, '--control', 'hide'
        )
        hidden_start_record = wait_application(False)
        hidden_start_status = detection_status(info)
        hidden_start_ack = hidden_start_record['application']['ack']
        time.sleep(6.0)
        hidden_end_record = owner_record()
        hidden_end_status = detection_status(info)
        hidden_end_ack = hidden_end_record['application']['ack']
        result['hidden_period'] = {
            'session': hidden_end_record['session'],
            'identities_start': identity_values(hidden_start_record),
            'identities_end': identity_values(hidden_end_record),
            'frame_id_start': hidden_start_status['frame_id'],
            'frame_id_end': hidden_end_status['frame_id'],
            'frame_id_delta': hidden_end_status['frame_id'] - hidden_start_status['frame_id'],
            'running_end': hidden_end_status['running'],
            'valid_end': hidden_end_status['valid'],
            'frame_requests_start': hidden_start_ack.get('frame_requests', 0),
            'frame_requests_end': hidden_end_ack.get('frame_requests', 0),
            'ui_instance_start': hidden_start_record['application']['ui']['instance'],
            'ui_instance_end': hidden_end_record['application']['ui']['instance'],
        }
        assert identity_values(hidden_start_record) == identity_values(hidden_end_record)
        assert hidden_end_status['frame_id'] > hidden_start_status['frame_id']
        assert hidden_end_ack.get('frame_requests', 0) == hidden_start_ack.get(
            'frame_requests', 0
        )

        result['reshow_reply'] = run_cli(
            APP / 'launch.py', '--root', ROOT, '--tab', 'detection'
        )
        reshow_record = wait_application(True, 'detection')
        reshow_status = detection_status(info)
        result['reshow'] = {
            'session': reshow_record['session'],
            'identities': identity_values(reshow_record),
            'ui_instance': reshow_record['application']['ui']['instance'],
            'frame_id': reshow_status['frame_id'],
        }
        assert reshow_record['session'] == session_a
        assert identity_values(reshow_record) == identities_a
        assert reshow_record['application']['ui']['instance'] == ui_instance_a
        assert reshow_status['frame_id'] >= hidden_end_status['frame_id']

        result['wifi_reply'] = run_cli(
            APP / 'launch.py', '--root', ROOT, '--tab', 'wifi'
        )
        wifi_record = wait_application(True, 'wifi')
        result['wifi_tab'] = {
            'session': wifi_record['session'],
            'identities': identity_values(wifi_record),
            'ui_instance': wifi_record['application']['ui']['instance'],
            'tab': wifi_record['application']['ack']['tab'],
        }
        assert wifi_record['session'] == session_a
        assert identity_values(wifi_record) == identities_a
        assert wifi_record['application']['ui']['instance'] == ui_instance_a

        result['detection_tab_reply'] = run_cli(
            APP / 'launch.py', '--root', ROOT, '--tab', 'detection'
        )
        detection_record = wait_application(True, 'detection')
        result['detection_tab'] = {
            'session': detection_record['session'],
            'identities': identity_values(detection_record),
            'ui_instance': detection_record['application']['ui']['instance'],
            'tab': detection_record['application']['ack']['tab'],
        }
        assert detection_record['session'] == session_a
        assert identity_values(detection_record) == identities_a
        assert detection_record['application']['ui']['instance'] == ui_instance_a

        workspace = Path(detection_record['workspace'])
        result['quit_reply'] = run_cli(
            APP / 'boot.py', 'stop', '--root', ROOT, timeout=40
        )
        quit_record = wait_until(
            lambda: (
                owner_record()
                if (owner_record() or {}).get('phase') == 'completed'
                else None
            ),
            'terminal resident record',
            timeout=35,
        )
        exit_receipt = json_file(workspace / 'exit.json')
        coordinator = resident.Coordinator().prepare()
        try:
            coordinator.assert_no_live_owners(quit_record)
            socket_exists = coordinator.directory.joinpath('control.sock').exists()
        finally:
            coordinator.close()
        result['terminal'] = {
            'session': quit_record['session'],
            'phase': quit_record['phase'],
            'exit': exit_receipt,
            'socket_exists': socket_exists,
            'process_alive': {
                role: process_alive(pid) for role, pid in identities_a.items()
            },
        }
        assert exit_receipt['cleanup_completed'] is True
        assert exit_receipt['resource_lease_released'] is True
        assert exit_receipt['phase'] == 'completed'
        assert exit_receipt['service_exit'] == 0
        assert socket_exists is False
        assert all(not value for value in result['terminal']['process_alive'].values())

        result['restart_reply'] = run_cli(
            APP / 'launch.py', '--root', ROOT, '--tab', 'detection'
        )
        restart_record = wait_application(True, 'detection')
        session_b = restart_record['session']
        result['restart'] = {
            'session': session_b,
            'identities': identity_values(restart_record),
            'ui_instance': restart_record['application']['ui']['instance'],
        }
        assert session_b != session_a
        result['restart_quit_reply'] = run_cli(
            APP / 'boot.py', 'stop', '--root', ROOT, timeout=40
        )
        final_record = wait_until(
            lambda: (
                owner_record()
                if (owner_record() or {}).get('phase') == 'completed'
                else None
            ),
            'restart terminal record',
            timeout=35,
        )
        coordinator = resident.Coordinator().prepare()
        try:
            coordinator.assert_no_live_owners(final_record)
        finally:
            coordinator.close()
        result['restart_terminal'] = {
            'session': final_record['session'],
            'phase': final_record['phase'],
        }
        result['passed'] = True
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        try:
            result['cleanup_after_error'] = run_cli(
                APP / 'boot.py', 'stop', '--root', ROOT, timeout=40
            )
        except BaseException as cleanup_error:
            result['cleanup_error'] = f'{type(cleanup_error).__name__}: {cleanup_error}'
    finally:
        path = OUT / 'resident-lan-acceptance.json'
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        os.chmod(path, 0o600)
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        'passed',
                        'error',
                        'first_open',
                        'hidden_period',
                        'reshow',
                        'wifi_tab',
                        'terminal',
                        'restart',
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
