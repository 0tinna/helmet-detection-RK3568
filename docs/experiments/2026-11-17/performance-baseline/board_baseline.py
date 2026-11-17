#!/usr/bin/env python3
"""Measure the installed App without changing detection or presentation."""
import argparse
import base64
import http.client
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--model', default='helmet_416x416.rknn')
    parser.add_argument('--source', default='1.jpg')
    parser.add_argument('--profile', default='responsive')
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--label', default='baseline')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--ui-mode', choices=('show', 'hide'), default='show')
    parser.add_argument('--preview-mode', choices=('legacy', 'binary'), default='legacy')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root)
    sys.path.insert(0, str(root / 'src'))
    from app import resident

    coordinator = resident.Coordinator().prepare()
    record = coordinator.read_record()
    if record is None or record.get('mode') != 'resident':
        raise RuntimeError('installed resident instance is not running')
    session = record['session']
    workspace = resident.Coordinator(Path(record['workspace'])).prepare()
    try:
        ready = workspace.read_json('ready.json')
        token = resident.service_token(workspace)
    finally:
        workspace.close()

    def private(route, payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', ready['port'], timeout=5)
        try:
            headers = {'X-Helmet-Token': token}
            body = None
            if payload is not None:
                headers['Content-Type'] = 'application/json'
                body = json.dumps(payload)
            connection.request('GET' if payload is None else 'POST', route,
                               body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw)
            if response.status not in (200, 202):
                raise RuntimeError('%s returned HTTP %d' % (route, response.status))
            return value, len(raw)
        finally:
            connection.close()

    def application():
        return private('/api/application')[0]

    def private_bytes(route):
        connection = http.client.HTTPConnection('127.0.0.1', ready['port'], timeout=5)
        try:
            connection.request('GET', route, headers={'X-Helmet-Token': token})
            response = connection.getresponse()
            raw = response.read()
            if response.status != 200 or response.getheader('Content-Type') != 'image/jpeg':
                raise RuntimeError('%s returned HTTP %d' % (route, response.status))
            return raw
        finally:
            connection.close()

    def control(name):
        command = [sys.executable, str(root / 'src/app/launch.py'),
                   '--root', str(root), '--control', name]
        run = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if run.returncode != 0:
            raise RuntimeError('resident control failed: ' + name)
        return json.loads(run.stdout)

    def wait_visible(value):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app = application()
            ack = app.get('ack') or {}
            if app.get('applied') is True and ack.get('visible') is value:
                return app
            time.sleep(.1)
        raise RuntimeError('Qt visibility ACK did not settle')

    def process_sample(pid):
        if not pid:
            return None
        try:
            fields = Path('/proc/%d/stat' % pid).read_text().split()
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            return None
        io = {}
        try:
            for line in Path('/proc/%d/io' % pid).read_text().splitlines():
                key, raw = line.split(':', 1)
                io[key] = int(raw.strip())
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            pass
        return {'pid': pid, 'ticks': int(fields[13]) + int(fields[14]), 'io': io}

    def cpu_percent(before, after, elapsed):
        if before is None or after is None or elapsed <= 0:
            return None
        return (after['ticks'] - before['ticks']) / os.sysconf('SC_CLK_TCK') / elapsed * 100

    def io_delta(before, after, name):
        if before is None or after is None:
            return None
        return after['io'].get(name, 0) - before['io'].get(name, 0)

    status = private('/api/status')[0]['detection']
    if status.get('running'):
        private('/api/stop', {})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not private('/api/status')[0]['detection'].get('running'):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('an earlier detection run did not stop')

    started = time.monotonic()
    result = {
        'label': args.label,
        'root': str(root),
        'session': session,
        'source': args.source,
        'model': args.model,
        'profile': args.profile,
        'started_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'rows': [],
    }
    try:
        result['start'] = private('/api/start', {
            'model': args.model, 'source': args.source, 'profile': args.profile,
        })[0]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = private('/api/status')[0]['detection']
            if status.get('running') and status.get('valid'):
                break
            if status.get('state') == 'fault':
                raise RuntimeError('detection entered fault before measurement')
            time.sleep(.1)
        else:
            raise RuntimeError('detection did not become valid')

        if args.ui_mode == 'show':
            control('show')
            first_app = wait_visible(True)
        else:
            app = application()
            if (app.get('ack') or {}).get('visible') is True:
                control('hide')
                wait_visible(False)
            first_app = application()
        owner = coordinator.read_record()
        roles = {
            'manager': owner['manager']['pid'],
            'service': owner['service']['pid'],
            'ui': owner['ui']['pid'] if owner.get('ui') else None,
        }
        before_processes = {role: process_sample(pid) for role, pid in roles.items()}
        began = time.monotonic()
        next_sample = began
        while time.monotonic() - began < args.seconds:
            delay = next_sample - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, .25))
                continue
            status_value, status_bytes = private('/api/status')
            app_value = application()
            sample_at = time.monotonic()
            result['rows'].append({
                'elapsed': sample_at - began,
                'detection': status_value['detection'],
                'ack': app_value.get('ack'),
                'status_bytes': status_bytes,
                'processes': {role: process_sample(pid) for role, pid in roles.items()},
            })
            next_sample += 1.0

        elapsed = time.monotonic() - began
        if args.ui_mode == 'show':
            control('hide')
            final_app = wait_visible(False)
        else:
            final_app = application()
        after_processes = {role: process_sample(pid) for role, pid in roles.items()}
        first_ack = first_app.get('ack') or {}
        last_ack = final_app.get('ack') or {}
        rows = result['rows']
        first_detection = rows[0]['detection']
        last_detection = rows[-1]['detection']
        if args.preview_mode == 'binary':
            meta_value, preview_bytes = private('/api/preview-meta')
            frame_token = meta_value['frame_token']
            jpeg_bytes = len(private_bytes('/api/frame?frame_token=' + frame_token))
            wrapper_overhead = preview_bytes
            base64_overhead = 0
        else:
            preview_value, preview_bytes = private('/api/preview')
            jpeg_bytes = len(base64.b64decode(preview_value['jpeg_base64'], validate=True))
            wrapper_overhead = preview_bytes - jpeg_bytes
            base64_overhead = wrapper_overhead
        result['measurement'] = {
            'ui_mode': args.ui_mode,
            'elapsed': elapsed,
            'samples': len(rows),
            'roles': roles,
            'ui_ack_requests': last_ack.get('frame_requests', 0) - first_ack.get('frame_requests', 0),
            'ui_ack_decodes': last_ack.get('frame_decodes', 0) - first_ack.get('frame_decodes', 0),
            'ui_ack_presentations': last_ack.get('preview_presentations', 0)
                - first_ack.get('preview_presentations', 0),
            'detected': last_detection.get('counters', {}).get('detected', 0)
                - first_detection.get('counters', {}).get('detected', 0),
            'output': last_detection.get('counters', {}).get('output', 0)
                - first_detection.get('counters', {}).get('output', 0),
            'first_frame_id': first_detection.get('frame_id'),
            'last_frame_id': last_detection.get('frame_id'),
            'detection_fps_last': last_detection.get('fps'),
            'latency_last': last_detection.get('latency'),
            'cpu_percent': {role: cpu_percent(before_processes[role],
                                              after_processes[role], elapsed)
                            for role in roles if roles[role] is not None},
            'io': {role: {
                'read_bytes': io_delta(before_processes[role], after_processes[role], 'read_bytes'),
                'write_bytes': io_delta(before_processes[role], after_processes[role], 'write_bytes'),
                'syscw': io_delta(before_processes[role], after_processes[role], 'syscw'),
                'cancelled_write_bytes': io_delta(before_processes[role], after_processes[role],
                                                    'cancelled_write_bytes'),
            } for role in roles if roles[role] is not None},
            'preview_json_bytes': preview_bytes,
            'preview_jpeg_bytes': jpeg_bytes,
            'preview_base64_overhead_bytes': base64_overhead,
            'preview_wrapper_overhead_bytes': wrapper_overhead,
            'preview_mode': args.preview_mode,
        }
        rates = {}
        if elapsed > 0:
            rates['detected_per_second'] = result['measurement']['detected'] / elapsed
            rates['ui_presentations_per_second'] = (
                result['measurement']['ui_ack_presentations'] / elapsed)
            rates['ui_requests_per_second'] = result['measurement']['ui_ack_requests'] / elapsed
        result['measurement']['rates'] = rates
        result['passed'] = (
            result['measurement']['detected'] > 0
            and (args.ui_mode == 'hide'
                 or result['measurement']['ui_ack_presentations'] > 0)
            and all(value is None or math.isfinite(value)
                    for value in result['measurement']['cpu_percent'].values())
        )
    finally:
        try:
            if private('/api/status')[0]['detection'].get('running'):
                private('/api/stop', {})
        except (OSError, RuntimeError, ValueError):
            pass
        coordinator.close()
    output = result if args.full else {
        key: value for key, value in result.items() if key != 'rows'
    }
    if not args.full:
        output['sample_frames'] = [
            {'elapsed': row['elapsed'], 'frame_id': row['detection'].get('frame_id'),
             'detected_fps': row['detection'].get('fps', {}).get('detected'),
             'ui_presentations': (row.get('ack') or {}).get('preview_presentations')}
            for row in result.get('rows', [])
        ]
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
