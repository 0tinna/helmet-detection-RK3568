#!/usr/bin/env python3
"""Observe the frozen normal vendor launcher, using UI input and read-only APIs.

probe/run require Linux and a new output below /userdata/helmet-validation.
verify is read-only and works on copied evidence on the host. No API start,
network writes, SDK injection, process kill, or production modification exists.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


_keyboard_spec = importlib.util.spec_from_file_location('normal_native_keyboard',
    Path(__file__).with_name('native_keyboard.py'))
native_keyboard = importlib.util.module_from_spec(_keyboard_spec)
_keyboard_spec.loader.exec_module(native_keyboard)
APP_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v2')
OUTPUT_ROOT = Path('/userdata/helmet-validation')
WRAPPER = Path('/opt/ui/src/apps/helmet_app')
NPU_ROOT = Path('/sys/class/devfreq/fde40000.npu')
SELECTION = {'model': 'helmet_416x416.rknn', 'source': 'construction-replay.avi',
             'profile': 'responsive'}
ROLES = ('launcher', 'service', 'ui')
STOP_REQUESTED = False
RAW_FILES = frozenset(('http.jsonl', 'resources.jsonl', 'input.jsonl', 'terminal.json',
                      'ready.json', 'processes.json', 'exit.json', 'launcher.log',
                      'service.log', 'ui.log', 'screen.png'))
# The manifest supplies the inventory; verification.json is a recomputed result.
# Both are explicitly excluded from the raw digest set, never optional raw data.
INVENTORY_EXCLUSIONS = frozenset(('manifest.json', 'verification.json'))


def require(condition, message, error=ValueError):
    if not condition:
        raise error(message)


def finite(value):
    require(type(value) in (int, float) and math.isfinite(value), 'non-finite numeric evidence')
    return value


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'),
                      parse_constant=lambda value: float(value))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
                          encoding='utf-8')


def append_json(path, value):
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')


def safe_id(run_id):
    require(isinstance(run_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', run_id),
            'run-id must be 1..64 ASCII letters/digits/underscore/hyphen with alphanumeric first')
    return run_id


def create_output(run_id, base=OUTPUT_ROOT):
    safe_id(run_id)
    base = Path(base)
    require(base.is_dir() and base.resolve() == base.absolute(), 'output base must be a real directory')
    output = base / run_id
    require(output.resolve().parent == base.resolve(), 'output escaped validation root')
    output.mkdir(mode=0o700, exist_ok=False)
    return output


def detection(row):
    require(row.get('status') == 200 and row.get('method') == 'GET'
            and row.get('route') == '/api/status', 'successful status boundary is required')
    require(isinstance(row.get('body'), dict), 'status response body is not a JSON object')
    value = row['body'].get('detection')
    require(isinstance(value, dict), 'missing detection status')
    return value


def valid_running(value):
    require(value.get('running') is True and value.get('valid') is True
            and value.get('state') in ('healthy', 'no-detection'), 'detection is not fresh and running')
    require(value.get('selection') == SELECTION, 'running selection does not match fixed configuration')
    require(isinstance(value.get('generation'), str) and value['generation'], 'missing run generation')
    require(type(value.get('source_epoch')) is int and value['source_epoch'] >= 0, 'missing source epoch')
    counters = value.get('counters')
    require(isinstance(counters, dict) and {'detected', 'output', 'captured'} <= counters.keys(),
            'incomplete detection counters')
    for count in counters.values():
        require(type(count) is int and count >= 0, 'counter must be a nonnegative integer')
    reject_runtime_failures(value, require_complete=True)


def reject_runtime_failures(value, require_complete=False):
    counters = value.get('counters')
    if counters is None and not require_complete:
        # The normal worker may not have initialized telemetry while starting.
        return
    require(isinstance(counters, dict), 'failure counters are unavailable')
    for name in ('read_failure', 'detection_failure'):
        require(type(counters.get(name)) is int and counters[name] == 0,
                'source/inference failure counter is missing or nonzero: ' + name)


def validate_interval(first, last):
    first_start, first_end = finite(first.get('request_started')), finite(first.get('response_finished'))
    last_start, last_end = finite(last.get('request_started')), finite(last.get('response_finished'))
    require(0 <= first_start <= first_end < last_start <= last_end, 'invalid or overlapping HTTP interval')
    before, after = detection(first), detection(last)
    valid_running(before)
    valid_running(after)
    for field in ('generation', 'source_epoch', 'selection'):
        require(before[field] == after[field], field + ' changed during observation')
    require(before['counters'].keys() == after['counters'].keys(), 'counter schema changed')
    for name, count in before['counters'].items():
        require(after['counters'][name] >= count, 'counter regressed: ' + name)
    lower, upper = last_start - first_end, last_end - first_start
    deltas = {name: after['counters'][name] - before['counters'][name]
              for name in ('detected', 'output', 'captured')}
    return {'duration_lower_seconds': lower, 'duration_upper_seconds': upper,
            'counter_deltas': deltas, 'conservative_fps': {name: count / upper for name, count in deltas.items()},
            'thirty_minutes_observed': lower >= 1800,
            'counter_scope': 'complete status counters; not a per-frame inventory or pending closure'}


def parse_proc_stat(text):
    closing = text.rfind(')')
    require(closing > 0, 'invalid proc stat')
    pid = int(text[:text.index('(')].strip())
    fields = text[closing + 1:].split()
    require(len(fields) >= 22, 'truncated proc stat')
    return {'pid': pid, 'state': fields[0], 'ppid': int(fields[1]),
            'cpu_ticks': int(fields[11]) + int(fields[12]), 'threads': int(fields[17]),
            'starttime': int(fields[19]), 'rss_pages': int(fields[21])}


def proc_read(pid):
    try:
        root = Path('/proc') / str(pid)
        row = parse_proc_stat((root / 'stat').read_text())
        row['argv'] = (root / 'cmdline').read_bytes().decode('utf-8').rstrip('\0').split('\0')
        row['rss_bytes'] = row['rss_pages'] * os.sysconf('SC_PAGE_SIZE')
        row['fds'] = len(list((root / 'fd').iterdir()))
        return row
    except FileNotFoundError:
        return None


def validate_live_identities(identities, reader=proc_read):
    require(set(identities) == set(ROLES), 'missing owned process identity', RuntimeError)
    for role, identity in identities.items():
        current = reader(identity['pid'])
        require(current is not None and current['pid'] == identity['pid']
                and current['starttime'] == identity['starttime']
                and current['state'] not in ('Z', 'X', 'x'), role + ' PID/starttime is no longer live', RuntimeError)


def validate_terminal(record):
    require(record.get('wrapper_exit') == 0, 'wrapper did not exit successfully')
    require(record.get('exit_receipt') == {'service_exit': 0, 'ui_exit': 0, 'cleanup_completed': True},
            'normal wrapper cleanup receipt is missing or failed')
    require(record.get('private_files_remaining') == [], 'private session material remains')
    require(set(record.get('processes', {})) == set(ROLES)
            and all(value.get('alive') is False for value in record['processes'].values()),
            'owned PID/starttime remains live or is unobserved')
    require(record.get('uinput_remaining') == [], 'temporary uinput device remains or is unobserved')
    require(record.get('stop_confirmed') is True, 'normal stop/worker termination was not confirmed')
    require(isinstance(record.get('governor_before'), str) and record['governor_before']
            and record.get('governor_after') == record['governor_before'], 'NPU governor changed or is unknown')


def validate_keys(document):
    require(isinstance(document, dict) and set(document) == {'probe_run_id', 'probe_screen_sha256', 'actions'},
            'keys require probe_run_id, probe_screen_sha256 and actions')
    safe_id(document['probe_run_id'])
    require(isinstance(document['probe_screen_sha256'], str)
            and re.fullmatch('[0-9a-f]{64}', document['probe_screen_sha256']), 'invalid probe image SHA256')
    actions = document['actions']
    require(isinstance(actions, list) and 1 <= len(actions) <= 128, 'keys require 1..128 actions')
    for action in actions:
        require(isinstance(action, dict) and set(action) <= {'key', 'modifier', 'pause_seconds'}
                and 'key' in action, 'invalid input action fields')
        require(isinstance(action['key'], str) and ('modifier' not in action or isinstance(action['modifier'], str)),
                'input key/modifier must be strings')
        if 'modifier' in action:
            require((action['modifier'], action['key']) == ('Shift', 'Tab'), 'only Shift+Tab is allowed in configuration')
        else:
            require(action['key'] in native_keyboard.KEYS, 'input key is outside whitelist')
        require(0 <= finite(action.get('pause_seconds', .15)) <= 2, 'action pause must be 0..2 seconds')
    return actions


def validate_probe_keys(document):
    actions = validate_keys(document)
    for action in actions:
        require(action.get('modifier') == 'Shift' and action['key'] == 'Tab'
                or 'modifier' not in action and action['key'] in ('Tab', 'Home', 'Up', 'Down', 'Escape'),
                'probe action must be nonactivating navigation')
    return actions


def load_bound_keys(path, probe=False):
    document = read_json(path)
    actions = validate_probe_keys(document) if probe else validate_keys(document)
    directory = OUTPUT_ROOT / document['probe_run_id']
    require(directory.resolve().parent == OUTPUT_ROOT.resolve(), 'probe evidence escaped validation root')
    proof = verify_directory(directory)
    require(proof.get('passed') is True and proof.get('mode') == 'probe'
            and proof.get('run_id') == document['probe_run_id']
            and proof.get('render', {}).get('sha256') == document['probe_screen_sha256'],
            'keys do not bind a verified actual probe screenshot')
    return document, actions, proof


def validate_probe_budget(smoke, actions):
    require(type(smoke) is int and 3 <= smoke <= 30, 'probe smoke must be 3..30 seconds')
    # Keep the existing native 2s screenshot tail plus 1s observer polling margin.
    require(sum(finite(action.get('pause_seconds', .15)) for action in actions) + 3 <= smoke,
            'probe action pauses leave insufficient native capture/stop margin')


def prepare_decoder():
    started = time.monotonic()
    try:
        cv2 = importlib.import_module('cv2')
        numpy = importlib.import_module('numpy')
    except (ImportError, OSError, RuntimeError) as error:
        raise RuntimeError('image decoder preparation failed before App launch') from error
    versions = {'cv2': cv2.__version__, 'numpy': numpy.__version__}
    require(all(isinstance(value, str) and value for value in versions.values()), 'decoder dependency version unavailable')
    return {'request_started': started, 'response_finished': time.monotonic(), 'versions': versions,
            'scope': 'actual decoder dependency imports before the vendor process/native Qt timer'}


def validate_render(screen, receipt, mode):
    require(mode in ('probe', 'run'), 'unknown render mode')
    require(isinstance(receipt, dict) and receipt.get('saved') is True and receipt.get('tab') == 0,
            'actual APP_RENDER saved detection page receipt is missing')
    if mode == 'run':
        require(receipt.get('valid') is True and receipt.get('previewVisible') is True,
                'APP_RENDER was not a fresh visible detection preview')
    require(Path(screen).is_file(), 'actual screen.png is missing')
    import cv2
    decoded = cv2.imread(str(screen))
    require(decoded is not None and decoded.size > 0, 'screen.png does not decode')
    return {'sha256': digest(screen), 'dimensions': [decoded.shape[1], decoded.shape[0]],
            'scope': 'Qt screenRoot only; excludes header and physical display', 'receipt': receipt}


def validate_preview(row, current):
    require(row.get('status') == 200, 'fresh preview HTTP response is unavailable')
    value = row.get('body')
    require(isinstance(value, dict) and value.get('valid') is True, 'invalid preview body')
    for field in ('generation', 'source_epoch'):
        require(value.get(field) == current.get(field) and value.get(field) is not None,
                'preview ' + field + ' does not match running status')
    require(type(value.get('frame_id')) is int and value['frame_id'] >= 0, 'invalid preview frame id')
    started, finished, captured = (finite(row.get('request_started')), finite(row.get('response_finished')),
                                   finite(value.get('captured_at')))
    age, ttl = finite(value.get('source_age_ms')), finite(value.get('remaining_ttl_ms'))
    require(0 <= started <= finished and 0 <= captured <= finished and 0 <= age <= 1000
            and 0 < ttl <= 1000 and abs(age + ttl - 1000) < .01
            and finished - captured <= 1 and ttl > (finished - started) * 1000,
            'preview expired by response completion or has invalid TTL/time')
    try:
        jpeg = base64.b64decode(value['jpeg_base64'], validate=True)
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError('invalid preview JPEG encoding') from error
    import cv2
    import numpy as np
    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    require(decoded is not None and decoded.size > 0, 'preview JPEG does not decode')
    return jpeg


class SessionOwner:
    """Bind newly-issued normal receipts to Linux process starttime and argv."""
    def __init__(self, session, child_pid, reader=proc_read):
        self.session, self.reader = Path(session), reader
        self.ready = read_json(self.session / 'ready.json')
        self.receipt = read_json(self.session / 'processes.json')
        require(self.ready.get('ready') is True and self.ready.get('host') == '127.0.0.1'
                and type(self.ready.get('port')) is int and 1 <= self.ready['port'] <= 65535,
                'invalid private ready receipt', RuntimeError)
        require(self.receipt.get('launcher') == child_pid and self.receipt.get('service') == self.ready.get('pid')
                and self.receipt.get('tab') == 'detection', 'receipts do not belong to this normal launcher', RuntimeError)
        self.identities = {}
        for role in ROLES:
            pid = self.receipt.get(role)
            require(type(pid) is int and pid > 0, 'invalid process receipt', RuntimeError)
            row = self.reader(pid)
            require(row is not None, role + ' exited before identity binding', RuntimeError)
            argv = row['argv']
            expected = str(APP_ROOT / 'src/app' / (role + '.py')) if role == 'service' else str(APP_ROOT / 'src/app/launch.py')
            if role == 'ui':
                require(Path(argv[0]).name == 'qmlscene' and argv[1:] == [str(self.session / 'App.qml')],
                        'Qt command is not this session App.qml', RuntimeError)
            else:
                require(expected in argv and '--root' in argv and argv[argv.index('--root') + 1] == str(APP_ROOT),
                        role + ' command path/root mismatch', RuntimeError)
            self.identities[role] = {'pid': pid, 'starttime': row['starttime'], 'argv': argv}
        require(self.reader(self.receipt['service'])['ppid'] == child_pid
                and self.reader(self.receipt['ui'])['ppid'] == child_pid, 'wrong child process parents', RuntimeError)
        self.assert_alive()

    def assert_alive(self):
        require(read_json(self.session / 'ready.json') == self.ready
                and read_json(self.session / 'processes.json') == self.receipt, 'session receipts changed', RuntimeError)
        validate_live_identities(self.identities, self.reader)
        for role, identity in self.identities.items():
            require(self.reader(identity['pid'])['argv'] == identity['argv'], role + ' command identity changed', RuntimeError)


class LocalAPI:
    def __init__(self, port, token, owner_check):
        require(type(port) is int and 1 <= port <= 65535, 'invalid local API port')
        require(isinstance(token, str) and token and '\n' not in token and '\r' not in token, 'invalid private token')
        self.port, self._token, self.owner_check = port, token, owner_check
        # Disable proxy/environment routing for the fixed loopback endpoint.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, route, payload=None):
        require((method == 'GET' and route in ('/api/config', '/api/status', '/api/preview') and payload is None)
                or (method == 'POST' and route == '/api/stop' and payload == {}), 'API operation is not authorized')
        self.owner_check()
        row = {'method': method, 'route': route, 'request_started': time.monotonic(), 'status': None, 'body': None}
        if method == 'POST':
            row['request_body'] = {}
        raw = b'{}' if method == 'POST' else None
        headers = {'X-Helmet-Token': self._token}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request('http://127.0.0.1:' + str(self.port) + route,
                                         data=raw, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                row['status'] = response.code
                data = response.read()
            # Keep the actual response, except never export a leaked private token.
            text = data.decode('utf-8')
            if self._token in text:
                row['error'] = 'private_token_in_response'
                row['body_redacted'] = True
            else:
                row['response_text'] = text
                row['body'] = json.loads(text)
                # Keep malformed/nonfinite responses as raw text, with an
                # error verdict; JSONL must never silently drop a NaN sample.
                json.dumps(row['body'], allow_nan=False)
        except (OSError, ValueError, UnicodeError):
            row['error'] = 'http_or_decode_failure'
            row['body'] = None
        row['response_finished'] = time.monotonic()
        return row


def render_receipt(path):
    if not Path(path).is_file():
        return None
    matches = []
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        marker = line.find('APP_RENDER ')
        if marker >= 0:
            try:
                matches.append(json.loads(line[marker + len('APP_RENDER '):]))
            except ValueError:
                return None
    return matches[0] if len(matches) == 1 else None


def smoke_duration(duration, actions):
    require(type(duration) is int and 1 <= duration <= 3600, 'duration must be integer seconds 1..3600')
    smoke = math.ceil(duration + sum(finite(action.get('pause_seconds', .15)) for action in actions) + 40)
    require(smoke <= 3600, 'normal Qt timer budget including navigation/tail exceeds 3600')
    return smoke


def json_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def raw_inventory(directory):
    paths = []
    for path in Path(directory).iterdir():
        if path.name in INVENTORY_EXCLUSIONS:
            continue
        require(path.is_file() and not path.is_symlink() and path.resolve().parent == Path(directory).resolve(),
                'raw artifact must be a bounded regular file: ' + path.name)
        require(path.name in RAW_FILES or re.fullmatch(r'preview-[0-9]+\.jpg', path.name),
                'unexpected raw artifact: ' + path.name)
        paths.append(path)
    return {path.name: digest(path) for path in sorted(paths)}


def validate_inventory(directory, inventory):
    require(isinstance(inventory, dict) and inventory, 'raw evidence SHA inventory is missing or empty')
    for relative, expected in inventory.items():
        require(isinstance(relative, str) and (relative in RAW_FILES or re.fullmatch(r'preview-[0-9]+\.jpg', relative)),
                'inventory path is not an allowed bounded raw artifact')
        require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected), 'malformed raw evidence SHA256')
    actual = raw_inventory(directory)
    require(RAW_FILES <= actual.keys(), 'required raw evidence files are missing')
    require(inventory.keys() == actual.keys(), 'raw evidence SHA inventory is partial or has excluded/extra paths')
    for relative, expected in inventory.items():
        require(actual[relative] == expected, 'raw evidence hash mismatch: ' + relative)


def validate_http_text(row):
    text, body, error = row.get('response_text'), row.get('body'), row.get('error')
    require(not row.get('body_redacted'), 'private response redaction cannot be passing raw evidence')
    if text is None:
        require(body is None and error == 'http_or_decode_failure', 'raw HTTP response text is missing or contradicts outcome')
        return
    require(isinstance(text, str), 'raw HTTP response text is not a string')
    try:
        parsed = json.loads(text)
        canonical = json.dumps(parsed, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        require(body is None and error == 'http_or_decode_failure', 'malformed raw HTTP text contradicts parsed body/error')
        return
    require(isinstance(body, dict) and not error and type(row.get('status')) is int
            and canonical == json.dumps(body, sort_keys=True, allow_nan=False),
            'raw HTTP response text disagrees with parsed body/error')


def resource_summary(rows, identities, first, last):
    require(len(rows) >= 2 and set(identities) == set(ROLES) | {'observer'}, 'resource identities/samples incomplete')
    previous = None
    for row in rows:
        start, end = finite(row.get('request_started')), finite(row.get('response_finished'))
        require(0 <= start <= end and (previous is None or start >= previous['response_finished']),
                'invalid resource sample timing')
        require(set(row.get('processes', {})) == set(identities), 'resource role missing')
        for role, identity in identities.items():
            sample = row['processes'][role]
            require(sample.get('pid') == identity['pid'] and sample.get('starttime') == identity['starttime'],
                    'resource PID/starttime changed: ' + role)
            for field in ('rss_bytes', 'cpu_ticks', 'threads', 'fds'):
                require(type(sample.get(field)) is int and sample[field] >= 0, 'resource field missing: ' + role + '/' + field)
            if previous is not None:
                require(sample['cpu_ticks'] >= previous['processes'][role]['cpu_ticks'], 'CPU ticks regressed')
        require(isinstance(row.get('temperature'), list) and row['temperature']
                and all('value' in item for item in row['temperature']), 'temperature evidence missing')
        require(isinstance(row.get('npu_frequency'), str) and row['npu_frequency'].isdigit()
                and isinstance(row.get('governor'), str) and row['governor'], 'NPU frequency/governor evidence missing')
        require(row['governor'] == rows[0].get('governor'), 'governor changed inside resource samples')
        previous = row
    require(rows[0]['request_started'] <= first['response_finished'] + 2
            and rows[-1]['response_finished'] >= last['request_started'] - 2, 'resource samples do not cover measurement')
    starts = [row['request_started'] for row in rows]
    measured = [row for row in rows if first['request_started'] <= row['request_started'] <= last['response_finished']]
    require(len(measured) >= 2 and max(b['request_started'] - a['request_started']
            for a, b in zip(measured, measured[1:])) <= 3, 'resource sampling gap exceeds recorded 1s schedule tolerance of 3s')
    result = {'samples': len(rows), 'observed_max_sample_gap_seconds': max(b - a for a, b in zip(starts, starts[1:])),
              'scope': 'descriptive process RSS/CPU/threads/FD; no leak threshold or acceptance claim', 'windows': {}}
    for name, begin, end in (('first_60_seconds', starts[0], starts[0] + 60),
                             ('last_300_seconds', max(starts[0], starts[-1] - 300), starts[-1])):
        selected = [row for row in rows if begin <= row['request_started'] <= end]
        window = {'samples': len(selected), 'observed_seconds': selected[-1]['request_started'] - selected[0]['request_started'],
                  'roles': {}}
        for role in identities:
            values = [row['processes'][role] for row in selected]
            window['roles'][role] = {field: {'first': values[0][field], 'last': values[-1][field],
                                                  'min': min(v[field] for v in values), 'max': max(v[field] for v in values)}
                                     for field in ('rss_bytes', 'threads', 'fds')}
            window['roles'][role]['cpu_ticks_delta'] = values[-1]['cpu_ticks'] - values[0]['cpu_ticks']
        result['windows'][name] = window
    return result


def verify_directory(directory):
    """Reject incomplete or contradictory raw evidence; never contact the board."""
    directory = Path(directory)
    result = {'kind': 'normal-launcher-offline-verification-v1', 'directory': str(directory),
              'passed': False, 'failures': [], 'gates': {'thirty_minutes_observed': False},
              'scope': 'unverified normal-launcher observation'}
    try:
        manifest = read_json(directory / 'manifest.json')
        require(isinstance(manifest, dict), 'manifest must be a JSON object')
        require(manifest.get('kind') == 'normal-launcher-observation-v1' and manifest.get('mode') in ('probe', 'run'),
                'wrong evidence kind/mode')
        safe_id(manifest.get('run_id'))
        result['run_id'], result['mode'] = manifest['run_id'], manifest['mode']
        require(manifest.get('failures') == [], 'recorded observer failure: ' + ','.join(manifest.get('failures', ['unknown'])))
        command = manifest.get('command', [])
        require(len(command) == 3 and command[:2] == [WRAPPER.as_posix(), '--smoke-seconds']
                and command[2].isdigit() and 3 <= int(command[2]) <= 3600, 'not the bounded normal wrapper command')
        require(manifest.get('wrapper_target') == (APP_ROOT / 'vendor-entry/helmet_app').as_posix(), 'wrong vendor wrapper target')
        require(manifest.get('source_hashes_before') and manifest.get('source_hashes_before') == manifest.get('source_hashes_after'),
                'frozen input/model/code hashes changed or are missing')
        require(not list(directory.rglob('token')) and not list(directory.rglob('App.qml')), 'private material was exported')
        validate_inventory(directory, manifest.get('evidence_sha256'))
        terminal = read_json(directory / 'terminal.json')
        validate_terminal(terminal)
        require(read_json(directory / 'exit.json') == terminal['exit_receipt'], 'copied exit receipt does not match terminal')
        identities = manifest.get('identities', {})
        for role in ROLES:
            require(role in identities and all(terminal['processes'][role].get(field) == identities[role].get(field)
                                             for field in ('pid', 'starttime')), 'terminal identity does not match: ' + role)
            current = terminal['processes'][role].get('current')
            require(current is None or current.get('starttime') != identities[role]['starttime']
                    or current.get('state') in ('Z', 'X', 'x'), 'terminal raw proc contradicts ended identity')
        result['gates']['normal_cleanup'] = True
        result['render'] = validate_render(directory / 'screen.png', render_receipt(directory / 'ui.log'), manifest['mode'])
        result['gates']['native_render'] = True
        rows = json_lines(directory / 'http.jsonl')
        require(rows and [row.get('index') for row in rows] == list(range(len(rows))), 'HTTP evidence sequence incomplete')
        previous = None
        for row in rows:
            validate_http_text(row)
            method, route = row.get('method'), row.get('route')
            require((method == 'GET' and route in ('/api/config', '/api/status', '/api/preview'))
                    or (method == 'POST' and route == '/api/stop' and row.get('request_body') == {}),
                    'forbidden API operation or nonempty stop payload in raw evidence')
            require(row.get('body') is None or isinstance(row['body'], dict), 'HTTP response body must be a JSON object')
            if route == '/api/status' and row.get('status') == 200:
                value = detection(row)
                if manifest['mode'] == 'run':
                    reject_runtime_failures(value)
                if value.get('running') is True:
                    require(manifest['mode'] == 'run' and value.get('selection') == SELECTION,
                            'wrong running selection appeared outside measured boundaries')
                require(value.get('state') != 'fault', 'fault status in raw observation')
            start, end = finite(row.get('request_started')), finite(row.get('response_finished'))
            require(0 <= start <= end and (previous is None or start >= previous['response_finished']), 'HTTP evidence time regressed')
            previous = row
        stops = [row for row in rows if row['method'] == 'POST' and row['route'] == '/api/stop'
                 and row.get('status') == 200 and (row.get('body') or {}).get('stopped') is True]
        stopped_status = [row for row in rows if row['route'] == '/api/status' and row.get('status') == 200
                          and (row.get('body') or {}).get('detection', {}).get('running') is False]
        require(stops and any(row['request_started'] >= stops[-1]['response_finished'] for row in stopped_status),
                'normal stop and subsequent running=false API evidence missing')
        resources = json_lines(directory / 'resources.jsonl')
        require(resources and all(row.get('governor') == terminal['governor_before'] for row in resources),
                'sampled governor differs from original governor')
        inputs = json_lines(directory / 'input.jsonl') if (directory / 'input.jsonl').is_file() else []
        keys = manifest.get('keys', {})
        calibrated = manifest['mode'] == 'probe' and 'actions' in keys
        if manifest['mode'] == 'run' or calibrated:
            expected_actions = validate_probe_keys(keys) if calibrated else validate_keys(keys)
            probe = manifest.get('probe_evidence', {})
            require(probe.get('passed') is True and probe.get('mode') == 'probe'
                    and probe.get('run_id') == manifest['keys']['probe_run_id']
                    and probe.get('render', {}).get('sha256') == manifest['keys']['probe_screen_sha256'],
                    'verified actual probe is not bound to input keys')
        else:
            tabs = manifest.get('keys', {}).get('tabs')
            require(type(tabs) is int and 0 <= tabs <= 16, 'probe input count is invalid')
            expected_actions = [{'key': 'Tab', 'pause_seconds': .12} for _ in range(tabs)]
        require([row.get('index') for row in inputs] == list(range(len(expected_actions)))
                and [row.get('action') for row in inputs] == expected_actions, 'actual input sequence differs or is missing')
        previous_input_time = None
        for row in inputs:
            current_time = finite(row.get('monotonic'))
            require(row.get('device_name') == manifest.get('uinput_name') and current_time >= 0
                    and (previous_input_time is None or current_time >= previous_input_time), 'input device/time changed')
            require(row.get('owner_identities') == {role: identities[role] for role in ROLES},
                    'input process ownership differs from session')
            previous_input_time = current_time
        if manifest['mode'] == 'probe':
            statuses = [row for row in rows if row['route'] == '/api/status']
            require(statuses and all(row.get('status') == 200 and detection(row).get('running') is False
                                    and not detection(row).get('generation') for row in statuses), 'probe started detection or status is unverified')
            if calibrated or 'decoder_preparation' in manifest:
                for index, action in enumerate(inputs):
                    checks = [row for row in statuses if row.get('after_input_index') == index]
                    require(len(checks) == 1 and checks[0]['request_started'] >= action['monotonic']
                            and (index + 1 == len(inputs) or checks[0]['response_finished'] <= inputs[index + 1]['monotonic']),
                            'probe lacks its ordered idle check after input ' + str(index))
            result['resources'] = resource_summary(resources, identities, statuses[0], statuses[-1])
            result['scope'] = 'normal-launcher keyboard navigation probe; no NPU acceptance'
            result['gates']['probe_no_detection'] = True
        else:
            require(manifest.get('selection') == SELECTION, 'manifest selection is wrong')
            measurement = manifest.get('measurement', {})
            indices = [measurement.get(field) for field in ('first_fresh_preview_index', 'first_status_index', 'last_status_index')]
            require(all(type(index) is int and 0 <= index < len(rows) for index in indices)
                    and indices[0] < indices[1] < indices[2], 'business observation boundary indices missing')
            preview, first, last = (rows[index] for index in indices)
            require(preview['route'] == '/api/preview' and preview['response_finished'] <= first['request_started'],
                    'status baseline precedes fresh running preview')
            validate_preview(preview, detection(first))
            result['interval'] = validate_interval(first, last)
            requested = measurement.get('requested_duration_seconds')
            require(type(requested) is int and requested > 0 and result['interval']['duration_lower_seconds'] >= requested,
                    'requested observation duration lower bound was not met')
            previous_status = first
            preview_rows = [preview]
            for row in rows[indices[1]:indices[2] + 1]:
                require(row.get('status') == 200 and not row.get('error'), 'HTTP/decode failure inside measurement')
                if row['route'] == '/api/status':
                    if row is not first:
                        validate_interval(previous_status, row)
                    previous_status = row
                elif row['route'] == '/api/preview':
                    validate_preview(row, detection(previous_status))
                    preview_rows.append(row)
                else:
                    require(row['route'] == '/api/config', 'stop occurred inside measurement')
            for row in preview_rows:
                relative = row.get('jpeg_file')
                require(isinstance(relative, str) and re.fullmatch(r'preview-[0-9]+\.jpg', relative), 'decoded preview file missing')
                jpeg = validate_preview(row, detection(first))
                require((directory / relative).is_file() and (directory / relative).read_bytes() == jpeg,
                        'saved JPEG differs from actual JSON response')
            status_starts = [row['request_started'] for row in rows[indices[1]:indices[2] + 1] if row['route'] == '/api/status']
            preview_starts = [row['request_started'] for row in preview_rows]
            result['sampling'] = {'status_samples': len(status_starts), 'preview_samples': len(preview_starts),
                'max_status_gap_seconds': max(b - a for a, b in zip(status_starts, status_starts[1:])),
                'max_preview_gap_seconds': max((b - a for a, b in zip(preview_starts, preview_starts[1:])), default=None),
                'scope': 'sampled API observations; not continuous GUI visibility or full-frame latency quantiles'}
            require(result['sampling']['max_status_gap_seconds'] <= 3,
                    'status sampling gap exceeds recorded 1s schedule tolerance of 3s')
            require((result['sampling']['max_preview_gap_seconds'] is None
                    or result['sampling']['max_preview_gap_seconds'] <= 10)
                    and last['response_finished'] - preview_rows[-1]['response_finished'] <= 10,
                    'preview sampling gap exceeds recorded 5s schedule tolerance of 10s')
            result['sampling']['schedule_tolerance_seconds'] = {'status': 3, 'preview': 10, 'resources': 3}
            result['resources'] = resource_summary(resources, identities, first, last)
            result['gates']['thirty_minutes_observed'] = result['interval']['thirty_minutes_observed']
            result['gates']['detected_fps_at_least_10'] = result['interval']['conservative_fps']['detected'] >= 10
            result['gates']['output_fps_at_least_10'] = result['interval']['conservative_fps']['output'] >= 10
            result['gates']['thirty_minutes_and_both_fps_at_least_10'] = all(result['gates'][name] for name in (
                'thirty_minutes_observed', 'detected_fps_at_least_10', 'output_fps_at_least_10'))
            result['gates']['fixed_selection_and_fresh_preview'] = True
            result['scope'] = ('30-minute normal-launcher replay observation' if result['gates']['thirty_minutes_observed']
                               else 'short normal-launcher replay observation')
        result['passed'] = True
    except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
        result['failures'].append(str(error))
    result['unverified'] = ['physical camera', 'human touch', 'independent 85% accuracy', 'continuous physical display',
                            'WiFi authentication/DHCP/save', 'independent direct SDK release receipt', 'no-leak acceptance']
    return result


def source_hashes():
    paths = [path for path in (APP_ROOT / 'src').rglob('*') if path.is_file()
             and path.suffix in ('.py', '.qml', '.svg') and '__pycache__' not in path.parts]
    paths += list((APP_ROOT / 'models').glob('*.rknn'))
    paths += [APP_ROOT / 'input/construction-replay.avi', WRAPPER]
    require(paths and (APP_ROOT / 'src/app/launch.py') in paths and (APP_ROOT / 'src/app/service.py') in paths
            and (APP_ROOT / 'src/app/qml/Main.qml') in paths
            and (APP_ROOT / 'models' / SELECTION['model']) in paths, 'frozen normal application files are missing')
    return {str(path.relative_to(APP_ROOT)) if path.is_relative_to(APP_ROOT) else str(path): digest(path)
            for path in sorted(paths)}


def preflight():
    require(sys.platform.startswith('linux'), 'probe/run require Linux; this host can only verify evidence')
    require(APP_ROOT.resolve() == APP_ROOT and WRAPPER.is_symlink()
            and WRAPPER.resolve() == APP_ROOT / 'vendor-entry/helmet_app', 'wrong installed normal vendor entry')
    require(Path('/dev/uinput').exists(), 'temporary uinput is unavailable')
    import fcntl
    with (APP_ROOT / 'app-state/launcher.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except BlockingIOError as error:
            raise RuntimeError('normal launcher already owns its lock; do not start another session') from error
    blocked = {'helmet_realtime.py', 'dump_dets.py', 'bench.py', 'service.py', 'launch.py', 'native_app_runner.py'}
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            argv = path.read_bytes().decode('utf-8').rstrip('\0').split('\0')
            require(not any(Path(value).name in blocked for value in argv),
                    'an existing App/inference process must finish before a new run', RuntimeError)
        except (FileNotFoundError, ProcessLookupError):
            continue


def resource_sample(owner, observer_identity):
    start = time.monotonic()
    processes = {}
    identities = {**owner.identities, 'observer': observer_identity}
    for role, identity in identities.items():
        try:
            row = proc_read(identity['pid'])
            if row is None or row['starttime'] != identity['starttime'] or row['state'] in ('Z', 'X', 'x'):
                processes[role] = {**{k: identity[k] for k in ('pid', 'starttime')}, 'error': 'identity_not_live'}
            else:
                processes[role] = {key: row[key] for key in ('pid', 'starttime', 'cpu_ticks', 'rss_bytes', 'threads', 'fds')}
        except OSError:
            processes[role] = {'error': 'proc_read_failure'}
    temperatures = []
    for path in sorted(Path('/sys/class/thermal').glob('thermal_zone*/temp')):
        try:
            temperatures.append({'path': str(path), 'value': path.read_text().strip()})
        except OSError:
            temperatures.append({'path': str(path), 'error': 'temperature_read_failure'})
    row = {'request_started': start, 'processes': processes, 'temperature': temperatures,
           'sampling_schedule_seconds': 1}
    for key, path in (('npu_frequency', NPU_ROOT / 'cur_freq'), ('governor', NPU_ROOT / 'governor')):
        try:
            row[key] = path.read_text().strip()
        except OSError:
            row[key + '_error'] = 'sysfs_read_failure'
    row['response_finished'] = time.monotonic()
    return row


def signal_stop(signum, frame):
    # Signal handler performs no lock, keyboard operation, or file IO.
    global STOP_REQUESTED
    STOP_REQUESTED = True


class Observation:
    def __init__(self, output, api):
        self.output, self.api, self.rows = output, api, []

    def request(self, method, route, payload=None, after_input_index=None):
        row = self.api.request(method, route, payload)
        row['index'] = len(self.rows)
        if after_input_index is not None:
            row['after_input_index'] = after_input_index
        self.rows.append(row)
        append_json(self.output / 'http.jsonl', row)
        return row

    def preview(self, current):
        row = self.api.request('GET', '/api/preview')
        row['index'] = len(self.rows)
        try:
            jpeg = validate_preview(row, current)
            relative = 'preview-' + str(row['index']) + '.jpg'
            (self.output / relative).write_bytes(jpeg)
            row['jpeg_file'] = relative
        except ValueError:
            row['preview_validation_failed'] = True
        self.rows.append(row)
        append_json(self.output / 'http.jsonl', row)
        return row

    def normal_stop(self, deadline):
        stopped = self.request('POST', '/api/stop', {})
        confirmed = stopped.get('status') == 200 and stopped.get('body', {}).get('stopped') is True
        while time.monotonic() < deadline:
            status = self.request('GET', '/api/status')
            if status.get('status') == 200 and status.get('body', {}).get('detection', {}).get('running') is False:
                return confirmed
            time.sleep(.2)
        return False


def capture_session(output, session, token, failures):
    for name in ('ready.json', 'processes.json', 'exit.json', 'service.log', 'ui.log', 'screen.png'):
        source = session / name
        if not source.is_file():
            continue
        if name.endswith('.log'):
            data = source.read_bytes()
            if token and token.encode('utf-8') in data:
                failures.append('private_token_in_' + name)
                data = data.replace(token.encode('utf-8'), b'[PRIVATE_REDACTED]')
            (output / name).write_bytes(data)
        else:
            shutil.copyfile(source, output / name)


def check_probe_action(observation, index):
    row = observation.request('GET', '/api/status', after_input_index=index)
    current = detection(row)
    require(current.get('running') is False and not current.get('generation'),
            'probe input unexpectedly started detection; normal stop required', RuntimeError)
    return row


def _observe_owned(args):
    """Actual normal wrapper path. Cleanup is normal stop and bounded observation."""
    preflight()
    actions, keys_document, proof = [], None, None
    if args.mode == 'run':
        keys_document, actions, proof = load_bound_keys(args.keys)
        smoke = smoke_duration(args.duration, actions)
    else:
        require(type(args.tabs) is int and 0 <= args.tabs <= 16, 'probe tabs must be 0..16')
        if getattr(args, 'keys', None) is not None:
            require(args.tabs == 0, 'probe keys and nonzero tabs cannot be combined')
            keys_document, actions, proof = load_bound_keys(args.keys, probe=True)
        else:
            actions = [{'key': 'Tab', 'pause_seconds': .12} for _ in range(args.tabs)]
        smoke = args.smoke_seconds
        validate_probe_budget(smoke, actions)
    decoder_preparation = prepare_decoder()
    output = create_output(args.run_id)
    before = {path.name for path in (APP_ROOT / 'app-state').glob('session-*')}
    hashes = source_hashes()
    governor = (NPU_ROOT / 'governor').read_text().strip()
    observer = proc_read(os.getpid())
    observer_identity = {key: observer[key] for key in ('pid', 'starttime')}
    command = [str(WRAPPER), '--smoke-seconds', str(smoke)]
    manifest = {'kind': 'normal-launcher-observation-v1', 'run_id': args.run_id, 'mode': args.mode,
        'command': command, 'wrapper_target': str(WRAPPER.resolve()), 'source_hashes_before': hashes,
        'selection': SELECTION if args.mode == 'run' else None, 'failures': [], 'identities': {},
        'keys': keys_document if keys_document is not None else {'tabs': args.tabs},
        'probe_evidence': proof, 'decoder_preparation': decoder_preparation,
        'helper_sha256': {name: digest(Path(__file__).with_name(name))
                          for name in ('native_keyboard.py', 'verify_normal_launcher.py')},
        'sampling_schedule_seconds': {'status': 1, 'preview': 5, 'resources': 1},
        'runtime': {'python': sys.version, 'uname': list(os.uname()), 'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
                    'page_size': os.sysconf('SC_PAGE_SIZE')},
        'screen_scope': 'Qt screenRoot only; no physical display or human touch evidence',
        'observation_load': 'normal bounded App telemetry plus external HTTP/proc observer; not comparable as a single optimization to older full-frame logging',
        'focus_scope': 'PID/starttime checks prove session ownership; actual probe screenshot and selected results check UI input; no compositor focus attestation',
        'npu_concurrency_scope': 'normal launcher lock plus known inference argv preflight; root serializes board experiments'}
    failures = manifest['failures']
    child = owner = observation = device = session = None
    token = None
    stopped = False
    started = time.monotonic()
    manifest['observer_started'] = started
    write_json(output / 'manifest.json', manifest)
    (output / 'input.jsonl').touch(exist_ok=False)
    try:
        with (output / 'launcher.log').open('xb') as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            new = [path for path in (APP_ROOT / 'app-state').glob('session-*') if path.name not in before]
            require(len(new) <= 1, 'multiple new sessions appeared; refuse ambiguous ownership', RuntimeError)
            if new:
                session = new[0]
                if all((session / name).is_file() for name in ('ready.json', 'processes.json', 'token')):
                    break
            require(child.poll() is None and not STOP_REQUESTED, 'launcher exited/interrupted before ready', RuntimeError)
            time.sleep(.05)
        else:
            raise RuntimeError('normal launcher did not produce unique ready/process receipts in time')
        owner = SessionOwner(session, child.pid)
        manifest['session'] = str(session)
        manifest['identities'] = {**owner.identities, 'observer': observer_identity}
        token_path = session / 'token'
        require(not token_path.is_symlink() and token_path.stat().st_mode & 0o077 == 0, 'private token permissions are unsafe')
        token = token_path.read_text().strip()
        observation = Observation(output, LocalAPI(owner.ready['port'], token, owner.assert_alive))
        configuration = observation.request('GET', '/api/config')
        require(configuration.get('status') == 200, 'normal configuration API failed')
        if args.mode == 'run':
            for name, list_name in (('model', 'models'), ('source', 'sources'), ('profile', 'profiles')):
                require(SELECTION[name] in {item['id'] for item in configuration['body'][list_name]}, 'fixed configuration is unavailable')
        initial = observation.request('GET', '/api/status')
        require(detection(initial).get('running') is False and not detection(initial).get('generation'), 'new normal session is not idle')
        append_json(output / 'resources.jsonl', resource_sample(owner, observer_identity))
        # Give Qt initialization and the seat time to consume its new device.
        device = native_keyboard.VirtualKeyboard(owner.assert_alive)
        with device:
            device_deadline = time.monotonic() + 2
            while not native_keyboard.device_nodes(device.name) and time.monotonic() < device_deadline:
                time.sleep(.05)
            require(native_keyboard.device_nodes(device.name), 'temporary keyboard did not appear in input sysfs', RuntimeError)
            time.sleep(.4)
            if args.mode == 'probe':
                append_json(output / 'resources.jsonl', resource_sample(owner, observer_identity))
            for index, action in enumerate(actions):
                require(not STOP_REQUESTED, 'input sequence interrupted', RuntimeError)
                if 'modifier' in action:
                    device.chord(action['modifier'], action['key'])
                else:
                    device.press(action['key'])
                append_json(output / 'input.jsonl', {'index': index, 'monotonic': time.monotonic(), 'action': action,
                                                   'device_name': device.name, 'owner_identities': owner.identities})
                time.sleep(action.get('pause_seconds', .15))
                if args.mode == 'probe':
                    check_probe_action(observation, index)
                    append_json(output / 'resources.jsonl', resource_sample(owner, observer_identity))
        manifest['uinput_name'] = device.name
        first = last = fresh = None
        previous_status = None
        next_preview = 0
        start_deadline = time.monotonic() + 20
        run_deadline = started + smoke + 15
        while time.monotonic() < run_deadline:
            tick = time.monotonic()
            require(not STOP_REQUESTED, 'observer interrupted', RuntimeError)
            row = observation.request('GET', '/api/status')
            current = detection(row)
            append_json(output / 'resources.jsonl', resource_sample(owner, observer_identity))
            if args.mode == 'probe':
                require(current.get('running') is False and not current.get('generation'), 'probe unexpectedly started NPU detection', RuntimeError)
            else:
                reject_runtime_failures(current)
                if current.get('running'):
                    require(current.get('selection') == SELECTION, 'actual UI selection differs; stop without retry', RuntimeError)
                require(current.get('state') != 'fault', 'normal detection reported fault', RuntimeError)
                if first is not None:
                    validate_interval(previous_status, row)
                    last, previous_status = row, row
                if current.get('running') is True and current.get('valid') is True:
                    valid_running(current)
                    if time.monotonic() >= next_preview:
                        preview = observation.preview(current)
                        next_preview = time.monotonic() + 5
                        if preview.get('preview_validation_failed'):
                            require(first is None, 'preview failed inside formal measurement', RuntimeError)
                        elif fresh is None:
                            fresh = preview
                    if fresh is not None and first is None and row['request_started'] >= fresh['response_finished']:
                        first = last = previous_status = row
                        manifest['measurement'] = {'first_fresh_preview_index': fresh['index'],
                            'first_status_index': first['index'], 'last_status_index': last['index'],
                            'requested_duration_seconds': args.duration}
                require(first is not None or time.monotonic() < start_deadline, 'no fresh running preview/status baseline in time', RuntimeError)
                if first is not None:
                    manifest['measurement']['last_status_index'] = last['index']
            receipt = render_receipt(session / 'ui.log')
            if receipt is not None and (session / 'screen.png').is_file():
                validate_render(session / 'screen.png', receipt, args.mode)
                if args.mode == 'run':
                    require(first is not None and last is not first
                            and validate_interval(first, last)['duration_lower_seconds'] >= args.duration,
                            'native screenshot arrived before requested measurement lower bound', RuntimeError)
                break
            require(child.poll() is None, 'normal wrapper exited before its native screenshot', RuntimeError)
            wait = tick + 1 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        else:
            raise RuntimeError('native screenshot or measurement deadline exceeded')
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, IndexError) as error:
        detail = str(error)
        if token:
            detail = detail.replace(token, '[PRIVATE_REDACTED]')
        failures.append('normal_observation_failed:' + type(error).__name__ + ':' + detail[:256])
    finally:
        if device is not None:
            try:
                device.close()
            except OSError:
                failures.append('uinput_cleanup_failed')
        if observation is not None and child is not None and child.poll() is None:
            try:
                stopped = observation.normal_stop(time.monotonic() + 8)
            except (OSError, ValueError, RuntimeError):
                failures.append('normal_stop_failed')
        if not stopped:
            failures.append('normal_stop_not_confirmed')
        # Default closing uses the original Qt smoke timer. Do not infer focus
        # from PID liveness and send Alt+F4 to an unrelated Wayland window.
        exit_deadline = max(time.monotonic() + 60, started + smoke + 60)
        while child is not None and child.poll() is None and time.monotonic() < exit_deadline:
            time.sleep(.2)
        terminal = {'wrapper_exit': child.poll() if child is not None else None,
                    'exit_receipt': None, 'private_files_remaining': None, 'processes': {},
                    'uinput_remaining': native_keyboard.device_nodes(device.name) if device is not None else None,
                    'governor_before': governor, 'governor_after': None, 'stop_confirmed': stopped}
        if child is not None and child.poll() is None:
            failures.append('failed_still_running_do_not_restart_or_kill')
        if session is not None:
            try:
                terminal['exit_receipt'] = read_json(session / 'exit.json')
            except (OSError, ValueError):
                failures.append('missing_or_invalid_exit_receipt')
            terminal['private_files_remaining'] = [name for name in ('token', 'App.qml') if (session / name).exists()]
            capture_session(output, session, token, failures)
        if owner is not None:
            for role, identity in owner.identities.items():
                try:
                    current = proc_read(identity['pid'])
                    live = current is not None and current['starttime'] == identity['starttime'] and current['state'] not in ('Z', 'X', 'x')
                    terminal['processes'][role] = {key: identity[key] for key in ('pid', 'starttime')}
                    terminal['processes'][role].update(alive=live,
                        current={key: current[key] for key in ('pid', 'starttime', 'state')} if current is not None else None)
                except OSError:
                    terminal['processes'][role] = {'alive': None, 'error': 'terminal_proc_read_failure'}
        try:
            terminal['governor_after'] = (NPU_ROOT / 'governor').read_text().strip()
            manifest['source_hashes_after'] = source_hashes()
        except (OSError, ValueError):
            failures.append('final_hash_or_governor_read_failed')
        write_json(output / 'terminal.json', terminal)
        manifest['observer_finished'] = time.monotonic()
        manifest['failures'] = sorted(set(failures))
        manifest['evidence_sha256'] = raw_inventory(output)
        write_json(output / 'manifest.json', manifest)
        if token is not None:
            # Audit every exported artifact for the actual private token before
            # dropping the in-memory reference. Keep failure evidence redacted.
            private_bytes = token.encode('utf-8')
            for path in output.iterdir():
                if path.is_file() and private_bytes in path.read_bytes():
                    failures.append('private_token_in_export')
                    path.write_bytes(path.read_bytes().replace(private_bytes, b'[PRIVATE_REDACTED]'))
            if 'private_token_in_export' in failures:
                manifest['failures'] = sorted(set(failures))
                manifest['evidence_sha256'] = raw_inventory(output)
                write_json(output / 'manifest.json', manifest)
        token = None
    return output, manifest


def publish_observation(output, manifest):
    """Consume late signal flags through the final derived-artifact write."""
    while True:
        if STOP_REQUESTED:
            manifest['failures'] = sorted(set(manifest['failures']) | {'observer_interrupted'})
        manifest['evidence_sha256'] = raw_inventory(output)
        write_json(output / 'manifest.json', manifest)
        result = verify_directory(output)
        write_json(output / 'verification.json', result)
        if STOP_REQUESTED and 'observer_interrupted' not in manifest['failures']:
            # The flag has a single False->True transition; this repeats at most
            # once if a signal arrived during the last manifest/result write.
            continue
        return result


def observation_stdout(args, output, result):
    print(json.dumps({'run_id': args.run_id, 'output': str(output), 'passed': result['passed'],
                      'scope': result['scope'], 'failures': result['failures'],
                      'physical_touch_verified': False}, ensure_ascii=False))


def observe(args):
    global STOP_REQUESTED
    STOP_REQUESTED = False
    old_handlers = {}
    output = manifest = result = None
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, signal_stop)
        output, manifest = _observe_owned(args)
        result = publish_observation(output, manifest)
        observation_stdout(args, output, result)
    finally:
        # Restore even on artifact IO errors, ending the handler-owned boundary.
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    # A signal caught during stdout or restoration remains our evidence. Consume
    # its already-set flag after both original handlers have been restored;
    # future OS signals follow those handlers and are outside this boundary.
    if output is not None and STOP_REQUESTED and 'observer_interrupted' not in manifest['failures']:
        result = publish_observation(output, manifest)
        observation_stdout(args, output, result)
    return 0 if result['passed'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    probe = sub.add_parser('probe', help='normal detection page navigation; never starts NPU')
    probe.add_argument('--run-id', required=True, type=safe_id)
    probe.add_argument('--tabs', type=int, default=0)
    probe.add_argument('--keys', type=Path, help='safe calibration actions bound to an existing actual probe screenshot')
    probe.add_argument('--smoke-seconds', type=int, default=8)
    run = sub.add_parser('run', help='fixed model/source/profile selected and started through real UI')
    run.add_argument('--run-id', required=True, type=safe_id)
    run.add_argument('--duration', required=True, type=int)
    run.add_argument('--keys', required=True, type=Path)
    verify = sub.add_parser('verify', help='read-only offline raw-evidence verification')
    verify.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == 'verify':
            result = verify_directory(args.directory)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result['passed'] else 1
        return observe(args)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, IndexError):
        print('Normal launcher verification refused or failed; inspect retained evidence if a run was created.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
