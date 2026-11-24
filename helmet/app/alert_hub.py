"""Compose no-hat tracking with durable evidence and a LAN event viewer."""
from pathlib import Path
import math
import threading
import time

try:
    from .alert_events import AlertEventStore, NoHatTrackTracker
    from .alert_viewer import LanEventServer, _is_lan_client
except ImportError:
    from alert_events import AlertEventStore, NoHatTrackTracker
    from alert_viewer import LanEventServer, _is_lan_client


class AlertHub:
    """One event owner whose failures never escape into detection."""

    def __init__(self, root, *, store, tracker, host='0.0.0.0', port=8092,
                 viewer=None, viewer_factory=None, cleanup_interval=3600):
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
        self.host = host
        self.port = port
        self.cleanup_interval = float(cleanup_interval)
        self.viewer = viewer
        self.viewer_factory = viewer_factory
        self._state_lock = threading.RLock()
        self._state_condition = threading.Condition(self._state_lock)
        self._event_lock = threading.Lock()
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
        self._store_closed = False
        self._status_provider = None

    @classmethod
    def from_root(cls, root, port=8092, host='0.0.0.0'):
        root = Path(root).resolve()
        hub = cls(
            root,
            store=AlertEventStore(root),
            tracker=NoHatTrackTracker(),
            host=host,
            port=port,
        )
        if port:
            hub.viewer_factory = lambda: LanEventServer(
                hub.store,
                hub.status,
                host=hub.host,
                port=hub.port,
            )
        return hub

    def lan_url(self, lan_host):
        with self._state_lock:
            port = self.port
        if not port:
            return None
        if not _is_lan_client(lan_host):
            raise ValueError('LAN event host is invalid')
        return 'http://%s:%d/' % (lan_host, port)

    def bind_status_provider(self, provider):
        if not callable(provider):
            raise ValueError('alert status provider is required')
        with self._state_lock:
            self._status_provider = provider

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
            if viewer is None and self.port and self.viewer_factory is not None:
                viewer = self.viewer_factory()
            if viewer is not None:
                viewer.start()
                self._viewer_closed = False
            with self._state_lock:
                self.viewer = viewer
                self._started = True
        except BaseException:
            self._stop_cleanup_thread(3.0)
            with self._state_lock:
                self._closed = True
                self._started = False
            if viewer is not None and not self._viewer_closed:
                try:
                    closed = viewer.close()
                except BaseException:
                    pass
                else:
                    self._viewer_closed = closed is not False
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
                })
            except BaseException:
                with self._state_lock:
                    self._last_error = 'event_store_failed'
                return False
            with self._state_lock:
                self._events_created += 1
                self._last_event_at = event.get('captured_wall_time')
                self._generation = generation
            return True

    def status(self):
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
                             ('state', 'running', 'valid', 'generation',
                              'alarm_state', 'frame_id', 'source_epoch')}
        try:
            store = self.store.status()
        except BaseException:
            store = {'state': 'unavailable'}
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
            'store': store,
            'viewer': {
                'alive': alive,
                'port': port if port else None,
                'scope': 'lan',
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
        result = True
        if viewer is not None and not self._viewer_closed:
            try:
                closed = viewer.close()
            except BaseException:
                result = False
            else:
                self._viewer_closed = closed is not False
                result = self._viewer_closed and result
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
