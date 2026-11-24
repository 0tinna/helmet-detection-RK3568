"""Compose no-hat tracking, evidence storage, mail and public event access."""
from pathlib import Path
import math
import threading
import time

try:
    from .alert_events import AlertEventStore, NoHatTrackTracker
    from .alert_mail import PublicAddressResolver, load_mailer
    from .alert_viewer import AlertAccessKey, PublicEventServer
except ImportError:
    from alert_events import AlertEventStore, NoHatTrackTracker
    from alert_mail import PublicAddressResolver, load_mailer
    from alert_viewer import AlertAccessKey, PublicEventServer


class AlertHub:
    """One event owner whose failures never escape into detection."""

    def __init__(self, root, *, store, tracker, mailer, resolver, access_key,
                 host='0.0.0.0', port=8092, viewer=None, viewer_factory=None,
                 cleanup_interval=3600):
        self.root = Path(root).resolve()
        if not isinstance(host, str) or not host:
            raise ValueError('alert viewer host is required')
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('alert viewer port is invalid')
        if (type(cleanup_interval) not in (int, float)
                or not math.isfinite(cleanup_interval)
                or cleanup_interval <= 0):
            raise ValueError('alert cleanup interval must be positive')
        self.store = store
        self.tracker = tracker
        self.mailer = mailer
        self.resolver = resolver
        self.access_key = access_key
        self.host = host
        self.port = port
        self.cleanup_interval = float(cleanup_interval)
        self.viewer = viewer
        self.viewer_factory = viewer_factory
        self._state_lock = threading.RLock()
        self._state_condition = threading.Condition(self._state_lock)
        self._event_lock = threading.Lock()
        self._public_ip_lock = threading.Lock()
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = None
        self._started = False
        self._closed = False
        self._closing = False
        self._generation = None
        self._events_created = 0
        self._last_event_at = None
        self._last_error = None
        self._close_result = None
        self._last_close_result = None
        self._close_attempt = 0
        self._viewer_closed = viewer is None
        self._mailer_closed = mailer is None
        self._store_closed = False
        self._status_provider = None
        self._public_ip_cache = None
        self._public_ip_refresh_at = 0.0

    @classmethod
    def from_root(cls, root, port=8092, host='0.0.0.0'):
        root = Path(root).resolve()
        resolver = PublicAddressResolver()
        hub = cls(
            root,
            store=AlertEventStore(root),
            tracker=NoHatTrackTracker(),
            mailer=None,
            resolver=resolver,
            access_key=None,
            host=host,
            port=port,
            viewer=None,
        )
        if port:
            hub.access_key = AlertAccessKey.load_or_create(
                root / 'app-state' / 'public-access.key')
            hub.viewer_factory = lambda: PublicEventServer(
                hub.store,
                hub.access_key,
                hub.status,
                host=hub.host,
                port=hub.port,
            )
            hub.mailer = load_mailer(
                root, hub.access_url, resolver=resolver)
        return hub

    def access_url(self, public_host):
        with self._state_lock:
            key, port = self.access_key, self.port
        if key is None or not port:
            return None
        return key.url_path(public_host, public_port=port, scheme='http')

    def bind_status_provider(self, provider):
        if not callable(provider):
            raise ValueError('alert status provider is required')
        with self._state_lock:
            self._status_provider = provider

    def _public_address_status(self, refresh):
        now = time.monotonic()
        with self._public_ip_lock:
            if not refresh:
                if self._public_ip_cache is not None:
                    return dict(self._public_ip_cache)
                return {'address': None, 'checked_at': None, 'stale': True}
            if (self._public_ip_cache is not None
                    and now < self._public_ip_refresh_at):
                return dict(self._public_ip_cache)
            try:
                observed = self.resolver.resolve()
                if not isinstance(observed, dict):
                    raise TypeError('public address status is invalid')
                public_ip = dict(observed)
            except BaseException:
                public_ip = {'address': None, 'checked_at': None, 'stale': True}
            self._public_ip_cache = dict(public_ip)
            self._public_ip_refresh_at = now + 60.0
            return public_ip

    def start(self):
        with self._state_lock:
            if self._closed or self._closing:
                raise RuntimeError('alert hub is closed')
            if self._started:
                raise RuntimeError('alert hub cannot be started twice')
        viewer = self.viewer
        try:
            self._run_cleanup()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name='helmet-alert-cleanup',
                daemon=True,
            )
            self._cleanup_thread.start()
            if self.mailer is not None:
                self.mailer.start()
            if viewer is None and self.port and self.viewer_factory is not None:
                viewer = self.viewer_factory()
            if viewer is not None:
                viewer.start()
            with self._state_lock:
                self.viewer = viewer
                self._started = True
        except BaseException:
            self._stop_cleanup_thread(3.0)
            with self._state_lock:
                self._closed = True
                self._started = False
            if self.mailer is not None:
                try:
                    mailer_closed = bool(self.mailer.close(3.0))
                except BaseException:
                    pass
                else:
                    self._mailer_closed = mailer_closed
            if viewer is not None and not self._viewer_closed:
                try:
                    viewer.close()
                except BaseException:
                    pass
                else:
                    self._viewer_closed = True
            try:
                self.store.close()
            except BaseException:
                pass
            else:
                self._store_closed = True
            raise

    def _run_cleanup(self):
        try:
            self.store.cleanup()
        except BaseException:
            with self._state_lock:
                self._last_error = 'event_cleanup_failed'
            return False
        with self._state_lock:
            if self._last_error == 'event_cleanup_failed':
                self._last_error = None
        return True

    def _cleanup_loop(self):
        while not self._cleanup_stop.wait(self.cleanup_interval):
            self._run_cleanup()

    def _stop_cleanup_thread(self, timeout):
        thread = self._cleanup_thread
        if thread is None:
            return True
        self._cleanup_stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout)
        return not thread.is_alive()

    def reset(self, generation):
        if not isinstance(generation, str) or not generation:
            raise ValueError('alert generation is required')
        with self._event_lock:
            with self._state_lock:
                if self._closed or self._closing:
                    return
            self.tracker.reset()
            with self._state_lock:
                self._generation = generation
                self._last_error = None

    def observe(self, packet, jpeg, generation):
        with self._state_lock:
            if not self._started or self._closed or self._closing:
                return False
            if not isinstance(generation, str) or not generation:
                return False
        with self._event_lock:
            with self._state_lock:
                if not self._started or self._closed or self._closing:
                    return False
                expected_generation = self._generation
                if (expected_generation is not None
                        and generation != expected_generation):
                    return False
                self._last_error = None
            try:
                metadata = packet.metadata
                if not isinstance(metadata, dict) or metadata.get('valid') is not True:
                    return False
                detections = packet.detections
                candidate = self.tracker.observe(
                    packet.frame_id,
                    packet.captured_at,
                    metadata.get('source_epoch'),
                    generation,
                    detections,
                )
            except BaseException:
                with self._state_lock:
                    self._last_error = 'invalid_alert_packet'
                return False
            if candidate is None:
                return False
            try:
                selection = metadata.get('selection')
                if not isinstance(selection, dict):
                    selection = {
                        'source': metadata.get('source'),
                        'profile': metadata.get('profile'),
                    }
                event = self.store.publish(candidate, jpeg, {
                    'model': metadata.get('model'),
                    'selection': selection,
                    'public_ip': None,
                })
            except BaseException:
                with self._state_lock:
                    self._last_error = 'event_store_failed'
                return False
            with self._state_lock:
                self._events_created += 1
                self._last_event_at = event.get('captured_wall_time')
                self._generation = generation
            if self.mailer is not None:
                try:
                    self.mailer.enqueue([event])
                except BaseException:
                    with self._state_lock:
                        self._last_error = 'mail_enqueue_failed'
            return True

    def status(self, refresh_public_ip=True):
        if type(refresh_public_ip) is not bool:
            raise ValueError('public address refresh must be boolean')
        with self._state_lock:
            viewer = self.viewer
            started, closed, generation = self._started, self._closed, self._generation
            events_created = self._events_created
            last_event_at = self._last_event_at
            last_error = self._last_error
            port = self.port
            status_provider = self._status_provider
        detection = {}
        if status_provider is not None:
            try:
                observed = status_provider()
            except BaseException:
                observed = {}
            if isinstance(observed, dict):
                nested = observed.get('detection')
                source = nested if isinstance(nested, dict) else observed
                detection = {key: source.get(key) for key in
                             ('state', 'running', 'valid', 'generation')}
        public_ip = self._public_address_status(refresh_public_ip)
        try:
            store = self.store.status()
        except BaseException:
            store = {'state': 'unavailable'}
        if self.mailer is None:
            mail = {
                'configured': False,
                'state': 'not_configured',
                'last_result': None,
                'pending_batches': 0,
                'dropped_batches': 0,
                'worker_alive': False,
            }
        else:
            try:
                mail = self.mailer.status()
            except BaseException:
                mail = {'configured': True, 'state': 'unavailable',
                        'worker_alive': False}
        alive = bool(
            viewer is not None
            and viewer.thread is not None
            and viewer.thread.is_alive()
            and not closed
        )
        return {
            'started': started,
            'closed': closed,
            'generation': generation,
            'events_created': events_created,
            'last_event_at': last_event_at,
            'last_error': last_error,
            'detection': detection,
            'public_ip': public_ip,
            'store': store,
            'mail': mail,
            'viewer': {
                'alive': alive,
                'port': port if port else None,
            },
            'received_at': time.monotonic(),
        }

    def close(self, timeout=3.0):
        if type(timeout) not in (int, float) or timeout < 0:
            raise ValueError('alert close timeout is invalid')
        with self._state_condition:
            if self._close_result is True:
                return self._close_result
            if self._closing:
                attempt = self._close_attempt
                while self._closing and self._close_attempt == attempt:
                    self._state_condition.wait()
                return bool(self._last_close_result)
            self._closing = True
            self._closed = True
            self._close_attempt += 1
            viewer = self.viewer
            mailer = self.mailer
        result = True
        if (viewer is not None and not self._viewer_closed):
            try:
                viewer.close()
            except BaseException:
                result = False
            else:
                self._viewer_closed = True
        if (mailer is not None and not self._mailer_closed):
            try:
                mailer_closed = bool(mailer.close(timeout))
            except BaseException:
                result = False
            else:
                self._mailer_closed = mailer_closed
                result = mailer_closed and result
        if not self._stop_cleanup_thread(timeout):
            result = False
        if not self._store_closed:
            try:
                self.store.close()
            except BaseException:
                result = False
            else:
                self._store_closed = True
        with self._state_condition:
            self._started = False
            self._closing = False
            self._last_close_result = result
            if result:
                self._close_result = True
            self._state_condition.notify_all()
        return result
