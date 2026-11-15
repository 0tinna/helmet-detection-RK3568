"""Bounded wpa_supplicant control access for manual local WiFi actions.

connect() and scan() are blocking, bounded jobs for the App's background worker.
status() uses a separate control connection so a connection job cannot hold the
UI status path. No shell, plaintext PSK, control response, or password is logged.
Only open and WPA2 personal networks are supported; other security is refused.
"""
from collections import deque
import hashlib
import ipaddress
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time


class WifiError(RuntimeError):
    """A fixed, public-safe failure message, never containing a peer response."""
    def __init__(self, message, kind='control'):
        super().__init__(message)
        self.kind = kind


def decode_ssid(value):
    """Decode wpa control's UTF8 byte escapes without interpreting shell syntax."""
    if not isinstance(value, str):
        raise WifiError('Invalid WiFi response')
    data = bytearray()
    index = 0
    escapes = {'\\': 92, '"': 34, 'n': 10, 'r': 13, 't': 9, 'e': 27}
    while index < len(value):
        char = value[index]
        if char != '\\':
            data.extend(char.encode('utf-8'))
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise WifiError('Invalid WiFi response')
        char = value[index]
        if char == 'x':
            token = value[index + 1:index + 3]
            if len(token) != 2 or not re.fullmatch('[0-9a-fA-F]{2}', token):
                raise WifiError('Invalid WiFi response')
            data.append(int(token, 16))
            index += 3
        elif char in escapes:
            data.append(escapes[char])
            index += 1
        else:
            raise WifiError('Invalid WiFi response')
    if len(data) > 32:
        raise WifiError('Invalid WiFi response')
    try:
        return data.decode('utf-8', errors='strict')
    except UnicodeError:
        raise WifiError('WiFi name is not valid UTF8') from None


class WpaControl:
    """One connected AF_UNIX datagram peer with private, temporary socket path."""
    def __init__(self, control_path, temp_root=None):
        self.socket = None
        self.temporary = None
        self.local_path = None
        self.events = deque(maxlen=64)
        try:
            if os.name != 'posix' or not hasattr(socket, 'AF_UNIX'):
                raise WifiError('WiFi control is unavailable on this platform')
            self.temporary = tempfile.TemporaryDirectory(prefix='helmet-wpa-', dir=temp_root)
            os.chmod(self.temporary.name, 0o700)
            self.local_path = str(Path(self.temporary.name) / 'ctrl')
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.socket.bind(self.local_path)
            os.chmod(self.local_path, 0o600)
            self.socket.connect(str(control_path))
        except Exception:
            self.close()
            raise WifiError('WiFi control is unavailable') from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _receive(self, timeout):
        try:
            self.socket.settimeout(timeout)
            value = self.socket.recv(65536)
            if not value or len(value) == 65536:
                raise WifiError('Invalid WiFi response')
            return value.decode('utf-8', errors='strict')
        except (TimeoutError, socket.timeout):
            raise WifiError('WiFi command timed out') from None
        except (OSError, UnicodeError):
            raise WifiError('WiFi control failed') from None

    def request(self, command, timeout=2):
        if (not isinstance(command, str) or not command or len(command) > 4096
                or '\n' in command or '\r' in command or '\x00' in command
                or not math.isfinite(timeout) or timeout <= 0):
            raise WifiError('Invalid WiFi command')
        deadline = time.monotonic() + timeout
        try:
            self.socket.settimeout(timeout)
            self.socket.send(command.encode('ascii', errors='strict'))
        except (OSError, UnicodeError):
            raise WifiError('WiFi control failed') from None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WifiError('WiFi command timed out')
            response = self._receive(remaining)
            if response.startswith('<'):
                self.events.append(response)
            else:
                return response

    def wait_event(self, timeout=2):
        if self.events:
            return self.events.popleft()
        if not math.isfinite(timeout) or timeout <= 0:
            raise WifiError('WiFi command timed out')
        response = self._receive(timeout)
        if not response.startswith('<'):
            raise WifiError('Invalid WiFi event')
        return response

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None


def _dhcp(interface, timeout):
    """Explicit manual connection's DHCP; its hook may update routes and DNS.

    Root's scope ruling authorizes necessary DHCP address/route/DNS changes only
    as part of a user-clicked connect action. No wpa kill or other-interface argv.
    """
    process = None
    try:
        process = subprocess.Popen(['udhcpc', '-i', interface, '-n', '-q', '-t', '3'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, shell=False,
                                   start_new_session=os.name == 'posix')
        return process.wait(timeout=timeout) == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        if process is not None:
            if os.name == 'posix':
                # Only our new session/process group. DHCP hook children share
                # it and must not outlive a failed/cancelled transaction.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
                try:
                    process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    pass
                # Parent may have exited while a hook still runs. Do not use
                # parent poll as proof that the whole process group is gone.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
            else:
                # Production WiFi control is POSIX-only. Keep test/error paths
                # bounded without making a process-group promise on Windows.
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=.2)
            except (subprocess.TimeoutExpired, OSError):
                pass


class WifiBackend:
    def __init__(self, interface='wlan0', control_path=None, ctrl_factory=None,
                 dhcp_runner=None, connect_timeout=20, command_timeout=2,
                 scan_timeout=8, rollback_timeout=5, clock=time.monotonic,
                 sleep=time.sleep):
        if not isinstance(interface, str) or not re.fullmatch(r'wlan[0-9]+', interface):
            raise ValueError('WiFi interface must be a named wlan interface')
        for value in (connect_timeout, command_timeout, scan_timeout, rollback_timeout):
            if not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError('WiFi timeout must be finite and positive')
        self.interface = interface
        self.control_path = control_path or '/var/run/wpa_supplicant/' + interface
        self.ctrl_factory = ctrl_factory or (lambda: WpaControl(self.control_path))
        self.dhcp_runner = dhcp_runner or _dhcp
        self.connect_timeout, self.command_timeout = connect_timeout, command_timeout
        self.scan_timeout, self.rollback_timeout = scan_timeout, rollback_timeout
        self.clock, self.sleep = clock, sleep
        self.operation = threading.Lock()
        self.cancelled = threading.Event()
        self.closed = threading.Event()

    def cancel(self):
        """Request rollback of the active job; its worker retains socket ownership."""
        self.cancelled.set()

    def close(self):
        """Prevent new jobs and request cancellation; owner must join its worker."""
        self.closed.set()
        self.cancel()

    def _check_cancelled(self):
        if self.closed.is_set() or self.cancelled.is_set():
            raise WifiError('WiFi operation cancelled')

    def _request(self, ctrl, command, deadline=None):
        timeout = self.command_timeout
        if deadline is not None:
            timeout = min(timeout, deadline - self.clock())
        if timeout <= 0:
            raise WifiError('WiFi operation timed out')
        try:
            response = ctrl.request(command, timeout=timeout)
        except Exception:
            raise WifiError('WiFi control failed or timed out') from None
        if not isinstance(response, str) or response.strip().startswith(('FAIL', 'UNKNOWN COMMAND')):
            raise WifiError('WiFi command was rejected', kind='rejected')
        return response

    def _ok(self, ctrl, command, deadline=None):
        if self._request(ctrl, command, deadline).strip() != 'OK':
            raise WifiError('Invalid WiFi response')

    def _status(self, ctrl, deadline=None):
        values = {}
        for line in self._request(ctrl, 'STATUS', deadline).splitlines():
            if '=' not in line:
                raise WifiError('Invalid WiFi status')
            key, value = line.split('=', 1)
            if not key or key in values:
                raise WifiError('Invalid WiFi status')
            values[key] = value
        state = values.get('wpa_state')
        if state not in ('DISCONNECTED', 'INTERFACE_DISABLED', 'INACTIVE', 'SCANNING',
                         'AUTHENTICATING', 'ASSOCIATING', 'ASSOCIATED', '4WAY_HANDSHAKE',
                         'GROUP_HANDSHAKE', 'COMPLETED'):
            raise WifiError('Invalid WiFi status')
        ssid = decode_ssid(values['ssid']) if 'ssid' in values else None
        if state == 'COMPLETED' and not ssid:
            raise WifiError('Invalid WiFi status')
        ip = values.get('ip_address') or None
        if ip is not None:
            try:
                address = ipaddress.ip_address(ip)
                if address.is_unspecified or address.is_loopback or address.is_multicast:
                    raise ValueError('not a usable wireless address')
            except ValueError:
                raise WifiError('Invalid WiFi address') from None
        number = values.get('id')
        if number is not None and not re.fullmatch(r'[0-9]+', number):
            raise WifiError('Invalid WiFi status')
        return {'state': state, 'ssid': ssid, 'ip': ip,
                'id': int(number) if number is not None else None}

    def status(self):
        try:
            with self.ctrl_factory() as ctrl:
                value = self._status(ctrl)
            return {name: value[name] for name in ('state', 'ssid', 'ip')} | {'error': None}
        except Exception:
            return {'state': 'UNKNOWN', 'ssid': None, 'ip': None,
                    'error': 'WiFi status is unavailable'}

    @staticmethod
    def _security(flags):
        tokens = re.findall(r'\[([^\]]+)\]', flags)
        if tokens and all(token in ('ESS', 'WPS') for token in tokens):
            return 'open'
        strong_rsn = False
        for token in tokens:
            if token in ('ESS', 'WPS'):
                continue
            # Transition WPA/TKIP advertisements may still offer an explicit
            # WPA2-PSK CCMP option. We only select that option and always force
            # proto RSN / pairwise CCMP / group CCMP at SET_NETWORK below.
            match = re.fullmatch(r'(WPA|WPA2|RSN)-PSK-((?:CCMP|TKIP)(?:\+(?:CCMP|TKIP))*)', token)
            if match is None:
                return 'unsupported'
            ciphers = match.group(2).split('+')
            if len(ciphers) != len(set(ciphers)):
                return 'unsupported'
            if match.group(1) in ('WPA2', 'RSN') and 'CCMP' in ciphers:
                strong_rsn = True
        if strong_rsn:
            return 'WPA2-PSK'
        return 'unsupported'

    def scan(self):
        if not self.operation.acquire(blocking=False):
            return {'networks': [], 'error': 'WiFi operation is busy'}
        try:
            if self.closed.is_set():
                raise WifiError('WiFi backend is closed')
            self.cancelled.clear()
            deadline = self.clock() + self.scan_timeout
            with self.ctrl_factory() as ctrl:
                self._ok(ctrl, 'ATTACH', deadline)
                try:
                    self._ok(ctrl, 'SCAN', deadline)
                    while True:
                        self._check_cancelled()
                        remaining = deadline - self.clock()
                        if remaining <= 0:
                            raise WifiError('WiFi scan timed out')
                        event = ctrl.wait_event(timeout=remaining)
                        if 'CTRL-EVENT-SCAN-FAILED' in event:
                            raise WifiError('WiFi scan failed')
                        if 'CTRL-EVENT-SCAN-RESULTS' in event:
                            break
                    lines = self._request(ctrl, 'SCAN_RESULTS', deadline).splitlines()
                finally:
                    # Closing the temporary peer detaches even when this fails.
                    try:
                        self._ok(ctrl, 'DETACH', deadline)
                    except Exception:
                        pass
            if not lines or lines[0].strip() != 'bssid / frequency / signal level / flags / ssid':
                raise WifiError('Invalid WiFi scan')
            networks = []
            for line in lines[1:]:
                parts = line.split('\t', 4)
                if (len(parts) != 5 or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', parts[0])
                        or not re.fullmatch(r'[0-9]+', parts[1]) or not re.fullmatch(r'-?[0-9]+', parts[2])
                        or not re.fullmatch(r'(?:\[[^\[\]]+\])+', parts[3])):
                    raise WifiError('Invalid WiFi scan')
                frequency, signal = int(parts[1]), int(parts[2])
                if frequency <= 0 or not -127 <= signal <= 0:
                    raise WifiError('Invalid WiFi scan')
                ssid = decode_ssid(parts[4])
                security = self._security(parts[3])
                networks.append({'ssid': ssid, 'security': security, 'signal': signal,
                                 'bssid': parts[0], 'frequency': frequency,
                                 'hidden': not bool(ssid), 'connectable': security != 'unsupported'})
            return {'networks': networks, 'error': None}
        except Exception:
            return {'networks': [], 'error': 'WiFi scan failed or timed out'}
        finally:
            self.operation.release()

    def _snapshot(self, ctrl, deadline):
        lines = self._request(ctrl, 'LIST_NETWORKS', deadline).splitlines()
        if not lines or lines[0].strip() != 'network id / ssid / bssid / flags':
            raise WifiError('Invalid WiFi network list')
        networks, current = {}, None
        for line in lines[1:]:
            parts = line.split('\t', 3)
            if (len(parts) != 4 or not re.fullmatch(r'[0-9]+', parts[0])
                    or not re.fullmatch(r'(?:\[[^\[\]]+\])*', parts[3])):
                raise WifiError('Invalid WiFi network list')
            number = int(parts[0])
            if number in networks:
                raise WifiError('Invalid WiFi network list')
            decode_ssid(parts[1])
            networks[number] = '[DISABLED]' not in parts[3]
            if '[CURRENT]' in parts[3]:
                if current is not None:
                    raise WifiError('Invalid WiFi network list')
                current = number
        return networks, current

    def _rollback(self, ctrl, added, networks, current, deadline, restore_saved=False,
                  renew_old_address=False):
        restored = True
        try:
            present, _ = self._snapshot(ctrl, deadline)
        except Exception:
            return False
        commands = ['REMOVE_NETWORK %d' % added] if added in present else []
        if current is not None:
            commands.append('SELECT_NETWORK %d' % current)
        commands.extend(('%s_NETWORK %d' % ('ENABLE' if enabled else 'DISABLE', number))
                        for number, enabled in networks.items())
        if current is None:
            commands.append('DISCONNECT')
        for command in commands:
            try:
                self._ok(ctrl, command, deadline)
            except Exception:
                restored = False
        try:
            observed, selected = self._snapshot(ctrl, deadline)
            restored = restored and observed == networks and selected == current
            if restored and restore_saved:
                self._ok(ctrl, 'SAVE_CONFIG', deadline)
            if restored and renew_old_address and current is not None:
                renewed = False
                while self.clock() < deadline:
                    old = self._status(ctrl, deadline)
                    if old['state'] == 'COMPLETED' and old['id'] == current:
                        if not renewed:
                            if not self.dhcp_runner(self.interface, deadline - self.clock()):
                                raise WifiError('Previous WiFi address restoration failed')
                            renewed = True
                            continue
                        if old['ip']:
                            break
                    self.sleep(min(.1, max(0, deadline - self.clock())))
                else:
                    restored = False
            elif renew_old_address and current is None:
                # No previous connected-network baseline exists to renew. A
                # DHCP hook may have changed address/routes/DNS; do not guess
                # or clear interface/global settings, and never claim full
                # rollback from network-list restoration alone.
                restored = False
        except Exception:
            restored = False
        return restored

    def connect(self, ssid, password, security, remember=False):
        failed = {'connected': False, 'state': 'FAILED', 'ssid': None, 'ip': None,
                  'saved': False, 'error': None, 'rollback_restored': None}
        try:
            if not isinstance(ssid, str) or not 1 <= len(ssid.encode('utf-8')) <= 32:
                raise WifiError('WiFi name must contain 1 to 32 UTF8 bytes')
            if not isinstance(password, str) or type(remember) is not bool:
                raise WifiError('Invalid WiFi credentials')
            if not isinstance(security, str) or security.lower() not in ('open', 'wpa2-psk', 'wpa2'):
                return failed | {'state': 'UNSUPPORTED', 'error': 'Only open and WPA2 personal WiFi are supported'}
            security = 'open' if security.lower() == 'open' else 'WPA2-PSK'
            if (security == 'open' and password) or (security != 'open' and
                    (not 8 <= len(password.encode('utf-8')) <= 63 or any(ord(c) < 32 for c in password))):
                raise WifiError('Invalid WiFi credentials')
        except (WifiError, UnicodeError) as error:
            return failed | {'error': str(error) if isinstance(error, WifiError) else 'Invalid WiFi credentials'}
        if not self.operation.acquire(blocking=False):
            return failed | {'error': 'WiFi operation is busy'}
        added = None
        try:
            if self.closed.is_set():
                raise WifiError('WiFi backend is closed')
            self.cancelled.clear()
            deadline = self.clock() + self.connect_timeout
            with self.ctrl_factory() as ctrl:
                networks, current = self._snapshot(ctrl, deadline)
                save_attempted = False
                dhcp_attempted = False
                try:
                    number = self._request(ctrl, 'ADD_NETWORK', deadline).strip()
                    if not re.fullmatch(r'[0-9]+', number) or int(number) in networks:
                        raise WifiError('Invalid new WiFi network')
                    added = int(number)
                    self._ok(ctrl, 'SET_NETWORK %d ssid %s' % (added, ssid.encode('utf-8').hex()), deadline)
                    self._ok(ctrl, 'SET_NETWORK %d scan_ssid 1' % added, deadline)
                    if security == 'open':
                        self._ok(ctrl, 'SET_NETWORK %d key_mgmt NONE' % added, deadline)
                    else:
                        psk = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), ssid.encode('utf-8'), 4096, 32).hex()
                        for field, value in (('key_mgmt', 'WPA-PSK'), ('proto', 'RSN'),
                                             ('pairwise', 'CCMP'), ('group', 'CCMP'), ('psk', psk)):
                            self._ok(ctrl, 'SET_NETWORK %d %s %s' % (added, field, value), deadline)
                        del psk
                    self._ok(ctrl, 'SELECT_NETWORK %d' % added, deadline)
                    dhcp_done = False
                    while self.clock() < deadline:
                        self._check_cancelled()
                        value = self._status(ctrl, deadline)
                        if value['state'] == 'COMPLETED' and value['id'] == added and value['ssid'] == ssid:
                            if not dhcp_done:
                                dhcp_attempted = True
                                if not self.dhcp_runner(self.interface, max(0.001, deadline - self.clock())):
                                    raise WifiError('WiFi address configuration failed')
                                dhcp_done = True
                                continue
                            if value['ip']:
                                self._check_cancelled()
                                if remember:
                                    save_attempted = True
                                    self._ok(ctrl, 'SAVE_CONFIG', deadline)
                                return {'connected': True, 'state': 'COMPLETED', 'ssid': ssid,
                                        'ip': value['ip'], 'saved': remember, 'error': None,
                                        'rollback_restored': None}
                        self.sleep(min(0.1, max(0, deadline - self.clock())))
                    raise WifiError('WiFi connection timed out')
                except Exception as error:
                    restored = None
                    if added is not None:
                        rollback_deadline = self.clock() + self.rollback_timeout
                        # A lost SAVE_CONFIG response may have persisted the new
                        # network. Restore disk state too when explicit remember
                        # authorized that write; a definite FAIL did not save.
                        restore_saved = save_attempted and getattr(error, 'kind', None) != 'rejected'
                        restored = self._rollback(ctrl, added, networks, current,
                                                  rollback_deadline, restore_saved, dhcp_attempted)
                        if not restored and self.clock() < rollback_deadline:
                            try:
                                with self.ctrl_factory() as recovery:
                                    restored = self._rollback(recovery, added, networks, current,
                                                              rollback_deadline, restore_saved, dhcp_attempted)
                            except Exception:
                                restored = False
                    return failed | {'error': 'WiFi connection failed; previous settings restored' if restored
                                     else 'WiFi connection failed; verify previous network settings',
                                     'rollback_restored': restored}
        except Exception:
            return failed | {'error': 'WiFi control is unavailable'}
        finally:
            self.operation.release()
