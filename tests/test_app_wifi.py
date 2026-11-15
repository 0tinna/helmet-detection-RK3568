"""WiFi behavior through a fake wpa control peer; no board/network changes."""
import importlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakePeer:
    def __init__(self):
        self.networks = {0: {'ssid': 'old', 'enabled': True},
                         1: {'ssid': 'disabled', 'enabled': False},
                         2: {'ssid': 'other', 'enabled': True}}
        self.selected = 0
        self.commands = []
        self.fail = None
        self.connects = True
        self.current_ip = '192.168.1.8'
        self.scan_reply = ('bssid / frequency / signal level / flags / ssid\n'
                           '00:11:22:33:44:55\t2412\t-47\t[WPA2-PSK-CCMP][ESS]\tCafe\\x20\\xe4\\xb8\\xad\\xe6\\x96\\x87\n'
                           '00:11:22:33:44:66\t5180\t-55\t[ESS]\tOpen\\\\Name\n'
                           '00:11:22:33:44:77\t2412\t-80\t[WPA2-EAP-CCMP][ESS]\tEnterprise\n'
                           '00:11:22:33:44:88\t2412\t-81\t[WPA2-PSK+SAE-CCMP][ESS]\tModern\n')
        self.status_reply = None
        self.event = '<3>CTRL-EVENT-SCAN-RESULTS'

    def factory(self):
        peer = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def request(self, command, timeout):
                peer.commands.append(command)
                if peer.fail and peer.fail(command):
                    return 'FAIL\n'
                if command == 'STATUS':
                    if peer.status_reply is not None:
                        return peer.status_reply
                    current = peer.networks.get(peer.selected)
                    if current and (peer.selected == 0 or peer.connects):
                        # wpa control exposes UTF8 bytes with nonprintable/backslash escapes.
                        escaped = ''.join('\\x%02x' % value for value in current['ssid'].encode('utf-8'))
                        return ('wpa_state=COMPLETED\nid=%d\nssid=%s\nip_address=%s\nkey_mgmt=WPA2-PSK\n'
                                % (peer.selected, escaped, peer.current_ip))
                    return 'wpa_state=DISCONNECTED\n'
                if command == 'LIST_NETWORKS':
                    lines = ['network id / ssid / bssid / flags']
                    for number, network in peer.networks.items():
                        flags = '[CURRENT]' if number == peer.selected else ''
                        if not network['enabled']:
                            flags += '[DISABLED]'
                        lines.append('%d\t%s\tany\t%s' % (number, network['ssid'], flags))
                    return '\n'.join(lines) + '\n'
                if command in ('ATTACH', 'DETACH', 'SCAN', 'SAVE_CONFIG'):
                    return 'OK\n'
                if command == 'SCAN_RESULTS':
                    return peer.scan_reply
                if command == 'ADD_NETWORK':
                    peer.networks[3] = {'ssid': '', 'enabled': False}
                    return '3\n'
                parts = command.split()
                if parts[0] == 'SET_NETWORK':
                    number = int(parts[1])
                    if parts[2] == 'ssid':
                        peer.networks[number]['ssid'] = bytes.fromhex(parts[3]).decode('utf-8')
                    return 'OK\n'
                if parts[0] == 'SELECT_NETWORK':
                    peer.selected = int(parts[1])
                    for number, network in peer.networks.items():
                        network['enabled'] = number == peer.selected
                    return 'OK\n'
                if parts[0] in ('ENABLE_NETWORK', 'DISABLE_NETWORK'):
                    peer.networks[int(parts[1])]['enabled'] = parts[0] == 'ENABLE_NETWORK'
                    return 'OK\n'
                if parts[0] == 'REMOVE_NETWORK':
                    del peer.networks[int(parts[1])]
                    return 'OK\n'
                if command == 'DISCONNECT':
                    peer.selected = None
                    return 'OK\n'
                raise AssertionError('unexpected control command: ' + parts[0])

            def wait_event(self, timeout):
                if peer.event is None:
                    raise TimeoutError('fake scan timed out')
                return peer.event

        return Connection()


class WifiBehavior(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('helmet.app.wifi')
        except ImportError:
            self.fail('WiFi backend not implemented')

    def backend(self, peer=None, **options):
        peer = peer or FakePeer()
        clock = FakeClock()
        defaults = {'ctrl_factory': peer.factory, 'clock': clock.monotonic,
                    'sleep': clock.sleep, 'connect_timeout': 1,
                    'dhcp_runner': lambda interface, timeout: True}
        defaults.update(options)
        return self.module().WifiBackend(**defaults), peer, clock

    def test_status_has_only_public_state_ssid_ip_and_safe_error(self):
        backend, peer, _ = self.backend()
        peer.status_reply = 'wpa_state=COMPLETED\nssid=Safe\\x20Name\nip_address=10.1.2.3\npsk=not-public\n'
        result = backend.status()
        self.assertEqual(result, {'state': 'COMPLETED', 'ssid': 'Safe Name', 'ip': '10.1.2.3', 'error': None})

    def test_scan_decodes_utf8_and_refuses_enterprise_and_wpa3_downgrade(self):
        backend, _, _ = self.backend()
        result = backend.scan()
        self.assertIsNone(result['error'])
        networks = result['networks']
        self.assertEqual([n['ssid'] for n in networks], ['Cafe 中文', 'Open\\Name', 'Enterprise', 'Modern'])
        self.assertEqual([n['security'] for n in networks], ['WPA2-PSK', 'open', 'unsupported', 'unsupported'])
        self.assertEqual([n['connectable'] for n in networks], [True, True, False, False])
        self.assertEqual(networks[0]['signal'], -47)

    def test_default_budget_accepts_fourteen_second_scan_without_selection_changes(self):
        peer, clock = FakePeer(), FakeClock()
        original = peer.factory
        requests = []
        before_networks = json.loads(json.dumps(peer.networks))

        def factory():
            connection = original()
            request = connection.request
            def timed_request(command, timeout):
                requests.append((command, timeout))
                return request(command, timeout)
            def delayed_scan(timeout):
                if timeout < 14.0:
                    clock.sleep(timeout)
                    raise TimeoutError('legitimate full radio scan is not ready')
                clock.sleep(14.0)
                return '<3>CTRL-EVENT-SCAN-RESULTS'
            connection.request, connection.wait_event = timed_request, delayed_scan
            return connection

        backend = self.module().WifiBackend(ctrl_factory=factory, clock=clock.monotonic, sleep=clock.sleep)
        result = backend.scan()
        self.assertIsNone(result['error'], 'default budget must cover the recorded full-scan latency')
        self.assertEqual([row['ssid'] for row in result['networks']],
                         ['Cafe 中文', 'Open\\Name', 'Enterprise', 'Modern'])
        self.assertEqual(clock.now, 114.0, 'test advances a fake monotonic clock, not real sleep')
        self.assertEqual([command for command, _ in requests], ['ATTACH', 'SCAN', 'SCAN_RESULTS', 'DETACH'])
        self.assertTrue(all(timeout <= 2.0 for _, timeout in requests), 'individual command bounds remain independent')
        self.assertEqual(peer.selected, 0)
        self.assertEqual(json.loads(json.dumps(peer.networks)), before_networks)
        self.assertIsNotNone(backend.status()['ssid'], 'scan must release its operation/control resources')

    def test_scan_malformed_response_never_returns_partial_results(self):
        backend, peer, _ = self.backend()
        for response in ('FAIL\n', 'unexpected header\n',
                         'bssid / frequency / signal level / flags / ssid\nbad\n'):
            peer.scan_reply = response
            result = backend.scan()
            self.assertEqual(result['networks'], [])
            self.assertIsNotNone(result['error'])

    def test_unknown_encryption_is_not_open(self):
        backend, peer, _ = self.backend()
        peer.scan_reply = ('bssid / frequency / signal level / flags / ssid\n'
                           '00:11:22:33:44:55\t2412\t-47\t[WEP][ESS]\tLegacy\n')
        self.assertFalse(backend.scan()['networks'][0]['connectable'])

    def test_unknown_security_extension_refuses_psk_even_with_known_rsn_token(self):
        backend, peer, _ = self.backend()
        peer.scan_reply = ('bssid / frequency / signal level / flags / ssid\n'
                           '00:11:22:33:44:55\t2412\t-47\t[RSN-PSK-CCMP][FUTURE-AUTH][ESS]\tNew\n')
        self.assertFalse(backend.scan()['networks'][0]['connectable'])

    def test_real_transition_flags_allow_only_advertised_strong_rsn_option(self):
        backend, peer, _ = self.backend()
        rows = [
            ('[WPA-PSK-CCMP][WPA2-PSK-CCMP][ESS]', 'WPA2-PSK'),
            ('[WPA-PSK-CCMP+TKIP][WPA2-PSK-CCMP+TKIP][WPS][ESS]', 'WPA2-PSK'),
            ('[RSN-PSK-TKIP+CCMP][ESS]', 'WPA2-PSK'),
            ('[WPA-PSK-CCMP][ESS]', 'unsupported'),
            ('[WPA2-PSK-TKIP][ESS]', 'unsupported'),
            ('[WPA2-EAP-CCMP][WPA2-PSK-CCMP][ESS]', 'unsupported'),
            ('[WPA2-SAE-CCMP][ESS]', 'unsupported'),
            ('[OWE-CCMP][ESS]', 'unsupported'),
        ]
        for flags, expected in rows:
            peer.scan_reply = ('bssid / frequency / signal level / flags / ssid\n'
                               '00:11:22:33:44:55\t2412\t-47\t%s\tfixture\n' % flags)
            result = backend.scan()
            self.assertIsNone(result['error'])
            self.assertEqual(result['networks'][0]['security'], expected, flags)
        connected = backend.connect('fixture', 'password', 'WPA2-PSK')
        self.assertTrue(connected['connected'])
        self.assertIn('SET_NETWORK 3 proto RSN', peer.commands)
        self.assertIn('SET_NETWORK 3 pairwise CCMP', peer.commands)
        self.assertIn('SET_NETWORK 3 group CCMP', peer.commands)
        self.assertFalse(any('proto WPA' in command or 'pairwise TKIP' in command for command in peer.commands))

    def test_scan_no_advertised_flags_is_not_implicitly_open(self):
        backend, peer, _ = self.backend()
        peer.scan_reply = ('bssid / frequency / signal level / flags / ssid\n'
                           '00:11:22:33:44:55\t2412\t-47\t\tNew\n')
        result = backend.scan()
        self.assertTrue(result['error'] or not result['networks'][0]['connectable'])

    def test_success_derives_known_wpa_psk_without_plaintext_commands(self):
        backend, peer, _ = self.backend()
        result = backend.connect('IEEE', 'password', 'WPA2-PSK')
        self.assertTrue(result['connected'])
        self.assertFalse(result['saved'])
        self.assertIn('SET_NETWORK 3 psk f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e', peer.commands)
        self.assertNotIn('password', '\n'.join(peer.commands))
        self.assertNotIn('psk', json.dumps(result))
        self.assertNotIn('SAVE_CONFIG', peer.commands)

    def test_special_ssid_is_encoded_without_command_injection(self):
        backend, peer, _ = self.backend()
        ssid = '中"\\\n;$(x)'
        result = backend.connect(ssid, '', 'open')
        self.assertTrue(result['connected'])
        self.assertIn('SET_NETWORK 3 ssid e4b8ad225c0a3b24287829', peer.commands)
        self.assertIn('SET_NETWORK 3 key_mgmt NONE', peer.commands)
        self.assertFalse(any('\n' in command for command in peer.commands))

    def test_unsupported_security_changes_no_network(self):
        backend, peer, _ = self.backend()
        for security in ('WPA3-SAE', 'enterprise', 'WEP', 'unknown'):
            result = backend.connect('net', 'password', security)
            self.assertFalse(result['connected'])
        self.assertEqual(peer.commands, [])

    def test_bad_credentials_and_interface_are_rejected_without_control_effects(self):
        backend, peer, _ = self.backend()
        for ssid, password, security in (('', 'password', 'WPA2-PSK'), ('x' * 33, 'password', 'WPA2-PSK'),
                                         ('net', 'short', 'WPA2-PSK'), ('net', 'password', 'open')):
            self.assertFalse(backend.connect(ssid, password, security)['connected'])
        self.assertEqual(peer.commands, [])
        with self.assertRaises(ValueError):
            self.module().WifiBackend(interface='wlan0;reboot')

    def test_configuration_failure_removes_own_network_and_restores_all_old_flags(self):
        backend, peer, _ = self.backend()
        peer.fail = lambda command: command == 'SET_NETWORK 3 psk f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e'
        result = backend.connect('IEEE', 'password', 'WPA2-PSK')
        self.assertFalse(result['connected'])
        self.assertTrue(result['rollback_restored'])
        self.assertEqual(peer.selected, 0)
        self.assertEqual(peer.networks, {0: {'ssid': 'old', 'enabled': True},
                                        1: {'ssid': 'disabled', 'enabled': False},
                                        2: {'ssid': 'other', 'enabled': True}})
        self.assertFalse(any(command.startswith('REMOVE_NETWORK ') and command != 'REMOVE_NETWORK 3' for command in peer.commands))

    def test_connection_timeout_rolls_back_without_secret_errors(self):
        backend, peer, clock = self.backend()
        peer.connects = False
        result = backend.connect('IEEE', 'password', 'WPA2-PSK')
        self.assertFalse(result['connected'])
        self.assertTrue(result['rollback_restored'])
        self.assertLessEqual(clock.now, 104)
        self.assertNotIn('password', json.dumps(result))
        self.assertNotIn('f42c6fc', json.dumps(result))
        self.assertEqual(peer.selected, 0)

    def test_status_malformed_response_cannot_claim_connection(self):
        backend, peer, _ = self.backend()
        for reply in ('FAIL\n', 'wpa_state=COMPLETED\nssid=bad\\xZZ\n',
                      'wpa_state=COMPLETED\nip_address=not-an-ip\n', 'wpa_state=COMPLETED\nwpa_state=DISCONNECTED\n'):
            peer.status_reply = reply
            result = backend.status()
            self.assertEqual(result['state'], 'UNKNOWN')
            self.assertIsNotNone(result['error'])

    def test_unspecified_or_loopback_address_is_not_wifi_connection_success(self):
        backend, peer, _ = self.backend()
        for address in ('0.0.0.0', '127.0.0.1', '::', 'ff02::1'):
            peer.current_ip = address
            self.assertFalse(backend.connect('net', '', 'open')['connected'])

    def test_save_failure_is_failed_transaction_and_restores_previous_network(self):
        backend, peer, _ = self.backend()
        peer.fail = lambda command: command == 'SAVE_CONFIG'
        result = backend.connect('IEEE', 'password', 'WPA2-PSK', remember=True)
        self.assertFalse(result['connected'])
        self.assertFalse(result['saved'])
        self.assertTrue(result['rollback_restored'])
        self.assertEqual(peer.selected, 0)

    def test_remember_success_is_only_explicit_persistent_write(self):
        backend, peer, _ = self.backend()
        result = backend.connect('IEEE', 'password', 'WPA2-PSK', remember=True)
        self.assertTrue(result['connected'])
        self.assertTrue(result['saved'])
        self.assertEqual(peer.commands.count('SAVE_CONFIG'), 1)

    def test_malformed_existing_network_snapshot_prevents_add(self):
        backend, peer, _ = self.backend()
        original = peer.factory

        def factory():
            connection = original()
            request = connection.request
            connection.request = lambda command, timeout: 'broken list\n' if command == 'LIST_NETWORKS' else request(command, timeout)
            return connection

        backend.ctrl_factory = factory
        self.assertFalse(backend.connect('net', '', 'open')['connected'])
        self.assertNotIn('ADD_NETWORK', peer.commands)

    def test_wrong_completed_network_is_not_success(self):
        backend, peer, _ = self.backend()
        peer.status_reply = 'wpa_state=COMPLETED\nid=0\nssid=old\nip_address=192.168.1.8\n'
        result = backend.connect('net', '', 'open')
        self.assertFalse(result['connected'])
        self.assertTrue(result['rollback_restored'])

    def test_status_and_busy_scan_remain_responsive_during_connection_worker(self):
        entered, proceed = threading.Event(), threading.Event()

        def dhcp(interface, timeout):
            entered.set()
            return proceed.wait(timeout=.5)

        backend, peer, _ = self.backend(dhcp_runner=dhcp)
        result = []
        worker = threading.Thread(target=lambda: result.append(backend.connect('net', '', 'open')))
        worker.start()
        self.assertTrue(entered.wait(timeout=.5))
        started = time.monotonic()
        self.assertEqual(backend.status()['state'], 'COMPLETED')
        self.assertIsNotNone(backend.scan()['error'])
        self.assertLess(time.monotonic() - started, .2)
        proceed.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0]['connected'])

    def test_cancel_during_address_work_rolls_back_before_worker_returns(self):
        entered, proceed = threading.Event(), threading.Event()
        calls = []

        def dhcp(interface, timeout):
            calls.append(interface)
            if len(calls) == 1:
                entered.set()
                proceed.wait(timeout=.5)
            return True

        backend, peer, _ = self.backend(dhcp_runner=dhcp)
        result = []
        worker = threading.Thread(target=lambda: result.append(backend.connect('net', '', 'open')))
        worker.start()
        self.assertTrue(entered.wait(timeout=.5))
        backend.cancel()
        proceed.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0]['connected'])
        self.assertTrue(result[0]['rollback_restored'])
        self.assertEqual(peer.selected, 0)
        self.assertNotIn(3, peer.networks)

    def test_close_prevents_later_mutation_jobs(self):
        backend, peer, _ = self.backend()
        backend.close()
        self.assertFalse(backend.connect('net', '', 'open')['connected'])
        self.assertIsNotNone(backend.scan()['error'])
        self.assertEqual(peer.commands, [])

    def test_disconnect_during_new_network_setup_reconnects_control_for_rollback(self):
        backend, peer, _ = self.backend()
        original = peer.factory
        created = [0]

        def factory():
            connection = original()
            created[0] += 1
            number = created[0]
            request = connection.request
            broken = [False]

            def failing(command, timeout):
                if number == 1 and (broken[0] or command.startswith('SET_NETWORK')):
                    broken[0] = True
                    raise OSError('private control disconnected')
                return request(command, timeout)

            connection.request = failing
            return connection

        backend.ctrl_factory = factory
        result = backend.connect('net', '', 'open')
        self.assertFalse(result['connected'])
        self.assertTrue(result['rollback_restored'])
        self.assertEqual(peer.selected, 0)
        self.assertNotIn(3, peer.networks)

    def test_lost_save_acknowledgement_restores_persistent_snapshot(self):
        backend, peer, _ = self.backend()
        original = peer.factory
        saved = []

        def factory():
            connection = original()
            request = connection.request

            def save(command, timeout):
                if command == 'SAVE_CONFIG':
                    saved.append(set(peer.networks))
                    if len(saved) == 1:
                        raise TimeoutError('secret save response lost')
                return request(command, timeout)

            connection.request = save
            return connection

        backend.ctrl_factory = factory
        result = backend.connect('net', '', 'open', remember=True)
        self.assertFalse(result['connected'])
        self.assertTrue(result['rollback_restored'])
        self.assertEqual(saved[-1], {0, 1, 2})

    def test_dhcp_failure_uses_only_named_interface_and_rolls_back(self):
        dhcp_calls = []
        def dhcp(interface, timeout):
            dhcp_calls.append((interface, timeout))
            if len(dhcp_calls) > 1:
                peer.current_ip = '192.168.1.8'
            return len(dhcp_calls) > 1
        backend, peer, _ = self.backend(dhcp_runner=dhcp)
        peer.current_ip = ''
        result = backend.connect('net', '', 'open')
        self.assertFalse(result['connected'])
        self.assertTrue(result['rollback_restored'])
        self.assertEqual(dhcp_calls[0][0], 'wlan0')
        self.assertLessEqual(dhcp_calls[0][1], 1)
        self.assertEqual(len(dhcp_calls), 2)

    def test_old_address_renew_failure_does_not_claim_complete_rollback(self):
        backend, peer, _ = self.backend(dhcp_runner=lambda interface, timeout: False)
        result = backend.connect('net', '', 'open')
        self.assertFalse(result['connected'])
        self.assertFalse(result['rollback_restored'])
        self.assertEqual(peer.selected, 0)
        self.assertNotIn(3, peer.networks)

    def test_no_prior_selection_after_dhcp_cannot_claim_address_rollback(self):
        backend, peer, _ = self.backend()
        peer.selected = None
        peer.current_ip = '10.9.8.7'
        peer.fail = lambda command: command == 'SAVE_CONFIG'
        result = backend.connect('net', '', 'open', remember=True)
        self.assertFalse(result['connected'])
        self.assertFalse(result['saved'])
        self.assertFalse(result['rollback_restored'])
        self.assertEqual(peer.current_ip, '10.9.8.7')
        self.assertIsNone(peer.selected)
        self.assertNotIn(3, peer.networks)

    def test_rollback_failure_is_explicit_not_claimed_restored(self):
        backend, peer, _ = self.backend()
        peer.connects = False
        peer.fail = lambda command: command == 'REMOVE_NETWORK 3'
        result = backend.connect('net', '', 'open')
        self.assertFalse(result['rollback_restored'])
        self.assertIsNotNone(result['error'])

    def test_scan_timeout_does_not_change_existing_selection(self):
        backend, peer, _ = self.backend()
        peer.event = None
        result = backend.scan()
        self.assertEqual(result['networks'], [])
        self.assertIsNotNone(result['error'])
        self.assertEqual(peer.selected, 0)
        self.assertNotIn('ADD_NETWORK', peer.commands)

    def test_ctrl_exceptions_never_echo_response_or_secret(self):
        backend, peer, _ = self.backend()
        original = peer.factory

        def factory():
            connection = original()
            connection.request = lambda command, timeout: (_ for _ in ()).throw(OSError('password sent: supersecret'))
            return connection

        backend.ctrl_factory = factory
        result = backend.connect('net', 'supersecret', 'WPA2-PSK')
        self.assertNotIn('supersecret', json.dumps(result))

    def test_dhcp_runner_bounds_only_interface_argv_and_suppresses_output(self):
        wifi = self.module()
        calls = []

        class Completed:
            pid = 99999999
            def wait(self, timeout):
                return 0
            def poll(self):
                return 0

        def process(argv, **options):
            calls.append((argv, options))
            return Completed()

        with patch.object(wifi.subprocess, 'Popen', side_effect=process):
            self.assertTrue(wifi._dhcp('wlan0', .25))
        argv, options = calls[0]
        self.assertEqual(argv, ['udhcpc', '-i', 'wlan0', '-n', '-q', '-t', '3'])
        self.assertFalse(options['shell'])
        self.assertEqual(options['start_new_session'], os.name == 'posix')
        self.assertEqual(options['stdout'], wifi.subprocess.DEVNULL)
        self.assertEqual(options['stderr'], wifi.subprocess.DEVNULL)

    def test_dhcp_runner_timeout_returns_failure(self):
        wifi = self.module()
        class Child:
            pid = 99999999
            def wait(self, timeout):
                raise wifi.subprocess.TimeoutExpired(['udhcpc'], timeout)
            def poll(self):
                return None
            def kill(self):
                pass
        with patch.object(wifi.subprocess, 'Popen', return_value=Child()):
            self.assertFalse(wifi._dhcp('wlan0', .1))


@unittest.skipUnless(hasattr(socket, 'AF_UNIX') and os.name == 'posix', 'AF_UNIX mode validation requires Linux')
class UnixControlBehavior(unittest.TestCase):
    def test_private_socket_command_timeout_and_cleanup(self):
        wifi = importlib.import_module('helmet.app.wifi')
        with tempfile.TemporaryDirectory() as folder:
            remote = str(Path(folder) / 'ctrl')
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            peer.bind(remote)
            peer.settimeout(1)

            def responder():
                command, address = peer.recvfrom(4096)
                if command == b'STATUS':
                    peer.sendto(b'<3>CTRL-EVENT-SIGNAL-CHANGE\n', address)
                    peer.sendto(b'wpa_state=DISCONNECTED\n', address)

            thread = threading.Thread(target=responder)
            thread.start()
            with wifi.WpaControl(remote, temp_root=folder) as ctrl:
                local = ctrl.local_path
                self.assertEqual(stat.S_IMODE(os.stat(Path(local).parent).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(local).st_mode), 0o600)
                self.assertEqual(ctrl.request('STATUS', timeout=.2), 'wpa_state=DISCONNECTED\n')
                with self.assertRaises(wifi.WifiError):
                    ctrl.request('PING', timeout=.02)
            thread.join(timeout=1)
            peer.close()
            self.assertFalse(Path(local).exists())
            self.assertFalse(Path(local).parent.exists())

    def test_dhcp_timeout_terminates_late_hook_child_in_owned_process_group(self):
        wifi = importlib.import_module('helmet.app.wifi')
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / 'hook-ready'
            marker = Path(folder) / 'late-write'
            child_code = ("import pathlib,time;pathlib.Path(%r).write_text('ready');"
                          "time.sleep(.5);pathlib.Path(%r).write_text('late')" % (str(ready), str(marker)))
            parent_code = ("import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',%r]);"
                           "time.sleep(30)" % child_code)
            original = wifi.subprocess.Popen

            def fake_udhcpc(argv, **options):
                process = original([sys.executable, '-c', parent_code], **options)
                for _ in range(100):
                    if ready.exists():
                        break
                    time.sleep(.005)
                self.assertTrue(ready.exists(), 'hook fixture did not start')
                return process

            with patch.object(wifi.subprocess, 'Popen', side_effect=fake_udhcpc):
                self.assertFalse(wifi._dhcp('wlan0', .03))
            time.sleep(.7)
            self.assertFalse(marker.exists(), 'late DHCP hook survived timeout')


if __name__ == '__main__':
    unittest.main()
