#!/usr/bin/env python3
"""Resident-v4 normal-wrapper calibration, observation and raw recomputation.

This observer never posts HTTP, starts detection through HTTP, fabricates Qt
receipts, kills owners or changes parent networking. Runtime requires Linux and
a separately reviewed release record. Host verification is software evidence.
"""
import argparse
import base64
import ctypes
import hashlib
import importlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

APP_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v4')
COORDINATOR = Path('/userdata/helmet-validation/resident-state')
OUTPUT_ROOT = Path('/userdata/helmet-validation/20261115-resident-normal')
WRAPPERS = {'detection': Path('/opt/ui/src/apps/helmet_app'), 'wifi': Path('/opt/ui/src/apps/helmet_wifi')}
VERSION = 'resident-v4'
MODEL_SHA = '7e0eefe4bfe86627bb6b58161cacca10912e0dbe1ba2513f58d091c40b26cd48'
REPLAY_SHA = 'f22897e1ebb40cc919caab335ee4b436d28ebaf0a138eb8b16c63166184af5cc'
IDENTITY_FIELDS = {'pid', 'starttime', 'uid'}
OWNER_ROLES = {'manager', 'service', 'qt'}
ACK_COUNTERS = ('frame_requests', 'frame_decodes', 'preview_presentations')
GET_ROUTES = {'/api/config', '/api/status', '/api/application', '/api/preview'}
RAW_REQUIRED = {'release.json', 'identities.json', 'preflight.json', 'http.jsonl',
                'resources.jsonl', 'clients.jsonl', 'actions.jsonl', 'captures.jsonl',
                'initial-record.json', 'ready.json', 'start.json', 'terminal.json',
                'exit.json', 'service-state.json', 'network.json', 'decoder.json'}
EXCLUDED = {'manifest.json', 'derived-verification.json'}
CODE_FILES = ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml')
INTERRUPTED = []


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    require(type(value) in (int, float) and math.isfinite(value), 'finite numeric value required')
    return value


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum, 'integer outside valid range')
    return value


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else _hash_stream(stream)


def _hash_stream(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(block)
    return result.hexdigest()


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError('nonfinite JSON constant')
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)
    except RecursionError:
        raise ValueError('unbounded JSON nesting') from None
    json.dumps(value, allow_nan=False)
    return value


def read_json(path):
    return strict_json(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def append_json(path, value):
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')


def rows(path):
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    require(lines and all(line.strip() for line in lines), 'empty/blank JSONL category')
    return [strict_json(line) for line in lines]


def safe_id(run_id):
    require(isinstance(run_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', run_id), 'unsafe run ID')
    return run_id


def clean_relative(value):
    require(isinstance(value, str) and value and '\\' not in value, 'unsafe relative artifact path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and '..' not in path.parts and str(path) == value, 'unsafe relative artifact path')
    return value


def no_links(path, directory=False):
    path = Path(path)
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'symlink outside explicit wrapper boundary')
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), 'wrong artifact type')
    if not directory:
        require(info.st_nlink == 1, 'multiply linked artifact')
    return info


def create_output(run_id, base=OUTPUT_ROOT):
    safe_id(run_id)
    no_links(base, directory=True)
    output = Path(base) / run_id
    require(not output.exists() and not output.is_symlink(), 'run directory already exists')
    output.mkdir(mode=0o700)
    return output


def identity(value):
    require(isinstance(value, dict) and set(value) == IDENTITY_FIELDS, 'invalid process identity schema')
    integer(value['pid'], 1)
    integer(value['starttime'], 1)
    integer(value['uid'])
    return value


def validate_ack(value, session, identities, visible, tab, received_before=None, received_after=None):
    require(isinstance(value, dict) and set(value) == {'session', 'root', 'version', 'phase', 'intent', 'ack', 'applied',
            'ui', 'service', 'progress', 'failure', 'verification_enabled', 'last_render'}
            and value.get('verification_enabled') is True
            and value.get('session') == session and value.get('root') == APP_ROOT.as_posix()
            and value.get('version') == VERSION, 'application session/root/version mismatch')
    require(value.get('phase') == 'running' and value.get('failure') is None
            and value.get('applied') is True, 'application intent lacks actual applied ACK')
    ui, intent, ack = value.get('ui'), value.get('intent'), value.get('ack')
    require(isinstance(ui, dict) and ui.get('alive') is True and ui.get('identity') == identities['qt']
            and isinstance(ui.get('instance'), str) and re.fullmatch('[0-9a-f]{32}', ui['instance']), 'Qt owner/UI mismatch')
    require(value.get('service') == identities['service'], 'service owner mismatch')
    require(isinstance(intent, dict) and isinstance(ack, dict), 'missing intent or actual ACK')
    integer(intent.get('sequence'), 1)
    integer(ack.get('sequence'), 1)
    finite(intent.get('preview_after'))
    require(intent.get('visible') is visible and intent.get('tab') == tab
            and ack.get('visible') is visible and ack.get('tab') == tab
            and ack.get('sequence') == intent['sequence'] and ack.get('ui_instance') == ui['instance']
            and ack.get('qt') == identities['qt'], 'stale UI/sequence/visibility/tab ACK')
    received_at = finite(ack.get('received_at'))
    require(received_at >= 0
            and (received_before is None or received_at <= received_before)
            and (received_after is None or received_at >= received_after),
            'backend ACK time outside observation')
    for name in ACK_COUNTERS:
        integer(ack.get(name))
    return ack


def validate_client(value, before):
    identity(value.get('identity'))
    require(value.get('exit_code') == 0 and type(value.get('exit_code')) is int
            and value.get('accepted') is True and value.get('observed') is None,
            'wrapper client is live, failed or did not accept intent')
    require(0 <= finite(value.get('terminal_at')) <= finite(before), 'formal observation precedes client terminal')


def selection(source):
    require(source in ('replay', 'camera'), 'unknown source mode')
    return {'model': 'helmet_416x416.rknn', 'source': 'construction-replay.avi' if source == 'replay' else '/dev/video10',
            'profile': 'responsive'}


def detection(row):
    require(row.get('method') == 'GET' and row.get('route') == '/api/status'
            and row.get('status') == 200 and isinstance(row.get('body'), dict), 'invalid status response')
    value = row['body'].get('detection')
    require(isinstance(value, dict), 'missing detection status')
    return value


def running(value, source):
    require(value.get('running') is True and value.get('valid') is True
            and value.get('state') in ('healthy', 'no-detection') and value.get('selection') == selection(source),
            'current healthy fixed detection selection required')
    require(isinstance(value.get('generation'), str) and re.fullmatch('[0-9a-f]{32}', value['generation']), 'missing generation')
    integer(value.get('source_epoch'))
    counters = value.get('counters')
    require(isinstance(counters, dict) and {'captured', 'detected', 'output', 'read_failure', 'detection_failure'} <= counters.keys(),
            'incomplete counters')
    for count in counters.values():
        integer(count)
    require(counters['read_failure'] == counters['detection_failure'] == 0, 'read/detection failure counter growth')


def validate_action_proof_timing(action, proof):
    inputs = action.get('inputs')
    require(isinstance(inputs, list) and inputs, 'UI action has no native input witness')
    sent = [finite(item.get('sent_at')) for item in inputs]
    start = finite(action.get('started'))
    finish = finite(action.get('finished'))
    proof_start, proof_finish = boundaries(proof)
    require(start <= sent[0] and all(a <= b for a, b in zip(sent, sent[1:]))
            and sent[-1] <= finish, 'native UI input timeline invalid')
    require(sent[-1] <= proof_start <= proof_finish <= finish,
            'UI confirmation does not follow its triggering native input')


def boundaries(row):
    start, finish = finite(row.get('request_started')), finite(row.get('response_finished'))
    require(0 <= start <= finish, 'invalid observation boundaries')
    return start, finish


def validate_interval(first, last, source):
    start, finish = boundaries(first)
    last_start, last_finish = boundaries(last)
    require(finish < last_start, 'overlapping or empty formal interval')
    before, after = detection(first), detection(last)
    running(before, source)
    running(after, source)
    for name in ('generation', 'source_epoch', 'selection'):
        require(before[name] == after[name], 'detection identity changed: ' + name)
    require(before['counters'].keys() == after['counters'].keys(), 'counter schema changed')
    for name, count in before['counters'].items():
        require(after['counters'][name] >= count, 'counter regressed: ' + name)
    lower, denominator = last_start - finish, last_finish - start
    delta = {name: after['counters'][name] - before['counters'][name] for name in ('captured', 'detected', 'output')}
    return {'hidden_lower_seconds': lower, 'fps_denominator_seconds': denominator,
            'counter_deltas': delta, 'fps': {name: count / denominator for name, count in delta.items()},
            'short_hidden': lower >= 30, 'long_hidden': lower >= 1800,
            'scope': 'sampled counter deltas; no all-frame, human-display or no-leak claim'}


def validate_schedule(samples, max_gap, first, last):
    require(len(samples) >= 2, 'insufficient scheduled samples')
    previous = None
    for row in samples:
        start, finish = boundaries(row)
        require(finish - start <= max_gap, 'late sample exceeds gap budget')
        if previous is not None:
            require(start >= previous[1] and start - previous[0] <= max_gap, 'sampling gap or clock regression')
        previous = start, finish
    require(samples[0]['request_started'] <= first + max_gap
            and samples[-1]['response_finished'] >= last - max_gap, 'samples do not cover formal observation')


def validate_statuses(samples, source, max_gap=3):
    require(len(samples) >= 2, 'missing formal statuses')
    validate_schedule(samples, max_gap, samples[0]['request_started'], samples[-1]['response_finished'])
    for first, last in zip(samples, samples[1:]):
        validate_interval(first, last, source)
    return validate_interval(samples[0], samples[-1], source)


def validate_active_timeline(before, hidden, after, source):
    groups = (before, hidden, after)
    for group in groups:
        require(isinstance(group, list) and len(group) >= 2, 'active detection segment incomplete')
        validate_statuses(group, source)
    require(before[-1]['response_finished'] <= hidden[0]['request_started']
            and hidden[-1]['response_finished'] <= after[0]['request_started'],
            'active detection segments overlap')
    rows = before + hidden + after
    identity = detection(rows[0])
    require(all(all(detection(row)[name] == identity[name]
                    for name in ('generation', 'source_epoch', 'selection')) for row in rows),
            'reshow changed detector identity')
    previous = None
    for group in groups:
        first, last = detection(group[0]), detection(group[-1])
        if previous is not None:
            require(first['counters'].keys() == previous['counters'].keys(), 'active counter schema changed')
            for name, count in previous['counters'].items():
                require(first['counters'][name] >= count, 'same-generation counter reset: ' + name)
        previous = last


def validate_hidden_apps(samples, session, identities):
    require(samples, 'missing hidden application evidence')
    baseline = None
    previous_received = None
    for row in samples:
        start, finish = boundaries(row)
        ack = validate_ack(row.get('body'), session, identities, False, 'detection',
                           received_before=finish, received_after=start - 3)
        values = {name: ack[name] for name in ('ui_instance', 'sequence', *ACK_COUNTERS)}
        require(baseline is None or values == baseline, 'hidden ACK changed or GUI preview counters grew')
        require(previous_received is None or ack['received_at'] > previous_received,
                'hidden QML heartbeat did not advance')
        previous_received = ack['received_at']
        baseline = values


def validate_formal_ack(proof, first, session, identities):
    start, finish = boundaries(proof)
    ack = validate_ack(proof.get('body'), session, identities, False, 'detection',
                       received_before=finish)
    require(0 <= ack['received_at'] <= finish <= first['request_started']
            and start <= first['request_started'], 'formal baseline precedes settled hidden ACK completion')


def validate_native_inputs(actions, keys, identities):
    for row in actions:
        if row.get('kind') != 'native-keyboard':
            continue
        expected = [action for action in keys['groups'][row['group']] for _ in range(action.get('count', 1))]
        actual = row.get('inputs')
        require(isinstance(actual, list) and [x.get('action') for x in actual] == expected, 'native inputs differ from calibrated sequence')
        previous = row['started']
        for item in actual:
            sent = finite(item.get('sent_at'))
            require(item.get('owner_checked') == identities and previous <= sent <= row['finished'], 'input owner/time unbound')
            previous = sent


def validate_quit_witness(row, input_at, session, identities, code_id):
    start, finish = boundaries(row)
    value = row.get('record')
    require(isinstance(value, dict) and value.get('session') == session and value.get('root') == APP_ROOT.as_posix()
            and value.get('version') == VERSION and value.get('code_id') == code_id
            and value.get('phase') == 'stopping' and value.get('application', {}).get('phase') == 'stopping'
            and value['application'].get('session') == session
            and all(value.get('ui' if role == 'qt' else role) == identities[role] for role in OWNER_ROLES),
            'actual current private stopping record missing')
    require(0 <= finite(input_at) <= finite(value.get('recorded_at')) <= finish and input_at <= start,
            'private stopping witness is stale or predates real UI input')


def validate_http(row):
    require(row.get('method') == 'GET' and row.get('route') in GET_ROUTES, 'unauthorized HTTP evidence route/method')
    boundaries(row)
    require(type(row.get('status')) is int and row['status'] == 200 and not row.get('error'), 'HTTP failure retained')
    headers = row.get('headers')
    require(isinstance(headers, list) and headers and all(isinstance(x, list) and len(x) == 2
            and all(isinstance(y, str) for y in x) for x in headers), 'missing raw response headers')
    require(any(k.lower() == 'content-type' and 'application/json' in v.lower() for k, v in headers), 'wrong response content type')
    try:
        data = base64.b64decode(row['response_bytes_base64'], validate=True)
        text = data.decode('utf-8')
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError('missing or invalid raw HTTP bytes') from None
    require(text == row.get('response_text') and strict_json(text) == row.get('body'), 'HTTP bytes/text/body mismatch')
    require(isinstance(row['body'], dict), 'HTTP JSON body must be object')


def prepare_decoder():
    start = time.monotonic()
    cv2, numpy = importlib.import_module('cv2'), importlib.import_module('numpy')
    return {'request_started': start, 'response_finished': time.monotonic(),
            'versions': {'cv2': cv2.__version__, 'numpy': numpy.__version__}}


def decode_image(data, dimensions=None):
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    require(image is not None and image.size and int(image.max()) > int(image.min()), 'empty/nondecodable image')
    actual = [image.shape[1], image.shape[0]]
    require(dimensions is None or actual == dimensions, 'actual decoded dimensions mismatch')
    return {'sha256': digest_bytes(data), 'dimensions': actual, 'nonempty': True}


def validate_preview(row, current):
    value = row.get('body')
    require(row.get('status') == 200 and isinstance(value, dict) and value.get('valid') is True, 'missing valid preview')
    for name in ('generation', 'source_epoch'):
        require(value.get(name) == current.get(name) and value.get(name) is not None, 'stale preview ' + name)
    integer(value.get('frame_id'))
    start, finish = boundaries(row)
    captured, age, ttl = finite(value.get('captured_at')), finite(value.get('source_age_ms')), finite(value.get('remaining_ttl_ms'))
    require(0 <= captured <= finish and finish - captured < 1 and 0 <= age < 1000
            and 0 < ttl <= 1000 and abs(age + ttl - 1000) < .02
            and ttl > (finish - start) * 1000, 'preview expired at response completion')
    try:
        data = base64.b64decode(value['jpeg_base64'], validate=True)
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid JPEG bytes') from None
    return decode_image(data, [640, 480])


def validate_render_metadata(receipt, application, current, mode, completed_before=None):
    ui, intent = application['ui'], application['intent']
    expected = 'show-' + str(intent['sequence']) + '-' + ui['instance']
    require(isinstance(receipt, dict) and receipt.get('capture_id') == expected
            and receipt.get('ui_instance') == ui['instance'] and receipt.get('sequence') == intent['sequence']
            and receipt.get('qt') == ui['identity'] and receipt.get('saved') is True
            and receipt.get('visible') is True and intent.get('visible') is True
            and receipt.get('tab') == intent['tab'], 'render is not actual current visible show')
    integer(receipt.get('width'), 1)
    integer(receipt.get('height'), 1)
    completion = finite(receipt.get('received_at'))
    require(completed_before is None or completion <= completed_before,
            'render receipt time exceeds enclosing observation')
    packet = receipt.get('packet')
    if packet is None:
        require(mode == 'probe' and receipt.get('valid_at_receipt') is None
                and finite(receipt.get('remaining_ttl_ms')) == 0, 'idle render cannot count as detection evidence')
        return
    require(isinstance(packet, dict) and set(packet) == {'frame_id', 'captured_at', 'generation', 'source_epoch', 'selection'},
            'incomplete actual Image.Ready packet')
    integer(packet.get('frame_id'))
    require(current is not None and all(packet.get(name) == current.get(name)
            for name in ('generation', 'source_epoch', 'selection')), 'stale displayed generation/epoch/selection')
    captured = finite(packet.get('captured_at'))
    ttl = finite(receipt.get('remaining_ttl_ms'))
    require(receipt.get('valid_at_receipt') is True and 0 < ttl <= 1000
            and 0 <= finite(intent.get('preview_after')) <= captured <= completion
            and completion - captured < 1 and abs(ttl - (1 - (completion - captured)) * 1000) < .02,
            'pre-show or expired backend completion packet')


def raw_inventory(directory):
    no_links(directory, directory=True)
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        relative = path.relative_to(directory).as_posix()
        require(not path.is_symlink(), 'raw symlink')
        if path.is_dir():
            continue
        no_links(path)
        if relative in EXCLUDED:
            continue
        clean_relative(relative)
        result[relative] = {'sha256': sha(path), 'bytes': path.stat().st_size}
    return result


def validate_inventory(directory, inventory, required=RAW_REQUIRED):
    require(isinstance(inventory, dict) and inventory, 'missing/empty raw inventory')
    actual = raw_inventory(directory)
    require(set(inventory) == set(actual) and set(required) <= set(actual), 'raw inventory missing/extra/category mismatch')
    for name, record in inventory.items():
        clean_relative(name)
        require(isinstance(record, dict) and set(record) == {'sha256', 'bytes'}
                and isinstance(record['sha256'], str) and re.fullmatch('[0-9a-f]{64}', record['sha256'])
                and type(record['bytes']) is int and record['bytes'] >= 0
                and (record['bytes'] > 0 or name not in required) and record == actual[name], 'raw inventory mismatch: ' + name)


def validate_action_gates(actions, identities):
    groups = ('configure', 'start', 'hide', 'stop', 'quit')
    required = [row for row in actions if row.get('group') in groups]
    require([row['group'] for row in required] == list(groups), 'missing/reordered/duplicate UI action groups')
    previous = -1
    for row in required:
        require(row.get('kind') == 'native-keyboard' and row.get('confirmed') is True
                and row.get('owner_checked') == identities and type(row.get('evidence')) is int,
                'emergency/unconfirmed action cannot satisfy real QML gate')
        start, finish = finite(row.get('started')), finite(row.get('finished'))
        require(previous <= start <= finish, 'UI action timing changed')
        previous = finish


def validate_terminal(value, identities):
    receipt = value.get('exit_receipt')
    require(isinstance(receipt, dict) and receipt.get('kind') == 'resident-terminal-v4'
            and receipt.get('session') == value.get('session') and receipt.get('root') == APP_ROOT.as_posix()
            and receipt.get('code_id') == value.get('code_id') and receipt.get('version') == VERSION
            and receipt.get('cleanup_completed') is True and receipt.get('resource_lease_released') is True
            and receipt.get('normal_stop') in ('http_quit', 'normal_term')
            and type(receipt.get('service_exit')) is int and receipt['service_exit'] == 0
            and receipt.get('phase') == 'completed', 'missing actual completed resident exit receipt')
    require(value.get('stopping_observed') is True, 'stopping transition unobserved')
    owners = value.get('owners')
    require(isinstance(owners, dict) and set(owners) == OWNER_ROLES, 'unobserved terminal owners')
    terminal_at = finite(receipt.get('terminal_at'))
    for role in OWNER_ROLES:
        row = owners[role]
        require(row.get('identity') == identities[role] and row.get('observed') is None
                and finite(row.get('checked_at')) >= terminal_at, 'owner still live/unknown at terminal: ' + role)
        require(receipt.get('ui' if role == 'qt' else role) == identities[role], 'exit receipt owner mismatch')
    lease = value.get('lease')
    require(isinstance(lease, dict) and lease.get('holders') == [] and lease.get('lock_available') is True,
            'inherited/live/unknown resource lease')
    integer(lease.get('device'))
    integer(lease.get('inode'), 1)
    require(value.get('private_remaining') == [] and value.get('uinput_remaining') == [], 'sensitive/uinput material remains')
    require(isinstance(value.get('governor_before'), str) and value['governor_before']
            and value.get('governor_after') == value['governor_before'], 'governor changed/unknown')
    progress = value.get('service_state')
    require(isinstance(progress, dict) and progress.get('session') == value['session']
            and progress.get('cleanup_completed') is True and progress.get('detection_alive') is False
            and progress.get('wifi_alive') is False and progress.get('detection_stopped') is True
            and progress.get('wifi_stopped') is True, 'actual service workers not terminal')
    ui_exit = value.get('ui_exit')
    require(isinstance(ui_exit, dict) and ui_exit.get('session') == value['session']
            and ui_exit.get('identity') == identities['qt'] and type(ui_exit.get('exit_code')) is int
            and ui_exit['exit_code'] in (0, -signal.SIGTERM), 'missing actual Qt exit receipt')
    for client in value.get('clients', []):
        validate_client(client, client['terminal_at'])


def validate_release(release, actual=None):
    required = {'schema', 'release_id', 'review', 'root', 'coordinator', 'version', 'code_id',
                'installed', 'sources', 'helpers', 'bundles', 'wrappers', 'cfg', 'fixed_parameters'}
    require(isinstance(release, dict) and set(release) == required
            and type(release.get('schema')) is int and release['schema'] == 1,
            'incomplete release-record schema')
    safe_id(release.get('release_id'))
    review = release['review']
    require(isinstance(review, dict) and set(review) == {'decision', 'reviewer', 'record_sha256'}
            and review['decision'] == 'approved-for-validation' and isinstance(review['reviewer'], str)
            and review['reviewer'].strip() and re.fullmatch('[0-9a-f]{64}', review['record_sha256']), 'review approval record unavailable')
    require(release['root'] == APP_ROOT.as_posix() and release['coordinator'] == COORDINATOR.as_posix()
            and release['version'] == VERSION and re.fullmatch('[0-9a-f]{64}', release['code_id']), 'wrong release root/version/code')
    fixed = {'conf': .35, 'nms': .45, 'skip': 0, 'result_ttl': 1.0, 'capture_request': [640, 480],
             'model': 'helmet_416x416.rknn', 'profile': 'responsive'}
    parameters = release['fixed_parameters']
    require(parameters == fixed, 'release fixed parameter mismatch')
    require(type(parameters['conf']) is float and type(parameters['nms']) is float
            and type(parameters['skip']) is int and type(parameters['result_ttl']) is float
            and type(parameters['capture_request']) is list
            and all(type(x) is int for x in parameters['capture_request']),
            'fixed parameters have wrong numeric type')
    installed = release['installed']
    require(isinstance(installed, dict) and installed and {'src/app/' + name for name in CODE_FILES}
            | {'src/pipeline_runtime.py', 'src/sources.py', 'models/helmet_416x416.rknn', 'input/construction-replay.avi'} <= installed.keys(),
            'installed source/model/input inventory incomplete')
    for name, digest in installed.items():
        clean_relative(name)
        require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'invalid installed SHA')
    require(installed['models/helmet_416x416.rknn'] == MODEL_SHA
            and installed['input/construction-replay.avi'] == REPLAY_SHA, 'fixed model/replay identity changed')
    code = hashlib.sha256()
    for name in CODE_FILES:
        code.update(name.encode('ascii') + b'\0')
        code.update(bytes.fromhex(installed['src/app/' + name]))
    require(code.hexdigest() == release['code_id'], 'reviewed code_id does not match installed sources')
    sources = release['sources']
    require(isinstance(sources, dict) and set(sources) == {x for x in installed if x.startswith('src/')}, 'source-to-install map incomplete')
    for name, record in sources.items():
        require(isinstance(record, dict) and set(record) == {'path', 'sha256'} and isinstance(record['path'], str)
                and record['sha256'] == installed[name], 'source/installed fingerprint mismatch')
    require(isinstance(release['helpers'], dict) and set(release['helpers']) == {'verifier', 'native_keyboard'}, 'helper identity missing')
    require(isinstance(release['bundles'], dict) and set(release['bundles']) == {'payload', 'internal', 'external'}, 'bundle identity missing')
    for record in [*release['helpers'].values(), release['cfg']]:
        require(isinstance(record, dict) and set(record) == {'path', 'sha256'}
                and isinstance(record['path'], str) and re.fullmatch('[0-9a-f]{64}', record['sha256']), 'invalid file fingerprint')
    require(release['cfg']['path'] == '/opt/ui/src/ATK-DLRK3568/apk3.cfg', 'wrong actual vendor config path')
    for bundle in release['bundles'].values():
        require(isinstance(bundle, dict) and set(bundle) == {'path', 'sha256', 'members', 'installed_map'}
                and isinstance(bundle['path'], str) and re.fullmatch('[0-9a-f]{64}', bundle['sha256'])
                and isinstance(bundle['members'], dict) and bundle['members'] and isinstance(bundle['installed_map'], dict)
                and set(bundle['installed_map']) == set(installed), 'bundle schema/install mapping incomplete')
        for name, digest in bundle['members'].items():
            clean_relative(name)
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'invalid bundle member digest')
        for name, member in bundle['installed_map'].items():
            require(bundle['members'].get(member) == installed[name], 'bundle payload/install identity mismatch')
    require(isinstance(release['wrappers'], dict) and set(release['wrappers']) == set(WRAPPERS), 'two wrapper identities required')
    for tab, wrapper in release['wrappers'].items():
        target = (APP_ROOT / 'vendor-entry' / ('helmet_app' if tab == 'detection' else 'helmet_wifi')).as_posix()
        require(isinstance(wrapper, dict) and set(wrapper) == {'path', 'target', 'sha256'}
                and wrapper['path'] == WRAPPERS[tab].as_posix() and wrapper['target'] == target
                and re.fullmatch('[0-9a-f]{64}', wrapper['sha256']), 'normal wrapper identity mismatch')
    if actual is not None:
        require(actual == release, 'actual release fingerprints differ from reviewed record')
    return release


def fingerprint_release(release):
    validate_release(release)
    actual = strict_json(json.dumps(release))
    for name in actual['installed']:
        path = APP_ROOT / name
        no_links(path)
        actual['installed'][name] = sha(path)
    require({p.relative_to(APP_ROOT).as_posix() for p in (APP_ROOT / 'src').rglob('*') if p.is_file()}
            == set(actual['sources']), 'unreviewed installed source file')
    for record in [*actual['sources'].values(), *actual['helpers'].values(), actual['cfg']]:
        no_links(record['path'])
        record['sha256'] = sha(record['path'])
    require(Path(actual['helpers']['verifier']['path']).resolve() == Path(__file__).resolve()
            and Path(actual['helpers']['native_keyboard']['path']).resolve() == Path(__file__).with_name('native_keyboard.py').resolve(),
            'reviewed helper path differs from actual invoked helper')
    for bundle in actual['bundles'].values():
        no_links(bundle['path'])
        bundle['sha256'] = sha(bundle['path'])
        bundle['members'] = validated_zip_members(bundle['path'])
    for tab, wrapper in actual['wrappers'].items():
        path = WRAPPERS[tab]
        require(path.is_symlink() and os.readlink(path) == wrapper['target'], 'wrapper migration target mismatch')
        no_links(wrapper['target'])
        wrapper['sha256'] = sha(wrapper['target'])
        expected = '#!/bin/sh\nexec /usr/bin/python3 ' + str(APP_ROOT / 'src/app/launch.py') + ' --root ' + APP_ROOT.as_posix() + ' --tab ' + tab + ' "$@"\n'
        require(Path(wrapper['target']).read_text(encoding='utf-8') == expected, 'wrapper argument forwarding changed')
    validate_release(release, actual)
    return actual


def validated_zip_members(path):
    members, seen = {}, set()
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            raw_name = item.filename
            directory = item.is_dir()
            name = raw_name[:-1] if directory else raw_name
            clean_relative(name)
            require(name not in seen, 'duplicate normalized ZIP entry')
            seen.add(name)
            mode = item.external_attr >> 16
            require(not (item.flag_bits & 0x1), 'encrypted ZIP entry')
            require(not stat.S_ISLNK(mode), 'symlink ZIP entry')
            if directory:
                require(stat.S_IFMT(mode) in (0, stat.S_IFDIR), 'ZIP directory entry has wrong type')
                continue
            require(stat.S_IFMT(mode) in (0, stat.S_IFREG), 'non-regular ZIP file entry')
            members[name] = digest_bytes(archive.read(item))
    require(members, 'ZIP has no regular files')
    return members


def validate_keys(document, probe=False):
    require(isinstance(document, dict) and set(document) == {'schema', 'probe', 'source', 'release_id', 'code_id', 'groups', 'window_close_calibrated'}
            and document['schema'] == 1 and document['source'] in ('replay', 'camera'), 'incomplete calibrated keys schema')
    proof = document['probe']
    require(isinstance(proof, dict) and set(proof) == {'directory', 'run_id', 'png', 'png_sha256'}
            and re.fullmatch('[0-9a-f]{64}', proof['png_sha256']), 'keys lack prior actual PNG binding')
    safe_id(proof['run_id'])
    clean_relative(proof['png'])
    groups = document['groups']
    require(isinstance(groups, dict) and set(groups) == ({'navigate'} if probe else {'configure', 'start', 'hide', 'stop', 'quit'}), 'wrong ordered action groups')
    count = 0
    for name, actions in groups.items():
        require(isinstance(actions, list) and 1 <= len(actions) <= 64, 'empty/oversized input group')
        for action in actions:
            require(isinstance(action, dict) and set(action) <= {'key', 'chord', 'count', 'pause'}
                    and ('key' in action) != ('chord' in action), 'invalid finite input action')
            repeat = integer(action.get('count', 1), 1)
            require(repeat <= 20 and 0 <= finite(action.get('pause', .2)) <= 2, 'input pause/count outside budget')
            count += repeat
            if 'key' in action:
                require(action['key'] in ('Tab', 'Home', 'Up', 'Down', 'Space', 'Escape', 'Enter'), 'key outside approved whitelist')
                require(not probe or action['key'] not in ('Space', 'Enter'), 'navigation-only calibration forbids activation')
            else:
                require(action['chord'] == ['Shift', 'Tab'] or (action['chord'] == ['Alt', 'F4']
                        and name == 'hide' and not probe and document['window_close_calibrated'] is True), 'uncalibrated window-close chord')
    require(count <= 256 and type(document['window_close_calibrated']) is bool, 'input budget exceeded')
    return document


def load_keys(path, release, source, probe=False):
    document = validate_keys(read_json(path), probe)
    require(document['release_id'] == release['release_id'] and document['code_id'] == release['code_id']
            and document['source'] == source, 'keys release/source/code mismatch')
    proof = document['probe']
    directory = Path(proof['directory'])
    result = verify_directory(directory)
    require(result['passed'] and result.get('mode') == 'probe', 'keys prior probe does not verify')
    manifest = read_json(directory / 'manifest.json')
    require(manifest['run_id'] == proof['run_id'] and manifest['release_id'] == release['release_id']
            and manifest['code_id'] == release['code_id'] and manifest.get('source') == source
            and sha(directory / proof['png']) == proof['png_sha256'],
            'prior calibration source/PNG identity mismatch')
    require(any(row.get('png') == proof['png'] and row.get('png_sha256') == proof['png_sha256']
            for row in rows(directory / 'captures.jsonl')), 'PNG not actual verified probe capture')
    return document


def validate_bound_calibration(directory, document, release, source):
    validate_keys(document)
    require(document['source'] == source and document['code_id'] == release['code_id']
            and document['release_id'] == release['release_id'], 'calibration source/release/code mismatch')
    proof = document['probe']
    captured = Path(directory) / 'calibration-proof'
    result = verify_directory(captured)
    require(result['passed'] and result['mode'] == 'probe', 'missing/failed local immutable prior probe')
    manifest = read_json(captured / 'manifest.json')
    require(manifest.get('run_id') == proof['run_id'] and manifest.get('release_id') == release['release_id']
            and manifest.get('code_id') == release['code_id'] and manifest.get('source') == source
            and sha(captured / clean_relative(proof['png'])) == proof['png_sha256']
            and any(x.get('png') == proof['png'] and x.get('png_sha256') == proof['png_sha256']
                    for x in rows(captured / 'captures.jsonl')), 'local verified calibration PNG identity mismatch')


def parse_proc_stat(text):
    closing = text.rfind(')')
    require(closing > 0, 'invalid proc stat')
    fields = text[closing + 1:].split()
    require(len(fields) >= 22, 'truncated proc stat')
    return {'pid': int(text[:text.index('(')].strip()), 'state': fields[0], 'ppid': int(fields[1]),
            'cpu_ticks': int(fields[11]) + int(fields[12]), 'threads': int(fields[17]),
            'starttime': int(fields[19]), 'rss_pages': int(fields[21])}


def proc_read(pid):
    try:
        root = Path('/proc') / str(integer(pid, 1))
        value = parse_proc_stat((root / 'stat').read_text())
        value['uid'] = root.stat().st_uid
        value['argv'] = (root / 'cmdline').read_bytes().decode('utf-8').rstrip('\0').split('\0')
        value['rss_bytes'] = value['rss_pages'] * os.sysconf('SC_PAGE_SIZE')
        value['fds'] = len(list((root / 'fd').iterdir()))
        value['mapping_count'] = len((root / 'maps').read_text().splitlines())
        return value
    except (FileNotFoundError, ProcessLookupError):
        return None


def current_identity(pid):
    row = proc_read(pid)
    return None if row is None or row['state'] in ('Z', 'X', 'x') else {key: row[key] for key in IDENTITY_FIELDS}


def lease_state(path, expected=None):
    import fcntl
    no_links(path)
    info = path.stat()
    result = {'device': info.st_dev, 'inode': info.st_ino, 'holders': [], 'lock_available': False}
    require(expected is None or all(result[k] == expected[k] for k in ('device', 'inode')), 'lease inode changed')
    for root in Path('/proc').glob('[0-9]*'):
        try:
            held = any((entry.stat().st_dev, entry.stat().st_ino) == (info.st_dev, info.st_ino) for entry in (root / 'fd').iterdir())
            if held:
                owner = current_identity(int(root.name))
                if owner is not None:
                    result['holders'].append(owner)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise ValueError('lease holder inventory unknown') from None
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result['lock_available'] = True
        except BlockingIOError:
            pass
    finally:
        os.close(fd)
    return result


def sys_text(path):
    return Path(path).read_text().strip()


def npu_root():
    matches = sorted(Path('/sys/class/devfreq').glob('*npu*'))
    require(len(matches) == 1, 'NPU sysfs identity unavailable')
    return matches[0]


def network_evidence():
    interfaces = []
    for name in sorted(Path('/sys/class/net').iterdir()):
        interfaces.append({'name': name.name, 'flags': int(sys_text(name / 'flags'), 16),
                           'operstate': sys_text(name / 'operstate')})
    result = subprocess.run(['ip', '-j', 'address', 'show'], capture_output=True, timeout=5, check=False)
    require(result.returncode == 0, 'network address inventory unavailable')
    return {'namespace': os.readlink('/proc/self/ns/net'), 'interfaces': interfaces,
            'addresses': strict_json(result.stdout.decode()), 'observed_at': time.monotonic()}


def network_identity(value):
    return {key: value[key] for key in ('namespace', 'interfaces', 'addresses')}


def enter_offline_namespace():
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.unshare(0x40000000) == 0, 'child-only CLONE_NEWNET unavailable')
    import fcntl
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        # The new namespace owns this socket. Assign only its loopback address;
        # the parent namespace/socket/interface set is untouched.
        for operation, address in ((0x8916, '127.0.0.1'), (0x891c, '255.0.0.0')):
            request = struct.pack('16sH2s4s8s', b'lo', socket.AF_INET, bytes(2), socket.inet_aton(address), bytes(8))
            fcntl.ioctl(sock.fileno(), operation, request)
        request = struct.pack('16sH14s', b'lo', 1, bytes(14))
        fcntl.ioctl(sock.fileno(), 0x8914, request)
    value = network_evidence()
    require([row['name'] for row in value['interfaces']] == ['lo']
            and value['interfaces'][0]['flags'] & 1, 'offline child interface inventory invalid')
    return value


def preflight(source):
    require(sys.platform.startswith('linux'), 'runtime requires actual Linux board; use verify on host')
    suspicious = []
    for root in Path('/proc').glob('[0-9]*'):
        row = proc_read(int(root.name))
        if row is None or row['pid'] == os.getpid() or row['state'] in ('Z', 'X', 'x'):
            continue
        argv = row['argv']
        if any(Path(x).name in ('helmet_realtime.py', 'dump_dets.py', 'bench.py', 'service.py', 'resident.py', 'launch.py') for x in argv):
            suspicious.append({k: row[k] for k in IDENTITY_FIELDS})
        for fd in (root / 'fd').iterdir():
            try:
                target = os.readlink(fd)
                if re.search(r'/(rknpu|galcore|dri/renderD\d+)$', target):
                    suspicious.append({k: row[k] for k in IDENTITY_FIELDS})
                    break
            except (FileNotFoundError, ProcessLookupError):
                continue
    require(not suspicious, 'legacy/NPU owner prevents idle preflight')
    old = read_json(COORDINATOR / 'record.json') if (COORDINATOR / 'record.json').exists() else None
    if old:
        for role in ('manager', 'service', 'ui'):
            if old.get(role):
                require(current_identity(identity(old[role])['pid']) != old[role], 'coordinator owner remains live')
    leases = {}
    for name in ('startup.lock', 'manager.lock', 'resource.lock'):
        path = COORDINATOR / name
        if path.exists():
            leases[name] = lease_state(path)
            require(leases[name]['lock_available'] and not leases[name]['holders'], 'coordinator lease busy or inherited')
    camera = None
    if source == 'camera':
        device = Path('/dev/video10')
        require(device.is_char_device(), 'camera device missing')
        result = subprocess.run(['v4l2-ctl', '--device=/dev/video10', '--all', '--list-formats-ext'], capture_output=True, timeout=5)
        require(result.returncode == 0, 'camera capability inspection failed')
        text = result.stdout.decode('utf-8')
        require('Video Capture' in text and 'Streaming' in text, 'camera lacks actual capture capabilities')
        info = device.stat()
        camera = {'path': str(device), 'device': info.st_dev, 'inode': info.st_ino, 'rdev': info.st_rdev,
                  'sysfs_target': str(Path('/sys/class/video4linux/video10/device').resolve()),
                  'command': ['v4l2-ctl', '--device=/dev/video10', '--all', '--list-formats-ext'],
                  'exit_code': result.returncode, 'stdout': text, 'stderr': result.stderr.decode('utf-8'),
                  'capture_request': [640, 480], 'scope': 'capability/identity; actual captured dimensions require live preview'}
    return {'suspicious_owners': suspicious, 'coordinator_record': old, 'leases': leases,
            'camera': camera, 'governor': sys_text(npu_root() / 'governor'), 'observed_at': time.monotonic()}


class Owner:
    def __init__(self, release):
        self.release = release
        self.ready = None
        self.lease = None
        self.last_lease = None
        self._token = None
        record = read_json(COORDINATOR / 'record.json')
        self._validate_record(record)
        self.session = record['session']
        require(re.fullmatch('[0-9a-f]{32}', self.session), 'invalid private session')
        self.workspace = APP_ROOT / 'app-state' / ('session-' + self.session)
        require(record['workspace'] == str(self.workspace), 'private workspace mismatch')
        no_links(self.workspace, directory=True)
        self.identities = {'manager': identity(record['manager'])}
        self.initial = record

    def _validate_record(self, record):
        require(isinstance(record, dict) and record.get('protocol') == 1 and record.get('version') == VERSION
                and record.get('root') == APP_ROOT.as_posix() and record.get('code_id') == self.release['code_id']
                and record.get('mode') == 'resident' and record.get('verification_enabled') is True,
                'private resident release mismatch')
        require(record.get('phase') in ('starting', 'running', 'stopping', 'failed', 'completed'),
                'private resident phase invalid')
        return record

    def assert_control_alive(self):
        record = self._validate_record(read_json(COORDINATOR / 'record.json'))
        require(record.get('session') == self.session and record.get('workspace') == str(self.workspace)
                and identity(record.get('manager')) == self.identities['manager'],
                'resident control owner identity changed')
        row = proc_read(self.identities['manager']['pid'])
        script = str(APP_ROOT / 'src/app/resident.py')
        require(row is not None and row['state'] not in ('Z', 'X', 'x')
                and {k: row[k] for k in IDENTITY_FIELDS} == self.identities['manager']
                and script in row['argv'] and '--root' in row['argv']
                and row['argv'][row['argv'].index('--root') + 1] == APP_ROOT.as_posix(),
                'resident control manager is not live')
        return record

    def wait_ready(self, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            record = self.assert_control_alive()
            if record.get('service') is not None and record.get('ui') is not None:
                require(record.get('phase') == 'running', 'resident readiness phase invalid')
                self.identities = {role: identity(record['ui' if role == 'qt' else role])
                                   for role in OWNER_ROLES}
                self.identities['observer'] = current_identity(os.getpid())
                ready_path = self.workspace / 'ready.json'
                token_path = self.workspace / 'token'
                try:
                    self.ready = read_json(ready_path)
                    token_info = no_links(token_path)
                    token = token_path.read_text(encoding='ascii').strip()
                except FileNotFoundError:
                    time.sleep(.05)
                    continue
                require(self.ready.get('ready') is True and self.ready.get('host') == '127.0.0.1'
                        and self.ready.get('session') == self.session
                        and self.ready.get('identity') == self.identities['service']
                        and self.ready.get('pid') == self.identities['service']['pid'],
                        'actual ready owner mismatch')
                integer(self.ready.get('port'), 1)
                require(self.ready['port'] <= 65535, 'ready port invalid')
                self.lease = {name: integer(record['lease'][name]) for name in ('device', 'inode')}
                require(self.ready.get('lease') == self.lease
                        and record['lease'].get('service_inherited') is True,
                        'ready inherited lease mismatch')
                require(token_info.st_uid == os.getuid() and token_info.st_mode & 0o077 == 0,
                        'private token ownership unsafe')
                require(re.fullmatch('[A-Za-z0-9_-]{32,120}', token), 'invalid private token')
                self._token = token
                self.initial = record
                self.assert_alive()
                return self
            current = current_identity(self.identities['manager']['pid'])
            require(current == self.identities['manager'], 'resident manager exited before ready handoff')
            time.sleep(.05)
        raise ValueError('trusted resident ready handoff timed out')

    def assert_alive(self):
        record = self.assert_control_alive()
        require(set(self.identities) == OWNER_ROLES | {'observer'}, 'trusted resident owners unavailable')
        for role, expected in self.identities.items():
            row = proc_read(expected['pid'])
            require(row is not None and row['state'] not in ('Z', 'X', 'x')
                    and {k: row[k] for k in IDENTITY_FIELDS} == expected, 'owned process no longer live: ' + role)
            if role in ('manager', 'service'):
                script = str(APP_ROOT / 'src/app' / ('resident.py' if role == 'manager' else 'service.py'))
                require(script in row['argv'] and '--root' in row['argv']
                        and row['argv'][row['argv'].index('--root') + 1] == APP_ROOT.as_posix(), 'owned command identity mismatch')
            elif role == 'qt':
                require(any(x == str(self.workspace / ('Ui-' + record['application']['ui']['instance'] + '.qml'))
                            for x in row['argv']), 'Qt wrapper identity mismatch')
        lease = lease_state(COORDINATOR / 'resource.lock', self.lease)
        require(not lease['lock_available'] and all(self.identities[role] in lease['holders'] for role in OWNER_ROLES),
                'actual inherited owners missing lease')
        self.last_lease = lease
        return record


class LocalAPI:
    def __init__(self, owner, directory):
        self.owner, self.directory = owner, directory
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, route, stage):
        require(route in GET_ROUTES, 'observer HTTP operation forbidden')
        self.owner.assert_alive()
        row = {'method': 'GET', 'route': route, 'stage': stage, 'request_started': time.monotonic(),
               'status': None, 'headers': [], 'response_bytes_base64': '', 'response_text': '', 'body': None}
        request = urllib.request.Request('http://127.0.0.1:' + str(self.owner.ready['port']) + route,
                                         headers={'X-Helmet-Token': self.owner._token}, method='GET')
        try:
            try:
                response = self.opener.open(request, timeout=2)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                data = response.read(8 * 1024 * 1024 + 1)
                headers = [list(x) for x in response.headers.items()]
                row['status'] = response.code
            require(len(data) <= 8 * 1024 * 1024, 'HTTP raw response exceeds bound')
            require(self.owner._token.encode() not in data
                    and self.owner._token not in json.dumps(headers), 'sensitive response cannot be exported')
            row['headers'] = headers
            row['response_bytes_base64'] = base64.b64encode(data).decode('ascii')
            row['response_text'] = data.decode('utf-8')
            row['body'] = strict_json(row['response_text'])
        except (OSError, ValueError, UnicodeError):
            row['error'] = 'http_or_sensitive_or_decode_failure'
        row['response_finished'] = time.monotonic()
        append_json(self.directory / 'http.jsonl', row)
        validate_http(row)
        return row


def resource_sample(owner, stage):
    start = time.monotonic()
    record = owner.assert_alive()
    processes = {}
    for role, expected in owner.identities.items():
        row = proc_read(expected['pid'])
        require(row is not None and current_identity(row['pid']) == expected, 'resource owner unknown')
        processes[role] = {name: row[name] for name in ('pid', 'starttime', 'uid', 'cpu_ticks', 'rss_bytes', 'threads', 'fds', 'mapping_count')}
    temperatures = [{'path': str(path), 'value': sys_text(path)} for path in sorted(Path('/sys/class/thermal').glob('thermal_zone*/temp'))]
    root = npu_root()
    result = {'stage': stage, 'request_started': start, 'processes': processes,
              'lease': record['lease'], 'lease_observed': owner.last_lease,
              'temperature': temperatures, 'npu_frequency': sys_text(root / 'cur_freq'),
              'governor': sys_text(root / 'governor'), 'load': sys_text('/proc/loadavg'),
              'record': record}
    result['response_finished'] = time.monotonic()
    return result


def validate_resource_row(row, identities, lease, governor=None):
    require(isinstance(row, dict) and isinstance(row.get('processes'), dict)
            and set(row['processes']) == set(identities), 'resource role missing')
    for role, expected in identities.items():
        value = row['processes'][role]
        require(isinstance(value, dict)
                and {k: value.get(k) for k in IDENTITY_FIELDS} == expected,
                'resource owner identity changed')
        for name in ('cpu_ticks', 'rss_bytes', 'threads', 'fds', 'mapping_count'):
            integer(value.get(name))
    row_lease = row.get('lease')
    require(isinstance(row_lease, dict)
            and all(row_lease.get(k) == lease[k] for k in ('device', 'inode'))
            and row_lease.get('service_inherited') is True
            and row_lease.get('service_alive') is True, 'resource lease missing')
    actual_lease = row.get('lease_observed')
    require(isinstance(actual_lease, dict) and actual_lease.get('lock_available') is False
            and all(actual_lease.get(k) == lease[k] for k in ('device', 'inode'))
            and all(identities[role] in actual_lease.get('holders', []) for role in OWNER_ROLES),
            'raw actual inherited FD lease owners unavailable')
    require(isinstance(row.get('temperature'), list) and row['temperature'] and row.get('load')
            and row.get('npu_frequency')
            and (governor is None or row.get('governor') == governor),
            'system resource evidence incomplete/changed')
    require(isinstance(row['npu_frequency'], str) and row['npu_frequency'].isdigit(),
            'invalid NPU frequency raw value')
    for temperature in row['temperature']:
        require(isinstance(temperature, dict) and isinstance(temperature.get('path'), str)
                and isinstance(temperature.get('value'), str)
                and re.fullmatch(r'-?\d+', temperature['value']), 'invalid raw temperature')
    loads = row['load'].split()
    require(len(loads) == 5 and all(finite(float(x)) >= 0 for x in loads[:3]),
            'invalid/nonfinite load raw values')


def validate_resources(samples, identities, lease, first, last):
    validate_schedule(samples, 3, first, last)
    previous = None
    for row in samples:
        validate_resource_row(row, identities, lease, samples[0].get('governor'))
        for role, expected in identities.items():
            value = row['processes'][role]
            if previous:
                require(value['cpu_ticks'] >= previous['processes'][role]['cpu_ticks'], 'CPU resource counter regressed')
        previous = row
    return {role: {'rss_min_bytes': min(row['processes'][role]['rss_bytes'] for row in samples),
                   'rss_max_bytes': max(row['processes'][role]['rss_bytes'] for row in samples)} for role in identities}


def validate_config(row, source):
    value = row['body']
    require(value.get('detection', {}).get('selection') == selection(source)
            and value.get('selection_available') == {'model': True, 'source': True, 'profile': True}, 'GET actual selection unavailable/mismatched')
    for name, collection in (('model', 'models'), ('source', 'sources'), ('profile', 'profiles')):
        require(selection(source)[name] in {x['id'] for x in value.get(collection, [])}, 'GET selected configuration missing')
    running(value['detection'], source)


def validate_offline(network, requested, observer_identity=None):
    require(isinstance(network, dict) and set(network) == {'requested', 'parent_before', 'parent_after', 'child', 'child_terminal'}, 'network evidence incomplete')
    require(network.get('requested') is requested, 'offline request mismatch')
    if requested:
        terminal = network['child_terminal']
        require(network_identity(network['parent_before']) == network_identity(network['parent_after'])
                and network['child']['namespace'] != network['parent_before']['namespace']
                and [x['name'] for x in network['child']['interfaces']] == ['lo']
                and network['child']['interfaces'][0]['flags'] & 1
                and all(x.get('ifname') == 'lo' for x in network['child']['addresses'])
                and isinstance(terminal, dict)
                and set(terminal) == {'identity', 'exit_code', 'observed', 'checked_at'}
                and identity(terminal['identity']) == observer_identity
                and type(terminal['exit_code']) is int and terminal['exit_code'] == 0
                and terminal['observed'] is None and finite(terminal['checked_at']) >= 0,
                'genuine child offline/parent unchanged/terminal identity gate failed')


def validate_decoder(decoder, first_client_started):
    require(isinstance(decoder, dict)
            and set(decoder) == {'request_started', 'response_finished', 'versions'},
            'decoder preparation schema incomplete')
    start, finish = boundaries(decoder)
    versions = decoder['versions']
    require(isinstance(versions, dict) and set(versions) == {'cv2', 'numpy'}
            and all(isinstance(versions[name], str) and versions[name].strip()
                    for name in ('cv2', 'numpy'))
            and finish <= finite(first_client_started), 'decoder imported after launcher')


def validate_preflight(pre):
    require(isinstance(pre, dict)
            and set(pre) == {'suspicious_owners', 'coordinator_record', 'leases',
                             'camera', 'governor', 'observed_at'},
            'idle preflight schema incomplete')
    require(pre['suspicious_owners'] == [] and isinstance(pre['leases'], dict)
            and (pre['coordinator_record'] is None or isinstance(pre['coordinator_record'], dict))
            and isinstance(pre['governor'], str) and pre['governor']
            and finite(pre['observed_at']) >= 0, 'idle preflight evidence invalid')
    for name, lease in pre['leases'].items():
        require(isinstance(name, str) and isinstance(lease, dict)
                and set(lease) == {'device', 'inode', 'holders', 'lock_available'}
                and lease['holders'] == [] and lease['lock_available'] is True,
                'idle preflight owner/lease busy')


def validate_probe_journals(directory, identities, lease, governor):
    resources = rows(directory / 'resources.jsonl')
    idle_resources = [row for row in resources if row.get('stage') == 'initial_idle']
    require(idle_resources, 'probe has no initial-idle resource sample')
    for row in resources:
        boundaries(row)
        validate_resource_row(row, identities, lease, governor)

    clients = rows(directory / 'clients.jsonl')
    for client in clients:
        validate_client(client, finite(client.get('terminal_at')))
    shows = [row for row in clients if row.get('control') is None]
    navigation = (Path(directory) / 'keys.json').is_file()
    expected_tabs = (['detection', 'detection', 'wifi', 'detection']
                     if navigation else ['detection', 'wifi', 'detection'])
    require([row.get('tab') for row in shows] == expected_tabs,
            'probe detection/WiFi/detection wrapper evidence incomplete')
    require(all(row.get('same_owners') is True for row in shows[1:]), 'probe wrapper reuse unproved')
    cleanup = [row for row in clients if row.get('control') == 'quit']
    require(len(cleanup) == 1 and cleanup[0].get('tab') == 'detection', 'probe cleanup client missing')

    actions = rows(directory / 'actions.jsonl')
    probe_cleanup = [row for row in actions if row.get('group') == 'probe-cleanup']
    require(len(probe_cleanup) == 1, 'probe cleanup action missing/duplicated')
    row = probe_cleanup[0]
    require(row.get('kind') == 'normal-control' and row.get('confirmed') is False
            and finite(row.get('started')) <= finite(row.get('finished'))
            and row.get('reason') == 'declared_software_cleanup_not_UI_Quit',
            'probe cleanup action invalid')

    captures = rows(directory / 'captures.jsonl')
    require([row.get('receipt', {}).get('tab') for row in captures]
            == expected_tabs,
            'probe per-show detection/WiFi/detection captures incomplete')


def verify_directory(directory):
    directory = Path(directory)
    result = {'schema': 1, 'passed': False, 'mode': None,
              'gates': {name: False for name in ('schema', 'inventory', 'release', 'release_binding', 'owner_schema',
                        'idle_preflight', 'decoder_preparation', 'private_identity', 'http_raw', 'terminal', 'calibration', 'offline')},
              'failures': [],
              'limits': ['sampled UI software ACK, not physical focus/touch/display', 'no direct SDK release or no-leak proof',
                         'camera capture acceptance, WiFi authentication and power remain separate']}
    def gate(name, check):
        try:
            value = check()
            result['gates'][name] = True
            return value
        except (ValueError, KeyError, TypeError, AttributeError, OSError, UnicodeError, IndexError):
            result['gates'][name] = False
            result['failures'].append(name + ':unmet_or_invalid_raw')
            return None
    manifest = gate('schema', lambda: read_json(directory / 'manifest.json'))
    if not isinstance(manifest, dict):
        return result
    result['mode'] = manifest.get('mode')
    gate('schema', lambda: require(manifest.get('schema') == 1 and manifest.get('mode') in ('probe', 'run')
                                  and safe_id(manifest.get('run_id')) and manifest.get('source') in ('replay', 'camera')
                                  and type(manifest.get('offline')) is bool and type(manifest.get('failures')) is list
                                  and not manifest['failures'] and type(manifest.get('interrupts')) is list
                                  and not manifest['interrupts'], 'manifest schema/failure/interrupt'))
    gate('inventory', lambda: validate_inventory(directory, manifest.get('inventory')))
    release = gate('release', lambda: validate_release(read_json(directory / 'release.json'), read_json(directory / 'identities.json')))
    if release is None:
        return result
    gate('release_binding', lambda: require(manifest.get('release_id') == release['release_id']
                                           and manifest.get('code_id') == release['code_id'], 'manifest release identity'))
    identities = manifest.get('owners', {})
    gate('owner_schema', lambda: require(set(identities) == OWNER_ROLES | {'observer'}
                                        and all(identity(x) for x in identities.values()), 'owner schema incomplete'))
    session = manifest.get('session')
    pre = gate('idle_preflight', lambda: read_json(directory / 'preflight.json'))
    if pre is not None:
        gate('idle_preflight', lambda: validate_preflight(pre))
    gate('decoder_preparation', lambda: validate_decoder(
        read_json(directory / 'decoder.json'), rows(directory / 'clients.jsonl')[0]['request_started']))
    initial = gate('private_identity', lambda: read_json(directory / 'initial-record.json'))
    if initial is not None:
        def check_private():
            ready = read_json(directory / 'ready.json')
            start = read_json(directory / 'start.json')
            require(initial.get('session') == session and initial.get('root') == APP_ROOT.as_posix()
                    and initial.get('code_id') == release['code_id'] and initial.get('version') == VERSION
                    and all(initial.get('ui' if role == 'qt' else role) == identities[role] for role in OWNER_ROLES), 'initial owner receipt mismatch')
            require(ready.get('ready') is True and ready.get('host') == '127.0.0.1'
                    and type(ready.get('port')) is int and 1 <= ready['port'] <= 65535
                    and ready.get('pid') == identities['service']['pid'] and ready.get('identity') == identities['service']
                    and ready.get('session') == session and ready.get('lease') == manifest['lease'], 'actual ready receipt incomplete/mismatched')
            require(start.get('kind') == 'resident-start-v4' and start.get('session') == session
                    and start.get('root') == APP_ROOT.as_posix() and start.get('version') == VERSION
                    and start.get('code_id') == release['code_id']
                    and all(start.get('ui' if role == 'qt' else role) == identities[role] for role in OWNER_ROLES), 'actual start receipt incomplete/mismatched')
            ui = read_json(directory / ('private/ui-start-' + initial['application']['ui']['instance'] + '.json'))
            require(ui.get('identity') == identities['qt'] and ui.get('session') == session
                    and ui.get('ui_instance') == initial['application']['ui']['instance'], 'actual Qt startup identity mismatch')
        gate('private_identity', check_private)
    http = gate('http_raw', lambda: rows(directory / 'http.jsonl'))
    if http is None:
        return result
    def check_http():
        require(http, 'empty HTTP raw')
        previous = -1
        for row in http:
            validate_http(row)
            require(row['request_started'] >= previous, 'HTTP timeline regression')
            previous = row['response_finished']
    gate('http_raw', check_http)
    terminal = gate('terminal', lambda: read_json(directory / 'terminal.json'))
    if terminal is not None:
        gate('terminal', lambda: (validate_terminal(terminal, identities),
                                 require(terminal.get('code_id') == release['code_id'] and terminal.get('session') == session
                                         and terminal['lease']['device'] == manifest['lease']['device']
                                         and terminal['lease']['inode'] == manifest['lease']['inode']
                                         and read_json(directory / 'exit.json') == terminal['exit_receipt']
                                         and read_json(directory / 'service-state.json') == terminal['service_state']
                                         and read_json(directory / ('private/ui-exit-' + terminal['ui_exit']['ui_instance'] + '.json')) == terminal['ui_exit'],
                                         'terminal raw mismatch')))
    captures = gate('calibration', lambda: rows(directory / 'captures.jsonl'))
    def check_captures():
        require(captures and any(x.get('purpose') == 'calibration' for x in captures), 'missing calibration capture')
        for capture in captures:
            application_row = http[capture['application_index']]
            application = application_row['body']
            current = None if capture['status_index'] is None else detection(http[capture['status_index']])
            require(application.get('last_render') == capture['receipt'], 'capture not current HTTP last_render')
            validate_ack(application, session, identities, True, capture['receipt']['tab'],
                         received_before=application_row['response_finished'])
            validate_render_metadata(capture['receipt'], application, current,
                                     'probe' if capture['purpose'] == 'calibration' else 'run',
                                     completed_before=application_row['response_finished'])
            data = (directory / clean_relative(capture['png'])).read_bytes()
            require(digest_bytes(data) == capture['png_sha256'], 'capture PNG hash mismatch')
            decode_image(data, [capture['receipt']['width'], capture['receipt']['height']])
            require(capture['receipt'] in render_lines(directory / capture['ui_log']), 'actual UI log render receipt missing')
    gate('calibration', check_captures)
    gate('offline', lambda: validate_offline(read_json(directory / 'network.json'),
                                             manifest.get('offline') is True,
                                             identities.get('observer')))
    if manifest.get('mode') == 'probe':
        gate('probe_cleanup', lambda: require(manifest.get('cleanup_kind') == 'normal-control-quit'
                                              and manifest.get('real_ui_quit') is False, 'probe cleanup declaration'))
        def check_probe_evidence():
            validate_probe_journals(directory, identities, manifest['lease'], pre['governor'])
            idle_status = [row for row in http if row.get('route') == '/api/status']
            require(idle_status and all(detection(row).get('running') is False for row in idle_status),
                    'probe HTTP lacks actual idle detection evidence')
        gate('probe_evidence', check_probe_evidence)
    else:
        source = manifest.get('source')
        bounds = manifest.get('formal', {})
        formal = [x for x in http if x.get('stage') == 'hidden']
        statuses = [x for x in formal if x['route'] == '/api/status']
        metric = gate('duration', lambda: validate_statuses(statuses, source))
        if metric is not None:
            result['measurement'] = metric
            gate('settled_hidden_ack', lambda: (validate_formal_ack(http[bounds['hidden_ack_index']], statuses[0], session, identities),
                                               require(http[bounds['hidden_ack_index']]['body']['ack']['received_at'] == bounds['hidden_ack_received_at'],
                                                       'formal settled ACK raw time mismatch')))
            gate('hidden_duration', lambda: require(30 <= finite(manifest.get('requested_hidden_seconds')) <= 3600
                                                    and metric['hidden_lower_seconds'] >= manifest['requested_hidden_seconds']
                                                    and bounds.get('first_request_started') == statuses[0]['request_started']
                                                    and bounds.get('first_response_finished') == statuses[0]['response_finished']
                                                    and bounds.get('last_request_started') == statuses[-1]['request_started']
                                                    and bounds.get('last_response_finished') == statuses[-1]['response_finished'], 'requested conservative hidden duration unmet'))
            result['gates']['long_hidden'] = metric['long_hidden'] if manifest['requested_hidden_seconds'] >= 1800 else None
            gate('detected_fps', lambda: require(metric['fps']['detected'] >= 10, 'fixed detected FPS gate'))
            gate('output_fps', lambda: require(metric['fps']['output'] >= 10, 'fixed output FPS gate'))
            apps = [x for x in formal if x['route'] == '/api/application']
            gate('hidden_ui', lambda: (validate_hidden_apps(apps, session, identities),
                                      validate_schedule(apps, 3, statuses[0]['request_started'], statuses[-1]['response_finished'])))
            gate('formal_clients', lambda: [validate_client(x, statuses[0]['request_started']) for x in rows(directory / 'clients.jsonl')
                                            if x['request_started'] < statuses[0]['request_started']])
            previews = [x for x in formal if x['route'] == '/api/preview']
            def check_previews():
                validate_schedule(previews, 10, statuses[0]['request_started'], statuses[-1]['response_finished'])
                for row in previews:
                    previous = [x for x in statuses if x['response_finished'] <= row['request_started']]
                    require(previous, 'preview before formal baseline')
                    validate_preview(row, detection(previous[-1]))
            gate('preview', check_previews)
            config_rows = [x for x in formal if x['route'] == '/api/config']
            gate('configuration', lambda: (require(config_rows, 'missing formal configuration'),
                                          [validate_config(row, source) for row in config_rows],
                                          require(all(all(row['body']['detection'][name] == detection(statuses[0])[name]
                                                          for name in ('generation', 'source_epoch', 'selection')) for row in config_rows),
                                                  'formal configuration generation/epoch changed')))
            def check_resources():
                samples = [x for x in rows(directory / 'resources.jsonl') if x['stage'] == 'hidden']
                value = validate_resources(samples, identities, manifest['lease'], statuses[0]['request_started'], statuses[-1]['response_finished'])
                require(samples[0]['governor'] == terminal['governor_before'], 'governor differs from preflight/terminal')
                return value
            result['resources'] = gate('resources', check_resources)
        actions = gate('ui_actions', lambda: rows(directory / 'actions.jsonl'))
        if actions is not None:
            gate('ui_actions', lambda: validate_action_gates(actions, identities))
            def check_action_evidence():
                for action in actions:
                    if action.get('kind') != 'native-keyboard':
                        continue
                    if action.get('evidence_source') == 'private-record':
                        require(action['group'] == 'quit' and action.get('inputs'), 'private witness outside real UI Quit')
                        proof = rows(directory / 'transitions.jsonl')[action['evidence']]
                        validate_action_proof_timing(action, proof)
                        validate_quit_witness(proof, action['inputs'][0]['sent_at'], session, identities, release['code_id'])
                        continue
                    proof = http[action['evidence']]
                    validate_action_proof_timing(action, proof)
                    if action['group'] == 'hide':
                        validate_ack(proof['body'], session, identities, False, 'detection',
                                     received_before=proof['response_finished'])
                    elif action['group'] == 'start':
                        running(detection(proof), source)
                        require(detection(proof)['generation'] != manifest.get('idle_generation'), 'Start did not create new generation')
                    elif action['group'] == 'stop':
                        require(detection(proof).get('running') is False, 'UI stop did not finish workers')
                    elif action['group'] == 'quit':
                        require(proof['body'].get('phase') == 'stopping', 'real UI quit did not show stopping')
                    else:
                        require(detection(proof).get('running') is False, 'configure was not idle')
            gate('ui_action_evidence', check_action_evidence)
        def check_visible():
            before = [x for x in http if x.get('stage') == 'visible_before' and x['route'] == '/api/status']
            after = [x for x in http if x.get('stage') == 'visible_after' and x['route'] == '/api/status']
            require(len(before) >= 2 and len(after) >= 2, 'visible detection observations missing')
            validate_active_timeline(before, statuses, after, source)
            require(after[-1]['request_started'] - before[0]['response_finished'] >= 120, 'short total observation <120s')
            require(any(x.get('purpose') == 'before' for x in captures) and any(x.get('purpose') == 'after' for x in captures), 'missing actual displayed detection captures')
            shows = [x for x in rows(directory / 'clients.jsonl') if x.get('control') is None]
            require({x['tab'] for x in shows} == {'detection', 'wifi'} and all(x.get('same_owners') is True for x in shows[1:]), 'two icon reuse unproved')
            require(not any(bounds['first_request_started'] <= x['request_started'] <= bounds['last_response_finished']
                            for x in shows), 're-show occurred inside formal interval')
            after_shows = [x for x in shows if x['request_started'] > bounds['last_response_finished']]
            require(after_shows and max(x['response_finished'] for x in formal) <= min(x['request_started'] for x in after_shows),
                    'last formal sample incomplete before re-show')
        gate('visible_and_reuse', check_visible)
        gate('keys_binding', lambda: validate_bound_calibration(directory, read_json(directory / 'keys.json'), release, source))
        gate('native_input_binding', lambda: validate_native_inputs(actions, read_json(directory / 'keys.json'), identities))
        gate('real_ui_cleanup', lambda: require(manifest.get('real_ui_quit') is True and manifest.get('cleanup_kind') == 'real-qml-quit'
                                               and all(x.get('control') is None for x in rows(directory / 'clients.jsonl')),
                                               'Unix/emergency controls cannot satisfy real UI cleanup'))
        if source == 'camera':
            gate('camera_capability', lambda: require(pre['camera']['path'] == '/dev/video10'
                                                    and pre['camera']['exit_code'] == 0 and 'Video Capture' in pre['camera']['stdout']
                                                    and 'Streaming' in pre['camera']['stdout'] and pre['camera']['capture_request'] == [640, 480], 'camera capability raw missing'))
        result['capture'] = {'requested_dimensions': [640, 480], 'observed_decoded_dimensions': [640, 480]
                             if result['gates'].get('preview') else None, 'resized_by_observer': False}
    result['passed'] = bool(result['gates']) and all(value is not False for value in result['gates'].values())
    return result


def render_lines(path):
    result = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        marker = 'APP_RESIDENT_RENDER '
        if marker in line:
            value = strict_json(line.split(marker, 1)[1])
            require(isinstance(value, dict) and isinstance(value.get('backend_receipt'), dict), 'missing UI backend render receipt')
            result.append(value['backend_receipt'])
    return result


def signal_stop(number, frame):
    INTERRUPTED.append(number)


def check_interrupt():
    require(not INTERRUPTED, 'observation interrupted')


def publish(directory, manifest, result, interrupted, final_boundary=None):
    manifest['inventory'] = raw_inventory(directory)
    manifest.setdefault('failures', [])
    manifest['interrupts'] = list(interrupted)
    manifest['publication_pending'] = True
    pending = strict_json(json.dumps(result))
    pending['passed'] = False
    pending.setdefault('failures', [])
    if 'publication_pending' not in pending['failures']:
        pending['failures'].append('publication_pending')
    write_json(Path(directory) / 'manifest.json', manifest)
    write_json(Path(directory) / 'derived-verification.json', pending)
    boundary_failed = False
    try:
        if final_boundary is not None:
            final_boundary()
    except Exception:
        boundary_failed = True
    if interrupted:
        manifest['interrupts'] = list(interrupted)
        for number in interrupted:
            failure = 'interrupted:' + str(number)
            if failure not in manifest['failures']:
                manifest['failures'].append(failure)
    if boundary_failed and 'final_boundary_failed' not in manifest['failures']:
        manifest['failures'].append('final_boundary_failed')
    if interrupted or boundary_failed:
        result['passed'] = False
        result.setdefault('failures', [])
        result['failures'].extend(x for x in manifest['failures']
                                  if x not in result['failures'])
        write_json(Path(directory) / 'derived-verification.json', result)
        write_json(Path(directory) / 'manifest.json', manifest)
        return result
    manifest['publication_pending'] = False
    write_json(Path(directory) / 'derived-verification.json', result)
    write_json(Path(directory) / 'manifest.json', manifest)
    return result


def invoke_client(directory, tab, control, owner, command=None):
    require(tab in WRAPPERS and control in (None, 'quit', 'hide', 'status'), 'wrapper control forbidden')
    if owner is not None:
        if control is None:
            owner.assert_alive()
        else:
            owner.assert_control_alive()
    argv = [str(WRAPPERS[tab])]
    argv += ['--verify-resident-ui'] if control is None else ['--control', control]
    # Injected executable boundary is used by host tests only; CLI cannot set it.
    if command is not None:
        argv = command
    started = time.monotonic()
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    token = current_identity(child.pid)
    require(token is not None, 'client exited before actual PID/start/UID binding')
    samples = []
    if sys.platform.startswith('linux'):
        samples.append({'observed_at': time.monotonic(), 'process': proc_read(child.pid)})
    deadline = started + 15
    while child.poll() is None:
        row = proc_read(child.pid) if sys.platform.startswith('linux') else None
        samples.append({'observed_at': time.monotonic(), 'process': row})
        if time.monotonic() > deadline or INTERRUPTED:
            # Client receives no kill/terminate. Preserve it and retain failure.
            append_json(Path(directory) / 'clients.jsonl', {'request_started': started, 'identity': token,
                        'tab': tab, 'control': control, 'argv': argv, 'resources': samples,
                        'terminal_at': None, 'exit_code': child.poll(), 'accepted': False,
                        'observed': current_identity(child.pid), 'error': 'client_terminal_deadline_or_interrupt'})
            raise ValueError('client terminal deadline or interruption')
        time.sleep(.05)
    stdout, stderr = child.communicate()
    # Public launcher response has no token. Do not export untrusted secrets.
    if owner is not None and owner._token is not None:
        require(owner._token.encode() not in stdout and owner._token.encode() not in stderr, 'client leaked sensitive material')
    text = stdout.decode('utf-8')
    reply = strict_json(text)
    result = {'request_started': started, 'terminal_at': time.monotonic(), 'identity': token,
              'tab': tab, 'control': control, 'argv': argv, 'resources': samples,
              'exit_code': child.returncode, 'accepted': isinstance(reply, dict) and reply.get('accepted') is True,
              'observed': current_identity(child.pid), 'stdout': text, 'stderr': stderr.decode('utf-8'),
              'same_owners': owner is not None, 'reply': reply}
    append_json(Path(directory) / 'clients.jsonl', result)
    validate_client(result, result['terminal_at'])
    return result


class Observation:
    def __init__(self, owner, directory, manifest):
        self.owner, self.directory, self.manifest = owner, directory, manifest
        self.api = LocalAPI(owner, directory)
        self.http = []
        self.captures = []
        self.transitions = []
        self.last_preview_started = None

    def get(self, route, stage):
        row = self.api.get(route, stage)
        self.http.append(row)
        return len(self.http) - 1, row

    def wait_ack(self, visible, tab, stage):
        deadline = time.monotonic() + 5
        while time.monotonic() <= deadline:
            check_interrupt()
            index, row = self.get('/api/application', stage)
            require(row['response_finished'] <= deadline and time.monotonic() <= deadline,
                    'actual current ACK exceeded five-second budget')
            try:
                validate_ack(row['body'], self.owner.session, self.owner.identities, visible, tab,
                             received_before=row['response_finished'])
                return index, row
            except ValueError:
                time.sleep(.1)
        raise ValueError('actual current ACK exceeded five-second budget')

    def wait_status(self, healthy, stage):
        deadline = time.monotonic() + 5
        while time.monotonic() <= deadline:
            check_interrupt()
            index, row = self.get('/api/status', stage)
            require(row['response_finished'] <= deadline and time.monotonic() <= deadline,
                    'QML Start/Stop confirmation exceeded five-second budget')
            value = detection(row)
            if healthy:
                if value.get('running') and value.get('valid'):
                    running(value, self.manifest['source'])
                    return index, row
            elif value.get('running') is False:
                return index, row
            counters = value.get('counters')
            if isinstance(counters, dict):
                require(counters.get('read_failure', 0) == counters.get('detection_failure', 0) == 0, 'failure during startup/stop')
            time.sleep(.1)
        raise ValueError('QML Start/Stop confirmation exceeded five-second budget')

    def sample(self, stage, preview=True):
        check_interrupt()
        index, row = self.get('/api/status', stage)
        current = detection(row)
        if stage in ('visible_before', 'hidden', 'visible_after'):
            running(current, self.manifest['source'])
        self.get('/api/application', stage)
        append_json(self.directory / 'resources.jsonl', resource_sample(self.owner, stage))
        if preview and (self.last_preview_started is None
                        or time.monotonic() >= self.last_preview_started + 5):
            _, preview_row = self.get('/api/preview', stage)
            validate_preview(preview_row, current)
            self.last_preview_started = preview_row['request_started']
        return index, row

    def observe_visible(self, stage, seconds=45):
        first = None
        next_status = time.monotonic()
        while True:
            if time.monotonic() >= next_status:
                _, last = self.sample(stage)
                first = first or last
                next_status += 1
                if last['request_started'] - first['response_finished'] >= seconds:
                    return first, last
            else:
                check_interrupt()
                time.sleep(min(.05, next_status - time.monotonic()))

    def capture(self, purpose, tab='detection'):
        deadline = time.monotonic() + 5
        while time.monotonic() <= deadline:
            app_index, row = self.get('/api/application', 'capture_' + purpose)
            application = row['body']
            validate_ack(application, self.owner.session, self.owner.identities, True, tab,
                         received_before=row['response_finished'])
            receipt = application.get('last_render')
            expected = 'show-' + str(application['intent']['sequence']) + '-' + application['ui']['instance']
            if isinstance(receipt, dict) and receipt.get('capture_id') == expected:
                status_index, current = None, None
                if receipt.get('packet') is not None:
                    status_index, status_row = self.get('/api/status', 'capture_' + purpose)
                    current = detection(status_row)
                validate_render_metadata(receipt, application, current,
                                         'probe' if purpose == 'calibration' else 'run',
                                         completed_before=row['response_finished'])
                source = self.owner.workspace / (expected + '.png')
                no_links(source)
                image = decode_image(source.read_bytes(), [receipt['width'], receipt['height']])
                capture = {'purpose': purpose, 'application_index': app_index, 'status_index': status_index,
                           'receipt': receipt, 'png': 'captures/' + expected + '.png', 'png_sha256': image['sha256'],
                           'ui_log': 'private/Ui-' + application['ui']['instance'] + '.log'}
                require(receipt in render_lines(self.owner.workspace / ('Ui-' + application['ui']['instance'] + '.log')),
                        'actual QML render log receipt absent')
                self.captures.append(capture)
                append_json(self.directory / 'captures.jsonl', capture)
                return capture
            check_interrupt()
            time.sleep(.1)
        raise ValueError('genuine per-show capture exceeded deadline')

    def group(self, keyboard, name, actions):
        record = {'group': name, 'kind': 'native-keyboard', 'started': time.monotonic(),
                  'owner_checked': self.owner.identities, 'confirmed': False, 'inputs': [], 'evidence': None}
        quit_proof = None
        try:
            if name == 'quit':
                self.quit_transition()
            for action in actions:
                for _ in range(action.get('count', 1)):
                    check_interrupt()
                    self.owner.assert_alive()
                    sent = time.monotonic()
                    if 'key' in action:
                        keyboard.press(action['key'])
                    else:
                        keyboard.chord(*action['chord'])
                    record['inputs'].append({'action': action, 'sent_at': sent, 'owner_checked': self.owner.identities})
                    if name == 'quit':
                        pause_end = time.monotonic() + action.get('pause', .2)
                        while True:
                            index, witness = self.quit_transition()
                            try:
                                validate_quit_witness(witness, record['inputs'][0]['sent_at'], self.owner.session,
                                                      self.owner.identities, self.owner.release['code_id'])
                                quit_proof = index
                            except ValueError:
                                pass
                            if time.monotonic() >= pause_end:
                                break
                            time.sleep(min(.05, max(0, pause_end - time.monotonic())))
                    else:
                        time.sleep(action.get('pause', .2))
            if name in ('configure', 'navigate'):
                index, row = self.wait_status(False, 'action_' + name)
            elif name == 'start':
                index, row = self.wait_status(True, 'action_start')
                require(detection(row)['generation'] != self.manifest['idle_generation'], 'generation did not change after real Start')
                _, config = self.get('/api/config', 'action_start')
                validate_config(config, self.manifest['source'])
            elif name == 'hide':
                index, row = self.wait_ack(False, 'detection', 'action_hide')
            elif name == 'stop':
                index, row = self.wait_status(False, 'action_stop')
            else:
                deadline = time.monotonic() + 5
                while quit_proof is None and time.monotonic() <= deadline:
                    check_interrupt()
                    index, witness = self.quit_transition()
                    try:
                        validate_quit_witness(witness, record['inputs'][0]['sent_at'], self.owner.session,
                                              self.owner.identities, self.owner.release['code_id'])
                        quit_proof = index
                    except ValueError:
                        time.sleep(.05)
                require(quit_proof is not None, 'actual real UI Quit stopping transition witness missing')
                index, row = quit_proof, self.transitions[quit_proof]
                record['evidence_source'] = 'private-record'
                self.manifest['stopping_observed'] = True
                self.manifest['real_ui_quit'] = True
            record.update(confirmed=True, evidence=index)
            return row
        finally:
            record['finished'] = time.monotonic()
            append_json(self.directory / 'actions.jsonl', record)

    def quit_transition(self):
        # Capture real public/private state before and immediately around Quit;
        # this faster cadence is outside the formal hidden interval.
        row = {'stage': 'real_quit', 'request_started': time.monotonic(), 'record': None, 'service_state': None}
        for field, path in (('record', COORDINATOR / 'record.json'), ('service_state', self.owner.workspace / 'service-state.json')):
            try:
                no_links(path)
                row[field] = read_json(path)
                require(self.owner._token not in json.dumps(row[field]), 'sensitive private transition cannot be exported')
            except (OSError, ValueError):
                row[field] = None
                row[field + '_error'] = 'private_transition_read_failure'
        row['response_finished'] = time.monotonic()
        self.transitions.append(row)
        append_json(self.directory / 'transitions.jsonl', row)
        return len(self.transitions) - 1, row

    def hidden(self, requested):
        ack_index, ack_row = self.wait_ack(False, 'detection', 'settled_hidden')
        ack = validate_ack(ack_row['body'], self.owner.session, self.owner.identities, False,
                           'detection', received_before=ack_row['response_finished'])
        for client in rows(self.directory / 'clients.jsonl'):
            validate_client(client, time.monotonic())
        first = None
        next_status = time.monotonic()
        while True:
            if time.monotonic() >= next_status:
                _, row = self.sample('hidden')
                first = first or row
                _, app_row = self.get('/api/application', 'hidden')
                current_ack = validate_ack(app_row['body'], self.owner.session, self.owner.identities,
                                           False, 'detection',
                                           received_before=app_row['response_finished'])
                require(all(current_ack[k] == ack[k] for k in ('sequence', 'ui_instance', *ACK_COUNTERS)), 'hidden GUI counter/ACK growth')
                if self.manifest.get('formal_baseline') is None:
                    self.manifest['formal_baseline'] = detection(first)
                    self.get('/api/config', 'hidden')
                else:
                    validate_interval(first, row, self.manifest['source'])
                next_status += 1
                if row['request_started'] - first['response_finished'] >= requested:
                    self.manifest['formal'] = {'first_request_started': first['request_started'],
                                              'first_response_finished': first['response_finished'],
                                              'last_request_started': row['request_started'],
                                              'last_response_finished': row['response_finished'],
                                              'hidden_ack_index': ack_index, 'hidden_ack_received_at': ack['received_at']}
                    return
            else:
                check_interrupt()
                time.sleep(min(.05, next_status - time.monotonic()))


def copy_private_after_terminal(owner, directory, observation):
    require(all(current_identity(x['pid']) != x for role, x in owner.identities.items() if role in OWNER_ROLES),
            'raw private copy before actual owner terminal')
    write_json(directory / 'initial-record.json', owner.initial)
    write_json(directory / 'ready.json', owner.ready)
    allowed = re.compile(r'^(start\.json|exit\.json|service-state\.json|ui-start-[0-9a-f]{32}\.json|ui-exit-[0-9a-f]{32}\.json|Ui-[0-9a-f]{32}\.log|service\.log)$')
    (directory / 'private').mkdir(mode=0o700)
    (directory / 'captures').mkdir(mode=0o700)
    for path in owner.workspace.iterdir():
        if allowed.fullmatch(path.name):
            no_links(path)
            data = path.read_bytes()
            require(owner._token.encode() not in data, 'private raw contains sensitive material')
            target = directory / path.name if path.name in ('start.json', 'exit.json', 'service-state.json') else directory / 'private' / path.name
            target.write_bytes(data)
    for capture in observation.captures:
        source = owner.workspace / Path(capture['png']).name
        no_links(source)
        data = source.read_bytes()
        require(digest_bytes(data) == capture['png_sha256'], 'native PNG changed before copy')
        (directory / capture['png']).write_bytes(data)
    keys_path = directory / 'keys.json'
    if keys_path.exists() and observation.manifest['mode'] == 'run':
        keys = read_json(keys_path)
        original = Path(keys['probe']['directory'])
        require(verify_directory(original)['passed'], 'prior probe changed before terminal raw copy')
        destination = directory / 'calibration-proof'
        destination.mkdir(mode=0o700)
        for path in original.rglob('*'):
            relative = path.relative_to(original)
            if relative.as_posix() == 'derived-verification.json':
                continue
            no_links(path, directory=path.is_dir())
            target = destination / relative
            if path.is_dir():
                target.mkdir(mode=0o700)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())


def terminal_observation(owner, observation, manifest, keyboard):
    deadline = time.monotonic() + 30
    while time.monotonic() <= deadline:
        progress_path = owner.workspace / 'service-state.json'
        if progress_path.exists():
            progress = read_json(progress_path)
            if progress.get('state') == 'stopping':
                manifest['stopping_observed'] = True
        actual = {role: current_identity(owner.identities[role]['pid']) for role in OWNER_ROLES}
        if all(actual[role] != owner.identities[role] for role in OWNER_ROLES) and (owner.workspace / 'exit.json').exists():
            break
        time.sleep(.1)
    # Unknown identity raises; a timeout preserves the owners and fails, no kill.
    owners = {role: {'identity': owner.identities[role], 'observed': actual[role] if actual[role] == owner.identities[role] else None,
                     'checked_at': time.monotonic()} for role in OWNER_ROLES}
    receipt = read_json(owner.workspace / 'exit.json') if (owner.workspace / 'exit.json').exists() else None
    progress = read_json(owner.workspace / 'service-state.json') if (owner.workspace / 'service-state.json').exists() else None
    ui = owner.initial['application']['ui']['instance']
    ui_exit = read_json(owner.workspace / ('ui-exit-' + ui + '.json')) if (owner.workspace / ('ui-exit-' + ui + '.json')).exists() else None
    private_remaining = sorted(path.name for path in owner.workspace.iterdir() if path.name == 'token' or path.suffix == '.qml')
    terminal = {'session': owner.session, 'root': APP_ROOT.as_posix(), 'code_id': owner.release['code_id'],
                'exit_receipt': receipt, 'service_state': progress, 'ui_exit': ui_exit, 'owners': owners,
                'lease': lease_state(COORDINATOR / 'resource.lock', owner.lease), 'private_remaining': private_remaining,
                'uinput_remaining': [] if keyboard is None else keyboard.remaining,
                'governor_before': manifest['governor_before'], 'governor_after': sys_text(npu_root() / 'governor'),
                'stopping_observed': manifest.get('stopping_observed') is True, 'clients': rows(observation.directory / 'clients.jsonl')}
    write_json(observation.directory / 'terminal.json', terminal)
    validate_terminal(terminal, owner.identities)
    copy_private_after_terminal(owner, observation.directory, observation)
    return terminal


def observe_owned(args, output, release, network):
    manifest = {'schema': 1, 'run_id': args.run_id, 'mode': args.mode, 'source': getattr(args, 'source', 'replay'),
                'release_id': release['release_id'], 'code_id': release['code_id'], 'offline': getattr(args, 'offline', False),
                'requested_hidden_seconds': getattr(args, 'duration', None), 'failures': [], 'interrupts': [],
                'real_ui_quit': False, 'cleanup_kind': None, 'stopping_observed': False,
                'runtime': {'python': sys.version, 'uname': list(os.uname()), 'clock_ticks_per_second': os.sysconf('SC_CLK_TCK')},
                'schedule_seconds': {'status': 1, 'resources': 1, 'preview': 5},
                'maximum_formal_gap_seconds': {'status': 3, 'resources': 3, 'preview': 10}}
    write_json(output / 'release.json', release)
    write_json(output / 'identities.json', fingerprint_release(release))
    decoder = prepare_decoder()
    write_json(output / 'decoder.json', decoder)
    if args.mode == 'probe' and args.keys:
        manifest['source'] = validate_keys(read_json(args.keys), probe=True)['source']
    pre = preflight(manifest['source'])
    write_json(output / 'preflight.json', pre)
    manifest['governor_before'] = pre['governor']
    for name in ('http.jsonl', 'resources.jsonl', 'clients.jsonl', 'actions.jsonl', 'captures.jsonl'):
        # An empty required category will fail verification; retain it as raw.
        (output / name).touch(exist_ok=False)
    owner = observation = keyboard = None
    try:
        keys = load_keys(args.keys, release, manifest['source'], args.mode == 'probe') if args.keys else None
        if keys:
            write_json(output / 'keys.json', keys)
        invoke_client(output, 'detection', None, None)
        owner = Owner(release)
        owner.wait_ready()
        manifest.update(session=owner.session, owners=owner.identities, lease=owner.lease)
        observation = Observation(owner, output, manifest)
        observation.wait_ack(True, 'detection', 'initial_idle')
        _, idle = observation.wait_status(False, 'initial_idle')
        manifest['idle_generation'] = detection(idle).get('generation')
        observation.get('/api/config', 'initial_idle')
        observation.capture('calibration')
        append_json(output / 'resources.jsonl', resource_sample(owner, 'initial_idle'))
        if args.mode == 'probe':
            if keys:
                native = importlib.import_module('native_keyboard')
                keyboard = native.VirtualKeyboard(owner.assert_alive)
                with keyboard:
                    observation.group(keyboard, 'navigate', keys['groups']['navigate'])
                invoke_client(output, 'detection', None, owner)
                observation.wait_ack(True, 'detection', 'probe_reshow')
                observation.capture('calibration')
            invoke_client(output, 'wifi', None, owner)
            observation.wait_ack(True, 'wifi', 'probe_wifi')
            observation.capture('calibration', 'wifi')
            invoke_client(output, 'detection', None, owner)
            observation.wait_ack(True, 'detection', 'probe_detection')
            observation.capture('calibration')
            manifest['cleanup_kind'] = 'normal-control-quit'
            append_json(output / 'actions.jsonl', {'group': 'probe-cleanup', 'kind': 'normal-control',
                        'confirmed': False, 'started': time.monotonic(), 'reason': 'declared_software_cleanup_not_UI_Quit'})
            invoke_client(output, 'detection', 'quit', owner)
        else:
            native = importlib.import_module('native_keyboard')
            keyboard = native.VirtualKeyboard(owner.assert_alive)
            with keyboard:
                observation.group(keyboard, 'configure', keys['groups']['configure'])
                observation.group(keyboard, 'start', keys['groups']['start'])
                invoke_client(output, 'detection', None, owner)
                observation.wait_ack(True, 'detection', 'before_reshow')
                observation.capture('before')
                observation.observe_visible('visible_before')
                observation.group(keyboard, 'hide', keys['groups']['hide'])
                observation.hidden(args.duration)
                # Last formal HTTP/resource/preview has completed here.
                invoke_client(output, 'wifi', None, owner)
                observation.wait_ack(True, 'wifi', 'after_wifi')
                observation.capture('calibration', 'wifi')
                invoke_client(output, 'detection', None, owner)
                observation.wait_ack(True, 'detection', 'after_reshow')
                observation.capture('after')
                observation.observe_visible('visible_after')
                observation.group(keyboard, 'stop', keys['groups']['stop'])
                observation.group(keyboard, 'quit', keys['groups']['quit'])
            manifest['cleanup_kind'] = 'real-qml-quit'
        terminal_observation(owner, observation, manifest, keyboard)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, UnicodeError, ImportError):
        manifest['failures'].append('runtime_observation_failed')
        if keyboard is not None and keyboard.fd is not None:
            try:
                keyboard.close()
            except (ValueError, OSError):
                manifest['failures'].append('keyboard_cleanup_failed')
        # Emergency control is recorded as cleanup; the UI action remains false.
        if owner is not None:
            try:
                if observation is not None:
                    append_json(output / 'actions.jsonl', {'group': 'emergency', 'kind': 'emergency-control',
                                'confirmed': False, 'started': time.monotonic(), 'reason': 'failed_observation_cleanup'})
                invoke_client(output, 'detection', 'quit', owner)
                if observation is not None:
                    terminal_observation(owner, observation, manifest, keyboard)
            except (ValueError, OSError, KeyError, TypeError, RuntimeError):
                manifest['failures'].append('cleanup_pending_owners_preserved')
    write_json(output / 'network.json', network)
    manifest['interrupts'] = list(INTERRUPTED)
    manifest['inventory'] = raw_inventory(output)
    write_json(output / 'manifest.json', manifest)
    return manifest


def observe(args):
    require(sys.platform.startswith('linux'), 'runtime requires Linux; host verification remains available')
    release = validate_release(read_json(args.release))
    fingerprint_release(release)
    output = create_output(args.run_id)
    original = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    for number in original:
        signal.signal(number, signal_stop)
    network = {'requested': getattr(args, 'offline', False), 'parent_before': network_evidence(),
               'parent_after': None, 'child': None, 'child_terminal': None}
    manifest = None
    try:
        if network['requested']:
            # Fork creates a child observer namespace. Parent stays in its own
            # namespace and never edits interfaces, routes, addresses or WiFi.
            child = os.fork()
            if child == 0:
                try:
                    network['child'] = enter_offline_namespace()
                    observe_owned(args, output, release, network)
                    code = 0
                except (OSError, ValueError, RuntimeError):
                    code = 1
                os._exit(code)
            child_id = current_identity(child)
            require(child_id is not None, 'offline child PID/start/UID unavailable')
            signal_sent = False
            while True:
                ended, status = os.waitpid(child, os.WNOHANG)
                if ended:
                    break
                if INTERRUPTED and not signal_sent and current_identity(child) == child_id:
                    os.kill(child, signal.SIGTERM)
                    signal_sent = True
                time.sleep(.1)
            network = read_json(output / 'network.json') if (output / 'network.json').exists() else network
            network['parent_after'] = network_evidence()
            network['child_terminal'] = {'identity': child_id, 'exit_code': os.waitstatus_to_exitcode(status),
                                         'observed': current_identity(child), 'checked_at': time.monotonic()}
            write_json(output / 'network.json', network)
            manifest = read_json(output / 'manifest.json')
            if network['child_terminal']['exit_code'] != 0:
                manifest['failures'].append('offline_child_failed')
        else:
            network['child'] = network['parent_before']
            manifest = observe_owned(args, output, release, network)
            network['parent_after'] = network_evidence()
            network['child_terminal'] = {'identity': current_identity(os.getpid()), 'exit_code': None, 'observed': 'in-process'}
            write_json(output / 'network.json', network)
        manifest['inventory'] = raw_inventory(output)
        write_json(output / 'manifest.json', manifest)
        result = verify_directory(output)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
        manifest = manifest or {'schema': 1, 'mode': args.mode, 'run_id': args.run_id, 'failures': ['preflight_or_observer_failed']}
        result = {'schema': 1, 'passed': False, 'gates': {}, 'failures': manifest['failures']}
    def final_boundary():
        if hasattr(signal, 'pthread_sigmask'):
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set(original))
            try:
                INTERRUPTED.extend(int(x) for x in signal.sigpending() if x in original and int(x) not in INTERRUPTED)
                for number, handler in original.items():
                    signal.signal(number, handler)
                INTERRUPTED.extend(int(x) for x in signal.sigpending() if x in original and int(x) not in INTERRUPTED)
                # Consume pending handled signals so caller default TERM cannot
                # bypass the persisted failure verdict during unblocking.
                pending = signal.sigpending() & set(original)
                while pending:
                    signal.sigwait(pending)
                    pending = signal.sigpending() & set(original)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        else:
            for number, handler in original.items():
                signal.signal(number, handler)
    result = publish(output, manifest, result, INTERRUPTED, final_boundary)
    print(json.dumps({'directory': str(output), 'passed': result['passed'], 'gates': result.get('gates', {})}, allow_nan=False))
    return 0 if result['passed'] and not INTERRUPTED else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    probe = commands.add_parser('probe', help='normal wrappers, idle GETs and genuine per-show calibration')
    probe.add_argument('--run-id', required=True)
    probe.add_argument('--release', required=True)
    probe.add_argument('--keys', help='prior-PNG-bound navigation-only document')
    run = commands.add_parser('run', help='real QML actions and fully hidden normal-wrapper observation')
    run.add_argument('--run-id', required=True)
    run.add_argument('--release', required=True)
    run.add_argument('--duration', required=True, type=int, help='requested fully hidden seconds, 30..3600')
    run.add_argument('--keys', required=True)
    run.add_argument('--source', required=True, choices=('replay', 'camera'))
    run.add_argument('--offline', action='store_true', help='child libc CLONE_NEWNET with only loopback UP')
    verify = commands.add_parser('verify', help='read-only host-compatible recomputation; derived result separately named')
    verify.add_argument('directory')
    args = parser.parse_args(argv)
    if args.mode == 'verify':
        no_links(Path(args.directory), directory=True)
        result = verify_directory(Path(args.directory))
        output = Path(args.directory) / 'derived-verification.json'
        require(not output.is_symlink(), 'derived result symlink')
        if output.exists():
            no_links(output)
        write_json(output, result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result['passed'] else 1
    if args.mode == 'run' and not 30 <= args.duration <= 3600:
        parser.error('duration must be 30..3600 fully hidden seconds')
    safe_id(args.run_id)
    return observe(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, UnicodeError):
        # Exception payloads and private paths may carry credentials. Persist
        # structured local evidence where possible; export no exception text.
        print('Resident validation failed; inspect preserved raw evidence and reviewed release inputs', file=sys.stderr)
        raise SystemExit(1)
