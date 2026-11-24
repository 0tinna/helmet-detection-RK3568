"""Private Linux resident manager for the native board application.

The icon process is a bounded client. A separate POSIX session owns the
service and Qt. A resource flock is inherited by the service before its PID
can be published, so losing the manager never authorizes a second SDK owner.
Close inherited descriptors; never LOCK_UN an inherited open description.
"""
import argparse
from collections import OrderedDict
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # Parsing/help and software state tests also run on Windows.
    fcntl = None


APP_VERSION = 'resident-v4'
PROTOCOL = 1
VALIDATION_ROOT = Path('/userdata/helmet-validation')
COORDINATOR_ROOT = VALIDATION_ROOT / 'resident-state'
MAX_CONTROL_BYTES = 2048
MAX_REPLY_BYTES = 8192
MAX_RECORD_BYTES = 16384
CONTROL_TIMEOUT = 1.0
STARTUP_TIMEOUT = 12.0
OWNER_RECORD_HEARTBEAT = 5.0
IDEMPOTENCY_LIMIT = 128
SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
SESSION_ID = re.compile(r'^[0-9a-f]{32}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
TABS = ('detection', 'wifi')
COMMAND_FIELDS = {
    'status': set(),
    'show': {'request_id', 'tab'},
    'hide': {'request_id'},
    'quit': {'request_id'},
    'ack': {'request_id', 'ui_instance', 'sequence', 'visible', 'tab'},
    'render': {'request_id', 'ui_instance', 'sequence', 'visible', 'tab', 'saved',
               'capture_id', 'width', 'height', 'packet'},
}
ACK_OPTIONAL = {'received_at', 'frame_requests', 'frame_decodes', 'preview_presentations'}
RENDER_OPTIONAL = {'received_at', 'valid_at_receipt', 'remaining_ttl_ms'}
COMMON_FIELDS = {'protocol', 'version', 'root', 'session', 'command'}


def require_linux():
    if not sys.platform.startswith('linux') or fcntl is None:
        raise RuntimeError('resident ownership requires Linux flock, proc and Unix peer credentials')


def service_arguments(app, root, workspace, lease_fd, *, alert_port=8092,
                      lan_port=None, resident_session=None):
    if type(alert_port) is not int or not 0 <= alert_port <= 65535:
        raise ValueError('alert port is invalid')
    if lan_port is not None and (type(lan_port) is not int or not 0 <= lan_port <= 65535):
        raise ValueError('LAN port is invalid')
    workspace = Path(workspace)
    arguments = [
        sys.executable, str(Path(app) / 'service.py'),
        '--root', str(root), '--port', '0', '--alert-port', str(alert_port),
        '--token-file', str(workspace / 'token'),
        '--ready-file', str(workspace / 'ready.json'),
        '--lifecycle-file', str(workspace / 'service-state.json'),
        '--lease-fd', str(lease_fd), '--lease-dir', str(COORDINATOR_ROOT),
    ]
    if lan_port is not None:
        arguments.extend(('--lan-port', str(lan_port)))
    if resident_session is not None:
        if not SESSION_ID.fullmatch(resident_session):
            raise ValueError('resident session is invalid')
        arguments.extend(('--resident-session', resident_session,
                          '--resident-dir', str(COORDINATOR_ROOT)))
    return arguments


def _integer(value, low=0, high=2147483647):
    return type(value) is int and low <= value <= high


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonfinite JSON value')


def _parse_json(text):
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > 8:
                raise ValueError('JSON nesting exceeds the finite schema')
        elif char in ']}':
            depth -= 1
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError('JSON nesting exceeds the finite schema') from None


def decode_message(raw, maximum=MAX_CONTROL_BYTES):
    if (not isinstance(raw, bytes) or len(raw) > maximum or not raw.endswith(b'\n')
            or raw.count(b'\n') != 1):
        raise ValueError('one bounded JSON line is required')
    value = _parse_json(raw[:-1].decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('JSON object is required')
    return value


def _identity_valid(value):
    return (isinstance(value, dict) and set(value) == {'pid', 'starttime', 'uid'}
            and _integer(value['pid'], 1) and _integer(value['starttime'], 1, 2 ** 63 - 1)
            and _integer(value['uid'], 0))


def process_identity(pid):
    """Linux starttime is field 22, after the final ')' in the comm field."""
    require_linux()
    if not _integer(pid, 1):
        raise ValueError('invalid process identity')
    try:
        proc = Path('/proc') / str(pid)
        text = (proc / 'stat').read_text(encoding='utf-8')
        fields = text[text.rfind(')') + 2:].split()
        if len(fields) < 20 or fields[0] in ('Z', 'X'):
            return None
        return {'pid': pid, 'starttime': int(fields[19]), 'uid': proc.stat().st_uid}
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        # An unreadable owner is unknown, not evidence that it is terminal.
        raise RuntimeError('process identity is unavailable') from None


def identity_alive(identity):
    if identity is None:
        return False
    if not _identity_valid(identity):
        raise ValueError('invalid process identity record')
    actual = process_identity(identity['pid'])
    return actual is not None and actual == identity


def peer_identity(connection):
    require_linux()
    pid, uid, _ = struct.unpack('3i', connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    actual = process_identity(pid)
    if actual is None or actual['uid'] != uid:
        raise ValueError('Unix peer is unavailable')
    return actual


def _check_ancestors(path):
    """Trust root/App-owned ancestors; writable ones also need sticky protection.

    Root-owned /tmp is usable by isolated fixtures. A foreign directory owner
    can replace its children even with sticky protection, so it is rejected.
    """
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute clean paths are required')
    trusted_uids = {0, os.geteuid()}
    for parent in reversed((path, *path.parents)):
        try:
            row = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode):
            raise ValueError('directory paths cannot contain symlinks or non-directories')
        if row.st_uid not in trusted_uids:
            raise ValueError('directory ancestors require root or application ownership')
        if row.st_mode & 0o022 and not row.st_mode & stat.S_ISVTX:
            raise ValueError('directory ancestors cannot be writable by other users')


def private_directory(path):
    require_linux()
    path = Path(path)
    _check_ancestors(path)
    path.mkdir(mode=0o700, exist_ok=True)
    row = path.lstat()
    if (not stat.S_ISDIR(row.st_mode) or row.st_uid != os.geteuid()
            or stat.S_IMODE(row.st_mode) != 0o700):
        raise ValueError('private directory owner and 0700 permissions are required')
    return path


class Lease:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def fileno(self):
        if self.descriptor is None:
            raise ValueError('lease is already closed')
        return self.descriptor

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _RecordReplaced(ValueError):
    """An otherwise trusted inode lost its link during atomic publication."""


class Coordinator:
    """All control/lock paths are fixed names relative to a verified dir FD."""
    def __init__(self, directory=COORDINATOR_ROOT):
        self.directory = Path(directory)
        self.descriptor = None

    def prepare(self):
        require_linux()
        self.close()
        private_directory(self.directory)
        self.descriptor = os.open(str(self.directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return self

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def _name(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', name):
            raise ValueError('invalid private file name')
        if self.descriptor is None:
            raise RuntimeError('coordinator is not open')
        return name

    def _stat(self, name):
        name = self._name(name)
        try:
            row = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(row.st_mode) or row.st_uid != os.geteuid() or row.st_mode & 0o077:
            raise ValueError('unsafe private file')
        return row

    def open_file(self, name, flags=os.O_RDONLY, exclusive=False):
        name = self._name(name)
        row = self._stat(name)
        if row is not None and stat.S_ISREG(row.st_mode) and row.st_nlink == 0:
            raise _RecordReplaced('private record was replaced during stat')
        if row is not None and (not stat.S_ISREG(row.st_mode) or row.st_nlink != 1):
            raise ValueError('private files must be regular and singly linked')
        if exclusive:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                                 dir_fd=self.descriptor)
        except OSError as error:
            if error.errno == 40:  # ELOOP; do not export an attacker-selected path.
                raise ValueError('unsafe private file') from None
            raise
        row = os.fstat(descriptor)
        if (not stat.S_ISREG(row.st_mode) or row.st_uid != os.geteuid()
                or row.st_mode & 0o077):
            os.close(descriptor)
            raise ValueError('unsafe private file descriptor')
        if row.st_nlink != 1:
            os.close(descriptor)
            if row.st_nlink == 0:
                raise _RecordReplaced('private record was replaced after open')
            raise ValueError('unsafe private file descriptor')
        return descriptor

    def acquire(self, name, timeout=0):
        if name not in ('startup.lock', 'manager.lock', 'resource.lock'):
            raise ValueError('unknown lease')
        # Revalidate namespace trust before opening/creating any lease file.
        _check_ancestors(self.directory)
        descriptor = self.open_file(name, os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + max(0, timeout)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return Lease(descriptor)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise RuntimeError('an application owner or startup still holds the lease') from None
                time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def read_json(self, name, maximum=MAX_RECORD_BYTES):
        for attempt in range(3):
            if self._stat(name) is None:
                return None
            try:
                with os.fdopen(self.open_file(name), 'rb') as stream:
                    raw = stream.read(maximum + 1)
                break
            except _RecordReplaced:
                # os.replace may unlink the inode during stat or after open.
                # Reopen only this race; unsafe metadata must fail immediately.
                if attempt == 2:
                    raise
        else:
            raise RuntimeError('private record replacement retry exhausted')
        if len(raw) > maximum:
            raise ValueError('private record exceeds its limit')
        value = _parse_json(raw.decode('utf-8'))
        if not isinstance(value, dict):
            raise ValueError('private record must be an object')
        return value

    def write_json(self, name, value):
        self._name(name)
        row = self._stat(name)
        if row is not None and (not stat.S_ISREG(row.st_mode) or row.st_nlink != 1):
            raise ValueError('unsafe private record target')
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode('utf-8')
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError('private record exceeds its limit')
        temporary = name + '.' + uuid.uuid4().hex + '.tmp'
        try:
            with os.fdopen(self.open_file(temporary, os.O_WRONLY, exclusive=True), 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=self.descriptor, dst_dir_fd=self.descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass

    def read_record(self):
        value = self.read_json('record.json')
        if value is None:
            return None
        required = {'protocol', 'version', 'root', 'code_id', 'session', 'workspace',
                    'manager', 'service', 'ui', 'phase', 'mode'}
        if not required <= set(value):
            raise ValueError('incomplete owner record')
        if (type(value['protocol']) is not int or value['protocol'] != PROTOCOL
                or not isinstance(value['version'], str) or not SAFE_ID.fullmatch(value['version'])
                or not isinstance(value['session'], str) or not SESSION_ID.fullmatch(value['session'])
                or not isinstance(value['code_id'], str) or not SHA256.fullmatch(value['code_id'])
                or not isinstance(value['root'], str) or len(value['root']) > 240
                or not Path(value['root']).is_absolute() or '..' in Path(value['root']).parts
                or value['workspace'] != str(Path(value['root']) / 'app-state' / ('session-' + value['session']))
                or value['mode'] not in ('resident', 'one-shot')
                or value['phase'] not in ('starting', 'running', 'stopping', 'failed', 'completed')):
            raise ValueError('invalid owner record')
        for role in ('manager', 'service', 'ui'):
            if value[role] is not None and not _identity_valid(value[role]):
                raise ValueError('invalid owner identity')
        return value

    def write_record(self, value):
        self.write_json('record.json', value)

    def check_socket(self):
        row = self._stat('control.sock')
        if row is None or not stat.S_ISSOCK(row.st_mode):
            raise ValueError('private control socket is unavailable')
        return row

    def remove_socket(self):
        row = self._stat('control.sock')
        if row is not None:
            if not stat.S_ISSOCK(row.st_mode):
                raise ValueError('unsafe control socket target')
            os.unlink('control.sock', dir_fd=self.descriptor)

    def assert_no_live_owners(self, record):
        if record is not None:
            for role in ('manager', 'service', 'ui'):
                if identity_alive(record.get(role)):
                    raise RuntimeError('an original application owner is still alive; reopening is refused')
        # The inherited lease covers manager death before child-record publication.
        with self.acquire('manager.lock'), self.acquire('resource.lock'):
            pass


def retain_resource_lease(descriptor, directory):
    """Retain only the secure coordinator's actual inherited resource file."""
    require_linux()
    if not _integer(descriptor, 3):
        raise ValueError('invalid inherited lease descriptor')
    coordinator = Coordinator(directory).prepare()
    try:
        path_row = coordinator._stat('resource.lock')
        row = os.fstat(descriptor)
        if (path_row is None or not stat.S_ISREG(row.st_mode) or row.st_nlink != 1
                or row.st_uid != os.geteuid() or row.st_mode & 0o077
                or (row.st_dev, row.st_ino) != (path_row.st_dev, path_row.st_ino)):
            raise ValueError('inherited resource lease is not the coordinator lease')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(descriptor, False)
        return Lease(descriptor)
    finally:
        coordinator.close()


def match_install(record, root, code_id):
    if (record.get('root') != str(root) or record.get('version') != APP_VERSION
            or record.get('code_id') != code_id):
        raise ValueError('live application belongs to a different root or release')


def validate_install(root, app):
    require_linux()
    root, app = Path(root), Path(app)
    if (not root.is_absolute() or root == VALIDATION_ROOT or VALIDATION_ROOT not in root.parents
            or len(str(root)) > 240 or any(not re.fullmatch(r'[A-Za-z0-9_-]+', part)
                                         for part in root.parts[1:])):
        raise ValueError('application root must be a clean path below /userdata/helmet-validation')
    _check_ancestors(root)
    if app != root / 'src' / 'app':
        raise ValueError('launcher path does not match the requested installed root')
    _check_ancestors(app / 'qml')
    # Sticky exceptions belong to global ancestors, not writable install dirs.
    for directory in (root, root / 'src', app, app / 'qml'):
        row = directory.lstat()
        if (not stat.S_ISDIR(row.st_mode) or row.st_uid != os.geteuid()
                or row.st_mode & 0o022):
            raise ValueError('installed application directories require safe ownership and permissions')
    for name in ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml'):
        row = (app / name).lstat()
        if (not stat.S_ISREG(row.st_mode) or row.st_uid != os.geteuid() or row.st_mode & 0o022):
            raise ValueError('installed application files require safe ownership and permissions')
    return root


def code_identity(app):
    fingerprint = hashlib.sha256()
    for name in ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml'):
        fingerprint.update(name.encode('ascii') + b'\0')
        fingerprint.update(hashlib.sha256((Path(app) / name).read_bytes()).digest())
    return fingerprint.hexdigest()


def verify_manager_command(record):
    identity = record['manager']
    if not identity_alive(identity):
        raise ValueError('recorded manager is not alive')
    try:
        raw = (Path('/proc') / str(identity['pid']) / 'cmdline').read_bytes()
        args = [item.decode('utf-8') for item in raw.split(b'\0') if item]
    except (OSError, UnicodeError):
        raise ValueError('recorded manager path cannot be verified') from None
    script = str(Path(record['root']) / 'src/app' /
                 ('resident.py' if record['mode'] == 'resident' else 'launch.py'))
    if len(args) < 4 or args[1] != script or '--root' not in args:
        raise ValueError('recorded manager path does not match the installation')
    index = args.index('--root')
    if index + 1 >= len(args) or args[index + 1] != record['root']:
        raise ValueError('recorded manager root does not match the installation')


class ApplicationState:
    """The resident owns desired visibility; Qt ACK reports applied reality."""
    def __init__(self, root, session, verification=False):
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
            raise ValueError('invalid application session')
        self.root, self.session = str(root), session
        self.lock = threading.RLock()
        self.phase = 'starting'
        self.intent = {'sequence': 0, 'visible': False, 'tab': 'detection', 'preview_after': 0.0}
        self.ack = None
        self.ui = {'alive': False, 'instance': None, 'identity': None, 'exit_code': None}
        self.service = None
        self.reopen = False
        self.failure = None
        self.progress = {'cleanup_completed': False}
        self.verification = verification
        self.last_render = None
        self.requests = OrderedDict()

    def message(self, command, **fields):
        return {'protocol': PROTOCOL, 'version': APP_VERSION, 'root': self.root,
                'session': self.session, 'command': command, **fields}

    def request(self, command, **fields):
        return self.handle(self.message(command, **fields))

    def _validate(self, message):
        if not isinstance(message, dict):
            raise ValueError('invalid control')
        command = message.get('command')
        if not isinstance(command, str) or command not in COMMAND_FIELDS:
            raise ValueError('invalid command')
        required = COMMON_FIELDS | COMMAND_FIELDS[command]
        optional = ACK_OPTIONAL if command == 'ack' else RENDER_OPTIONAL if command == 'render' else set()
        if (not required <= set(message) or set(message) - required - optional
                or type(message.get('protocol')) is not int or message['protocol'] != PROTOCOL
                or message.get('version') != APP_VERSION or message.get('root') != self.root
                or message.get('session') != self.session):
            raise ValueError('invalid session or control fields')
        if command != 'status' and (not isinstance(message['request_id'], str)
                                    or not SAFE_ID.fullmatch(message['request_id'])):
            raise ValueError('invalid request identity')
        if command in ('show', 'ack', 'render') and message['tab'] not in TABS:
            raise ValueError('invalid tab')
        if command in ('ack', 'render'):
            if (not _integer(message['sequence']) or type(message['visible']) is not bool
                    or not isinstance(message['ui_instance'], str)
                    or not SESSION_ID.fullmatch(message['ui_instance'])
                    or ('received_at' in message and not _finite(message['received_at']))):
                raise ValueError('invalid Qt receipt')
        if command == 'ack':
            for field in ACK_OPTIONAL - {'received_at'}:
                if field in message and not _integer(message[field]):
                    raise ValueError('invalid preview counter')
        if command == 'render':
            if (not self.verification or type(message['saved']) is not bool
                    or not _integer(message['width'], 1, 8192) or not _integer(message['height'], 1, 8192)
                    or message['capture_id'] != 'show-' + str(message['sequence']) + '-' + message['ui_instance']
                    or ('valid_at_receipt' in message and message['valid_at_receipt'] is not None
                        and type(message['valid_at_receipt']) is not bool)
                    or ('remaining_ttl_ms' in message and not _finite(message['remaining_ttl_ms']))):
                raise ValueError('invalid native capture receipt')
            packet = message['packet']
            if packet is not None:
                if (not isinstance(packet, dict)
                        or set(packet) != {'frame_id', 'captured_at', 'generation', 'source_epoch', 'selection'}
                        or not _integer(packet['frame_id'], 0, 2 ** 53 - 1)
                        or not _integer(packet['source_epoch']) or not _finite(packet['captured_at'])
                        or not isinstance(packet['generation'], str) or not SESSION_ID.fullmatch(packet['generation'])
                        or not isinstance(packet['selection'], dict)
                        or set(packet['selection']) != {'model', 'source', 'profile'}
                        or any(not isinstance(value, str) or not 1 <= len(value) <= 240
                               for value in packet['selection'].values())):
                    raise ValueError('invalid displayed packet identity')
        return command

    def handle(self, message, peer=None):
        with self.lock:
            command = self._validate(message)
            if command in ('ack', 'render'):
                if (not self.ui['alive'] or message['ui_instance'] != self.ui['instance']
                        or message['sequence'] > self.intent['sequence']
                        or (peer is not None and self.service is not None and peer != self.service)):
                    raise ValueError('Qt receipt does not belong to the current owner')
            if command in ('show', 'hide') and self.phase not in ('starting', 'running'):
                raise RuntimeError('application is stopping or failed')
            duplicate = False
            if command in ('show', 'hide', 'quit'):
                request_id = message['request_id']
                signature = json.dumps(message, sort_keys=True, allow_nan=False)
                previous = self.requests.get(request_id)
                if previous is not None:
                    if previous[0] != signature:
                        raise ValueError('request identity was reused with different fields')
                    duplicate = True
                else:
                    if command == 'quit':
                        if self.phase != 'failed':
                            self.phase = 'stopping'
                        self.reopen = False
                    else:
                        self.intent = {'sequence': self.intent['sequence'] + 1,
                                       'visible': command == 'show',
                                       'tab': message['tab'] if command == 'show' else self.intent['tab'],
                                       'preview_after': time.monotonic() if command == 'show' else self.intent['preview_after']}
                        if command == 'show' and not self.ui['alive']:
                            self.reopen = True
                        if command == 'hide':
                            self.reopen = False
                    self.requests[request_id] = (signature, self.intent['sequence'])
                    while len(self.requests) > IDEMPOTENCY_LIMIT:
                        self.requests.popitem(last=False)
            elif command == 'ack':
                if self.ack is None or message['sequence'] >= self.ack['sequence']:
                    self.ack = {key: message[key] for key in ('sequence', 'visible', 'tab', 'ui_instance')}
                    self.ack['qt'] = self.ui['identity']
                    self.ack['received_at'] = message.get('received_at', time.monotonic())
                    for key in ACK_OPTIONAL - {'received_at'}:
                        self.ack[key] = message.get(key, 0)
            elif command == 'render':
                self.last_render = {key: message[key] for key in COMMAND_FIELDS['render'] - {'request_id'}}
                self.last_render.update(qt=self.ui['identity'],
                                        received_at=message.get('received_at', time.monotonic()),
                                        valid_at_receipt=message.get('valid_at_receipt', False),
                                        remaining_ttl_ms=message.get('remaining_ttl_ms', 0.0))
            return {'ok': True, 'accepted': command != 'status', 'duplicate': duplicate,
                    'application': self.snapshot()}

    def mark_running(self):
        with self.lock:
            if self.phase == 'starting':
                self.phase = 'running'

    def service_started(self, identity):
        with self.lock:
            self.service = identity

    def ui_started(self, instance, identity=None):
        if not SESSION_ID.fullmatch(instance) or (identity is not None and not _identity_valid(identity)):
            raise ValueError('invalid Qt owner')
        with self.lock:
            self.ui = {'alive': True, 'instance': instance, 'identity': identity, 'exit_code': None}
            self.ack = None
            self.reopen = False

    def ui_exited(self, code):
        with self.lock:
            self.ui.update(alive=False, exit_code=code)
            self.ack = None
            self.reopen = False

    def needs_ui(self):
        with self.lock:
            return (self.phase in ('starting', 'running') and self.reopen
                    and self.intent['visible'] and not self.ui['alive'])

    def service_failed(self, reason):
        with self.lock:
            self.phase = 'failed'
            self.failure = reason
            self.reopen = False

    def snapshot(self):
        with self.lock:
            ack = self.ack
            applied = bool(ack is not None and ack['sequence'] == self.intent['sequence']
                           and ack['visible'] == self.intent['visible'] and ack['tab'] == self.intent['tab'])
            value = {'session': self.session, 'root': self.root, 'version': APP_VERSION,
                     'phase': self.phase, 'intent': self.intent, 'ack': ack, 'applied': applied,
                     'ui': self.ui, 'service': self.service, 'progress': self.progress,
                     'failure': self.failure, 'verification_enabled': self.verification,
                     'last_render': self.last_render}
            return json.loads(json.dumps(value, allow_nan=False))


def _read_line(connection, maximum):
    raw = bytearray()
    while b'\n' not in raw:
        part = connection.recv(min(4096, maximum + 1 - len(raw)))
        if not part:
            raise ValueError('incomplete control message')
        raw.extend(part)
        if len(raw) > maximum:
            raise ValueError('control message exceeds its limit')
    return bytes(raw)


class ControlServer:
    def __init__(self, coordinator, application):
        self.coordinator, self.application = coordinator, application
        self.stop_event = threading.Event()
        self.socket = None
        self.thread = None
        self.inode = None

    def start(self):
        self.coordinator.remove_socket()
        path = self.coordinator.directory / 'control.sock'
        if len(os.fsencode(path)) > 107:
            raise ValueError('control socket path is too long')
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        os.chmod(path, 0o600)
        self.inode = self.coordinator.check_socket().st_ino
        self.socket.listen(8)
        self.socket.settimeout(.2)
        self.thread = threading.Thread(target=self._serve, name='helmet-resident-control', daemon=False)
        self.thread.start()

    def _serve(self):
        while not self.stop_event.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(CONTROL_TIMEOUT)
                try:
                    peer = peer_identity(connection)
                    if peer['uid'] != os.geteuid():
                        raise ValueError('same UID is required')
                    value = decode_message(_read_line(connection, MAX_CONTROL_BYTES))
                    reply = self.application.handle(value, peer=peer)
                except RuntimeError:
                    reply = {'ok': False, 'error': 'stopping_or_owner_unavailable'}
                except (OSError, ValueError, TypeError, UnicodeError):
                    reply = {'ok': False, 'error': 'invalid_control'}
                raw = json.dumps(reply, allow_nan=False, ensure_ascii=False).encode('utf-8') + b'\n'
                if len(raw) > MAX_REPLY_BYTES:
                    raw = b'{"ok":false,"error":"reply_limit"}\n'
                try:
                    connection.sendall(raw)
                except OSError:
                    pass

    def close(self):
        self.stop_event.set()
        if self.socket is not None:
            self.socket.close()
        if self.thread is not None:
            self.thread.join(2)
        row = self.coordinator._stat('control.sock')
        if row is not None and row.st_ino == self.inode:
            self.coordinator.remove_socket()


def exchange(coordinator, message, expected_manager):
    coordinator.check_socket()
    if not _identity_valid(expected_manager) or not identity_alive(expected_manager):
        raise ValueError('recorded manager identity is stale')
    raw = json.dumps(message, allow_nan=False, ensure_ascii=False).encode('utf-8') + b'\n'
    if len(raw) > MAX_CONTROL_BYTES:
        raise ValueError('control message exceeds its limit')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(CONTROL_TIMEOUT)
        connection.connect(str(coordinator.directory / 'control.sock'))
        peer = peer_identity(connection)
        if peer != expected_manager or peer['uid'] != os.geteuid():
            raise ValueError('control socket peer does not match the recorded manager')
        connection.sendall(raw)
        reply = decode_message(_read_line(connection, MAX_REPLY_BYTES), MAX_REPLY_BYTES)
    if reply.get('ok') is not True:
        if reply.get('error') == 'stopping_or_owner_unavailable':
            raise RuntimeError('application is stopping or its original owner is unavailable')
        raise ValueError('control was rejected')
    application = reply.get('application')
    if (not isinstance(application, dict) or application.get('session') != message['session']
            or application.get('root') != message['root'] or application.get('version') != APP_VERSION):
        raise ValueError('control response belongs to a different session')
    return reply


class ResidentConnection:
    """Service-side bridge; the HTTP token stays in the service, not Unix JSON."""
    def __init__(self, root, session, directory=COORDINATOR_ROOT):
        self.root, self.session, self.directory = str(root), session, Path(directory)

    def request(self, command, **fields):
        coordinator = Coordinator(self.directory).prepare()
        try:
            record = coordinator.read_record()
            if record is None or record['session'] != self.session or record['mode'] != 'resident':
                raise RuntimeError('resident session is unavailable')
            match_install(record, self.root, code_identity(Path(self.root) / 'src/app'))
            verify_manager_command(record)
            message = {'protocol': PROTOCOL, 'version': APP_VERSION, 'root': self.root,
                       'session': self.session, 'command': command, **fields}
            return exchange(coordinator, message, record['manager'])
        finally:
            coordinator.close()


def qml_environment():
    env = os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR', '/var/run')
    env.setdefault('WAYLAND_DISPLAY', 'wayland-0')
    env.setdefault('QT_QPA_PLATFORM', 'wayland')
    env.pop('QT_IM_MODULE', None)
    env['QT_QUICK_CONTROLS_STYLE'] = 'Material'
    env['QT_QUICK_CONTROLS_MATERIAL_THEME'] = 'Dark'
    return env


def write_qml_wrapper(workspace, name, app, properties):
    text = ('import QtQuick 2.15\nimport QtQuick.Window 2.15\n'
            'Loader { Component.onCompleted: setSource('
            + json.dumps((Path(app) / 'qml/Main.qml').as_uri()) + ', '
            + json.dumps(properties, ensure_ascii=False, allow_nan=False) + ') }\n')
    with os.fdopen(workspace.open_file(name, os.O_WRONLY, exclusive=True), 'w', encoding='utf-8') as stream:
        stream.write(text)
    return workspace.directory / name


def service_ready(workspace, service, session=None, expected_alert_port=None):
    value = workspace.read_json('ready.json', MAX_REPLY_BYTES)
    if value is None:
        return None
    if (value.get('ready') is not True or value.get('host') != '127.0.0.1'
            or not _integer(value.get('port'), 1, 65535) or value.get('pid') != service.pid
            or (session is not None and value.get('session') != session)
            or value.get('identity') != process_identity(service.pid)):
        raise ValueError('service readiness does not match the actual owner')
    if expected_alert_port is not None:
        if type(expected_alert_port) is not int or not 0 <= expected_alert_port <= 65535:
            raise ValueError('expected alert port is invalid')
        alert_port = value.get('alert_port')
        alert_alive = value.get('alert_viewer_alive')
        valid = ((expected_alert_port > 0
                  and alert_port == expected_alert_port
                  and alert_alive is True)
                 or (expected_alert_port == 0
                     and alert_port in (None, 0)
                     and alert_alive is False))
        if not valid:
            raise ValueError('service alert readiness does not match configuration')
    return value


def service_token(workspace):
    with os.fdopen(workspace.open_file('token'), 'rb') as stream:
        raw = stream.read(129)
    if len(raw) > 128:
        raise ValueError('invalid private service token')
    token = raw.decode('ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,120}', token):
        raise ValueError('invalid private service token')
    return token


def request_service_shutdown(service, identity, port, token, session=None):
    """HTTP quit first; TERM is the service's normal cancellation handler."""
    if service.poll() is not None:
        return 'terminal'
    if not identity_alive(identity):
        raise RuntimeError('service identity cannot be confirmed for normal shutdown')
    if port is not None and token is not None:
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=CONTROL_TIMEOUT)
        try:
            payload = ({'session': session, 'request_id': 'manager-quit-' + session}
                       if session is not None else {})
            route = '/api/application/quit' if session is not None else '/api/shutdown'
            connection.request('POST', route, body=json.dumps(payload).encode('utf-8'),
                               headers={'X-Helmet-Token': token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            response.read(MAX_REPLY_BYTES + 1)
            if response.status in (200, 202):
                return 'http_quit'
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
    # This handler requests normal stop/rollback; there is no SIGKILL fallback.
    service.send_signal(signal.SIGTERM)
    return 'normal_term'


class ResidentManager:
    def __init__(self, root, tab='detection', verification=False, background=False):
        self.app = Path(__file__).resolve().parent
        self.root = validate_install(Path(root), self.app)
        self.code_id = code_identity(self.app)
        self.coordinator = Coordinator()
        self.workspace = None
        self.manager_lease = self.resource_lease = None
        self.session = uuid.uuid4().hex
        self.application = ApplicationState(self.root, self.session, verification)
        if not background:
            self.application.request('show', request_id='initial-' + self.session, tab=tab)
        self.control = None
        self.service = self.ui = None
        self.service_identity = self.ui_identity = None
        self.manager_identity = process_identity(os.getpid())
        self.service_log = self.ui_log = None
        self.wrapper = None
        self.port = self.token = None
        self.stop_requested = False
        self.normal_stop = None
        self.qmlscene = shutil.which('qmlscene')
        self.previous_record = None
        self._last_record_signature = None
        self._last_record_published = 0.0

    def record(self):
        lease = os.fstat(self.resource_lease.fileno())
        state = self.application.snapshot()
        return {'protocol': PROTOCOL, 'version': APP_VERSION, 'code_id': self.code_id,
                'root': str(self.root), 'session': self.session, 'mode': 'resident',
                'workspace': str(self.workspace.directory), 'phase': state['phase'],
                'manager': self.manager_identity, 'service': self.service_identity, 'ui': self.ui_identity,
                'lease': {'device': lease.st_dev, 'inode': lease.st_ino,
                          'service_inherited': self.service is not None,
                          'service_alive': self.service is not None and self.service.poll() is None},
                'verification_enabled': self.application.verification,
                'application': state, 'recorded_at': time.monotonic()}

    @staticmethod
    def _record_signature(value):
        stable = json.loads(json.dumps(value, allow_nan=False))
        stable.pop('recorded_at', None)
        application = stable.get('application')
        if isinstance(application, dict):
            # ACK/render telemetry is available on the private control channel.
            # Persisting every receipt would turn a discovery record into an
            # fsync-heavy telemetry log and compete with inference and Qt.
            for key in ('ack', 'applied', 'last_render'):
                application.pop(key, None)
            progress = application.get('progress')
            if isinstance(progress, dict):
                progress.pop('received_at', None)
        return json.dumps(stable, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def publish(self, force=False):
        value = self.record()
        signature = self._record_signature(value)
        now = time.monotonic()
        if (not force and signature == self._last_record_signature
                and now - self._last_record_published < OWNER_RECORD_HEARTBEAT):
            return False
        self.coordinator.write_record(value)
        self._last_record_signature = signature
        self._last_record_published = now
        return True

    def _start_service(self):
        self.service_log = os.fdopen(self.workspace.open_file('service.log', os.O_WRONLY, exclusive=True), 'wb')
        self.service = subprocess.Popen(service_arguments(
            self.app, self.root, self.workspace.directory,
            self.resource_lease.fileno(), alert_port=8092,
            resident_session=self.session,
        ), pass_fds=(self.resource_lease.fileno(),), stdin=subprocess.DEVNULL,
            stdout=self.service_log, stderr=subprocess.STDOUT)
        self.service_identity = process_identity(self.service.pid)
        if self.service_identity is None:
            raise RuntimeError('service exited at startup')
        self.application.service_started(self.service_identity)
        self.publish()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.service.poll() is not None or self.stop_requested:
                raise RuntimeError('service failed to become ready')
            ready = service_ready(
                self.workspace, self.service, self.session,
                expected_alert_port=8092)
            if ready is not None:
                self.port, self.token = ready['port'], service_token(self.workspace)
                self.application.mark_running()
                return
            time.sleep(.05)
        raise RuntimeError('service readiness timed out')

    def _start_ui(self):
        instance = uuid.uuid4().hex
        self.wrapper = write_qml_wrapper(self.workspace, 'Ui-' + instance + '.qml', self.app,
            {'apiBase': 'http://127.0.0.1:' + str(self.port), 'apiToken': self.token,
             'initialTab': self.application.snapshot()['intent']['tab'], 'residentMode': True,
             'appSession': self.session, 'uiInstance': instance,
             'verificationResidentUi': self.application.verification,
             'verificationDir': str(self.workspace.directory) if self.application.verification else ''})
        self.ui_log = os.fdopen(self.workspace.open_file('Ui-' + instance + '.log', os.O_WRONLY, exclusive=True), 'wb')
        self.ui = subprocess.Popen([self.qmlscene, str(self.wrapper)], env=qml_environment(),
                                   pass_fds=(self.resource_lease.fileno(),), stdin=subprocess.DEVNULL,
                                   stdout=self.ui_log, stderr=subprocess.STDOUT)
        self.ui_identity = process_identity(self.ui.pid)
        self.application.ui_started(instance, self.ui_identity)
        self.workspace.write_json('ui-start-' + instance + '.json',
                                  {'session': self.session, 'ui_instance': instance,
                                   'identity': self.ui_identity, 'requested_at': time.monotonic()})
        self.publish()

    def _ui_terminal(self):
        if self.ui is not None and self.ui.poll() is not None:
            instance = self.application.snapshot()['ui']['instance']
            self.application.ui_exited(self.ui.returncode)
            self.workspace.write_json('ui-exit-' + instance + '.json',
                                      {'session': self.session, 'ui_instance': instance,
                                       'identity': self.ui_identity, 'exit_code': self.ui.returncode,
                                       'terminal_at': time.monotonic()})
            if self.wrapper is not None:
                self.workspace._stat(self.wrapper.name)
                os.unlink(self.wrapper.name, dir_fd=self.workspace.descriptor)
            if self.ui_log is not None:
                self.ui_log.close()
            self.ui, self.ui_log, self.wrapper = None, None, None

    def _progress(self):
        value = self.workspace.read_json('service-state.json', MAX_REPLY_BYTES)
        if value is not None and value.get('session') == self.session:
            fields = ('detection_stopped', 'wifi_stopped', 'alert_stopped',
                      'alert_viewer_alive', 'alert_port', 'mail', 'http_alive',
                      'cleanup_completed', 'detection_alive', 'wifi_alive',
                      'state', 'received_at', 'lan_alive', 'lan_port')
            with self.application.lock:
                self.application.progress = {key: value.get(key) for key in fields}
            if value.get('state') == 'failed' and self.application.phase not in ('stopping', 'failed'):
                self.application.service_failed('local service failed; normal cleanup remains required')

    def _tick(self):
        if self.stop_requested and self.application.phase in ('starting', 'running'):
            self.application.request('quit', request_id='signal-' + self.session)
        self._ui_terminal()
        self._progress()
        if self.service is not None and self.service.poll() is not None:
            if self.application.phase not in ('stopping', 'failed'):
                self.application.service_failed('local service exited unexpectedly')
            if self.service.returncode != 0:
                self.application.service_failed('local service reported a failure')
        stopping = self.application.phase in ('stopping', 'failed')
        if stopping:
            if self.service is not None and self.service.poll() is None and self.normal_stop is None:
                self.normal_stop = request_service_shutdown(self.service, self.service_identity,
                                                           self.port, self.token, self.session)
            # Qt owns no SDK, and is asked to end only after service owners are terminal.
            if (self.service is None or self.service.poll() is not None) and self.ui is not None:
                if self.ui.poll() is None and identity_alive(self.ui_identity):
                    self.ui.terminate()
        elif self.application.needs_ui():
            self._start_ui()
        self.publish()
        return stopping and (self.service is None or self.service.poll() is not None) and self.ui is None

    def run(self):
        if self.qmlscene is None:
            raise RuntimeError('qmlscene is required on the board')
        os.umask(0o077)
        self.coordinator.prepare()
        self.previous_record = self.coordinator.read_record()
        self.coordinator.assert_no_live_owners(self.previous_record)
        self.manager_lease = self.coordinator.acquire('manager.lock')
        try:
            self.resource_lease = self.coordinator.acquire('resource.lock')
            runtime = private_directory(self.root / 'app-state')
            self.workspace = Coordinator(runtime / ('session-' + self.session)).prepare()
            self.control = ControlServer(self.coordinator, self.application)
            self.control.start()
            for number in (signal.SIGTERM, signal.SIGINT):
                signal.signal(number, lambda number, frame: setattr(self, 'stop_requested', True))
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            self.publish(force=True)
            try:
                self._start_service()
                if self.application.needs_ui():
                    self._start_ui()
                self.workspace.write_json('start.json', {**self.record(), 'kind': 'resident-start-v4'})
                while not self._tick():
                    time.sleep(.1)
            except BaseException:
                self.application.service_failed('resident startup or supervision failed')
                # Losing a status write must never abandon a live service/lease.
                while True:
                    try:
                        if self._tick():
                            break
                    except (OSError, ValueError, RuntimeError):
                        if self.service is not None and self.service.poll() is None and self.normal_stop is None:
                            self.normal_stop = request_service_shutdown(self.service, self.service_identity,
                                                                       self.port, self.token, self.session)
                        if (self.service is None or self.service.poll() is not None) and self.ui is not None:
                            if self.ui.poll() is None:
                                self.ui.terminate()
                            else:
                                self.ui = None
                        if (self.service is None or self.service.poll() is not None) and self.ui is None:
                            break
                    time.sleep(.2)
            failed = self.application.phase == 'failed'
            with self.application.lock:
                self.application.phase = 'failed' if failed else 'completed'
                self.application.progress['cleanup_completed'] = True
            self.publish(force=True)
            self.control.close()
            self.control = None
            for name in ('token',):
                if self.workspace._stat(name) is not None:
                    os.unlink(name, dir_fd=self.workspace.descriptor)
            self.resource_lease.close()
            self.resource_lease = None
            self.workspace.write_json('exit.json',
                {'kind': 'resident-terminal-v4', 'session': self.session, 'root': str(self.root),
                 'version': APP_VERSION, 'code_id': self.code_id, 'manager': self.manager_identity,
                 'service': self.service_identity, 'ui': self.ui_identity,
                 'service_exit': self.service.returncode if self.service else None,
                 'phase': self.application.phase, 'cleanup_completed': True,
                 'resource_lease_released': True, 'normal_stop': self.normal_stop,
                 'terminal_at': time.monotonic()})
            return 1 if failed else 0
        finally:
            if self.control is not None:
                self.control.close()
            for stream in (self.service_log, self.ui_log):
                if stream is not None:
                    stream.close()
            if self.resource_lease is not None:
                # Normal paths above wait for actual children. Any inherited
                # child still retains its FD if an unrecoverable manager error occurs.
                self.resource_lease.close()
            if self.workspace is not None:
                self.workspace.close()
            self.manager_lease.close()
            self.coordinator.close()


def client(root, tab='detection', command='show', verification=False, background=False):
    if background and (command != 'show' or verification):
        raise ValueError('background bootstrap cannot change controls or verification mode')
    app = Path(__file__).resolve().parent
    root = validate_install(Path(root), app)
    fingerprint = code_identity(app)
    coordinator = Coordinator().prepare()
    try:
        with coordinator.acquire('startup.lock', STARTUP_TIMEOUT):
            record = coordinator.read_record()
            if record is not None and identity_alive(record['manager']):
                match_install(record, root, fingerprint)
                verify_manager_command(record)
                if record['mode'] != 'resident':
                    raise RuntimeError('a bounded one-shot still owns the application')
                if verification and record.get('verification_enabled') is not True:
                    raise ValueError('active session did not enable native verification captures')
            else:
                coordinator.assert_no_live_owners(record)
                if command != 'show':
                    return {'ok': True, 'accepted': False, 'application': {'phase': 'absent'}}
                coordinator.remove_socket()
                log_name = 'client-start-' + uuid.uuid4().hex + '.log'
                with os.fdopen(coordinator.open_file(log_name, os.O_WRONLY, exclusive=True), 'wb') as log:
                    args = [sys.executable, str(app / 'resident.py'), '--root', str(root), '--tab', tab]
                    if verification:
                        args.append('--verify-resident-ui')
                    if background:
                        args.append('--background')
                    manager = subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=subprocess.STDOUT, close_fds=True)
                deadline = time.monotonic() + STARTUP_TIMEOUT
                while time.monotonic() < deadline:
                    candidate = coordinator.read_record()
                    if (candidate is not None and candidate['manager'] is not None
                            and candidate['manager']['pid'] == manager.pid):
                        match_install(candidate, root, fingerprint)
                        verify_manager_command(candidate)
                        if candidate['phase'] in ('failed', 'stopping', 'completed'):
                            raise RuntimeError('resident startup failed or cleanup is still in progress')
                        try:
                            coordinator.check_socket()
                            record = candidate
                            break
                        except ValueError:
                            pass
                    if manager.poll() is not None:
                        raise RuntimeError('resident startup failed; inspect the private startup receipt')
                    time.sleep(.05)
                else:
                    raise RuntimeError('resident startup budget elapsed; original owners remain protected')
            if background:
                command = 'status'
            fields = {} if command == 'status' else {'request_id': uuid.uuid4().hex}
            if command == 'show':
                fields['tab'] = tab
            message = {'protocol': PROTOCOL, 'version': APP_VERSION, 'root': str(root),
                       'session': record['session'], 'command': command, **fields}
            return exchange(coordinator, message, record['manager'])
    finally:
        coordinator.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--tab', choices=TABS, default='detection')
    parser.add_argument('--verify-resident-ui', action='store_true')
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args(argv)
    if args.background and args.verify_resident_ui:
        parser.error('background bootstrap cannot enable UI verification')
    return ResidentManager(args.root, args.tab, args.verify_resident_ui, args.background).run()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        # No attacker-selected path, exception text, credential or token is exported.
        print('Resident startup failed; inspect private ownership and lifecycle receipts', file=sys.stderr)
        raise SystemExit(1)
