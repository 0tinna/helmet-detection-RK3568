#!/usr/bin/env python3
"""Measure resident idle CPU and private-record replacement frequency."""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--seconds', type=float, default=10)
    args = parser.parse_args(argv)
    root = Path(args.root)
    sys.path.insert(0, str(root / 'src'))
    from app import resident

    coordinator = resident.Coordinator().prepare()
    record = coordinator.read_record()
    if record is None or record.get('mode') != 'resident':
        raise RuntimeError('installed resident instance is not running')
    workspace = resident.Coordinator(Path(record['workspace'])).prepare()
    try:
        role_pids = {
            'manager': record['manager']['pid'],
            'service': record['service']['pid'],
            'ui': record['ui']['pid'] if record.get('ui') else None,
        }

        def ticks(pid):
            fields = Path('/proc/%d/stat' % pid).read_text().split()
            return int(fields[13]) + int(fields[14])

        def mtime(path):
            try:
                return path.stat().st_mtime_ns
            except FileNotFoundError:
                return None

        before = {role: ticks(pid) for role, pid in role_pids.items() if pid}
        record_path = coordinator.directory / 'record.json'
        service_path = workspace.directory / 'service-state.json'
        previous = {
            'record': mtime(record_path),
            'service_state': mtime(service_path),
        }
        changes = {key: 0 for key in previous}
        began = time.monotonic()
        next_sample = began + 0.05
        while time.monotonic() - began < args.seconds:
            delay = next_sample - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            for key, path in (('record', record_path), ('service_state', service_path)):
                current = mtime(path)
                if current != previous[key]:
                    changes[key] += 1
                    previous[key] = current
            next_sample += 0.05
        elapsed = time.monotonic() - began
        after = {role: ticks(pid) for role, pid in role_pids.items() if pid}
        result = {
            'session': record['session'],
            'roles': role_pids,
            'elapsed': elapsed,
            'cpu_percent': {
                role: (after[role] - before[role]) / os.sysconf('SC_CLK_TCK') / elapsed * 100
                for role in before
            },
            'record_replacements': changes['record'],
            'record_replacements_per_second': changes['record'] / elapsed,
            'service_state_replacements': changes['service_state'],
            'service_state_replacements_per_second': changes['service_state'] / elapsed,
        }
    finally:
        workspace.close()
        coordinator.close()
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
