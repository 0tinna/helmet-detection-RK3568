"""Bounded SMTP alert delivery and local-network address resolution."""
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
import ipaddress
import json
import math
import os
from pathlib import Path
import smtplib
import socket
import ssl
import stat
import threading
import time


MAX_SETTINGS_BYTES = 64 * 1024
LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8',
    '172.16.0.0/12',
    '192.168.0.0/16',
))


def _valid_email(value):
    if not isinstance(value, str) or len(value) > 254:
        return False
    display, address = parseaddr(value)
    return (not display and address == value and '@' in address
            and not any(character.isspace() for character in address)
            and address.rsplit('@', 1)[1].count('.') >= 0)


def _private_file_mode(mode):
    return stat.S_ISREG(mode) and stat.S_IMODE(mode) & 0o077 == 0


@dataclass(frozen=True)
class MailSettings:
    enabled: bool
    smtp_host: str
    smtp_port: int
    security: str
    username: str
    password: str = field(repr=False)
    sender: str
    recipient: str
    timeout_seconds: float = 8.0
    batch_seconds: float = 5.0

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('mail enabled must be boolean')
        if not isinstance(self.smtp_host, str) or not self.smtp_host.strip():
            raise ValueError('SMTP host is required')
        if type(self.smtp_port) is not int or not 1 <= self.smtp_port <= 65535:
            raise ValueError('SMTP port is invalid')
        if self.security not in ('ssl', 'starttls', 'none'):
            raise ValueError('SMTP security mode is invalid')
        if not isinstance(self.username, str) or not self.username:
            raise ValueError('SMTP username is required')
        if not isinstance(self.password, str) or not self.password:
            raise ValueError('SMTP password is required')
        if not _valid_email(self.sender) or not _valid_email(self.recipient):
            raise ValueError('SMTP sender or recipient is invalid')
        for name, value in (('timeout', self.timeout_seconds),
                            ('batch', self.batch_seconds)):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value <= 0 or value > 3600):
                raise ValueError('SMTP %s interval is invalid' % name)

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            details = path.lstat()
        except OSError as error:
            raise OSError('mail configuration is unavailable') from error
        if (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)
                or details.st_size > MAX_SETTINGS_BYTES):
            raise ValueError('mail configuration is not a regular bounded file')
        if os.name != 'nt' and not _private_file_mode(details.st_mode):
            raise ValueError('mail configuration permissions must be 0600')
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode('utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError('mail configuration is invalid') from error
        if not isinstance(value, dict):
            raise ValueError('mail configuration must be an object')
        allowed = {'enabled', 'smtp_host', 'smtp_port', 'security', 'username',
                   'password', 'sender', 'recipient', 'timeout_seconds', 'batch_seconds'}
        if set(value) - allowed:
            raise ValueError('mail configuration contains unknown fields')
        required = allowed - {'timeout_seconds', 'batch_seconds'}
        if not required <= set(value):
            raise ValueError('mail configuration is incomplete')
        return cls(
            enabled=value['enabled'],
            smtp_host=value['smtp_host'],
            smtp_port=value['smtp_port'],
            security=value['security'],
            username=value['username'],
            password=value['password'],
            sender=value['sender'],
            recipient=value['recipient'],
            timeout_seconds=value.get('timeout_seconds', 8.0),
            batch_seconds=value.get('batch_seconds', 5.0),
        )


def _default_lan_address():
    """Ask the OS which IPv4 source the current route would use."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(.5)
        # UDP connect only selects a route; it does not send application data.
        probe.connect(('192.0.2.1', 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


class LanAddressResolver:
    """Resolve one private IPv4 address without contacting an external service."""
    def __init__(self, address_getter=None):
        self.address_getter = address_getter or _default_lan_address

    def resolve(self):
        try:
            address = ipaddress.ip_address(str(self.address_getter()).strip())
            if (address.version != 4
                    or not any(address in network for network in LAN_NETWORKS)):
                raise ValueError('address is not a private IPv4 interface')
            return {'address': str(address), 'source': 'local_interface'}
        except (OSError, ValueError, TypeError):
            return {'address': None, 'source': 'unavailable'}


@dataclass
class _MailBatch:
    events: list
    attachment: bytes
    attachment_name: str
    deadline: float


class SmtpAlertMailer:
    """One bounded sender thread; delivery failure never reaches detection."""
    def __init__(self, settings, access_url_builder, resolver,
                 smtp_factory=None, batch_seconds=None, retry_delays=(30, 120, 600)):
        if not isinstance(settings, MailSettings):
            raise TypeError('mail settings are required')
        if not callable(access_url_builder):
            raise ValueError('access URL builder is required')
        if not isinstance(resolver, LanAddressResolver):
            raise TypeError('LAN address resolver is required')
        self.settings = settings
        self.access_url_builder = access_url_builder
        self.resolver = resolver
        self.smtp_factory = smtp_factory
        self.batch_seconds = (settings.batch_seconds if batch_seconds is None
                              else float(batch_seconds))
        if not math.isfinite(self.batch_seconds) or self.batch_seconds <= 0:
            raise ValueError('mail batch interval must be positive')
        self.retry_delays = tuple(float(value) for value in retry_delays)
        if (len(self.retry_delays) != 3
                or any(not math.isfinite(value) or value < 0
                       for value in self.retry_delays)):
            raise ValueError('three nonnegative retry delays are required')
        self._condition = threading.Condition()
        self._queue = deque()
        self._worker = None
        self._started = False
        self._closing = False
        self._stopped = False
        self._state = 'disabled' if not settings.enabled else 'idle'
        self._last_result = 'disabled' if not settings.enabled else None
        self._last_attempt_at = None
        self._dropped_batches = 0

    @property
    def _configured(self):
        return self.settings.enabled

    def start(self):
        if not self._configured:
            return
        with self._condition:
            if self._started:
                raise RuntimeError('mail worker cannot be started twice')
            if self._closing:
                raise RuntimeError('mail worker is closing')
            self._started = True
            self._worker = threading.Thread(
                target=self._run, name='helmet-alert-mail', daemon=False)
            self._worker.start()

    def _prepare_event(self, event):
        if not isinstance(event, dict):
            raise ValueError('mail events must be objects')
        event_id = event.get('event_id')
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('mail event id is required')
        path = event.get('media_path')
        if isinstance(path, str) and path:
            try:
                attachment = Path(path).read_bytes()
            except OSError:
                attachment = None
        else:
            attachment = None
        return dict(event), attachment

    def enqueue(self, events):
        if not self._configured:
            return False
        if not isinstance(events, list) or not events:
            raise ValueError('mail batch must contain events')
        prepared = [self._prepare_event(event) for event in events]
        with self._condition:
            if self._closing or self._stopped:
                raise RuntimeError('mail worker is closed')
            now = time.monotonic()
            aggregate = (self._queue and not self._closing
                         and self.batch_seconds >= .001
                         and now <= self._queue[-1].deadline)
            if aggregate:
                batch = self._queue[-1]
                batch.events.extend(row[0] for row in prepared)
                if not batch.attachment:
                    first = next((row for row in prepared if row[1]), None)
                    if first is not None:
                        batch.attachment = first[1]
                        batch.attachment_name = first[0]['event_id'] + '.jpg'
            else:
                first = next((row for row in prepared if row[1]), None)
                deadline = now + (self.batch_seconds
                                  if self.batch_seconds >= .001 else 0)
                self._queue.append(_MailBatch(
                    events=[row[0] for row in prepared],
                    attachment=first[1] if first else None,
                    attachment_name=(first[0]['event_id'] + '.jpg') if first else '',
                    deadline=deadline,
                ))
                if len(self._queue) > 8:
                    self._queue.popleft()
                    self._dropped_batches += 1
            self._condition.notify_all()
        return True

    def _run(self):
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._closing:
                        self._condition.wait()
                    if not self._queue and self._closing:
                        break
                    batch = self._queue[0]
                    if not self._closing:
                        remaining = batch.deadline - time.monotonic()
                        if remaining > 0:
                            self._condition.wait(remaining)
                            continue
                    batch = self._queue.popleft()
                    self._state = 'sending'
                    self._last_attempt_at = time.monotonic()
                result = self._send_with_retry(batch)
                with self._condition:
                    self._last_result = result
                    self._state = 'sent' if result == 'sent' else 'failed'
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._state = 'stopped'
                self._stopped = True
                self._condition.notify_all()

    def _connection(self):
        if self.smtp_factory is not None:
            connection = self.smtp_factory(self.settings)
        elif self.settings.security == 'ssl':
            connection = __import__('smtplib').SMTP_SSL(
                self.settings.smtp_host, self.settings.smtp_port,
                timeout=self.settings.timeout_seconds)
        else:
            connection = __import__('smtplib').SMTP(
                self.settings.smtp_host, self.settings.smtp_port,
                timeout=self.settings.timeout_seconds)
        if self.settings.security == 'starttls':
            connection.starttls(context=ssl.create_default_context())
        return connection

    def _message(self, batch):
        address = self.resolver.resolve()
        access_url = None
        if address.get('address'):
            try:
                access_url = self.access_url_builder(address['address'])
            except (OSError, ValueError, TypeError):
                access_url = None
        people = 0
        tracks = set()
        for event in batch.events:
            generation = event.get('generation')
            source_epoch = event.get('source_epoch')
            visible = event.get('visible_no_hat_track_ids')
            if (isinstance(generation, str) and type(source_epoch) is int
                    and isinstance(visible, list)):
                ids = [track_id for track_id in visible
                       if type(track_id) is int]
                if ids:
                    tracks.update((generation, source_epoch, track_id)
                                  for track_id in ids)
                    continue
            people += max(1, int(event.get('count', 1)))
        people += len(tracks)
        first_time = str(batch.events[0].get('captured_wall_time') or '未知')
        message = EmailMessage()
        message['Subject'] = '[RK3568安全帽告警] 发现 %d 名未戴安全帽人员' % people
        message['From'] = self.settings.sender
        message['To'] = self.settings.recipient
        link = (access_url
                or '内网地址暂不可用；请让手机连接与开发板相同的 Wi-Fi/局域网，'
                   '然后打开开发板当前 IPv4 的 8092 端口。')
        message.set_content(
            'RK3568 安全帽检测告警\n'
            '发生时间：%s\n'
            '本批人数：%d\n'
            '本批事件：%d\n'
            '内网查看：%s\n'
            '访问要求：手机与开发板处于同一 Wi-Fi/局域网\n'
            '事件照片默认保留 7 天后自动删除。\n'
            % (first_time, people, len(batch.events),
               link))
        if batch.attachment:
            message.add_attachment(
                batch.attachment, maintype='image', subtype='jpeg',
                filename=batch.attachment_name or 'helmet-alert.jpg')
        return message

    def _send_once(self, batch):
        message = self._message(batch)
        connection = self._connection()
        try:
            connection.login(self.settings.username, self.settings.password)
            connection.send_message(message)
        finally:
            try:
                connection.quit()
            except (OSError, RuntimeError):
                pass

    def probe(self):
        """Send one synchronous configuration test without an event queue."""
        if not self._configured:
            raise RuntimeError('SMTP is disabled')
        address = self.resolver.resolve()
        access_url = None
        if address.get('address'):
            try:
                access_url = self.access_url_builder(address['address'])
            except (OSError, ValueError, TypeError):
                access_url = None
        link = access_url or (
            'http://开发板内网IP:8092/'
            '（请让手机与开发板连接同一 Wi-Fi/局域网）')
        message = EmailMessage()
        message['Subject'] = '[RK3568安全帽告警] SMTP自检'
        message['From'] = self.settings.sender
        message['To'] = self.settings.recipient
        message.set_content(
            '这是一封 RK3568 安全帽告警邮件链路自检。\n'
            '收到本邮件表示 SMTP 配置可以发送到目标邮箱。\n'
            '内网事件站：%s\n'
            '访问要求：手机与开发板处于同一 Wi-Fi/局域网\n'
            '本邮件不是安全帽检测告警，不包含事件照片。\n'
            % link)
        connection = None
        try:
            connection = self._connection()
            connection.login(self.settings.username, self.settings.password)
            connection.send_message(message)
        except (OSError, RuntimeError, ValueError, smtplib.SMTPException):
            raise OSError('SMTP probe failed') from None
        finally:
            if connection is not None:
                try:
                    connection.quit()
                except (OSError, RuntimeError):
                    pass
        return {
            'sent': True,
            'recipient': self.settings.recipient,
            'lan_address_available': access_url is not None,
        }

    def _send_with_retry(self, batch):
        attempts = len(self.retry_delays) + 1
        for attempt in range(attempts):
            try:
                self._send_once(batch)
                return 'sent'
            except (OSError, RuntimeError, ValueError, smtplib.SMTPException):
                if attempt + 1 >= attempts:
                    break
                with self._condition:
                    if self._closing:
                        break
                    self._state = 'retrying'
                    deadline = time.monotonic() + self.retry_delays[attempt]
                    while not self._closing:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._condition.wait(remaining)
                    if self._closing:
                        break
        return 'failed'

    def status(self):
        with self._condition:
            worker_alive = self._worker is not None and self._worker.is_alive()
            return {'configured': self._configured, 'state': self._state,
                    'last_result': self._last_result,
                    'last_attempt_at': self._last_attempt_at,
                    'pending_batches': len(self._queue),
                    'dropped_batches': self._dropped_batches,
                    'worker_alive': worker_alive}

    def close(self, timeout=3.0):
        if type(timeout) not in (int, float) or timeout < 0:
            raise ValueError('mail close timeout is invalid')
        with self._condition:
            if self._stopped:
                return True
            self._closing = True
            if not self._started:
                self._stopped = True
                self._state = 'stopped'
                return True
            if self._queue:
                self._queue[0].deadline = 0
            self._condition.notify_all()
            worker = self._worker
        worker.join(timeout)
        with self._condition:
            return not worker.is_alive()


def load_mailer(root, access_url_builder, resolver=None, smtp_factory=None):
    path = Path(root) / 'app-state' / 'mail.json'
    if not path.exists():
        return None
    return SmtpAlertMailer(
        MailSettings.load(path),
        access_url_builder,
        resolver or LanAddressResolver(),
        smtp_factory=smtp_factory,
    )
