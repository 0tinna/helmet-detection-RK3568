#!/usr/bin/env python3
"""Record the actual v2 vendor wrapper path, optionally in an isolated netns.

Only launches the already-installed App's bounded verification flags. Network
isolation changes only the child namespace; WiFi tests read/scan, never connect.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


APP_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v2')
OUTPUT = Path('/userdata/helmet-validation/20261115-native-launch-checks')
SERVICE_SHA = '9a2f7454db6fb48ebcf7f5fd301a831817374c2be2fc5c9036af60feb40df1f7'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def child(args):
    # This board has no util-linux unshare executable. The kernel namespace
    # operation is performed only in this freshly created helper child.
    library = ctypes.CDLL(None, use_errno=True)
    require(library.unshare(0x40000000) == 0, 'child CLONE_NEWNET failed: ' + str(ctypes.get_errno()))
    own_namespace = os.readlink('/proc/self/ns/net')
    require(own_namespace != args.host_namespace, 'network namespace was not isolated')
    subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
    interfaces = json.loads(subprocess.check_output(['ip', '-j', 'link'], text=True))
    require(len(interfaces) == 1 and interfaces[0]['ifname'] == 'lo' and 'UP' in interfaces[0]['flags'],
            'isolated App network contains unexpected interfaces')
    receipt = {'network_namespace_isolated': True, 'host_namespace': args.host_namespace,
               'child_namespace': own_namespace, 'interfaces': interfaces}
    (args.output / 'network.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    with (args.output / 'launcher.log').open('xb') as log:
        return subprocess.run(['/opt/ui/src/apps/helmet_app', '--auto-demo', '--smoke-seconds', '12'],
                              stdout=log, stderr=subprocess.STDOUT).returncode


def main(args):
    require(APP_ROOT.resolve() == APP_ROOT, 'unexpected App root symlink')
    require(digest(APP_ROOT / 'src/app/service.py') == SERVICE_SHA, 'wrong App service revision')
    key = 'helmet_wifi' if args.mode == 'wifi' else 'helmet_app'
    wrapper = Path('/opt/ui/src/apps') / key
    require(wrapper.is_symlink() and wrapper.resolve() == APP_ROOT / 'vendor-entry' / key,
            'vendor wrapper does not point to v2')
    output = OUTPUT / args.run_id
    require(args.run_id.isalnum() or all(c.isalnum() or c == '-' for c in args.run_id), 'invalid run id')
    require(output.resolve().is_relative_to(OUTPUT), 'output escaped validation root')
    output.mkdir(parents=True, exist_ok=False)
    state = APP_ROOT / 'app-state'
    before = {path.name for path in state.glob('session-*')}
    namespace = os.readlink('/proc/self/ns/net')
    governor_path = Path('/sys/class/devfreq/fde40000.npu/governor')
    governor = governor_path.read_text().strip()
    started = time.monotonic()
    if args.mode == 'offline':
        command = [sys.executable, str(Path(__file__).resolve()), '--child',
                   '--output', str(output), '--host-namespace', namespace]
        code = subprocess.run(command).returncode
    else:
        # Leave enough time for the bounded eight-second scan to finish before
        # the launcher's final keyboard/content screenshot.
        flags = ['--verify-keyboard', '--verify-scan', '--smoke-seconds', '30'] if args.mode == 'wifi' else ['--auto-demo', '--smoke-seconds', '12']
        command = [str(wrapper), *flags]
        with (output / 'launcher.log').open('xb') as log:
            code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
    after = {path.name for path in state.glob('session-*')}
    new_sessions = sorted(after - before)
    require(len(new_sessions) == 1, 'launcher did not produce exactly one new session')
    session = state / new_sessions[0]
    exit_receipt = json.loads((session / 'exit.json').read_text(encoding='utf-8'))
    require(code == 0 and exit_receipt == {'service_exit': 0, 'ui_exit': 0, 'cleanup_completed': True},
            'actual launcher or cleanup did not succeed')
    require(not (session / 'token').exists() and not (session / 'App.qml').exists(), 'private session material was not cleaned')
    import cv2
    screen = session / 'screen.png'
    decoded = cv2.imread(str(screen))
    require(decoded is not None and decoded.size > 0, 'actual Qt verification screenshot is missing')
    require(os.readlink('/proc/self/ns/net') == namespace, 'parent host namespace changed')
    require(governor_path.read_text().strip() == governor, 'NPU governor changed')
    record = {'kind': 'actual-v2-vendor-launcher-verification', 'run_id': args.run_id, 'mode': args.mode,
              'command': command, 'wrapper': str(wrapper), 'wrapper_target': str(wrapper.resolve()),
              'wrapper_sha256': digest(wrapper), 'service_sha256': SERVICE_SHA,
              'wifi_sha256': digest(APP_ROOT / 'src/app/wifi.py'),
              'launch_py_executed': True, 'elapsed_including_startup_cleanup_seconds': time.monotonic() - started,
              'exit_code': code, 'session': str(session), 'exit_receipt': exit_receipt,
              'screen': {'path': str(screen), 'sha256': digest(screen), 'size': list(decoded.shape)},
              'private_material_cleaned': True, 'host_namespace_preserved': True,
              'network_namespace_isolated': args.mode == 'offline',
              'governor_before': governor, 'governor_after': governor_path.read_text().strip(),
              'wifi_connect_attempted': False, 'physical_camera_tested': False,
              'human_icon_tap': False, 'human_touch_verified': False,
              'screen_scope': 'Qt screenRoot contents; excludes top header; not a physical screen photo',
              'acceptance_scope': 'bounded actual wrapper/launch.py/service/Qt verification; not a new30-minute performance run'}
    (output / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'run_id': args.run_id, 'exit_code': code, 'session': str(session), 'output': str(output)}))
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id')
    parser.add_argument('--mode', choices=('detection', 'wifi', 'offline'))
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--host-namespace')
    args = parser.parse_args()
    try:
        if args.child:
            raise SystemExit(child(args))
        require(args.run_id and args.mode, 'run-id and mode are required')
        raise SystemExit(main(args))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('v2 launcher verification failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
