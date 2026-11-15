"""Temporal alarm policy and a single bounded audio subprocess."""
import math
import subprocess
import time


class AlarmStateMachine:
    def __init__(self, dwell=0.5, cooldown=10.0, clear_interval=1.0, max_gap=1.0):
        for name, value in [('dwell', dwell), ('cooldown', cooldown),
                            ('clear_interval', clear_interval), ('max_gap', max_gap)]:
            if not math.isfinite(value) or value < 0 or (name == 'max_gap' and value == 0):
                raise ValueError(name + ' has an invalid interval')
        self.dwell, self.cooldown = dwell, cooldown
        self.clear_interval, self.max_gap = clear_interval, max_gap
        self.state = 'unknown'
        self.last_id = -1
        self.last_at = None
        self.danger_since = None
        self.clear_since = None
        self.last_trigger = -math.inf

    def invalidate(self):
        self.state = 'unknown'
        self.danger_since = self.clear_since = None
        self.last_at = None

    def update(self, frame_id, now, danger, valid, has_detections):
        if not valid or not has_detections:
            self.invalidate()
            self.last_id = max(self.last_id, frame_id)
            return False
        if frame_id <= self.last_id:
            return False
        if self.last_at is not None and (now < self.last_at or now - self.last_at > self.max_gap):
            self.invalidate()
        self.last_id, self.last_at = frame_id, now
        if danger:
            self.clear_since = None
            if self.danger_since is None:
                self.danger_since = now
            if self.state == 'active':
                return False
            self.state = 'pending'
            if now - self.danger_since >= self.dwell:
                if now - self.last_trigger >= self.cooldown:
                    self.last_trigger = now
                    self.state = 'active'
                    return True
                self.state = 'cooldown'
        else:
            self.danger_since = None
            if self.clear_since is None:
                self.clear_since = now
            if self.state == 'active' and now - self.clear_since < self.clear_interval:
                return False
            self.state = 'clear'
        return False


class AudioAlarm:
    def __init__(self, command=None, timeout=3.0, event_callback=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('audio timeout must be positive')
        self.command = command or ['aplay', '-q', '/oem/alarm.wav']
        self.timeout = timeout
        self.event_callback = event_callback or (lambda event: None)
        self.process = None
        self.started = None

    def _failure(self, reason):
        self.event_callback({'type': 'audio_failure', 'reason': reason,
                             'monotonic': time.monotonic()})

    def trigger(self):
        self.poll()
        if self.process is not None:
            return
        try:
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, 'CREATE_NO_WINDOW') else {}
            self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            shell=False, **options)
            self.started = time.monotonic()
        except OSError as error:
            self._failure(str(error))

    def poll(self):
        if self.process is None:
            return
        code = self.process.poll()
        if code is not None:
            if code != 0:
                self._failure('audio exit code %d' % code)
            self.process = None
        elif time.monotonic() - self.started > self.timeout:
            self._terminate()
            self._failure('audio timeout')

    def _terminate(self):
        process, self.process = self.process, None
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.2)

    def close(self):
        self._terminate()
