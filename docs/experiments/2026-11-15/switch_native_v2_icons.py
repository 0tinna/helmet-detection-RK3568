#!/usr/bin/env python3
"""One recorded migration of the four known App links on this validation board.

This is an experiment maintenance helper, not a general upgrade installer.
It never restarts the desktop, starts detection, or changes WiFi.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import uuid
import zipfile


OLD_ROOT = Path('/userdata/helmet-validation/20261114-native-app')
NEW_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v2')
RUN_ROOT = Path('/userdata/helmet-validation/20261115-native-runtime-tools/native-longrun-1')
CONFIG = Path('/opt/ui/src/ATK-DLRK3568/apk3.cfg')
PROC_ROOT = Path('/proc')
NPU_LOAD = Path('/sys/kernel/debug/rknpu/load')
EXPECTED_SERVICE = '9a2f7454db6fb48ebcf7f5fd301a831817374c2be2fc5c9036af60feb40df1f7'
DESKTOPS = (('helmet-detection.desktop', '安全帽检测', 'detection', 'helmet'),
            ('helmet-wifi.desktop', 'Wi-Fi 设置', 'wifi', 'wifi'))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def checked_directory(path, allow_missing=False):
    require(path.resolve().is_relative_to(NEW_ROOT), 'output directory escapes v2 root: ' + str(path))
    require(not path.is_symlink(), 'output directory is a symlink: ' + str(path))
    if path.exists():
        require(stat.S_ISDIR(path.lstat().st_mode), 'output path is not a directory: ' + str(path))
    else:
        require(allow_missing, 'output directory is missing: ' + str(path))


def exclusive_bytes(path, data, mode):
    checked_directory(path.parent)
    require(not path.exists() and not path.is_symlink(), 'output already exists: ' + str(path))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags, mode), 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        if hasattr(os, 'fchmod'):
            os.fchmod(stream.fileno(), mode)
        else:
            os.chmod(path, mode)


def file_identity(info, data):
    return {'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode),
            'uid': info.st_uid, 'gid': info.st_gid, 'mtime_ns': info.st_mtime_ns,
            'ctime_ns': info.st_ctime_ns, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def ordinary_snapshot(path):
    require(not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode), 'desktop must be an existing ordinary file: ' + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), 'desktop is not ordinary: ' + str(path))
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
            mode = restore_metadata['identity']['mode'] if restore_metadata is not None else 0o644
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


def link_plan():
    return [
        (Path('/opt/ui/src/apps/helmet_app'), OLD_ROOT / 'vendor-entry/helmet_app', NEW_ROOT / 'vendor-entry/helmet_app'),
        (Path('/opt/ui/src/apps/helmet_wifi'), OLD_ROOT / 'vendor-entry/helmet_wifi', NEW_ROOT / 'vendor-entry/helmet_wifi'),
        (Path('/opt/ui/src/appicons/helmet_app.svg'), OLD_ROOT / 'src/app/icons/helmet.svg', NEW_ROOT / 'src/app/icons/helmet.svg'),
        (Path('/opt/ui/src/appicons/helmet_wifi.svg'), OLD_ROOT / 'src/app/icons/wifi.svg', NEW_ROOT / 'src/app/icons/wifi.svg'),
    ]


def check_bundle(archive, external_manifest):
    record = json.loads(external_manifest.read_text(encoding='utf-8'))
    require(record['manifest']['version'] == '20261115-v2', 'wrong release version')
    require(sha256(archive) == record['sha256'], 'ZIP SHA mismatch')
    require(archive.stat().st_size == record['bytes'], 'ZIP length mismatch')
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read('manifest.json'))
        require(manifest == record['manifest'], 'internal and external manifests differ')
        require(set(zipped.namelist()) == set(manifest['files']) | {'README.txt', 'manifest.json'}, 'unexpected ZIP inventory')
        for name, identity in manifest['files'].items():
            destination = NEW_ROOT / name
            require(destination.resolve().is_relative_to(NEW_ROOT.resolve()), 'payload escapes installation root')
            data = zipped.read(name)
            require(len(data) == identity['bytes'] and hashlib.sha256(data).hexdigest() == identity['sha256'], 'bad payload: ' + name)
            require(destination.is_file() and destination.stat().st_size == identity['bytes']
                    and sha256(destination) == identity['sha256'], 'installed payload differs: ' + name)
    require(sha256(NEW_ROOT / 'src/app/service.py') == EXPECTED_SERVICE, 'preview fix is absent')
    return {'sha256': record['sha256'], 'bytes': record['bytes'], 'payload_files': len(manifest['files']),
            'files': manifest['files']}


def process_identity(pid):
    process = PROC_ROOT / str(pid)
    try:
        text = (process / 'stat').read_text()
    except FileNotFoundError:
        require(not process.exists(), 'process state is unreadable: ' + str(pid))
        return None
    fields = text.rsplit(')', 1)[1].split()
    require(len(fields) >= 20, 'malformed process identity: ' + str(pid))
    return {'pid': pid, 'state': fields[0], 'start_id': fields[19]}


def assert_npu_idle():
    require(os.geteuid() == 0, 'root visibility is required to prove no concurrent NPU task')
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
            known_app = any(Path(part).name in ('service.py', 'launch.py') and '/app/' in part for part in command)
            require(not known_python and not known_app and not any(token in command_text for token in ('.rknn', 'rknnlite', 'rknpu')),
                    'identified detection/NPU command remains live: ' + str(pid) + ' ' + ' '.join(command))
            require(not any(token in maps.lower() for token in ('librknn', 'librknpu', '/rknnlite/')),
                    'RKNN/NPU runtime mapping remains live: ' + str(pid))
            for descriptor in (directory / 'fd').iterdir():
                try:
                    destination = os.readlink(descriptor)
                    fdinfo = (directory / 'fdinfo' / descriptor.name).read_text()
                    require(not any(token in destination.lower() for token in ('rknpu', '.rknn'))
                            and 'rknpu' not in fdinfo.lower(), 'NPU handle remains live: ' + str(pid))
                    if destination.startswith('/dev/dri/'):
                        device = descriptor.stat().st_rdev
                        driver = Path('/sys/dev/char') / ('%d:%d' % (os.major(device), os.minor(device))) / 'device/driver'
                        driver_name = str(driver.resolve(strict=True)).lower()
                        require('rknpu' not in driver_name, 'NPU DRM handle remains live: ' + str(pid))
                except FileNotFoundError:
                    require(not descriptor.exists(), 'NPU handle identity cannot be read: ' + str(pid))
            ending = process_identity(pid)
            require(ending is None or ending['start_id'] == identity['start_id'],
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


def assert_run_finished():
    manifest = json.loads((RUN_ROOT / 'manifest.json').read_text(encoding='utf-8'))
    result = json.loads((RUN_ROOT / 'run-verification.json').read_text(encoding='utf-8'))
    require(manifest['status'] == 'completed' and manifest['exit_code'] == 0, 'longrun is not successfully terminal')
    require(manifest['run_id'] == 'native-longrun-1', 'unexpected run identity')
    require(result['passed'] is True and result['thirty_minute_app_passed'] is True, 'longrun continuity/render gate is not accepted')
    receipt = json.loads((RUN_ROOT / 'cleanup.json').read_text(encoding='utf-8'))
    require(manifest.get('cleanup_contract') == 'direct-sdk-v1', 'unknown service/worker ownership contract')
    for key, value in (('cleanup', manifest['cleanup']), ('service', manifest['service_cleanup']),
                       ('gui', manifest['gui']), ('cleanup_errors', manifest.get('cleanup_errors', []))):
        require(receipt.get(key) == value, 'direct cleanup receipt differs: ' + key)
    require(not receipt['cleanup_errors'], 'run cleanup has errors')
    for key, expected in (('engine_released', True), ('source_released', True),
                          ('capture_worker_alive', False), ('detect_worker_alive', False), ('shutdown_timeout', False)):
        require(receipt['cleanup'].get(key) is expected, 'detection worker/SDK cleanup is not proven: ' + key)
    for key, expected in (('detection_stopped', True), ('wifi_stopped', True),
                          ('http_worker_alive', False), ('app_worker_alive', False)):
        require(receipt['service'].get(key) is expected, 'runner service thread cleanup is not proven: ' + key)
    gui = manifest['gui']
    require(gui.get('requested') is True and gui.get('started') is True and gui.get('exited') is True
            and gui.get('returncode') == 0, 'recorded GUI is not successfully terminal')
    gui_starts = set()
    with (RUN_ROOT / 'resources.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            child = row.get('gui')
            if isinstance(child, dict):
                require(child.get('pid') == gui['pid'] and child.get('start_id'), 'GUI resource identity is missing/inconsistent')
                gui_starts.add(str(child['start_id']))
    require(len(gui_starts) == 1, 'GUI start identity cannot be proved')
    identities = []
    for role, pid, start in (('runner/direct-service-workers', manifest['pid'], manifest.get('process_start_id')),
                             ('gui', gui['pid'], next(iter(gui_starts)))):
        current = process_identity(pid)
        outcome = 'absent'
        if current is not None:
            same = str(start) == current['start_id'] if start is not None else None
            if same is None:
                # This runner format records monotonic launch time, not an exact
                # stat token. A live PID cannot pass without proving later reuse.
                same = int(current['start_id']) / os.sysconf('SC_CLK_TCK') <= manifest['started_monotonic'] + 1
            outcome = 'zombie' if current['state'] == 'Z' else 'reused-pid'
            require(not same or current['state'] == 'Z', role + ' remains live or its identity cannot be excluded')
        identities.append({'role': role, 'pid': pid, 'recorded_start_id': start, 'observed': current, 'outcome': outcome})
    idle = assert_npu_idle()
    return {'run_id': manifest['run_id'], 'status': manifest['status'], 'exit_code': manifest['exit_code'],
            'thirty_minute_app_passed': result['thirty_minute_app_passed'], 'owned_processes': identities, 'npu_idle': idle}


def replace_link(target, source):
    staging = target.with_name('.helmet-v2-' + uuid.uuid4().hex)
    try:
        staging.symlink_to(source)
        os.replace(staging, target)
    finally:
        if staging.is_symlink():
            staging.unlink()


def wrapper_bytes(tab):
    return ('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(NEW_ROOT / 'src/app/launch.py'))
            + ' --root ' + shlex.quote(str(NEW_ROOT)) + ' --tab ' + tab + ' "$@"\n').encode('utf-8')


def check_new_targets(plan, release, wrapper_hashes):
    for target, old, new in plan:
        require(new.resolve(strict=True).is_relative_to(NEW_ROOT), 'new link target escapes v2 root: ' + str(new))
        relative = new.relative_to(NEW_ROOT).as_posix()
        expected = wrapper_hashes.get(relative)
        if expected is None:
            require(relative in release['files'], 'new target is not a verified payload: ' + relative)
            identity = release['files'][relative]
            expected = identity['sha256']
            require(new.stat().st_size == identity['bytes'], 'new target length differs: ' + relative)
        require(new.is_file() and sha256(new) == expected, 'new target SHA differs: ' + relative)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='apply the reviewed migration after all preconditions pass')
    args = parser.parse_args()
    require(sys.platform == 'linux', 'this migration is only for the named Linux board')
    require(OLD_ROOT.resolve() == OLD_ROOT and NEW_ROOT.resolve() == NEW_ROOT, 'unexpected installation-root symlink')
    checked_directory(NEW_ROOT)
    release = check_bundle(args.bundle, args.manifest)
    run = assert_run_finished()
    plan = link_plan()
    before = []
    for target, old, new in plan:
        require(target.is_symlink() and target.resolve() == old.resolve(), 'entry is not owned by the expected v1 root: ' + str(target))
        before.append({'path': str(target), 'old_target': os.readlink(target), 'new_target': str(new)})
    require(CONFIG.is_file(), 'vendor configuration is missing')
    config_before = CONFIG.read_bytes()
    config_sha = hashlib.sha256(config_before).hexdigest()
    desktops = Path.home() / '.local/share/applications'
    desktop_before = {}
    desktop_after = {}
    for filename, name, tab, icon in DESKTOPS:
        entry = desktops / filename
        saved = ordinary_snapshot(entry)
        require(saved['data'] == desktop_bytes(OLD_ROOT, name, tab, icon), 'desktop is not the expected v1 entry: ' + str(entry))
        desktop_before[filename] = saved
        desktop_after[filename] = desktop_bytes(NEW_ROOT, name, tab, icon)
    assets = NEW_ROOT / 'vendor-entry'
    checked_directory(assets, allow_missing=True)
    wrappers = {assets / key: wrapper_bytes(tab) for key, tab in (('helmet_app', 'detection'), ('helmet_wifi', 'wifi'))}
    for executable in wrappers:
        require(not executable.exists() and not executable.is_symlink(), 'new wrapper already exists; inspect rather than replace it')
    report = {'kind': 'native-v2-owned-icon-migration', 'applied': False, 'release': release, 'run': run,
              'links': before, 'vendor_config_sha256_before': config_sha, 'desktop_restarted': False,
              'detection_started': False, 'wifi_changed': False, 'human_icon_tap': False,
              'desktop_before': {name: dict(saved['identity'], atime_ns=saved['atime_ns']) for name, saved in desktop_before.items()},
              'desktop_expected_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in desktop_after.items()}}
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    backup = assets / ('migration-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    checked_directory(backup, allow_missing=True)
    require(not backup.exists() and not backup.is_symlink(), 'backup already exists')
    if not assets.exists():
        assets.mkdir(mode=0o755)
    checked_directory(assets)
    backup.mkdir(mode=0o700)
    checked_directory(backup)
    exclusive_bytes(backup / 'apk3.before.cfg', config_before, 0o600)
    for name, saved in desktop_before.items():
        exclusive_bytes(backup / name, saved['data'], 0o600)
    report['backup_directory'] = str(backup)
    exclusive_bytes(backup / 'before.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'), 0o600)
    changed = []
    owned_desktops = []
    try:
        for executable, data in wrappers.items():
            exclusive_bytes(executable, data, 0o755)
        wrapper_hashes = {path.relative_to(NEW_ROOT).as_posix(): hashlib.sha256(data).hexdigest() for path, data in wrappers.items()}
        check_new_targets(plan, release, wrapper_hashes)
        # Recheck all owned entries and live NPU evidence immediately before the
        # first system write. The direct service/worker cleanup belongs to runner.
        report['run_before_system_write'] = assert_run_finished()
        require(sha256(CONFIG) == config_sha, 'vendor config changed since preflight')
        for target, old, new in plan:
            require(target.is_symlink() and target.resolve() == old.resolve(), 'entry changed since preflight: ' + str(target))
        for name, saved in desktop_before.items():
            require(ordinary_snapshot(desktops / name)['identity'] == saved['identity'], 'desktop changed since preflight: ' + name)
        for name, saved in desktop_before.items():
            conditional_desktop_write(desktops / name, saved, desktop_after[name], owned_desktops)
        for target, old, new in plan:
            require(target.is_symlink() and target.resolve() == old.resolve(), 'entry changed since preflight: ' + str(target))
            check_new_targets([(target, old, new)], release, wrapper_hashes)
            replace_link(target, new)
            changed.append((target, old, new))
        require(sha256(CONFIG) == config_sha, 'vendor config changed during migration')
        for target, old, new in plan:
            require(target.is_symlink() and target.resolve() == new.resolve(), 'new link verification failed')
        check_new_targets(plan, release, wrapper_hashes)
        for path, written in owned_desktops:
            require(ordinary_snapshot(path)['identity'] == written['identity'], 'desktop final verification failed: ' + str(path))
        report['applied'] = True
        report['vendor_config_sha256_after'] = sha256(CONFIG)
        report['wrappers'] = {key: sha256(assets / key) for key in ('helmet_app', 'helmet_wifi')}
        report['desktop_files'] = {path.name: written['identity'] for path, written in owned_desktops}
        exclusive_bytes(backup / 'result.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'), 0o600)
    except Exception as error:
        report['rollback'] = []
        for target, old, new in reversed(changed):
            try:
                require(target.is_symlink() and os.readlink(target) == str(new), 'link no longer belongs to this write; preserved')
                replace_link(target, old)
                report['rollback'].append({'path': str(target), 'restored': True})
            except Exception as rollback_error:
                report['rollback'].append({'path': str(target), 'restored': False, 'reason': str(rollback_error)})
        for entry, written in reversed(owned_desktops):
            try:
                require(written['identity'] is not None, written.get('ownership_error') or 'failed write ownership cannot be proved; preserved')
                saved = desktop_before[entry.name]
                conditional_desktop_write(entry, written, saved['data'], [], restore_metadata=saved)
                report['rollback'].append({'path': str(entry), 'restored': True})
            except Exception as rollback_error:
                report['rollback'].append({'path': str(entry), 'restored': False, 'reason': str(rollback_error)})
        report['error'] = str(error)
        report['rollback_complete'] = all(row['restored'] for row in report['rollback'])
        report['applied'] = False
        exclusive_bytes(backup / 'result.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'), 0o600)
        raise
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        print('Migration refused: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
