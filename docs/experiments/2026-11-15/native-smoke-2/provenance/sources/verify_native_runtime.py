#!/usr/bin/env python3
"""Evidence for the unchanged native Service/Pipeline/Qt app.

Run on the board with preinstalled RKNNLite and Qt. Verify copied evidence on
the host without RKNN/Qt (OpenCV/NumPy decode the actual saved JPEG/PNG files).
Only the coordinating owner runs board experiments. This explicit full-log,
30-second snapshot configuration adds resource/logging threads and disk I/O to
the default app. Replay, automatic Qt rendering and independent accuracy are
separate acceptance gates. No governor/network configuration is changed.
"""
import argparse
import base64
import glob
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


# Importing unchanged board modules must not create caches outside the new run.
sys.dont_write_bytecode = True
HELPER_PATH = Path(__file__).resolve().parent.parent / '2026-11-14/verify_runtime.py'
_spec = importlib.util.spec_from_file_location('native_runtime_shared_evidence', HELPER_PATH)
old = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(old)
PUBLIC_TARGET_FPS = 10.0
PROBE_INTERVAL = 5.0
REQUIRED = ('frames.jsonl', 'frames.jsonl.resources.jsonl', 'session.json',
            'events.jsonl', 'resources.jsonl', 'http.jsonl', 'cleanup.json')


def inside(path, parent):
    try:
        return path != parent and path.relative_to(parent) is not None
    except ValueError:
        return False


def validate_args(args):
    if args.command != 'run':
        return
    if not old.is_number(args.duration) or args.duration <= 0:
        raise ValueError('duration must be positive and finite')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', args.run_id):
        raise ValueError('run ID must be a single safe directory name')
    if (args.model != 'helmet_416x416.rknn' or args.source != 'construction-replay.avi'
            or args.profile != 'responsive'):
        raise ValueError('this experiment fixes model, input and responsive profile')


def new_directory(args):
    root = Path(args.output_root).resolve()
    directory = (root / args.run_id).resolve()
    if not inside(directory, root):
        raise ValueError('run directory escapes output root')
    # Check the lexical leaf too: an existing symlink must not redirect writes.
    if (root / args.run_id).exists() or (root / args.run_id).is_symlink():
        raise FileExistsError('run directory already exists')
    root.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    return directory


def load_app(root):
    root = Path(root).resolve()
    candidates = (root / 'src', root / 'helmet')
    runtime_dir = next((p for p in candidates if (p / 'app/service.py').is_file()), None)
    if runtime_dir is None:
        raise ValueError('app root needs src/app/service.py or helmet/app/service.py')
    # Each run owns a serial SDK instrumentation scope. Reject cached modules
    # from another install rather than silently measuring different code.
    for name, expected in (('pipeline_runtime', runtime_dir / 'pipeline_runtime.py'),
                           ('app.service', runtime_dir / 'app/service.py')):
        cached = sys.modules.get(name)
        if cached is not None and Path(cached.__file__).resolve() != expected.resolve():
            # Host tests stage identical source into new temporary installs.
            # Remove flat runtime modules together so provenance remains exact.
            for key in tuple(sys.modules):
                if key in ('pipeline_runtime', 'sources', 'telemetry', 'alarm', 'yolo_post', 'output_sinks', 'app') or key.startswith('app.'):
                    sys.modules.pop(key, None)
            break
    sys.path.insert(0, str(runtime_dir))
    return importlib.import_module('pipeline_runtime'), importlib.import_module('app.service')


def runtime_sampler(runtime):
    return runtime.RuntimeSampler()


def read_governors():
    return [{'path': p, 'before': Path(p).read_text().strip(), 'after': None,
             'restore_success': False, 'requested': None}
            for p in sorted(glob.glob('/sys/class/devfreq/*npu*/governor'))]


def finish_governors(records):
    for row in records:
        try:
            row['after'] = Path(row['path']).read_text().strip()
            row['restore_success'] = row['after'] == row['before']
        except OSError as error:
            row['error'] = type(error).__name__
    return records


def process_sample(pid, previous=None):
    """Linux child measurement bound to PID and /proc start token; no psutil."""
    now = time.monotonic()
    text = Path('/proc/%d/stat' % pid).read_text()
    fields = text[text.rfind(')') + 2:].split()
    start_id, ticks = fields[19], int(fields[11]) + int(fields[12])
    rss = int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
    cpu_seconds = ticks / os.sysconf('SC_CLK_TCK')
    percent = 0.0
    if previous and previous.get('start_id') == start_id:
        percent = 100 * (cpu_seconds - previous['cpu_seconds']) / max(now - previous['monotonic'], 1e-9)
    return {'pid': pid, 'start_id': start_id, 'monotonic': now, 'rss_bytes': rss,
            'cpu_seconds': cpu_seconds, 'cpu_percent': percent}


class AppSampler:
    def __init__(self, delegate, gui):
        self.delegate, self.gui, self.previous = delegate, gui, None

    def sample(self):
        row = self.delegate.sample()
        child = self.gui.get('process')
        row['gui'] = None
        if child is not None and child.poll() is None:
            try:
                self.previous = row['gui'] = process_sample(child.pid, self.previous)
            except (OSError, ValueError, IndexError) as error:
                row['gui_error'] = type(error).__name__
        elif child is not None:
            row['gui_exited'] = True
        row['total_rss_bytes'] = (row['rss_bytes'] + row['gui']['rss_bytes']) if row['gui'] and old.is_number(row.get('rss_bytes')) else row.get('rss_bytes')
        row['total_cpu_percent'] = (row['cpu_percent'] + row['gui']['cpu_percent']) if row['gui'] and old.is_number(row.get('cpu_percent')) else row.get('cpu_percent')
        return row


class HttpProbe:
    def __init__(self, service, directory, journal):
        self.service, self.directory, self.journal = service, directory, journal
        self.number = 0

    def request(self, route, payload=None, phase='control', observation=None):
        started = time.monotonic()
        request = Request('http://127.0.0.1:%d%s' % (self.service.port, route),
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={'X-Helmet-Token': self.service.token, 'Content-Type': 'application/json'})
        row = {'route': route, 'method': 'GET' if payload is None else 'POST',
               'started_monotonic': started, 'phase': phase, 'observation': observation}
        try:
            try:
                response = urlopen(request, timeout=3)
            except HTTPError as response_error:
                response = response_error
            with response:
                raw = response.read()
                row.update(status=response.code, body=json.loads(raw), raw_response=raw.decode('utf-8'),
                           response_sha256=hashlib.sha256(raw).hexdigest())
            if route == '/api/preview' and row['status'] == 200:
                jpeg = base64.b64decode(row['body']['jpeg_base64'], validate=True)
                import cv2
                import numpy as np
                image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError('HTTP JPEG did not decode')
                path = self.directory / 'previews' / ('probe-%06d.jpg' % self.number)
                path.parent.mkdir(exist_ok=True)
                with path.open('xb') as stream:
                    stream.write(jpeg)
                row['jpeg'] = {'artifact': path.relative_to(self.directory).as_posix(),
                               **old.file_record(path), 'decoded_size': [image.shape[1], image.shape[0]]}
        except BaseException as error:
            # Never record arbitrary device/network exceptions that might echo
            # credentials. A type plus the route/time is sufficient evidence.
            row['failure'] = type(error).__name__
        row.update(finished_monotonic=time.monotonic())
        row['latency_ms'] = (row['finished_monotonic'] - started) * 1000
        self.journal.write(row)
        self.number += 1
        return row

    def observe(self, number, phase='active'):
        before = self.request('/api/status', phase=phase, observation=number)
        preview = self.request('/api/preview', phase=phase, observation=number)
        after = self.request('/api/status', phase=phase, observation=number)
        return before, preview, after


def private_file(path, text):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def launch_gui(app, directory, service, duration, state):
    qmlscene = shutil.which('qmlscene')
    if qmlscene is None:
        raise RuntimeError('qmlscene unavailable')
    if os.environ.get('QT_QPA_PLATFORM', 'wayland') != 'wayland':
        raise RuntimeError('this experiment requires the existing Wayland session')
    private = directory / 'private'
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    wrapper = private / 'App.qml'
    properties = {'apiBase': 'http://127.0.0.1:%d' % service.port,
                  'apiToken': service.token, 'initialTab': 'detection',
                  'automaticDemo': False, 'smokeSeconds': 0, 'verificationScan': False}
    control = directory / 'gui-control.json'
    old.write_json(control, {'phase': 'starting'})
    state['ready'] = threading.Event()
    # The installed Main.qml is loaded unchanged. The wrapper reads only a
    # local control file; no extra GUI HTTP poll is added. Its 15-second safety
    # deadline cannot substitute for the owner's Pipeline-stop -> Qt.quit path.
    text = ('import QtQuick 2.15\nimport QtQuick.Window 2.15\nItem {\n'
            'property bool captured: false\nproperty bool controlPending: false\n'
            'function renderSurface(){if(!app.item)return null;var children=app.item.contentItem.children;'
            'for(var i=0;i<children.length;i++){var nested=children[i].children;'
            'for(var j=0;j<nested.length;j++){if(String(nested[j]).indexOf("QQuickStackLayout")>=0)'
            'return children[i];}}return null;}\n'
            'function previewImage(item){if(!item)return null;'
            'if(item.source!==undefined&&String(item.source).indexOf("data:image/jpeg;base64,")===0)return item;'
            'var list=item.children||[];for(var i=0;i<list.length;i++){var found=previewImage(list[i]);if(found)return found;}return null;}\n'
            'function syncChoices(item,selected){if(!item)return;'
            'var wanted={model:"helmet_416x416.rknn",source:"construction-replay.avi",profile:"responsive"};'
            'if(String(item).indexOf("ComboBox")>=0&&item.model){for(var key in wanted){'
            'var index=app.item.chooseIndex(item.model,wanted[key]);'
            'if(item.model[index]&&item.model[index].id===wanted[key]){item.currentIndex=index;'
            'selected[key]=item.model[item.currentIndex].id;}}}'
            'var children=item.children||[];for(var i=0;i<children.length;i++)syncChoices(children[i],selected);}\n'
            'Loader { id: app; Component.onCompleted: setSource(' +
            json.dumps((Path(app.__file__).parent / 'qml/Main.qml').as_uri()) + ', ' +
            json.dumps(properties) + '); onLoaded: console.log("NATIVE_READY") }\n'
            'Timer { interval: 500; repeat: true; running: true; onTriggered: {\n'
            'if(controlPending) return; controlPending=true; var xhr=new XMLHttpRequest();\n'
            'xhr.open("GET",' + json.dumps(control.as_uri()) + ');\n'
            'xhr.onreadystatechange=function(){if(xhr.readyState!==XMLHttpRequest.DONE)return;'
            'controlPending=false;var c={};try{c=JSON.parse(xhr.responseText)}catch(e){return}\n'
            'if(c.phase==="stopped"){console.log("NATIVE_EXIT","pipeline-stopped");Qt.quit();return}\n'
            'if(c.phase==="running"&&app.item&&app.item.haveConfig){var selected={};syncChoices(app.item.contentItem,selected);}\n'
            'if(c.phase!=="running"||captured||Date.now()<c.capture_at_wall_ms||!app.item)return;'
            'captured=true;var surface=renderSurface();'
            'console.log("NATIVE_SURFACE",surface?String(surface):"not-found");'
            'if(!surface)return;surface.grabToImage(function(r) {\n'
            'var saved=r.saveToFile(' + json.dumps(str(directory / 'screen.png')) + ');\n'
            'console.log("NATIVE_RENDER",JSON.stringify({saved:saved,valid:!!app.item.detection.valid,'
            'frameId:app.item.detection.frame_id||null,generation:app.item.detection.generation||null,'
            'rendered_wall_ms:Date.now(),surfaceWidth:surface.width,surfaceHeight:surface.height,'
            'previewVisible:!!(previewImage(surface)&&previewImage(surface).visible),'
            'previewReady:!!(previewImage(surface)&&previewImage(surface).status===Image.Ready),'
            'previewSourceKind:previewImage(surface)?"inline-jpeg":"unavailable",'
            'previewSourceSize:previewImage(surface)?[previewImage(surface).sourceSize.width,previewImage(surface).sourceSize.height]:null,'
            'uiSelection:selected,'
            'width:app.item.width,height:app.item.height})); }); };xhr.send(); } }\n'
            'Timer { interval: %d; running: true; onTriggered: {console.log("NATIVE_EXIT","watchdog");Qt.quit()} }\n}\n' % (int(duration * 1000) + 15000))
    private_file(wrapper, text)
    env = os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR', '/var/run')
    env.setdefault('WAYLAND_DISPLAY', 'wayland-0')
    env['QT_QPA_PLATFORM'] = 'wayland'
    env.pop('QT_IM_MODULE', None)
    env['QT_QUICK_CONTROLS_STYLE'] = 'Material'
    env['QT_QUICK_CONTROLS_MATERIAL_THEME'] = 'Dark'
    env['QML_XHR_ALLOW_FILE_READ'] = '1'
    env['QML_DISABLE_DISK_CACHE'] = '1'
    env['XDG_CACHE_HOME'] = str(directory / 'qt-cache')
    child = subprocess.Popen([qmlscene, str(wrapper)], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    state.update(process=child, started=True, pid=child.pid,
                 started_monotonic=time.monotonic(), executable=old.file_record(qmlscene),
                 wayland={'display': env['WAYLAND_DISPLAY'], 'runtime_dir': env['XDG_RUNTIME_DIR'], 'platform': 'wayland'})
    log = (directory / 'gui.log').open('x', encoding='utf-8')

    def drain():
        try:
            for line in iter(child.stdout.readline, b''):
                decoded = line.decode('utf-8', errors='replace').replace(service.token, '[redacted]')
                if re.fullmatch(r'\s*(?:qml:\s*)?NATIVE_READY\s*', decoded):
                    state['ready'].set()
                if re.match(r'\s*(?:qml:\s*)?NATIVE_RENDER\s+\{', decoded):
                    state['render_received_monotonic'] = time.monotonic()
                log.write(decoded)
                log.flush()
        except BaseException as error:
            state['log_failure'] = type(error).__name__
        finally:
            child.stdout.close()
            log.close()
    state['log_thread'] = threading.Thread(target=drain, name='native-qt-log', daemon=True)
    state['log_thread'].start()
    if not state['ready'].wait(timeout=10):
        raise RuntimeError('Qt did not load the installed Main.qml')
    state['ready_monotonic'] = time.monotonic()


def close_gui(state, directory):
    child = state.get('process')
    if child is None:
        clean_private(directory)
        return
    old.write_json(directory / 'gui-control.json', {'phase': 'stopped'})
    try:
        child.wait(timeout=4)
    except subprocess.TimeoutExpired:
        state['forced_termination'] = True
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            state['kill_required'] = True
            child.kill()
            child.wait(timeout=3)
    state.update(exited=child.poll() is not None, returncode=child.returncode,
                 exited_monotonic=time.monotonic())
    state['log_thread'].join(timeout=3)
    state['log_thread_alive'] = state['log_thread'].is_alive()
    state['screen_saved'] = (directory / 'screen.png').is_file()
    # Token exists only in a private wrapper; remove it before inventory/export.
    clean_private(directory)


def clean_private(directory):
    wrapper = directory / 'private/App.qml'
    if wrapper.exists():
        wrapper.unlink()
    if wrapper.parent.exists():
        wrapper.parent.rmdir()


def source_inventory(runtime, app):
    directory = Path(runtime.__file__).parent
    records = {p.name: old.file_record(p) for p in sorted(directory.glob('*.py'))}
    app_dir = Path(app.__file__).parent
    records.update({'app/' + p.relative_to(app_dir).as_posix(): old.file_record(p)
                    for p in app_dir.rglob('*') if p.is_file() and p.suffix in ('.py', '.qml')})
    records['verify_runtime.py'] = old.file_record(HELPER_PATH)
    records['verify_native_runtime.py'] = old.file_record(__file__)
    return records


def copy_provenance(manifest, directory):
    """Bind offline SHA checks to the actual measured input/model/source bytes."""
    mapping = {}
    for kind in ('model', 'input'):
        mapping[kind] = 'provenance/%s/%s' % (kind, Path(manifest[kind]['path']).name)
    mapping.update({'source:' + name: 'provenance/sources/' + name for name in manifest['sources']})
    for key, relative in mapping.items():
        record = manifest[key] if key in ('model', 'input') else manifest['sources'][key.split(':', 1)[1]]
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with Path(record['path']).open('rb') as source, target.open('xb') as destination:
            shutil.copyfileobj(source, destination)
        if old.file_record(target)['sha256'] != record['sha256']:
            raise RuntimeError('source changed while provenance was copied')
    manifest['provenance_copies'] = mapping


def environment_versions():
    result = {'python': sys.version, 'platform': platform.platform(), 'installation': 'preinstalled; no package installation performed'}
    for package in ('numpy', 'opencv-python', 'opencv-python-headless', 'rknn-toolkit-lite2', 'psutil'):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    import cv2
    import numpy as np
    result.update(opencv=cv2.__version__, numpy=np.__version__)
    return result


def run_native(args):
    handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        return _run_native(args)
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


def _run_native(args):
    validate_args(args)
    directory = new_directory(args)
    started = time.monotonic()
    gui = {'requested': bool(args.gui), 'started': False, 'exited': False}
    manifest = {'schema_version': 2, 'kind': 'native-app-longrun', 'cleanup_contract': 'direct-sdk-v1',
                'run_id': args.run_id, 'status': 'running', 'pid': os.getpid(),
                'signal': None, 'signal_received_monotonic': None,
                'started_monotonic': started, 'started_wall_time': old.utc_now(),
                'requested_duration_seconds': args.duration, 'replay_fps': 20.0, 'result_ttl': 1.0,
                'public_target_fps': PUBLIC_TARGET_FPS, 'profile': args.profile,
                'thresholds': {'conf': .35, 'nms': .45, 'skip': 0}, 'input_size': [416, 416],
                'class_order': ['person', 'hat'], 'source_kind': 'video-replay',
                'physical_camera_tested': False, 'human_display_tested': False,
                'wifi_switching_tested': False, 'wifi_operations': ['status'],
                'probe_interval_seconds': PROBE_INTERVAL, 'gui': gui,
                'launch_path': 'direct DetectionController/LocalService + private QML wrapper; nonstandard launcher instrumentation; launch.py is not executed',
                'evidence_configuration': {'duration': args.duration, 'full_jsonl': True,
                    'session_json': True, 'save_interval_seconds': 30.0,
                    'additional_load': ['resource-monitor thread', 'HTTP observation every 5 seconds',
                                        'Qt stdout reader when GUI enabled', 'full per-frame disk logs',
                                        'periodic annotated saves', 'GUI wrapper local control-file poll every .5 seconds'],
                    'default_app_has_full_logs': False}, 'governors': [], 'exit_code': None}
    old.write_json(directory / 'manifest.json', manifest)
    events = http_journal = monitor = sdk = controller = service = None
    owned = {'pipeline': None, 'source': None, 'engine': None}
    interrupted = threading.Event()
    old_handlers = {}
    signal_number = None
    code = 1

    def handle_signal(number, frame):
        nonlocal signal_number
        signal_number = number
        interrupted.set()
        # No file I/O or Journal locks in a signal handler: a signal can arrive
        # during Journal.close or hashing. All fields already exist in manifest.
        manifest['signal'] = number
        manifest['signal_received_monotonic'] = time.monotonic()
        manifest['status'] = 'failed'
        manifest['exit_code'] = 128 + number
        if owned['pipeline'] is not None:
            owned['pipeline'].stop_event.set()

    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            old_handlers[number] = signal.signal(number, handle_signal)
        runtime, app = load_app(args.app_root)
        model, source_path = app.DetectionController(args.app_root)._resolve(args.model, args.source, args.profile)
        manifest.update(model=old.file_record(model), input=old.file_record(source_path),
                        sources=source_inventory(runtime, app), environment=environment_versions(),
                        governors=read_governors())
        copy_provenance(manifest, directory)
        importlib.import_module('rknnlite.api')
        manifest['backend'] = 'rknnlite'
        events = old.Journal(directory / 'events.jsonl')
        http_journal = old.Journal(directory / 'http.jsonl')
        monitor = old.ResourceMonitor(AppSampler(runtime_sampler(runtime), gui), directory / 'resources.jsonl')
        monitor.start()
        sdk = old.SDKLifecycleMonitor()
        sdk.__enter__()

        def factory(runtime_args, output_callback, event_callback):
            runtime_args.duration = args.duration
            runtime_args.jsonl = str(directory / 'frames.jsonl')
            runtime_args.session_json = str(directory / 'session.json')
            runtime_args.save_dir = str(directory / 'snapshots')
            runtime_args.save_interval = 30.0
            owned['source'] = old.RecordingSource(runtime.make_source(runtime_args.source, replay_fps=runtime_args.replay_fps))
            delegate = owned['source'].source
            if not getattr(delegate, 'is_file', False) or getattr(delegate, 'pacer', None) is None:
                raise RuntimeError('native evidence requires a real looping file VideoSource')
            import cv2
            manifest['actual_source'] = {
                'kind': type(delegate).__name__, 'is_file': delegate.is_file,
                'header_fps': delegate.cap.get(cv2.CAP_PROP_FPS),
                'header_frame_count': delegate.cap.get(cv2.CAP_PROP_FRAME_COUNT),
                'decoded_image_size': list(delegate.prefetched.shape[1::-1]),
                'requested_replay_fps': runtime_args.replay_fps}
            owned['engine'] = old.RecordingEngine(runtime.RKNNEngine(runtime_args.model, 416, 416))

            def on_packet(packet):
                monitor.check()
                output_callback(packet)

            def on_event(event):
                events.write(event)
                event_callback(event)
            owned['pipeline'] = runtime.Pipeline(runtime_args, engine=owned['engine'], source=owned['source'],
                                                  output_callback=on_packet, event_callback=on_event)
            return owned['pipeline']

        controller = app.DetectionController(args.app_root, pipeline_factory=factory)
        wifi_module = importlib.import_module(app.__package__ + '.wifi')
        wifi_temp = directory / 'wifi-control'
        wifi_temp.mkdir(mode=0o700)
        wifi = wifi_module.WifiBackend(ctrl_factory=lambda: wifi_module.WpaControl(
            '/var/run/wpa_supplicant/wlan0', temp_root=str(wifi_temp)))
        service = app.LocalService(controller, wifi, token=secrets.token_urlsafe(32))
        service.start()
        probe = HttpProbe(service, directory, http_journal)
        config = probe.request('/api/config')
        wifi = probe.request('/api/wifi/status')
        manifest['wifi_status_read'] = {'status': wifi.get('status'), 'failure': wifi.get('failure')}
        if config.get('status') != 200:
            raise RuntimeError('HTTP configuration unavailable')
        if args.gui:
            launch_gui(app, directory, service, args.duration, gui)
        start_receipt = probe.request('/api/start', {'model': args.model, 'source': args.source, 'profile': args.profile})
        manifest['app_generation'] = controller.run_uuid
        manifest['http_start_monotonic'] = start_receipt['started_monotonic']
        manifest['gui'] = {k: v for k, v in gui.items() if k not in ('process', 'log_thread', 'ready')}
        old.write_json(directory / 'manifest.json', manifest)
        if start_receipt.get('status') != 200 or not start_receipt.get('body', {}).get('started'):
            raise RuntimeError('HTTP start failed')
        if args.gui:
            old.write_json(directory / 'gui-control.json', {'phase': 'running',
                           'capture_at_wall_ms': time.time() * 1000 + max(100, args.duration * 1000 - 1000)})
        next_probe = time.monotonic() + min(.5, args.duration / 2)
        observation = 0
        while controller.worker is not None and controller.worker.is_alive() and not interrupted.is_set():
            monitor.check()
            child = gui.get('process')
            if child is not None and child.poll() is not None:
                raise RuntimeError('Qt exited during active detection')
            if time.monotonic() >= next_probe:
                probe.observe(observation)
                observation += 1
                next_probe += PROBE_INTERVAL
            interrupted.wait(.05)
        manifest['active_worker_finished_monotonic'] = time.monotonic()
        stop = probe.request('/api/stop', {}, phase='stop')
        probe.observe(observation, phase='stopped')
        if stop.get('status') != 200 or stop.get('body', {}).get('stopped') is not True:
            raise RuntimeError('HTTP stop did not stop owned workers')
        if controller.error:
            raise RuntimeError('app detection worker reported failure')
        code = 0
    except BaseException as error:
        manifest['error'] = type(error).__name__
        with (directory / 'error.txt').open('x', encoding='utf-8') as stream:
            stream.write('Native evidence failure: ' + type(error).__name__ + '\n')
    finally:
        if service is not None:
            try:
                manifest['service_cleanup'] = service.close()
                manifest['service_cleanup']['http_worker_alive'] = bool(service.thread and service.thread.is_alive())
                manifest['service_cleanup']['app_worker_alive'] = bool(controller.worker and controller.worker.is_alive())
            except BaseException as error:
                manifest.setdefault('cleanup_errors', []).append('service close: ' + type(error).__name__)
        if owned['pipeline'] is None:
            for resource in (owned['source'], owned['engine']):
                if resource is not None:
                    try:
                        resource.release()
                    except BaseException as error:
                        manifest.setdefault('cleanup_errors', []).append('partial startup release: ' + type(error).__name__)
        try:
            close_gui(gui, directory)
        except BaseException as error:
            manifest.setdefault('cleanup_errors', []).append('GUI cleanup: ' + type(error).__name__)
        try:
            clean_private(directory)
        except BaseException as error:
            manifest.setdefault('cleanup_errors', []).append('private wrapper removal: ' + type(error).__name__)
        if signal_number is not None and events is not None:
            try:
                events.write({'type': 'signal', 'signal': signal_number,
                              'monotonic': time.monotonic(),
                              'received_monotonic': manifest['signal_received_monotonic']})
            except BaseException as error:
                manifest.setdefault('cleanup_errors', []).append('signal receipt: ' + type(error).__name__)
        for resource in (monitor, events, http_journal):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    manifest.setdefault('cleanup_errors', []).append(type(error).__name__)
        old.collect_cleanup(manifest, owned['pipeline'], owned['engine'], owned['source'], sdk.records if sdk else [])
        if sdk is not None and hasattr(sdk, 'original'):
            sdk.__exit__(None, None, None)
        manifest['governors'] = finish_governors(manifest['governors'])
        manifest['gui'] = {k: v for k, v in gui.items() if k not in ('process', 'log_thread', 'ready')}
        engine, source = owned['engine'], owned['source']
        manifest.update(actual_engine_calls=getattr(engine, 'actual_engine_calls', 0),
                        actual_source_reads=getattr(source, 'actual_source_reads', 0),
                        actual_source_read_failures=getattr(source, 'actual_source_read_failures', 0),
                        signal=signal_number, finished_wall_time=old.utc_now(), elapsed_seconds=time.monotonic() - started)
        if signal_number is not None:
            code = 128 + signal_number
        if manifest.get('cleanup_errors'):
            code = 1
        service_cleanup = manifest.get('service_cleanup', {})
        if any(service_cleanup.get(k) is not value for k, value in
               [('detection_stopped', True), ('wifi_stopped', True), ('http_worker_alive', False), ('app_worker_alive', False)]):
            code = 1
        if args.gui and (not gui.get('exited') or gui.get('returncode') != 0 or not gui.get('screen_saved')
                         or gui.get('forced_termination') or gui.get('log_thread_alive') or gui.get('log_failure')):
            code = 1
        manifest['exit_code'] = code
        manifest['status'] = 'completed' if code == 0 else 'failed'
        old.write_json(directory / 'cleanup.json', {'cleanup': manifest['cleanup'],
                       'cleanup_errors': manifest.get('cleanup_errors', []),
                       'service': manifest.get('service_cleanup'), 'gui': manifest['gui']})
        manifest['artifacts'] = {p.relative_to(directory).as_posix(): old.file_record(p)
                                 for p in directory.rglob('*') if p.is_file() and p.name != 'manifest.json'
                                 and 'private' not in p.relative_to(directory).parts}
        if signal_number is not None:
            code = 128 + signal_number
            manifest.update(signal=signal_number, status='failed', exit_code=code)
        old.write_json(directory / 'manifest.json', manifest)
        result = verify_evidence(directory)
        if signal_number is not None:
            code = 128 + signal_number
            manifest.update(signal=signal_number, status='failed', exit_code=code)
        interval = result.get('timeline', {})
        manifest['intervals'] = {'initialization_seconds': interval.get('active_start', started) - started,
                                'active_seconds': interval.get('observed_active_seconds'),
                                'finalization_seconds': started + manifest['elapsed_seconds'] - interval.get('active_stop', started)}
        if code == 0 and not result['passed']:
            code = 1
            manifest.update(status='failed', exit_code=code)
            old.write_json(directory / 'manifest.json', manifest)
        old.write_json(directory / 'manifest.json', manifest)
        old.write_json(directory / 'run-verification.json', result)
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
    print(json.dumps({'run_dir': str(directory), 'status': manifest['status'], 'exit_code': code}))
    return code


def artifact_path(directory, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('invalid artifact path')
    path = (directory / name).resolve()
    if not inside(path, directory) or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('artifact path escapes run directory')
    return path


def decoded_image(path):
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('saved image does not decode')
    return [image.shape[1], image.shape[0]]


def validate_http(manifest, rows, outputs, directory, timeline=None):
    errors, decoded, probes = [], 0, []
    generation = manifest.get('app_generation')
    if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{32}', generation):
        errors.append('App run generation missing/invalid')
    output_by_id = {r.get('frame_id'): r for r in outputs}
    starts = [r for r in rows if r.get('route') == '/api/start' and r.get('method') == 'POST']
    stops = [r for r in rows if r.get('route') == '/api/stop' and r.get('method') == 'POST']
    if len(starts) != 1 or starts[0].get('status') != 200 or starts[0].get('body', {}).get('started') is not True:
        errors.append('HTTP POST start receipt missing/failed')
    if len(stops) != 1 or stops[0].get('status') != 200 or stops[0].get('body', {}).get('stopped') is not True:
        errors.append('HTTP POST stop receipt missing/failed')
    if not any(r.get('route') == '/api/config' and r.get('status') == 200 for r in rows):
        errors.append('HTTP configuration receipt missing')
    wifi_receipts = [r for r in rows if r.get('route') == '/api/wifi/status']
    if not wifi_receipts:
        errors.append('read-only WiFi status receipt missing')
    observations = {}
    for row in rows:
        start, end, latency = (row.get(k) for k in ('started_monotonic', 'finished_monotonic', 'latency_ms'))
        if not all(old.is_number(v) for v in (start, end, latency)) or end < start or abs(latency - (end - start) * 1000) > .02:
            errors.append('HTTP observation timing invalid')
        if row.get('failure'):
            errors.append('HTTP observation failure: ' + str(row.get('route')))
        if row.get('route') not in ('/api/preview', '/api/wifi/status') and row.get('status') != 200:
            errors.append('HTTP route failed: ' + str(row.get('route')))
        try:
            raw = row['raw_response'].encode('utf-8')
            if hashlib.sha256(raw).hexdigest() != row.get('response_sha256') or json.loads(raw) != row.get('body'):
                errors.append('HTTP original response hash/body mismatch')
        except (KeyError, ValueError, TypeError, AttributeError):
            if not row.get('failure'):
                errors.append('HTTP original response bytes missing/malformed')
        if row.get('route') in ('/api/wifi/scan', '/api/wifi/connect'):
            errors.append('WiFi mutation/scan is outside evidence scope')
        key = row.get('observation')
        if key is not None:
            observations.setdefault((row.get('phase'), key), []).append(row)
    for (phase, key), group in observations.items():
        if [r.get('route') for r in group] != ['/api/status', '/api/preview', '/api/status']:
            errors.append('incomplete HTTP status/preview/status observation')
            continue
        before, preview, after = group
        if phase == 'active':
            probes.append(before.get('started_monotonic'))
        statuses = [r.get('body', {}).get('detection', {}) for r in (before, after)]
        body = preview.get('body', {})
        if preview.get('status') == 503:
            published = any(old.is_number(r.get('callback_finished_at')) and
                            r['callback_finished_at'] <= preview.get('started_monotonic', -math.inf)
                            for r in outputs)
            if phase == 'active' and published and all(s.get('valid') and s.get('running') for s in statuses):
                errors.append('fresh healthy HTTP status has no preview')
            continue
        if preview.get('status') != 200:
            errors.append('HTTP preview failed with unexpected status')
            continue
        try:
            if phase != 'active':
                errors.append('preview remained available after stop')
            if body.get('valid') is not True or body.get('generation') != generation:
                errors.append('preview validity/run generation mismatch')
            if any(s.get('generation') != generation or s.get('source_epoch') != body.get('source_epoch')
                   or not s.get('valid') or not s.get('running') for s in statuses):
                errors.append('preview/status source epoch/generation mismatch')
            captured, age, remaining = (body.get(k) for k in ('captured_at', 'source_age_ms', 'remaining_ttl_ms'))
            if not all(old.is_number(v) for v in (captured, age, remaining)) or age < 0 or remaining <= 0 or abs(age + remaining - 1000) > .02:
                errors.append('preview TTL metadata inconsistent/expired')
            elif not preview['started_monotonic'] - .001 <= captured + age / 1000 <= preview['finished_monotonic'] + .001:
                errors.append('preview capture/age not bound to HTTP observation')
            if old.is_number(captured) and old.is_number(preview.get('finished_monotonic')) and preview['finished_monotonic'] - captured > 1:
                errors.append('preview expired before HTTP receipt completed')
            if old.is_number(remaining) and old.is_number(preview.get('latency_ms')) and remaining - preview['latency_ms'] <= 0:
                errors.append('preview QML conservative remaining TTL budget exhausted')
            output = output_by_id.get(body.get('frame_id'))
            if output is None or output.get('captured_at') != captured or output.get('source_epoch') != body.get('source_epoch'):
                errors.append('HTTP preview frame/capture/source binding mismatch')
            record = preview.get('jpeg', {})
            path = artifact_path(directory, record.get('artifact'))
            raw = base64.b64decode(body['jpeg_base64'], validate=True)
            if path.read_bytes() != raw or old.file_record(path)['sha256'] != record.get('sha256'):
                errors.append('HTTP JPEG payload/hash mismatch')
            if decoded_image(path) != record.get('decoded_size'):
                errors.append('HTTP JPEG decoded dimensions mismatch')
            decoded += 1
        except (KeyError, ValueError, OSError, TypeError):
            errors.append('HTTP JPEG/preview evidence malformed')
    if not decoded:
        errors.append('no decoded fresh HTTP preview evidence')
    if any(not old.is_number(p) for p in probes) or any(b - a < PROBE_INTERVAL - .02 or b - a > PROBE_INTERVAL + 3 for a, b in zip(probes, probes[1:])):
        errors.append('HTTP observation cadence violates five-second policy')
    coverage = {'first': probes[0] if probes else None, 'last': probes[-1] if probes else None,
                'count': len(probes), 'allowed_gap_seconds': PROBE_INTERVAL + 3}
    if timeline is not None:
        start, stop = timeline.get('active_start'), timeline.get('active_stop')
        if not all(old.is_number(v) for v in (start, stop)) or stop <= start:
            errors.append('HTTP active timeline coverage cannot be established')
        else:
            minimum_count = max(1, math.floor(max(0, stop - start - 3) / PROBE_INTERVAL))
            coverage.update(active_start=start, active_stop=stop, minimum_count=minimum_count)
            if len(probes) < minimum_count:
                errors.append('HTTP observation count does not cover active interval')
            if not probes or not all(old.is_number(v) for v in probes) or probes[0] > start + 3 or probes[-1] < stop - (PROBE_INTERVAL + 3):
                errors.append('HTTP observation start/end coverage missing')
            elif any(p < start or p > stop for p in probes):
                errors.append('HTTP active observations lie outside the actual active interval')
    return {'decoded_previews': decoded, 'active_observations': len(probes),
            'wifi_status_available': bool(wifi_receipts) and all(r.get('status') == 200 and not r.get('body', {}).get('error') for r in wifi_receipts),
            'wifi_status_receipts': [{'status': r.get('status'), 'failure': r.get('failure')} for r in wifi_receipts],
            'coverage': coverage,
            'latency_ms': old.percentiles([r['latency_ms'] for r in rows if old.is_number(r.get('latency_ms'))]),
            'errors': list(dict.fromkeys(errors))}


def verify_evidence(run_dir):
    try:
        result = _verify_evidence(run_dir)
    except (AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError, ZeroDivisionError) as error:
        result = {'passed': False, 'errors': ['malformed evidence structure: ' + type(error).__name__],
                  'native_gui_verified': False, 'thirty_minute_app_passed': False}

    def finite_json(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: finite_json(item) for key, item in value.items()}
        if isinstance(value, list):
            return [finite_json(item) for item in value]
        return value
    return finite_json(result)


def _verify_evidence(run_dir):
    directory = Path(run_dir).resolve()
    errors = []
    try:
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if not isinstance(manifest, dict):
            raise ValueError('manifest must be object')
    except (OSError, ValueError):
        return {'passed': False, 'errors': ['manifest missing/malformed'], 'thirty_minute_app_passed': False}
    for name in REQUIRED:
        if name not in manifest.get('artifacts', {}):
            errors.append('unhashed required artifact: ' + name)
    actual_inventory = {}
    for name, record in manifest.get('artifacts', {}).items():
        try:
            path = artifact_path(directory, name)
            actual = old.file_record(path)
            actual_inventory[name] = actual
            if actual['sha256'] != record.get('sha256') or actual['bytes'] != record.get('bytes'):
                errors.append('artifact hash/size mismatch: ' + name)
        except (OSError, ValueError, TypeError):
            errors.append('missing/unsafe artifact: ' + name)
    for path in directory.rglob('*'):
        if path.is_file() and path.name not in ('manifest.json', 'run-verification.json') and path.relative_to(directory).as_posix() not in manifest.get('artifacts', {}):
            errors.append('unhashed raw artifact: ' + path.relative_to(directory).as_posix())
    provenance = manifest.get('provenance_copies', {})
    required_provenance = ['model', 'input', *['source:' + name for name in manifest.get('sources', {})]]
    for key in required_provenance:
        try:
            path = artifact_path(directory, provenance[key])
            expected = manifest[key] if key in ('model', 'input') else manifest['sources'][key.split(':', 1)[1]]
            actual = old.file_record(path)
            if provenance[key] not in manifest.get('artifacts', {}) or actual['sha256'] != expected.get('sha256') or actual['bytes'] != expected.get('bytes'):
                errors.append('actual provenance hash/bytes mismatch: ' + key)
        except (OSError, ValueError, KeyError, TypeError):
            errors.append('actual provenance copy missing/invalid: ' + key)
    def rows(name):
        try:
            records = old.read_jsonl(artifact_path(directory, name))
            if not all(isinstance(r, dict) for r in records):
                raise ValueError('log rows must be objects')
            return records
        except (OSError, ValueError, TypeError):
            errors.append('missing/malformed log: ' + name)
            return []
    frames, resources, events, http_rows = (rows(n) for n in ('frames.jsonl', 'resources.jsonl', 'events.jsonl', 'http.jsonl'))
    sidecar = rows('frames.jsonl.resources.jsonl')
    try:
        session = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        errors.append('session missing/malformed')
        session = {}
    try:
        basic = old.validate_longrun(manifest, session, frames, resources, events)
    except (ValueError, KeyError, TypeError, ZeroDivisionError) as error:
        basic = {'passed': False, 'errors': ['malformed runtime evidence: ' + type(error).__name__],
                 'observed_rates': {}, 'metrics': {}, 'timeline': {}}
    errors.extend(basic['errors'])
    if manifest.get('kind') != 'native-app-longrun' or manifest.get('status') != 'completed':
        errors.append('native run did not complete')
    if manifest.get('profile') != 'responsive' or manifest.get('replay_fps') != 20 or manifest.get('public_target_fps') != PUBLIC_TARGET_FPS:
        errors.append('native public target/profile differs from prespecified configuration')
    if manifest.get('thresholds') != {'conf': .35, 'nms': .45, 'skip': 0} or manifest.get('result_ttl') != 1:
        errors.append('native operation point changed')
    actual_source = manifest.get('actual_source', {})
    if (actual_source.get('kind') != 'VideoSource' or actual_source.get('is_file') is not True
            or actual_source.get('requested_replay_fps') != 20):
        errors.append('actual paced decoded video source evidence missing/invalid')
    if manifest.get('signal') is not None:
        errors.append('run was interrupted by a signal')
    for name in ('app/service.py', 'app/wifi.py', 'app/qml/Main.qml', 'output_sinks.py', 'verify_native_runtime.py'):
        record = manifest.get('sources', {}).get(name, {})
        if not re.fullmatch('[0-9a-f]{64}', str(record.get('sha256', ''))):
            errors.append('native called-source hash missing: ' + name)
    retention = session.get('runtime_sample_retention', {})
    if not sidecar or retention.get('total_count') != len(sidecar) or retention.get('retained_count') != len(session.get('runtime_samples') or []):
        errors.append('complete Pipeline resource sidecar inventory missing/inconsistent')
    elif session.get('runtime_samples') != sidecar[-retention['retained_count']:]:
        errors.append('Pipeline retained resources disagree with complete sidecar')
    # Recompute source/SDK invocation inventory, not cached manifest validation.
    counters = session.get('counters', {})
    if manifest.get('actual_source_read_failures') != counters.get('read_failure'):
        errors.append('real source read-failure inventory mismatch')
    if not old.is_number(manifest.get('actual_source_reads')) or manifest['actual_source_reads'] < counters.get('captured', math.inf):
        errors.append('real source reads smaller than capture inventory')
    outputs = [r for r in frames if r.get('record_type') == 'output']
    http = validate_http(manifest, http_rows, outputs, directory, basic.get('timeline', {}))
    errors.extend(http['errors'])
    snapshots = validate_snapshots(manifest, events, outputs, directory)
    errors.extend(snapshots['errors'])
    cleanup = manifest.get('service_cleanup', {})
    for name, expected in [('detection_stopped', True), ('wifi_stopped', True), ('http_worker_alive', False), ('app_worker_alive', False)]:
        if cleanup.get(name) is not expected:
            errors.append('Service worker cleanup missing/failed: ' + name)
    try:
        receipt = json.loads((directory / 'cleanup.json').read_text(encoding='utf-8'))
        if (receipt.get('cleanup') != manifest.get('cleanup') or receipt.get('service') != cleanup
                or receipt.get('gui') != manifest.get('gui') or receipt.get('cleanup_errors') != manifest.get('cleanup_errors', [])):
            errors.append('direct cleanup receipt disagrees with manifest')
    except (ValueError, OSError):
        errors.append('direct cleanup receipt missing/malformed')
    gui = manifest.get('gui', {})
    gui_ok = False
    if gui.get('requested') is True:
        if not gui.get('started') or gui.get('exited') is not True or gui.get('returncode') != 0 or gui.get('forced_termination') or gui.get('kill_required') or gui.get('log_thread_alive') or gui.get('log_failure'):
            errors.append('GUI start/normal exit receipt missing/failed')
        child_rows = [r.get('gui') for r in resources if isinstance(r.get('gui'), dict)]
        if any(not old.is_number(r['gui'].get('monotonic')) or not old.is_number(r.get('monotonic'))
               or not 0 <= r['gui']['monotonic'] - r['monotonic'] <= 1
               for r in resources if isinstance(r.get('gui'), dict)):
            errors.append('GUI resource clock is nonfinite or inconsistent with parent sample')
        timeline = basic.get('timeline', {})
        active_start, active_stop = timeline.get('active_start'), timeline.get('active_stop')
        ready, exited = gui.get('ready_monotonic'), gui.get('exited_monotonic')
        if not all(old.is_number(v) for v in (ready, active_start, active_stop, exited)) or not ready <= active_start < active_stop <= exited:
            errors.append('GUI did not cover the complete observed Pipeline active interval')
        rendered_at = gui.get('render_received_monotonic')
        if not old.is_number(rendered_at) or not old.is_number(active_stop) or not active_stop - 5 <= rendered_at <= active_stop:
            errors.append('GUI rendered screen was not observed in final five active seconds')
        if len(child_rows) < 2 or any(r.get('pid') != gui.get('pid') or not r.get('start_id')
            or not old.resource_value_valid(r.get('rss_bytes'), 'rss_bytes')
            or not old.resource_value_valid(r.get('cpu_percent'), 'cpu_percent') for r in child_rows):
            errors.append('GUI child CPU/RSS resource evidence missing/invalid')
        elif len({r['start_id'] for r in child_rows}) != 1 or any(not old.is_number(r.get('monotonic')) for r in child_rows) or any(not 0 <= b['monotonic'] - a['monotonic'] <= 3 for a, b in zip(child_rows, child_rows[1:])):
            errors.append('GUI resource identity/coverage inconsistent')
        if child_rows and (child_rows[0]['monotonic'] > gui.get('started_monotonic', -math.inf) + 3
                           or child_rows[-1]['monotonic'] < manifest.get('active_worker_finished_monotonic', math.inf) - 3):
            errors.append('GUI resource active start/end coverage missing')
        try:
            if 'screen.png' not in manifest.get('artifacts', {}) or 'gui.log' not in manifest.get('artifacts', {}):
                raise ValueError('unhashed GUI artifacts')
            screen_size = decoded_image(directory / 'screen.png')
            log = (directory / 'gui.log').read_text(encoding='utf-8')
            render_lines = [line.split('NATIVE_RENDER', 1)[1].strip() for line in log.splitlines() if 'NATIVE_RENDER' in line]
            rendered = json.loads(render_lines[-1])
            if rendered.get('saved') is not True or rendered.get('valid') is not True or rendered.get('generation') != manifest.get('app_generation') or rendered.get('frameId') not in {r.get('frame_id') for r in outputs}:
                raise ValueError('Qt screenshot lacks fresh render identity')
            surface_size = [rendered.get('surfaceWidth'), rendered.get('surfaceHeight')]
            if any(not old.is_number(v) or v <= 0 for v in surface_size) or screen_size != [round(v) for v in surface_size]:
                raise ValueError('Qt PNG dimensions disagree with actual surface')
            if rendered.get('previewVisible') is not True or rendered.get('previewReady') is not True or rendered.get('previewSourceKind') != 'inline-jpeg':
                raise ValueError('Qt did not render a visible ready JPEG preview')
            source_size = rendered.get('previewSourceSize')
            if not isinstance(source_size, list) or len(source_size) != 2 or any(not old.is_number(v) or v <= 0 for v in source_size):
                raise ValueError('Qt preview source has no decoded dimensions')
            if rendered.get('uiSelection') != {'model': 'helmet_416x416.rknn',
                                               'source': 'construction-replay.avi', 'profile': 'responsive'}:
                raise ValueError('Qt dropdown configuration differs from actual HTTP selection')
            if 'NATIVE_EXIT pipeline-stopped' not in log or 'NATIVE_EXIT watchdog' in log:
                raise ValueError('Qt exit did not follow Pipeline stop')
        except (OSError, ValueError, IndexError):
            errors.append('GUI actual rendered screen evidence missing/invalid')
        gui_ok = not any('GUI' in error for error in errors)
    elif gui.get('requested') is not False or gui.get('started') is not False:
        errors.append('GUI absence must be explicit')
    rates = basic.get('observed_rates', {})
    target_met = all(old.is_number(rates.get(name)) and rates[name] >= PUBLIC_TARGET_FPS for name in ('detected_fps', 'output_fps'))
    passed = not errors
    rss = [r['total_rss_bytes'] for r in resources if old.is_number(r.get('total_rss_bytes'))]
    result = {**basic, 'passed': passed, 'errors': list(dict.fromkeys(errors)),
              'public_target_fps': PUBLIC_TARGET_FPS, 'requested_replay_fps': manifest.get('replay_fps'),
              'requested_supply_target_met': basic.get('throughput_target_met', False),
              'throughput_target_met': target_met, 'http': http, 'native_gui_verified': gui_ok,
              'thirty_minute_app_passed': passed and gui_ok and old.is_number(manifest.get('requested_duration_seconds')) and manifest['requested_duration_seconds'] >= 1800,
              'artifact_inventory': actual_inventory, 'raw_counts': {'frame_rows': len(frames), 'resource_rows': len(resources), 'pipeline_resource_rows': len(sidecar), 'event_rows': len(events), 'http_rows': len(http_rows)},
              'app_resources': {'rss_start_bytes': rss[0] if rss else None, 'rss_end_bytes': rss[-1] if rss else None,
                                'rss_peak_bytes': max(rss) if rss else None, 'rss_growth_bytes': rss[-1] - rss[0] if rss else None},
              'scope': 'continuity, original-frame inventory, freshness and automatic native render; fixed 10 FPS is separate; no physical camera, human display/touch, WiFi switching or independent 85% accuracy acceptance',
              'evidence_configuration': manifest.get('evidence_configuration'),
              'snapshot_integrity': snapshots, 'actual_source': actual_source,
              'display_evidence': {'qml_preview_request_hz': 5, 'qml_status_poll_hz': 2,
                 'actual_display_fps': None, 'screen_status_frame_is_exact_preview_identity': False},
              'verifier': old.file_record(__file__), 'shared_verifier': old.file_record(HELPER_PATH)}
    return result


def validate_snapshots(manifest, events, outputs, directory):
    errors, saved = [], []
    by_id = {r.get('frame_id'): r for r in outputs}
    artifacts = manifest.get('artifacts', {})
    for event in events:
        if event.get('type') != 'snapshot':
            continue
        try:
            image_name = 'snapshots/' + Path(event['snapshot']).name
            metadata_name = 'snapshots/' + Path(event['metadata']).name
            if image_name not in artifacts or metadata_name not in artifacts:
                raise ValueError('snapshot pair is not hashed')
            image = artifact_path(directory, image_name)
            metadata = json.loads(artifact_path(directory, metadata_name).read_text(encoding='utf-8'))
            output = by_id.get(metadata.get('frame_id'))
            if output is None or event.get('frame_id') != metadata.get('frame_id'):
                raise ValueError('snapshot frame not in output inventory')
            if any(output.get(k) != metadata.get(k) for k in ('captured_at', 'source_epoch', 'model_sha256', 'classes', 'scores', 'boxes')):
                raise ValueError('snapshot metadata lost original frame binding')
            captured, saved_at, age = (metadata.get(k) for k in ('captured_at', 'saved_at', 'snapshot_age_ms'))
            if (not all(old.is_number(v) for v in (captured, saved_at, age)) or metadata.get('valid') is not True
                    or not 0 <= saved_at - captured <= 1 or abs(age - (saved_at - captured) * 1000) > .02):
                raise ValueError('snapshot expired or has inconsistent capture time')
            if decoded_image(image) != metadata.get('image_size'):
                raise ValueError('snapshot decoded dimensions mismatch')
            saved.append({'image': image_name, 'metadata': metadata_name, 'saved_at': saved_at,
                          'reason': event.get('reason'), 'frame_id': event.get('frame_id')})
        except (KeyError, TypeError, ValueError, OSError):
            errors.append('annotated snapshot pair/inventory/freshness invalid')
    referenced = {r[k] for r in saved for k in ('image', 'metadata')}
    if any(name.startswith('snapshots/') and name not in referenced for name in artifacts):
        errors.append('snapshot artifact has no matching valid event receipt')
    periodic = [r['saved_at'] for r in saved if r['reason'] == 'periodic']
    starts = [e.get('monotonic') for e in events if e.get('state') == 'startup']
    stops = [e.get('monotonic') for e in events if e.get('state') == 'stop']
    if len(starts) == len(stops) == 1 and all(old.is_number(v) for v in (starts[0], stops[0])):
        duration = stops[0] - starts[0]
        if duration >= 31:
            if not periodic or periodic[0] > starts[0] + 31 or periodic[-1] < stops[0] - 31:
                errors.append('periodic annotated snapshot start/end coverage missing')
            if any(b - a > 31 for a, b in zip(periodic, periodic[1:])):
                errors.append('periodic annotated snapshot cadence exceeded 30-second interval allowance')
    return {'pairs': saved, 'periodic_count': len(periodic), 'errors': errors}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='real native app; all new evidence below output root')
    run.add_argument('--app-root', required=True)
    run.add_argument('--output-root', required=True)
    run.add_argument('--run-id', required=True)
    run.add_argument('--duration', type=float, default=1800)
    run.add_argument('--model', default='helmet_416x416.rknn')
    run.add_argument('--source', default='construction-replay.avi')
    run.add_argument('--profile', choices=('responsive',), default='responsive')
    run.add_argument('--gui', action='store_true')
    verify = commands.add_parser('verify', help='derived evidence; no RKNN/Qt import')
    verify.add_argument('run_dir')
    verify.add_argument('--out', required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    validate_args(args)
    if args.command == 'run':
        return run_native(args)
    output = Path(args.out).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError('derived output already exists')
    result = verify_evidence(args.run_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(json.dumps({'passed': result['passed'], 'out': str(output), 'errors': result['errors']}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print('Native verification error: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
