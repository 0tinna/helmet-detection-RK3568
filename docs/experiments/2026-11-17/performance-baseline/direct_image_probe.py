#!/usr/bin/env python3
"""Benchmark Qt Image loading through the existing binary frame endpoint.

This is a measurement probe, not a product implementation. It intentionally
does not claim generation, source epoch or TTL acceptance at the Qt boundary.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


QML = r'''
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Window 2.15
ApplicationWindow {
    id: root
    width: Screen.width
    height: Screen.height
    visibility: Window.FullScreen
    visible: true
    color: "#101e2a"
    property string apiBase: __API_BASE__
    property string apiToken: __API_TOKEN__
    property int serial: 0
    property int readyCount: 0
    property int errorCount: 0
    property bool showingA: true
    property int presentationCount: 0
    function tick() {
        var target = showingA ? imageB : imageA
        if (target.status === Image.Loading) return
        serial += 1
        target.source = apiBase + "/api/frame?token=" + encodeURIComponent(apiToken)
                + "&probe=" + serial
    }
    function settle(image, status) {
        if (status === Image.Ready) {
            readyCount += 1
            var old = image === imageA ? imageB : imageA
            image.opacity = 1
            image.z = 2
            old.z = 1
            showingA = image === imageA
            presentationCount += 1
            hideTimer.restart()
        } else if (status === Image.Error) {
            errorCount += 1
        }
    }
    Image {
        id: imageA
        anchors.fill: parent
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        cache: false
        opacity: 0
        onStatusChanged: root.settle(imageA, status)
    }
    Image {
        id: imageB
        anchors.fill: parent
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        cache: false
        opacity: 0
        onStatusChanged: root.settle(imageB, status)
    }
    Timer {
        id: hideTimer
        interval: 100
        repeat: false
        onTriggered: {
            var old = showingA ? imageB : imageA
            old.opacity = 0
            old.z = 1
            imageA.z = 0
            imageB.z = 0
        }
    }
    Timer { interval: 125; repeat: true; running: true; onTriggered: root.tick() }
    Timer {
        interval: 1000
        repeat: true
        running: true
        onTriggered: console.log("DIRECT_IMAGE_PROBE", JSON.stringify({
            requests: root.serial, ready: root.readyCount, errors: root.errorCount,
            presentations: root.presentationCount, width: root.width, height: root.height
        }))
    }
}
'''


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--model', default='helmet_416x416.rknn')
    parser.add_argument('--source', default='1.jpg')
    parser.add_argument('--profile', default='responsive')
    parser.add_argument('--seconds', type=float, default=20)
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
    workspace = resident.Coordinator(Path(record['workspace'])).prepare()
    try:
        ready = workspace.read_json('ready.json')
        token = resident.service_token(workspace)
    finally:
        workspace.close()

    def request(route, payload=None):
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
            value = json.loads(response.read())
            if response.status not in (200, 202):
                raise RuntimeError('%s returned HTTP %d' % (route, response.status))
            return value
        finally:
            connection.close()

    def process_ticks(pid):
        try:
            fields = Path('/proc/%d/stat' % pid).read_text().split()
            return int(fields[13]) + int(fields[14])
        except (FileNotFoundError, ValueError, IndexError):
            return None

    def cpu_percent(before, after, elapsed):
        if before is None or after is None or elapsed <= 0:
            return None
        return (after - before) / os.sysconf('SC_CLK_TCK') / elapsed * 100

    qmlscene = shutil.which('qmlscene')
    if qmlscene is None:
        raise RuntimeError('qmlscene is required')
    status = request('/api/status')['detection']
    if status.get('running'):
        request('/api/stop', {})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not request('/api/status')['detection'].get('running'):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('earlier detection did not stop')

    app_state = root / 'app-state'
    app_state.mkdir(mode=0o700, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='direct-image-probe-', dir=app_state))
    os.chmod(directory, 0o700)
    qml_path = directory / 'Probe.qml'
    qml_path.write_text(
        QML.replace('__API_BASE__', json.dumps('http://127.0.0.1:%d' % ready['port']))
           .replace('__API_TOKEN__', json.dumps(token)),
        encoding='utf-8')
    os.chmod(qml_path, 0o600)
    process = None
    output = ''
    result = {'label': 'v5-r3-direct-image-416', 'root': str(root),
              'session': record['session'], 'source': args.source,
              'model': args.model, 'profile': args.profile,
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    try:
        result['start'] = request('/api/start', {
            'model': args.model, 'source': args.source, 'profile': args.profile,
        })
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = request('/api/status')['detection']
            if status.get('running') and status.get('valid'):
                break
            if status.get('state') == 'fault':
                raise RuntimeError('detection entered fault before probe')
            time.sleep(.1)
        else:
            raise RuntimeError('detection did not become valid')
        process = subprocess.Popen([qmlscene, str(qml_path)], env=resident.qml_environment(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        time.sleep(.5)
        if process.poll() is not None:
            diagnostic, _ = process.communicate(timeout=5)
            output = diagnostic
            raise RuntimeError('qmlscene probe failed to start: ' + diagnostic[-2000:])
        began = time.monotonic()
        before = process_ticks(process.pid)
        first = request('/api/status')['detection']
        rows = []
        while time.monotonic() - began < args.seconds:
            if process.poll() is not None:
                raise RuntimeError('qmlscene probe exited early')
            time.sleep(1)
            current = request('/api/status')['detection']
            rows.append({'elapsed': time.monotonic() - began,
                         'frame_id': current.get('frame_id'),
                         'fps': current.get('fps'),
                         'latency': current.get('latency')})
        elapsed = time.monotonic() - began
        after = process_ticks(process.pid)
        last = request('/api/status')['detection']
        result['measurement'] = {
            'elapsed': elapsed,
            'detected': last['counters']['detected'] - first['counters']['detected'],
            'first_frame_id': first.get('frame_id'),
            'last_frame_id': last.get('frame_id'),
            'detection_fps_last': last.get('fps'),
            'latency_last': last.get('latency'),
            'qt_cpu_percent': cpu_percent(before, after, elapsed),
            'rows': rows,
        }
        if elapsed > 0:
            result['measurement']['detected_per_second'] = (
                result['measurement']['detected'] / elapsed)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=5)
        elif process is not None and process.returncode is not None and not output:
            try:
                output, _ = process.communicate(timeout=1)
            except (ValueError, subprocess.TimeoutExpired):
                pass
        try:
            if request('/api/status')['detection'].get('running'):
                request('/api/stop', {})
        except (OSError, RuntimeError, ValueError):
            pass
        for child in directory.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
        directory.rmdir()
        coordinator.close()
    result['qt_probe_log'] = [
        line for line in output.splitlines() if 'DIRECT_IMAGE_PROBE' in line
    ]
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result.get('measurement', {}).get('detected', 0) > 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
