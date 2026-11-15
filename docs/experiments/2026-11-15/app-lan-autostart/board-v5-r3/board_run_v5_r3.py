import base64
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/userdata/helmet-validation/20261115-native-app-v5-r3-lan-3')
OUT = Path('/userdata/helmet-validation/20261115-app-lan-validation-3')
sys.path.insert(0, str(ROOT / 'src'))
from app import resident

coordinator = resident.Coordinator().prepare()
record = coordinator.read_record()
SESSION = record['session']
workspace = resident.Coordinator(Path(record['workspace'])).prepare()
ready = workspace.read_json('ready.json')
token = resident.service_token(workspace)
workspace.close()


def private(route, payload=None):
    connection = http.client.HTTPConnection('127.0.0.1', ready['port'], timeout=4)
    try:
        headers = {'X-Helmet-Token': token}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers['Content-Type'] = 'application/json'
        connection.request('GET' if payload is None else 'POST', route, body=body, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        if response.status not in (200, 202):
            raise RuntimeError('private operation failed: ' + route + ' ' + str(response.status))
        return value
    finally:
        connection.close()


def lan(route='/status.json'):
    connection = http.client.HTTPConnection('127.0.0.1', 8091, timeout=4)
    try:
        connection.request('GET', route)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.headers)
    finally:
        connection.close()


def cli(arguments):
    run = subprocess.run(arguments, capture_output=True, text=True, timeout=40)
    if run.returncode != 0:
        raise RuntimeError('normal entry did not confirm the operation: ' + run.stderr)
    return json.loads(run.stdout)


def application():
    return private('/api/application')


def wait_visible(expected):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        app = application()
        if app.get('applied') and app.get('ack', {}).get('visible') is expected:
            return app
        time.sleep(.1)
    raise RuntimeError('actual Qt visibility ACK did not settle')


result = {'scope': 'v5-r3 actual App/USB camera; local authenticated start/stop API and normal icon wrappers; not human phone/touch',
          'root': str(ROOT), 'session': SESSION, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'zip_sha256': 'ce7dffe010f5da5828dfce8586bc6cf799dbd43b9ed3dcdbe71eb2042d030472',
          'code_identity': resident.code_identity(ROOT / 'src/app'),
          'model_sha256': hashlib.sha256((ROOT / 'models/helmet_416x416.rknn').read_bytes()).hexdigest(),
          'config': {'model': 'helmet_416x416.rknn', 'source': '/dev/video10', 'profile': 'responsive',
                     'conf': .35, 'nms': .45, 'skip': 0, 'ttl': 1.0}, 'actions': []}
try:
    result['start'] = private('/api/start', {'model': 'helmet_416x416.rknn',
                                            'source': '/dev/video10', 'profile': 'responsive'})
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        status = private('/api/status')['detection']
        if status.get('running') and status.get('valid'):
            break
        if status.get('state') == 'fault':
            raise RuntimeError('camera detection reported a fault')
        time.sleep(.2)
    else:
        raise RuntimeError('camera did not become valid')
    result['open'] = cli(['/opt/ui/src/apps/helmet_app'])
    first_app = wait_visible(True)
    assert first_app['session'] == SESSION
    owner = coordinator.read_record()
    identities = {role: owner[role] for role in ('manager', 'service', 'ui')}
    assert all(identities.values())
    result['identities'] = identities
    result['manager_sid'] = __import__('os').getsid(identities['manager']['pid'])
    camera_format = subprocess.run(['v4l2-ctl', '-d', '/dev/video10', '--get-fmt-video', '--get-parm'],
                                   capture_output=True, text=True)
    result['camera_format'] = {'exit': camera_format.returncode, 'text': camera_format.stdout}
    code, raw, _ = lan('/api/preview')
    assert code == 200
    package = json.loads(raw)
    jpeg = base64.b64decode(package.pop('jpeg_base64'), validate=True)
    import cv2, numpy as np
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    result['first_lan_frame'] = {'package': package, 'sha256': hashlib.sha256(jpeg).hexdigest(),
                                 'shape': list(decoded.shape)}
    (OUT / 'camera-first.jpg').write_bytes(jpeg)
    print(json.dumps({'started': True, 'session': SESSION, 'identities': identities,
                      'lan_frame': package['frame_id'], 'shape': list(decoded.shape)}), flush=True)
    began = time.monotonic()
    rows = []
    done = set()
    with (OUT / 'camera-180s.jsonl').open('x', encoding='utf-8') as stream:
        while time.monotonic() - began < 180:
            elapsed = time.monotonic() - began
            if elapsed >= 30 and 'hide' not in done:
                cli([sys.executable, str(ROOT / 'src/app/launch.py'), '--root', str(ROOT), '--control', 'hide'])
                app = wait_visible(False)
                result['actions'].append({'action': 'hide', 'elapsed': time.monotonic() - began,
                                          'application': app})
                done.add('hide')
            if elapsed >= 65 and 'wifi' not in done:
                result['actions'].append({'action': 'wifi', 'elapsed': elapsed,
                                          'reply': cli(['/opt/ui/src/apps/helmet_wifi'])})
                wait_visible(True)
                done.add('wifi')
            if elapsed >= 70 and 'reshow' not in done:
                result['actions'].append({'action': 'reshow', 'elapsed': elapsed,
                                          'reply': cli(['/opt/ui/src/apps/helmet_app'])})
                wait_visible(True)
                done.add('reshow')
            if elapsed >= 90 and 'ensure' not in done:
                before = application()['intent']
                again = cli([sys.executable, str(ROOT / 'src/app/boot.py'), 'start', '--root', str(ROOT)])
                after = application()['intent']
                assert before == after and again['session'] == SESSION
                result['actions'].append({'action': 'ensure', 'elapsed': elapsed,
                                          'reply': again, 'visibility_unchanged': before == after})
                done.add('ensure')
            own = coordinator.read_record()
            detection = private('/api/status')['detection']
            code, body, _ = lan()
            public = json.loads(body)
            row = {'elapsed': time.monotonic() - began, 'detection': detection,
                   'lan_http': code, 'lan': public,
                   'identities': {role: own[role] for role in identities},
                   'application': own['application']}
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            rows.append(row)
            if len(rows) % 20 == 0:
                print(json.dumps({'elapsed': row['elapsed'], 'frame_id': public['frame_id'],
                                  'valid': public['valid'], 'visible': row['application']['intent']['visible']}), flush=True)
            time.sleep(1)
    hidden = [row for row in rows if not row['application']['intent']['visible']]
    frames = [row['lan']['frame_id'] for row in rows]
    result['continuous'] = {'elapsed': time.monotonic() - began, 'samples': len(rows),
                            'first_frame': frames[0], 'last_frame': frames[-1],
                            'frame_regressions': sum(b < a for a, b in zip(frames, frames[1:])),
                            'invalid': sum(row['lan']['valid'] is not True for row in rows),
                            'not_running': sum(row['detection']['running'] is not True for row in rows),
                            'pid_changes': sum(row['identities'] != identities for row in rows),
                            'generations': len({row['lan']['generation'] for row in rows}),
                            'hidden_samples': len(hidden),
                            'hidden_first_frame': hidden[0]['lan']['frame_id'],
                            'hidden_last_frame': hidden[-1]['lan']['frame_id']}
    result['stop_detection'] = private('/api/stop', {})
    code, body, _ = lan('/api/preview')
    result['after_stop'] = {'http': code, 'body': json.loads(body)}
    assert code == 503 and result['after_stop']['body']['valid'] is False
    result['quit'] = cli([sys.executable, str(ROOT / 'src/app/boot.py'), 'stop', '--root', str(ROOT)])
    result['repeat_quit'] = cli([sys.executable, str(ROOT / 'src/app/boot.py'), 'stop', '--root', str(ROOT)])
    assert result['repeat_quit']['phase'] == 'completed' and result['repeat_quit']['session'] == SESSION
    result['hidden_preview_counts'] = {key: sorted({row['application']['ack'][key] for row in hidden}) for key in ('frame_requests', 'frame_decodes', 'preview_presentations')}
    coordinator.assert_no_live_owners(coordinator.read_record())
    result['npu_after'] = Path('/sys/kernel/debug/rknpu/load').read_text().strip()
    result['passed'] = (result['continuous']['frame_regressions'] == 0
                        and result['continuous']['invalid'] == 0 and result['continuous']['not_running'] == 0
                        and result['continuous']['pid_changes'] == 0
                        and result['continuous']['hidden_last_frame'] > result['continuous']['hidden_first_frame']
                        and result['quit']['stopped'] is True)
except BaseException as error:
    result['passed'] = False
    result['error'] = type(error).__name__ + ': ' + str(error)
    try:
        result['cleanup_after_error'] = cli([sys.executable, str(ROOT / 'src/app/boot.py'), 'stop', '--root', str(ROOT)])
    except BaseException as cleanup_error:
        result['cleanup_error'] = type(cleanup_error).__name__
finally:
    coordinator.close()
    with (OUT / 'camera-summary.json').open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: result.get(key) for key in ('passed', 'session', 'continuous', 'quit', 'error')},
                     ensure_ascii=False), flush=True)
raise SystemExit(0 if result['passed'] else 1)
