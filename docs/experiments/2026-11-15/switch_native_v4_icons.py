#!/usr/bin/env python3
"""One migration of six owned frozen-v3 icons to the reviewed resident-v4 root.

Default is a read-only plan. No App/service/SDK/Qt is started or stopped.
Unmodified legacy launchers require external serialization by the root owner.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import sys
import uuid
import zipfile

OLD_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v2')
NEW_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v4')
COORDINATOR = Path('/userdata/helmet-validation/resident-state')
CONFIG = Path('/opt/ui/src/ATK-DLRK3568/apk3.cfg')
EXPECTED_CONFIG_SHA = 'f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11'
DESKTOP_DIR = Path.home() / '.local/share/applications'
PROC_ROOT = Path('/proc')
NPU_LOAD = Path('/sys/kernel/debug/rknpu/load')
DESKTOPS = (('helmet-detection.desktop', '安全帽检测', 'detection', 'helmet'),
            ('helmet-wifi.desktop', 'Wi-Fi 设置', 'wifi', 'wifi'))
CODE_NAMES = ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml')
LOCK_NAMES = ('startup.lock', 'manager.lock', 'resource.lock')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def effective_uid():
    return os.geteuid()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON field')
        result[key] = value
    return result


def json_bytes(data):
    require(len(data) <= 4 * 1024 * 1024, 'evidence JSON is too large')
    def reject(value):
        raise ValueError('nonfinite JSON: ' + value)
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=unique_object, parse_constant=reject)
    except RecursionError:
        raise ValueError('evidence JSON depth is excessive') from None
    require(isinstance(value, dict), 'evidence JSON must be an object')
    return value


def no_symlink_path(path, allow_missing=False):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute clean path required: ' + str(path))
    for component in reversed((path, *path.parents)):
        try:
            row = component.lstat()
        except FileNotFoundError:
            require(allow_missing and component == path, 'path is missing: ' + str(component))
            continue
        require(not stat.S_ISLNK(row.st_mode), 'symlink path refused: ' + str(component))
        if component != path:
            require(stat.S_ISDIR(row.st_mode), 'ancestor is not a directory: ' + str(component))
    require(path.resolve() == path, 'noncanonical path refused: ' + str(path))


def checked_directory(path, allow_missing=False):
    no_symlink_path(path, allow_missing)
    require(path.is_relative_to(NEW_ROOT), 'output directory escapes v4 root: ' + str(path))
    if path.exists():
        row = path.lstat()
        require(stat.S_ISDIR(row.st_mode), 'output path is not a directory: ' + str(path))
        if sys.platform.startswith('linux'):
            require(row.st_uid == os.geteuid() and not row.st_mode & 0o022, 'unsafe output directory owner/mode')


def safe_read(path):
    no_symlink_path(path)
    saved = ordinary_snapshot(path)
    if sys.platform.startswith('linux'):
        require(saved['identity']['uid'] == os.geteuid() and not saved['identity']['mode'] & 0o022,
                'unsafe evidence owner/mode: ' + str(path))
    return saved['data']


def evidence_ref(ref):
    require(isinstance(ref, dict) and set(ref) == {'path', 'bytes', 'sha256'}, 'invalid artifact reference')
    require(type(ref['bytes']) is int and ref['bytes'] > 0 and re.fullmatch('[0-9a-f]{64}', ref['sha256']),
            'invalid artifact length/SHA')
    data = safe_read(Path(ref['path']))
    require(len(data) == ref['bytes'] and hashlib.sha256(data).hexdigest() == ref['sha256'],
            'artifact length/SHA differs: ' + ref['path'])
    return data


def load_candidate(path, version, service_sha):
    candidate = json_bytes(safe_read(path))
    require(set(candidate) == {'kind', 'version', 'app_version', 'code_id', 'files', 'service_sha256',
                               'resident_sha256', 'frozen_manifest', 'reviews'}, 'invalid reviewed candidate fields')
    require(candidate['kind'] == 'resident-v4-reviewed-candidate' and candidate['app_version'] == 'resident-v4'
            and candidate['version'] == version, 'candidate version differs')
    require(re.fullmatch('[0-9a-f]{64}', candidate['code_id']) is not None, 'candidate code_id invalid')
    frozen = json_bytes(evidence_ref(candidate['frozen_manifest']))
    require(frozen == {key: candidate[key] for key in ('version', 'app_version', 'code_id', 'files')}
            | {'kind': 'resident-v4-frozen-candidate'}, 'frozen candidate differs')
    require(isinstance(candidate['reviews'], list) and 1 <= len(candidate['reviews']) <= 8,
            'independent review artifacts required')
    for ref in candidate['reviews']:
        review = json_bytes(evidence_ref(ref))
        require(set(review) == {'kind', 'disposition', 'frozen_manifest_sha256', 'report'}
                and review['kind'] == 'resident-v4-independent-review' and review['disposition'] == 'approved'
                and review['frozen_manifest_sha256'] == candidate['frozen_manifest']['sha256'],
                'review does not approve this frozen candidate')
        require(len(evidence_ref(review['report'])) > 0, 'independent report is empty')
    files = candidate['files']
    require(isinstance(files, dict) and 1 <= len(files) <= 4096, 'invalid candidate inventory')
    for name, record in files.items():
        relative = PurePosixPath(name)
        require(relative.as_posix() == name and not relative.is_absolute() and '..' not in relative.parts
                and '\\' not in name and name not in ('README.txt', 'manifest.json'), 'unsafe payload member: ' + name)
        require(isinstance(record, dict) and set(record) == {'bytes', 'sha256'}
                and type(record['bytes']) is int and record['bytes'] >= 0
                and isinstance(record['sha256'], str) and re.fullmatch('[0-9a-f]{64}', record['sha256']),
                'invalid payload identity: ' + name)
    required = {'src/app/' + name for name in CODE_NAMES} | {
        'src/app/icons/helmet.svg', 'src/app/icons/wifi.svg'}
    require(required <= files.keys() and any(name.startswith('models/') and name.endswith('.rknn') for name in files),
            'candidate lacks source/model/icon inventory')
    require(candidate['service_sha256'] == service_sha == files['src/app/service.py']['sha256'],
            'actual reviewed service SHA differs')
    require(candidate['resident_sha256'] == files['src/app/resident.py']['sha256'], 'reviewed resident SHA differs')
    return candidate


def verify_installed(files):
    for name, record in files.items():
        path = NEW_ROOT / name
        require(path.is_relative_to(NEW_ROOT), 'installed payload escapes root')
        data = safe_read(path)
        require(len(data) == record['bytes'] and hashlib.sha256(data).hexdigest() == record['sha256'],
                'installed payload differs: ' + name)


def check_bundle(args):
    candidate = load_candidate(args.reviewed_candidate, args.version, args.service_sha256)
    external = json_bytes(safe_read(args.manifest))
    require(set(external) == {'manifest', 'bytes', 'sha256'}, 'external manifest fields differ')
    archive = safe_read(args.bundle)
    require(type(external['bytes']) is int and len(archive) == external['bytes']
            and hashlib.sha256(archive).hexdigest() == external['sha256'], 'ZIP length/SHA differs')
    # Open the same verified bytes, avoiding pathname replacement after hashing.
    import io
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        names = zipped.namelist()
        require(len(names) == len(set(names)), 'duplicate ZIP member')
        manifest = json_bytes(zipped.read('manifest.json'))
        require(manifest == external['manifest'] and manifest.get('version') == args.version,
                'internal/external/version manifests differ')
        require(manifest.get('files') == candidate['files'], 'ZIP inventory differs from frozen reviewed source')
        require(set(names) == set(candidate['files']) | {'README.txt', 'manifest.json'}, 'unexpected ZIP inventory')
        for info in zipped.infolist():
            mode = info.external_attr >> 16
            require(stat.S_IFMT(mode) in (0, stat.S_IFREG) and not info.is_dir(), 'ZIP nonordinary member refused')
        for name, record in candidate['files'].items():
            info = zipped.getinfo(name)
            require(info.file_size == record['bytes'], 'ZIP member length differs: ' + name)
            data = zipped.read(name)
            require(hashlib.sha256(data).hexdigest() == record['sha256'], 'ZIP member SHA differs: ' + name)
    verify_installed(candidate['files'])
    require(code_identity(NEW_ROOT / 'src/app') == candidate['code_id'], 'actual source code_id differs')
    return dict(sha256=external['sha256'], bytes=external['bytes'], files=candidate['files']), candidate


def code_identity(app):
    fingerprint = hashlib.sha256()
    for name in CODE_NAMES:
        fingerprint.update(name.encode('ascii') + b'\0')
        fingerprint.update(hashlib.sha256(safe_read(app / name)).digest())
    return fingerprint.hexdigest()


def load_resident(candidate):
    path = NEW_ROOT / 'src/app/resident.py'
    data = safe_read(path)
    require(hashlib.sha256(data).hexdigest() == candidate['resident_sha256'], 'resident module changed before import')
    # Execute verified bytes only. resident.py imports stdlib and defines classes;
    # its __main__ entry is never invoked, and service/SDK/Qt is never imported.
    import types
    module = types.ModuleType('reviewed_icon_resident_' + uuid.uuid4().hex)
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    require(module.APP_VERSION == 'resident-v4' and module.PROTOCOL == 1, 'resident runtime protocol differs')
    require(module.code_identity(path.parent) == candidate['code_id'], 'resident computed code_id differs')
    return module


@contextmanager
def migration_barrier(module):
    module.require_linux()
    require(COORDINATOR.exists(), 'initialize and normally close reviewed App first: coordinator is absent')
    no_symlink_path(COORDINATOR)
    module._check_ancestors(COORDINATOR)
    row = COORDINATOR.lstat()
    require(stat.S_ISDIR(row.st_mode) and row.st_uid == os.geteuid() and stat.S_IMODE(row.st_mode) == 0o700,
            'initialize and normally close reviewed App first: existing private coordinator required')
    coordinator = module.Coordinator(COORDINATOR)
    coordinator.descriptor = os.open(COORDINATOR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    leases = []
    identities = {}
    try:
        opened = os.fstat(coordinator.descriptor)
        require((opened.st_dev, opened.st_ino) == (row.st_dev, row.st_ino), 'coordinator inode changed')
        for name in LOCK_NAMES:
            # Do not call acquire(O_CREAT): a plan must never initialize missing state.
            try:
                descriptor = coordinator.open_file(name, os.O_RDWR)
            except FileNotFoundError:
                raise RuntimeError('initialize and normally close reviewed App first: missing ' + name) from None
            lease = module.Lease(descriptor)
            leases.append(lease)
            info = os.fstat(descriptor)
            module.fcntl.flock(descriptor, module.fcntl.LOCK_EX | module.fcntl.LOCK_NB)
            actual = coordinator._stat(name)
            require(actual is not None and (actual.st_dev, actual.st_ino) == (info.st_dev, info.st_ino),
                    'lease inode replaced during acquisition')
            identities[name] = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}
        value = {'coordinator': str(COORDINATOR), 'locks': identities, 'nonblocking': True,
                 'held_through': 'verification/write/final-readback/conditional-rollback',
                 'legacy_boundary': 'root serializes unmodified frozen-v3 and standalone launches'}
        yield coordinator, value
    except BlockingIOError:
        raise RuntimeError('resident startup/manager/inherited resource lease is busy; no takeover') from None
    finally:
        for lease in reversed(leases):
            lease.close()  # Never LOCK_UN inherited open descriptions.
        coordinator.close()


def barrier_readback(coordinator, barrier):
    no_symlink_path(COORDINATOR)
    current = COORDINATOR.lstat()
    opened = os.fstat(coordinator.descriptor)
    require((current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino), 'coordinator changed while barrier held')
    require(current.st_uid == os.geteuid() and stat.S_IMODE(current.st_mode) == 0o700,
            'coordinator ownership/permissions changed while barrier held')
    for name, expected in barrier['locks'].items():
        row = coordinator._stat(name)
        require(row is not None and stat.S_ISREG(row.st_mode) and row.st_nlink == 1
                and (row.st_dev, row.st_ino, row.st_uid) ==
                (expected['device'], expected['inode'], expected['uid']), 'barrier lock replaced: ' + name)


def identity_valid(identity):
    return (isinstance(identity, dict) and set(identity) == {'pid', 'starttime', 'uid'}
            and all(type(identity[key]) is int for key in identity)
            and identity['pid'] > 0 and identity['starttime'] > 0 and identity['uid'] >= 0)


def terminal_identity(identity):
    require(identity_valid(identity), 'terminal process identity invalid')
    current = process_identity(identity['pid'])
    if current is not None:
        require(current['state'] in ('Z', 'X') or
                (int(current['start_id']) > identity['starttime']),
                'original owner remains live or PID reuse cannot be proved')
    return {'recorded': identity, 'observed': current}


def assert_terminal(args, module, coordinator, candidate):
    terminal = json_bytes(safe_read(args.terminal_evidence))
    require(set(terminal) == {'kind', 'candidate_code_id', 'resident_sessions', 'legacy_preflight'}
            and terminal['kind'] == 'resident-v4-icon-terminal-evidence'
            and terminal['candidate_code_id'] == candidate['code_id'], 'terminal candidate binding differs')
    sessions = terminal['resident_sessions']
    require(isinstance(sessions, list) and len(sessions) <= 128, 'terminal session list invalid')
    checked_sessions = []
    observed_record = coordinator.read_record()
    matched_record = observed_record is None
    for session in sessions:
        require(isinstance(session, dict) and set(session) == {'record', 'exit', 'extra_owners'}, 'session fields invalid')
        record = json_bytes(evidence_ref(session['record']))
        exit_receipt = json_bytes(evidence_ref(session['exit']))
        require(record.get('protocol') == 1 and record.get('version') == 'resident-v4'
                and record.get('root') == str(NEW_ROOT) and record.get('code_id') == candidate['code_id']
                and isinstance(record.get('session'), str) and re.fullmatch('[0-9a-f]{32}', record['session'])
                and record.get('phase') in ('completed', 'failed') and record.get('mode') in ('resident', 'one-shot')
                and record.get('workspace') == str(NEW_ROOT / 'app-state' / ('session-' + record['session'])),
                'resident terminal record root/session/phase/code differs')
        expected_exit = NEW_ROOT / 'app-state' / ('session-' + record['session']) / 'exit.json'
        require(Path(session['exit']['path']) == expected_exit, 'exit must be actual session receipt')
        require(exit_receipt.get('cleanup_completed') is True and exit_receipt.get('resource_lease_released') is True,
                'actual terminal cleanup/lease receipt required')
        if record['mode'] == 'resident':
            require(exit_receipt.get('kind') == 'resident-terminal-v4' and exit_receipt.get('phase') == record['phase'],
                    'resident terminal kind/phase differs')
            for key in ('session', 'root', 'version', 'code_id', 'manager', 'service', 'ui'):
                require(key in record and exit_receipt.get(key) == record[key], 'exit/record identity differs: ' + key)
        else:
            # The reviewed launch.py one-shot receipt has no root/code/role fields.
            # Bind its actual fixed workspace path and session to the full record.
            require(exit_receipt.get('mode') == 'one-shot' and exit_receipt.get('session') == record['session']
                    and exit_receipt.get('journal_failed') is False and exit_receipt.get('ui_exit') in (0, None),
                    'one-shot terminal journal/session/UI differs')
            require(all(role in record for role in ('manager', 'service', 'ui')), 'one-shot record owner fields absent')
        require(record.get('manager') is not None and exit_receipt.get('service_exit') in (0, None),
                'terminal manager/service completion invalid')
        lease = record.get('lease')
        current_lease = (COORDINATOR / 'resource.lock').lstat()
        require(isinstance(lease, dict)
                and (lease.get('service_alive') is False if record['mode'] == 'resident'
                     else lease.get('service_alive', False) is False)
                and type(lease.get('device')) is int and type(lease.get('inode')) is int
                and (lease['device'], lease['inode']) == (current_lease.st_dev, current_lease.st_ino),
                'terminal record retains live service evidence')
        require(isinstance(session['extra_owners'], list), 'extra owner list invalid')
        owners = [terminal_identity(record[key]) for key in ('manager', 'service', 'ui') if record[key] is not None]
        for extra in session['extra_owners']:
            require(set(extra) == {'role', 'identity'} and extra['role'] in ('client', 'observer', 'native', 'wifi'),
                    'extra owner schema invalid')
            owners.append(terminal_identity(extra['identity']))
        if observed_record == record:
            matched_record = True
        checked_sessions.append({'session': record['session'], 'owners': owners, 'exit_sha256': session['exit']['sha256']})
    require(matched_record, 'present manager record lacks exact current terminal evidence')
    if observed_record is not None:
        require(observed_record['phase'] in ('completed', 'failed'), 'resident is idle/hidden/live, not terminal')
        for role in ('manager', 'service', 'ui'):
            if observed_record[role] is not None:
                terminal_identity(observed_record[role])
    legacy = json_bytes(evidence_ref(terminal['legacy_preflight']))
    require(set(legacy) == {'kind', 'root', 'owners', 'cleanup_errors', 'terminal', 'legacy_launches_serialized'}
            and legacy['kind'] == 'legacy-owner-terminal-preflight' and legacy['root'] == str(OLD_ROOT)
            and legacy['cleanup_errors'] == [] and legacy['terminal'] is True
            and legacy['legacy_launches_serialized'] is True and isinstance(legacy['owners'], list),
            'legacy terminal preflight and external launch serialization required')
    legacy_owners = []
    for owner in legacy['owners']:
        require(set(owner) == {'role', 'identity', 'receipt'}, 'legacy owner schema invalid')
        require(isinstance(owner['role'], str) and owner['role'], 'legacy owner role invalid')
        receipt = json_bytes(evidence_ref(owner['receipt']))
        require(receipt.get('identity') == owner['identity'] and receipt.get('terminal') is True
                and receipt.get('cleanup_errors') == [], 'legacy actual cleanup receipt differs')
        legacy_owners.append(terminal_identity(owner['identity']))
    idle = assert_npu_idle()
    return {'sessions': checked_sessions, 'legacy_owners': legacy_owners,
            'terminal_evidence_sha256': sha256(args.terminal_evidence), 'present_idle': idle}


def exclusive_bytes(path, data, mode):
    checked_directory(path.parent)
    require(not path.exists() and not path.is_symlink(), 'output already exists: ' + str(path))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags, mode), 'wb') as stream:
        require(stream.write(data) == len(data), 'exclusive write was incomplete: ' + str(path))
        stream.flush()
        os.fsync(stream.fileno())
        if hasattr(os, 'fchmod'):
            os.fchmod(stream.fileno(), mode)
        else:
            os.chmod(path, mode)
    require(safe_read(path) == data, 'exclusive artifact readback differs: ' + str(path))


def file_identity(info, data):
    return {'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode),
            'uid': info.st_uid, 'gid': info.st_gid, 'mtime_ns': info.st_mtime_ns,
            'ctime_ns': info.st_ctime_ns, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def ordinary_snapshot(path):
    require(not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode), 'desktop must be an existing ordinary file: ' + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'desktop is not ordinary/singly linked: ' + str(path))
        data = stream.read()
        identity = file_identity(info, data)
        require(file_identity(os.fstat(stream.fileno()), data) == identity, 'desktop changed while reading: ' + str(path))
    require(file_identity(path.lstat(), data) == identity, 'desktop replaced while reading: ' + str(path))
    return {'data': data, 'identity': identity, 'atime_ns': info.st_atime_ns}


def desktop_bytes(root, name, tab, icon):
    # Exactly the format used by this release's existing app/install.py.
    app = root / 'src/app'
    return ('[Desktop Entry]\nType=Application\nVersion=1.0\nName=' + name
            + '\nExec=/usr/bin/python3 "' + str(app / 'launch.py') + '" --root "'
            + str(root) + '" --tab ' + tab + '\nIcon=' + str(app / 'icons' / (icon + '.svg'))
            + '\nTerminal=false\nCategories=Utility;\n').encode('utf-8')


def conditional_desktop_write(path, previous, data, owned_writes, restore_metadata=None):
    require(ordinary_snapshot(path)['identity'] == previous['identity'], 'desktop changed since owned snapshot: ' + str(path))
    # Register the attempted write before any bytes can change. Unbuffered I/O
    # ensures closing after a write/flush exception cannot later publish bytes
    # after the exception snapshot has been taken.
    written = {'data': None, 'identity': None, 'ownership_error': 'write state was not captured'}
    with os.fdopen(os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)), 'r+b', buffering=0) as stream:
        current = stream.read()
        require(file_identity(os.fstat(stream.fileno()), current) == previous['identity'], 'desktop changed before write: ' + str(path))
        owned_writes.append((path, written))
        try:
            stream.seek(0)
            require(stream.write(data) == len(data), 'desktop write was incomplete: ' + str(path))
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            mode = restore_metadata['identity']['mode'] if restore_metadata is not None else previous['identity']['mode']
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), mode)
            else:
                os.chmod(path, mode)
            if restore_metadata is not None:
                timestamps = (restore_metadata['atime_ns'], restore_metadata['identity']['mtime_ns'])
                os.utime(stream.fileno(), ns=timestamps)
            written.update(data=data, identity=file_identity(os.fstat(stream.fileno()), data), ownership_error=None)
        except Exception:
            try:
                info = os.fstat(stream.fileno())
                stream.seek(0)
                observed = stream.read()
                identity = file_identity(info, observed)
                original = previous['identity']
                require(all(identity[key] == original[key] for key in ('device', 'inode', 'uid', 'gid')),
                        'desktop file owner changed during failed write')
                # A failed write can leave a prefix overwrite and the original
                # tail, or the complete new content after truncate. Accept only
                # states reachable by this one write, never unrelated bytes.
                old = previous['data']
                reachable = observed == data or any(
                    observed == data[:count] + old[count:] for count in range(len(data) + 1))
                require(reachable, 'desktop contains an unowned concurrent change')
                require(file_identity(os.fstat(stream.fileno()), observed) == identity
                        and file_identity(path.lstat(), observed) == identity and not path.is_symlink(),
                        'desktop identity changed while capturing failed write')
                written.update(data=observed, identity=identity, ownership_error=None)
            except Exception as capture_error:
                written['ownership_error'] = str(capture_error)
            raise
    require(ordinary_snapshot(path)['identity'] == written['identity'], 'desktop replaced during write: ' + str(path))
    return written



def process_identity(pid):
    process = PROC_ROOT / str(pid)
    try:
        text = (process / 'stat').read_text()
    except FileNotFoundError:
        require(not process.exists(), 'process state is unreadable: ' + str(pid))
        return None
    fields = text.rsplit(')', 1)[1].split()
    require(len(fields) >= 20, 'malformed process identity: ' + str(pid))
    require(fields[19].isdigit(), 'malformed process start identity: ' + str(pid))
    return {'pid': pid, 'state': fields[0], 'start_id': fields[19], 'uid': process.stat().st_uid}


def descriptor_entry_present(descriptor):
    try:
        descriptor.lstat()
    except FileNotFoundError:
        return False
    return True


def assert_npu_idle():
    require(effective_uid() == 0, 'root visibility is required to prove no concurrent NPU task')
    mounts = [line.split() for line in (PROC_ROOT / 'mounts').read_text().splitlines()]
    proc_mounts = [row for row in mounts if len(row) >= 4 and row[1:3] == ['/proc', 'proc']]
    require(len(proc_mounts) == 1 and not any(option.startswith('hidepid=') and option != 'hidepid=0'
            for option in proc_mounts[0][3].split(',')), 'process visibility is hidden or cannot be proved')
    require(re.fullmatch(r'NPU load:\s*0%\s*', NPU_LOAD.read_text()) is not None,
            'NPU load is active or cannot be interpreted; migration refused')
    checked = []
    initial_pids = {int(p.name) for p in PROC_ROOT.iterdir() if p.name.isdigit()}
    require(bool(initial_pids), 'process inventory is empty; NPU quiescence cannot be proved')
    detection_names = {'helmet_realtime.py', 'dump_dets.py', 'benchmark.py', 'verify_runtime.py',
                       'verify_native_runtime.py'}
    for pid in sorted(initial_pids):
        try:
            identity = process_identity(pid)
            if identity is None or identity['state'] == 'Z':
                continue
            directory = PROC_ROOT / str(pid)
            command = [part.decode('utf-8', errors='replace') for part in (directory / 'cmdline').read_bytes().split(b'\0') if part]
            maps = (directory / 'maps').read_text()
            command_text = ' '.join(command).lower()
            known_python = any(Path(part).name in detection_names for part in command)
            known_app = any(Path(part).name in ('resident.py', 'service.py', 'launch.py') and '/app/' in part for part in command)
            known_qt = any(Path(part).name in ('qmlscene', 'qml') for part in command) and any('/app/' in part or '/app-state/' in part for part in command)
            root_owner = pid != os.getpid() and any(
                part == str(root) or part.startswith(str(root) + '/') for root in (OLD_ROOT, NEW_ROOT) for part in command)
            require(not known_python and not known_app and not known_qt and not root_owner and not any(token in command_text for token in ('.rknn', 'rknnlite', 'rknpu')),
                    'identified detection/NPU command remains live: ' + str(pid) + ' ' + ' '.join(command))
            require(not any(token in maps.lower() for token in ('librknn', 'librknpu', '/rknnlite/')),
                    'RKNN/NPU runtime mapping remains live: ' + str(pid))
            for descriptor in (directory / 'fd').iterdir():
                try:
                    destination = os.readlink(descriptor)
                except FileNotFoundError:
                    require(not descriptor_entry_present(descriptor),
                            'NPU handle identity cannot be read: ' + str(pid))
                    continue
                require(not any(token in destination.lower() for token in ('rknpu', '.rknn')),
                        'NPU handle remains live: ' + str(pid))
                try:
                    fdinfo = (directory / 'fdinfo' / descriptor.name).read_text()
                except FileNotFoundError:
                    require(not descriptor_entry_present(descriptor),
                            'NPU handle identity cannot be read: ' + str(pid))
                    continue
                require('rknpu' not in fdinfo.lower(), 'NPU handle remains live: ' + str(pid))
                if destination.startswith('/dev/dri/'):
                    try:
                        device = descriptor.stat().st_rdev
                        driver = Path('/sys/dev/char') / ('%d:%d' % (os.major(device), os.minor(device))) / 'device/driver'
                        driver_name = str(driver.resolve(strict=True)).lower()
                        require('rknpu' not in driver_name, 'NPU DRM handle remains live: ' + str(pid))
                    except FileNotFoundError:
                        require(not descriptor_entry_present(descriptor),
                                'NPU handle identity cannot be read: ' + str(pid))
            ending = process_identity(pid)
            require(ending is None or (ending['start_id'] == identity['start_id'] and ending['uid'] == identity['uid']),
                    'process start identity changed during NPU check: ' + str(pid))
            checked.append({'pid': pid, 'start_id': identity['start_id'], 'command': command})
        except FileNotFoundError:
            require(process_identity(pid) is None, 'live process is not fully inspectable: ' + str(pid))
    final_pids = {int(p.name) for p in PROC_ROOT.iterdir() if p.name.isdigit()}
    require(final_pids <= initial_pids, 'new process appeared during NPU check; retry read-only preflight')
    final_load = NPU_LOAD.read_text()
    require(re.fullmatch(r'NPU load:\s*0%\s*', final_load) is not None, 'NPU became active during preflight')
    return {'npu_load': final_load.strip(), 'processes': checked,
            'observation': 'read-only proc stat/cmdline/maps/fd/fdinfo plus NPU load; no process was stopped'}



class MigrationFailure(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(report['error'])


def link_plan():
    return [(Path('/opt/ui/src/apps/helmet_app'), OLD_ROOT / 'vendor-entry/helmet_app', NEW_ROOT / 'vendor-entry/helmet_app'),
            (Path('/opt/ui/src/apps/helmet_wifi'), OLD_ROOT / 'vendor-entry/helmet_wifi', NEW_ROOT / 'vendor-entry/helmet_wifi'),
            (Path('/opt/ui/src/appicons/helmet_app.svg'), OLD_ROOT / 'src/app/icons/helmet.svg', NEW_ROOT / 'src/app/icons/helmet.svg'),
            (Path('/opt/ui/src/appicons/helmet_wifi.svg'), OLD_ROOT / 'src/app/icons/wifi.svg', NEW_ROOT / 'src/app/icons/wifi.svg')]


def owned_link(path, target):
    no_symlink_path(path.parent)
    require(path.is_symlink() and os.readlink(path) == str(target), 'entry is not the exact owned literal target: ' + str(path))
    row = path.lstat()
    if sys.platform.startswith('linux'):
        require(row.st_uid == os.geteuid(), 'link owner differs: ' + str(path))
    return {'device': row.st_dev, 'inode': row.st_ino, 'uid': row.st_uid, 'target': str(target)}


def replace_link(path, target):
    staging = path.with_name('.helmet-v4-' + uuid.uuid4().hex)
    try:
        staging.symlink_to(target)
        os.replace(staging, path)
    finally:
        if staging.is_symlink():
            staging.unlink()


def wrapper_bytes(tab):
    return ('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(NEW_ROOT / 'src/app/launch.py'))
            + ' --root ' + shlex.quote(str(NEW_ROOT)) + ' --tab ' + tab + ' "$@"\n').encode('utf-8')


def check_new_targets(plan, release, wrappers):
    for path, old, new in plan:
        no_symlink_path(new)
        require(new.is_relative_to(NEW_ROOT), 'new target escapes root')
        relative = new.relative_to(NEW_ROOT).as_posix()
        record = wrappers.get(relative, release['files'].get(relative))
        require(record is not None, 'target is not in reviewed payload/generated wrappers')
        data = safe_read(new)
        require(len(data) == record['bytes'] and hashlib.sha256(data).hexdigest() == record['sha256'],
                'new target length/SHA differs: ' + relative)


def migrate(args):
    no_symlink_path(OLD_ROOT)
    require(OLD_ROOT.is_dir(), 'frozen-v3 root must be an existing directory')
    checked_directory(NEW_ROOT)
    candidate = load_candidate(args.reviewed_candidate, args.version, args.service_sha256)
    module = load_resident(candidate)
    with migration_barrier(module) as (coordinator, barrier):
        release, verified = check_bundle(args)
        require(candidate == verified, 'candidate changed before barrier verification')
        terminal = assert_terminal(args, module, coordinator, candidate)
        plan = link_plan()
        links = {str(path): owned_link(path, old) for path, old, new in plan}
        no_symlink_path(CONFIG)
        config = ordinary_snapshot(CONFIG)
        require(config['identity']['sha256'] == EXPECTED_CONFIG_SHA, 'vendor configuration differs from fixed SHA')
        desktops, after = {}, {}
        for filename, name, tab, icon in DESKTOPS:
            entry = DESKTOP_DIR / filename
            no_symlink_path(entry)
            saved = ordinary_snapshot(entry)
            if sys.platform.startswith('linux'):
                require(saved['identity']['uid'] == os.geteuid(), 'desktop owner differs: ' + str(entry))
            require(saved['data'] == desktop_bytes(OLD_ROOT, name, tab, icon), 'desktop is not exact frozen-v3 entry')
            desktops[filename] = saved
            after[filename] = saved['data'].replace(str(OLD_ROOT).encode('utf-8'), str(NEW_ROOT).encode('utf-8'))
        assets = NEW_ROOT / 'vendor-entry'
        checked_directory(assets, allow_missing=True)
        wrappers = {assets / name: wrapper_bytes(tab) for name, tab in (('helmet_app', 'detection'), ('helmet_wifi', 'wifi'))}
        for path in wrappers:
            require(not path.exists() and not path.is_symlink(), 'preexisting wrapper: inspect failed/staging evidence; ambiguous repeat refused')
        report = {'kind': 'resident-v4-owned-icon-migration', 'applied': False, 'release': release,
                  'candidate_sha256': sha256(args.reviewed_candidate), 'code_id': candidate['code_id'],
                  'version': args.version, 'service_sha256': args.service_sha256, 'resident_sha256': candidate['resident_sha256'],
                  'candidate': candidate, 'barrier': barrier, 'terminal_preflight': terminal,
                  'links_before': links, 'vendor_config_sha256_before': config['identity']['sha256'],
                  'desktop_before': {name: dict(saved['identity'], atime_ns=saved['atime_ns']) for name, saved in desktops.items()},
                  'desktop_expected': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in after.items()},
                  'human_icon_tap': False, 'detection_started': False, 'desktop_restarted': False, 'wifi_changed': False}
        if not args.apply:
            barrier_readback(coordinator, barrier)
            return report
        backup = assets / ('migration-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex)
        report['backup_directory'] = str(backup)
        operations = []
        try:
            if not assets.exists():
                assets.mkdir(mode=0o755)
            checked_directory(assets)
            backup.mkdir(mode=0o700)
            checked_directory(backup)
            exclusive_bytes(backup / 'apk3.before.cfg', config['data'], 0o600)
            for name, saved in desktops.items():
                exclusive_bytes(backup / name, saved['data'], 0o600)
            exclusive_bytes(backup / 'before.json', report_bytes(report), 0o600)
            wrapper_ids = {}
            for path, data in wrappers.items():
                exclusive_bytes(path, data, 0o755)
                wrapper_ids[path.relative_to(NEW_ROOT).as_posix()] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            check_new_targets(plan, release, wrapper_ids)
            verify_installed(release['files'])
            report['terminal_before_system_write'] = assert_terminal(args, module, coordinator, candidate)
            barrier_readback(coordinator, barrier)
            require(ordinary_snapshot(CONFIG)['identity'] == config['identity'], 'cfg changed before write; preserved')
            for path, old, new in plan:
                require(owned_link(path, old) == links[str(path)], 'link changed before write')
            for name, saved in desktops.items():
                require(ordinary_snapshot(DESKTOP_DIR / name)['identity'] == saved['identity'], 'desktop changed before write')
            for name, saved in desktops.items():
                writes = []
                operations.append(('desktop', DESKTOP_DIR / name, writes))
                conditional_desktop_write(DESKTOP_DIR / name, saved, after[name], writes, restore_metadata=saved)
            for path, old, new in plan:
                require(owned_link(path, old) == links[str(path)], 'link changed before replacement')
                check_new_targets([(path, old, new)], release, wrapper_ids)
                # Register before replace; an exception after actual rename is owned.
                operations.append(('link', path, (old, new)))
                replace_link(path, new)
                owned_link(path, new)
            report['terminal_after_system_write'] = assert_terminal(args, module, coordinator, candidate)
            barrier_readback(coordinator, barrier)
            require(ordinary_snapshot(CONFIG)['identity'] == config['identity'], 'cfg changed during migration; preserved')
            for path, old, new in plan:
                owned_link(path, new)
            check_new_targets(plan, release, wrapper_ids)
            verify_installed(release['files'])
            for kind, path, value in operations:
                if kind == 'desktop':
                    require(len(value) == 1 and ordinary_snapshot(path)['identity'] == value[0][1]['identity'], 'desktop final readback differs')
            report.update(applied=True, vendor_config_sha256_after=sha256(CONFIG), wrappers=wrapper_ids,
                          links_after={str(path): owned_link(path, new) for path, old, new in plan},
                          desktop_files={name: ordinary_snapshot(DESKTOP_DIR / name)['identity'] for name in desktops})
            exclusive_bytes(backup / 'result.json', report_bytes(report), 0o600)
            # Final present-state checks also cover the result publication boundary.
            assert_terminal(args, module, coordinator, candidate)
            barrier_readback(coordinator, barrier)
            require(ordinary_snapshot(CONFIG)['identity'] == config['identity'], 'cfg changed during result publication; preserved')
            for path, old, new in plan:
                owned_link(path, new)
            check_new_targets(plan, release, wrapper_ids)
            for kind, path, value in operations:
                if kind == 'desktop':
                    require(ordinary_snapshot(path)['identity'] == value[0][1]['identity'], 'desktop changed during result publication')
            return report
        except Exception as error:
            report.update(applied=False, error=str(error), rollback=[])
            for kind, path, value in reversed(operations):
                try:
                    if kind == 'link':
                        old, new = value
                        if path.is_symlink() and os.readlink(path) == str(old):
                            require(owned_link(path, old) == links[str(path)], 'old link now has unowned identity; preserved')
                        else:
                            owned_link(path, new)
                            replace_link(path, old)
                            owned_link(path, old)
                    elif value:
                        written = value[0][1]
                        require(written['identity'] is not None, written.get('ownership_error') or 'failed desktop ownership unknown')
                        saved = desktops[path.name]
                        conditional_desktop_write(path, written, saved['data'], [], restore_metadata=saved)
                    report['rollback'].append({'path': str(path), 'restored': True})
                except Exception as rollback_error:
                    report['rollback'].append({'path': str(path), 'restored': False, 'reason': str(rollback_error)})
            report['rollback_complete'] = all(row['restored'] for row in report['rollback'])
            try:
                barrier_readback(coordinator, barrier)
            except Exception as barrier_error:
                report['barrier_error'] = str(barrier_error)
            try:
                report['vendor_config_sha256_after'] = sha256(CONFIG)
            except Exception as cfg_error:
                report['cfg_readback_error'] = str(cfg_error)
            # Preserve result.json even if partially written. Always fresh failure receipt.
            receipt_parent = backup if backup.is_dir() and not backup.is_symlink() else assets
            failure = receipt_parent / ('failure-' + uuid.uuid4().hex + '.json')
            report['failure_receipt'] = str(failure)
            try:
                exclusive_bytes(failure, report_bytes(report), 0o600)
            except Exception as receipt_error:
                report['failure_receipt_error'] = str(receipt_error)
                report['failure_receipt_complete'] = False
            else:
                report['failure_receipt_complete'] = True
            raise MigrationFailure(report) from error


def report_bytes(report):
    return json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'manifest', 'reviewed-candidate', 'terminal-evidence'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--service-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    require(sys.platform.startswith('linux'), 'only the named Linux board can run migration')
    print(report_bytes(migrate(args)).decode('utf-8'))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except MigrationFailure as error:
        print(report_bytes(error.report).decode('utf-8'), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print('Migration refused: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
