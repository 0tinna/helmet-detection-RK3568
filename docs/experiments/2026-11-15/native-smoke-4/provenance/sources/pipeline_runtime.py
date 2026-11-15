"""Bounded realtime pipeline with source-independent output and event callbacks.

Engine: infer(RGB HWC)/release. Source: read(stop_event)/release.
Callbacks execute on the run() caller's thread; metadata is JSON-safe.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import queue
import re
import threading
import time
from types import SimpleNamespace
import uuid

import cv2
import numpy as np

try:
    from .alarm import AlarmStateMachine, AudioAlarm
    from .sources import make_source
    from .telemetry import RuntimeSampler, Telemetry, write_session
    from .yolo_post import CLASSES, CLS_HELMET, letterbox, postprocess
except ImportError:
    from alarm import AlarmStateMachine, AudioAlarm
    from sources import make_source
    from telemetry import RuntimeSampler, Telemetry, write_session
    from yolo_post import CLASSES, CLS_HELMET, letterbox, postprocess


DEFAULTS = dict(source='/dev/video10', model='/userdata/models/helmet_640x640.rknn',
                conf=0.35, nms=0.45, skip=0, cam_w=640, cam_h=480,
                no_display=False, save_dir='', save_interval=0, alarm_beep=False,
                duration=0, replay_fps=10.0, result_ttl=1.0, shutdown_timeout=2.0,
                alarm_dwell=0.5, alarm_cooldown=10.0, alarm_clear=1.0,
                audio_timeout=3.0, jsonl='', session_json='', stats_window=5.0,
                sample_interval=1.0)


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class FramePacket:
    frame_id: int
    original_bgr: np.ndarray
    captured_at: float
    captured_wall_time: str
    source_epoch: int


@dataclass(frozen=True)
class DetectionPacket:
    frame: FramePacket
    boxes: np.ndarray
    scores: np.ndarray
    classes: np.ndarray
    detections: list
    processed_at: float
    prep_ms: float
    infer_ms: float
    post_ms: float


@dataclass(frozen=True)
class OutputPacket:
    frame_id: int
    original_bgr: np.ndarray
    annotated_bgr: np.ndarray
    captured_at: float
    processed_at: float
    output_at: float
    detections: list
    boxes: np.ndarray
    scores: np.ndarray
    classes: np.ndarray
    state: str
    alarm_state: str
    metadata: dict


class LatestMailbox:
    """One item; take removes ownership so a frame is never consumed twice."""
    def __init__(self):
        self.condition = threading.Condition()
        self.item = None

    def put(self, item):
        with self.condition:
            overwritten = self.item is not None
            self.item = item
            self.condition.notify()
            return overwritten

    def take(self, stop_event, timeout=0.02):
        with self.condition:
            if self.item is None and not stop_event.is_set():
                self.condition.wait(timeout)
            if self.item is None or stop_event.is_set():
                return None
            item, self.item = self.item, None
            return item

    def wake(self):
        with self.condition:
            self.condition.notify_all()

    def pending(self):
        with self.condition:
            return int(self.item is not None)


def parse_model_size(path):
    match = re.search(r'(?:^|[_-])(\d+)x(\d+)(?:$|[_-])', Path(path).stem)
    if match:
        width, height = map(int, match.groups())
        if width <= 0 or height <= 0:
            raise ValueError('model dimensions must be positive')
        return width, height
    return 640, 640


class RKNNEngine:
    def __init__(self, model_path, input_w=640, input_h=640):
        try:
            from rknnlite.api import RKNNLite as engine_class
        except ImportError:
            try:
                from rknn.api import RKNN as engine_class
            except ImportError as error:
                raise PipelineError('RKNN runtime unavailable; use the board runtime or inject an engine') from error
        self.w, self.h = input_w, input_h
        self.rknn = engine_class(verbose=False)
        try:
            if self.rknn.load_rknn(model_path) != 0:
                raise PipelineError('load_rknn failed: ' + model_path)
            if self.rknn.init_runtime() != 0:
                raise PipelineError('init_runtime failed')
        except BaseException:
            self.rknn.release()
            raise

    def infer(self, rgb_hw3):
        outputs = self.rknn.inference(inputs=[rgb_hw3[np.newaxis]], data_format='nhwc')
        if outputs is None:
            raise PipelineError('RKNN inference returned no outputs')
        return outputs

    def release(self):
        self.rknn.release()


def judge_wearing(boxes, classes, scores, iou_th=0.3):
    """Preserve existing person/hat overlap suppression and class semantics."""
    results = []
    hats = boxes[classes == CLS_HELMET]
    for box, class_id, score in zip(boxes, classes, scores):
        wearing = int(class_id) == CLS_HELMET
        if not wearing and len(hats):
            intersection = np.clip(np.minimum(box[2:], hats[:, 2:])
                                   - np.maximum(box[:2], hats[:, :2]), 0, None).prod(axis=1)
            area = (box[2] - box[0]) * (box[3] - box[1])
            hat_areas = (hats[:, 2] - hats[:, 0]) * (hats[:, 3] - hats[:, 1])
            wearing = bool(np.max(intersection / (area + hat_areas - intersection + 1e-9)) > iou_th)
        results.append((box, wearing, float(score)))
    return results


def draw_results(image, results, alarm_state=None):
    danger = False
    for box, wearing, score in results:
        x1, y1, x2, y2 = map(int, box)
        color = (0, 200, 0) if wearing else (0, 0, 255)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        cv2.putText(image, '%s %.0f%%' % ('hat' if wearing else 'NO HAT', score * 100),
                    (x1, max(y1 - 4, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        danger |= not wearing
    if danger and alarm_state in (None, 'active'):
        cv2.putText(image, 'WARNING: HELMET NOT DETECTED', (8, 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return danger


def normalize_args(args):
    values = dict(DEFAULTS)
    values.update(vars(args))
    for name in ('cam_w', 'cam_h', 'skip'):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if name == 'skip' else 1):
            raise ValueError(name + ' has an invalid integer value')
    for name in ('duration', 'save_interval', 'alarm_dwell', 'alarm_cooldown', 'alarm_clear'):
        if not math.isfinite(values[name]) or values[name] < 0:
            raise ValueError(name + ' must be finite and nonnegative')
    for name in ('replay_fps', 'result_ttl', 'shutdown_timeout', 'audio_timeout',
                 'stats_window', 'sample_interval'):
        if not math.isfinite(values[name]) or values[name] <= 0:
            raise ValueError(name + ' must be finite and positive')
    for name in ('conf', 'nms'):
        if not math.isfinite(values[name]) or not 0 < values[name] < 1:
            raise ValueError(name + ' must be between 0 and 1')
    if values['save_interval'] and not values['save_dir']:
        raise ValueError('--save-interval requires --save-dir')
    if not str(values['source']).strip() or not str(values['model']).strip():
        raise ValueError('source and model cannot be empty')
    return SimpleNamespace(**values)


class Pipeline:
    def __init__(self, args, engine=None, source=None, output_callback=None, event_callback=None):
        self.args = normalize_args(args)
        self.mw, self.mh = parse_model_size(self.args.model)
        self.engine, self.source = engine, source
        self.output_callback = output_callback or (lambda packet: None)
        self.event_callback = event_callback or (lambda event: None)
        self.frames, self.results = LatestMailbox(), LatestMailbox()
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.paused = False
        self.events, self.errors = queue.Queue(), queue.Queue()
        self.state, self.reason = 'startup', 'initializing'
        self.source_epoch = 0
        self.source_interrupted = False
        self.last_frame_id = None
        self.latest_output = None
        self.status_captured_at = None
        self.alarm = AlarmStateMachine(self.args.alarm_dwell, self.args.alarm_cooldown,
                                       self.args.alarm_clear, self.args.result_ttl)
        self.telemetry = None
        self.capture_thread = self.detect_thread = None
        self.started = None
        self.used = False
        self.model_sha256 = None
        self._queue_state('startup', 'initializing')

    def _event(self, kind, **fields):
        return {'type': kind, 'monotonic': time.monotonic(),
                'wall_time': datetime.now(timezone.utc).isoformat(), **fields}

    def _queue_state(self, state, reason):
        with self.lock:
            self.state, self.reason = state, reason
            if state in ('startup', 'degraded', 'fault', 'stop'):
                self.alarm.invalidate()
            self.events.put(self._event('state', state=state, reason=reason,
                                        alarm_state=self.alarm.state, frame_id=self.last_frame_id,
                                        valid=state in ('healthy', 'no-detection')))

    def _set_state(self, state, reason):
        with self.lock:
            if self.state == 'fault' and state != 'fault':
                return
            if state != self.state:
                self._queue_state(state, reason)

    def _fail(self, component, error):
        self.errors.put(PipelineError('%s: %s' % (component, error)))
        self._set_state('fault', '%s: %s' % (component, error))
        self.stop_event.set()
        self.frames.wake()
        self.results.wake()

    def _drain_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            self.event_callback(event)

    def status_snapshot(self):
        with self.lock:
            result = {'state': self.state, 'reason': self.reason,
                      'alarm_state': self.alarm.state, 'frame_id': self.last_frame_id,
                      'valid': self.state in ('healthy', 'no-detection'),
                      'model': self.args.model, 'model_sha256': self.model_sha256,
                      'source': self.args.source, 'monotonic': time.monotonic()}
            # HTTP/status consumers remain truthful even if the main sink is busy.
            if (result['valid'] and self.status_captured_at is not None
                    and result['monotonic'] - self.status_captured_at > self.args.result_ttl):
                result.update(state='degraded', reason='result expired', valid=False, alarm_state='unknown')
        if self.telemetry is not None:
            summary = self.telemetry.summary(include_full=False)
            result['counters'] = summary['counters']
            result['fps'] = summary['sliding_window']['fps']
            result['latency'] = summary['sliding_window']['timings']
        return result

    def capture_loop(self):
        frame_id = 0
        try:
            while not self.stop_event.is_set():
                if self.paused:
                    self.stop_event.wait(0.02)
                    continue
                ok, image = self.source.read(self.stop_event)
                if self.stop_event.is_set():
                    break
                if not ok or image is None or image.size == 0:
                    self.telemetry.increment('read_failure')
                    with self.lock:
                        self.source_epoch += 1
                        self.source_interrupted = True
                        self._set_state('degraded', 'source read failure')
                    self.stop_event.wait(0.02)
                    continue
                if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
                    raise ValueError('source must return uint8 BGR HWC frames')
                captured_at = time.monotonic()
                frame_id += 1
                with self.lock:
                    epoch = self.source_epoch
                    self.source_interrupted = False
                packet = FramePacket(frame_id, image.copy(), captured_at,
                                     datetime.now(timezone.utc).isoformat(), epoch)
                self.telemetry.increment('captured', captured_at)
                if self.frames.put(packet):
                    self.telemetry.increment('overwritten')
        except BaseException as error:
            self._fail('capture worker', error)
        finally:
            try:
                self.source.release()
            except BaseException as error:
                self._fail('source release', error)

    def detect_loop(self):
        consumed = 0
        try:
            while not self.stop_event.is_set():
                packet = self.frames.take(self.stop_event)
                if packet is None:
                    continue
                consumed += 1
                self.telemetry.increment('consumed')
                if self.args.skip and consumed % (self.args.skip + 1) != 0:
                    self.telemetry.increment('active_skip')
                    continue
                started = time.monotonic()
                image, ratio, pad = letterbox(packet.original_bgr, self.mw, self.mh)
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                prepared = time.monotonic()
                try:
                    outputs = self.engine.infer(rgb)
                    inferred = time.monotonic()
                    if outputs is None:
                        raise PipelineError('engine returned no outputs')
                    h, w = packet.original_bgr.shape[:2]
                    boxes, scores, classes = postprocess(outputs, ratio, pad, w, h,
                                                        conf_th=self.args.conf, iou_th=self.args.nms,
                                                        input_size=(self.mw, self.mh))
                    detections = judge_wearing(boxes, classes, scores)
                except BaseException:
                    self.telemetry.increment('detection_failure')
                    raise
                processed = time.monotonic()
                result = DetectionPacket(packet, boxes, scores, classes, detections, processed,
                                         (prepared - started) * 1000, (inferred - prepared) * 1000,
                                         (processed - inferred) * 1000)
                self.telemetry.increment('detected', processed)
                self.telemetry.record_detection({
                    'frame_id': packet.frame_id, 'captured_at': packet.captured_at,
                    'captured_wall_time': packet.captured_wall_time, 'processed_at': processed,
                    'model': self.args.model, 'model_sha256': self.model_sha256,
                    'source': self.args.source, 'class_order': list(CLASSES),
                    'classes': classes.tolist(), 'scores': scores.tolist(), 'boxes': boxes.tolist(),
                    'prep_ms': result.prep_ms, 'infer_ms': result.infer_ms, 'post_ms': result.post_ms})
                if self.results.put(result):
                    self.telemetry.increment('output_overwritten')
        except BaseException as error:
            self._fail('detection worker', error)
        finally:
            try:
                self.engine.release()
            except BaseException as error:
                self._fail('engine release', error)

    def _output(self, result):
        now = time.monotonic()
        frame = result.frame
        with self.lock:
            valid = (not self.source_interrupted and not self.paused
                     and frame.source_epoch == self.source_epoch
                     and now - frame.captured_at <= self.args.result_ttl
                     and self.state != 'fault')
            state = ('healthy' if result.detections else 'no-detection') if valid else 'degraded'
            self.last_frame_id = frame.frame_id
            self.status_captured_at = frame.captured_at
            self._set_state(state, 'fresh detections' if valid else 'result expired or source interrupted')
        # A status sink may take time; freshness must be checked after it returns.
        self._drain_events()
        with self.lock:
            valid = (valid and not self.source_interrupted and not self.paused
                     and frame.source_epoch == self.source_epoch
                     and time.monotonic() - frame.captured_at <= self.args.result_ttl
                     and self.state != 'fault')
            if not valid:
                state = 'fault' if self.state == 'fault' else 'degraded'
                self._set_state(state, 'result expired or source interrupted')
            previous_trigger = self.alarm.last_trigger
            triggered = self.alarm.update(frame.frame_id, frame.captured_at,
                                           any(not wearing for _, wearing, _ in result.detections),
                                           valid, bool(result.detections))
            alarm_state = self.alarm.state
        annotated = frame.original_bgr.copy()
        if valid:
            draw_results(annotated, result.detections, alarm_state)
        color = (0, 200, 200) if state != 'healthy' else (200, 200, 200)
        cv2.putText(annotated, '%s | alarm: %s | frame: %d' % (state, alarm_state, frame.frame_id),
                    (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        output_at = time.monotonic()
        metadata = {'frame_id': frame.frame_id, 'source': self.args.source,
                    'captured_at': frame.captured_at, 'processed_at': result.processed_at,
                    'output_at': output_at, 'captured_wall_time': frame.captured_wall_time,
                    'output_wall_time': datetime.now(timezone.utc).isoformat(),
                    'model': self.args.model, 'model_sha256': self.model_sha256,
                    'input_size': [self.mw, self.mh], 'image_size': list(frame.original_bgr.shape[1::-1]),
                    'class_order': list(CLASSES), 'classes': result.classes.tolist(),
                    'class_names': [CLASSES[int(value)] for value in result.classes],
                    'scores': result.scores.tolist(), 'boxes': result.boxes.tolist(),
                    'wearing': [bool(value) for _, value, _ in result.detections],
                    'conf': self.args.conf, 'nms': self.args.nms, 'skip': self.args.skip,
                    'state': state, 'valid': valid, 'valid_at_dispatch': valid, 'alarm_state': alarm_state,
                    'source_epoch': frame.source_epoch,
                    'expires_at': frame.captured_at + self.args.result_ttl,
                    'alarm_triggered': triggered, 'prep_ms': result.prep_ms,
                    'infer_ms': result.infer_ms, 'post_ms': result.post_ms,
                    'capture_to_output_ms': (output_at - frame.captured_at) * 1000}
        packet = OutputPacket(frame.frame_id, frame.original_bgr, annotated, frame.captured_at,
                              result.processed_at, output_at, result.detections, result.boxes,
                              result.scores, result.classes, state, alarm_state, metadata)
        self.latest_output = packet
        self._drain_events()
        packet = self._presentation_packet(packet)
        metadata['valid_at_dispatch'] = metadata['valid']
        if not metadata['valid']:
            self.alarm.last_trigger = previous_trigger
            triggered = False
        self._drain_events()
        packet = self._presentation_packet(packet)
        metadata['valid_at_dispatch'] = metadata['valid']
        if not metadata['valid']:
            self.alarm.last_trigger = previous_trigger
            triggered = False
        output_at = time.monotonic()
        metadata.update(output_at=output_at, capture_to_output_ms=(output_at - frame.captured_at) * 1000)
        packet = replace(packet, output_at=output_at)
        self.latest_output = packet
        self.telemetry.increment('output', output_at)
        sink_started = time.monotonic()
        sink_error = None
        try:
            self.output_callback(packet)
        except BaseException as error:
            sink_error = error
            self._set_state('fault', 'output callback: ' + str(error))
        finished = time.monotonic()
        with self.lock:
            fresh_at_completion = (metadata['valid'] and sink_error is None and not self.source_interrupted
                                   and not self.paused and frame.source_epoch == self.source_epoch
                                   and finished - frame.captured_at <= self.args.result_ttl
                                   and self.state != 'fault')
            if not fresh_at_completion:
                self._set_state('fault' if self.state == 'fault' else 'degraded',
                                'result expired or source interrupted during output')
                if triggered:
                    # No alarm was emitted; do not consume its cooldown budget.
                    self.alarm.last_trigger = previous_trigger
                triggered = False
        metadata.update(callback_finished_at=finished, sink_ms=(finished - sink_started) * 1000,
                        capture_to_sink_complete_ms=(finished - frame.captured_at) * 1000,
                        valid_at_sink_complete=fresh_at_completion, alarm_triggered=triggered,
                        sink_error=None if sink_error is None else str(sink_error))
        packet = self._presentation_packet(packet)
        # Sinks clear any now-expired JPEG as soon as their publication call returns.
        self._drain_events()
        packet = self._presentation_packet(packet)
        self.telemetry.record(metadata)
        if sink_error is not None:
            raise sink_error
        packet = self._presentation_packet(packet)
        alarm_emitted = False
        if triggered and packet.metadata['valid']:
            self.event_callback(self._event('alarm', state=packet.state, alarm_state=packet.alarm_state,
                                            frame_id=frame.frame_id, metadata=dict(metadata)))
            alarm_emitted = True
            # Event callbacks can block too. Do not carry their formerly valid
            # warning onward to later audio, file, or display consumers.
            packet = self._presentation_packet(packet)
        if triggered and not alarm_emitted:
            with self.lock:
                self.alarm.last_trigger = previous_trigger
        self._drain_events()
        return packet, triggered and packet.metadata['valid']

    def _presentation_packet(self, packet):
        """Check current source/TTL/fault state at an actual consumer boundary.

        Dispatch history is kept separately; ``valid`` describes whether the
        packet may still be presented now. An expired image is rebuilt from its
        own original frame, so no stale boxes or alarm banner survive.
        """
        now = time.monotonic()
        with self.lock:
            fresh = (packet.metadata['valid'] and not self.source_interrupted and not self.paused
                     and packet.metadata.get('source_epoch') == self.source_epoch
                     and now - packet.captured_at <= self.args.result_ttl
                     and self.state not in ('fault', 'stop'))
            packet.metadata['presentation_checked_at'] = now
            if fresh:
                return packet
            state = self.state if self.state in ('fault', 'stop') else 'degraded'
            self._set_state(state, 'result expired or source interrupted at presentation')
            packet.metadata.update(valid=False, state=state, alarm_state='unknown', alarm_triggered=False)
        image = packet.original_bgr.copy()
        cv2.putText(image, '%s | alarm: unknown | frame: %d' % (state, packet.frame_id),
                    (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 200), 1)
        current = replace(packet, annotated_bgr=image, state=state, alarm_state='unknown', detections=[])
        if self.latest_output is not None and self.latest_output.frame_id == packet.frame_id:
            self.latest_output = current
        return current

    def _handle_key(self, key, packet=None):
        if key in (ord('q'), 27):
            return True
        if key == ord('p'):
            self.paused = not self.paused
            if self.paused:
                self._set_state('degraded', 'paused')
        if key == ord('s') and packet is not None and packet.metadata['valid']:
            if time.monotonic() - packet.captured_at <= self.args.result_ttl:
                self._save(packet, 'manual')
        return False

    def _save(self, packet, reason):
        packet = self._presentation_packet(packet)
        if not packet.metadata['valid']:
            self._drain_events()
            return False
        directory = Path(self.args.save_dir or '/tmp')
        directory.mkdir(parents=True, exist_ok=True)
        name = '%s_%d_%s_%s' % (reason, packet.frame_id, time.time_ns(), uuid.uuid4().hex[:8])
        image_path, json_path = directory / (name + '.jpg'), directory / (name + '.json')
        ok, encoded = cv2.imencode('.jpg', packet.annotated_bgr)
        if not ok:
            raise PipelineError('snapshot JPEG encoding failed')
        packet = self._presentation_packet(packet)
        if not packet.metadata['valid']:
            self._drain_events()
            return False
        try:
            with image_path.open('xb') as stream:
                stream.write(encoded.tobytes())
            packet = self._presentation_packet(packet)
            if not packet.metadata['valid']:
                image_path.unlink(missing_ok=True)
                self._drain_events()
                return False
            saved_at = time.monotonic()
            with json_path.open('x', encoding='utf-8') as stream:
                import json
                json.dump({**packet.metadata, 'reason': reason, 'snapshot': str(image_path),
                           'saved_at': saved_at, 'snapshot_age_ms': (saved_at - packet.captured_at) * 1000},
                          stream, indent=2, allow_nan=False)
            packet = self._presentation_packet(packet)
            if not packet.metadata['valid']:
                image_path.unlink(missing_ok=True)
                json_path.unlink(missing_ok=True)
                self._drain_events()
                return False
        except BaseException:
            image_path.unlink(missing_ok=True)
            json_path.unlink(missing_ok=True)
            raise
        self.event_callback(self._event('snapshot', reason=reason, frame_id=packet.frame_id,
                                        snapshot=str(image_path), metadata=str(json_path)))
        return True

    def _check_expiry(self):
        latest = self.latest_output
        if latest is not None and time.monotonic() - latest.captured_at > self.args.result_ttl:
            self._set_state('degraded', 'result expired')

    def run(self):
        if self.used:
            raise PipelineError('a pipeline session cannot be run twice')
        self.used = True
        self.started = time.monotonic()
        failure = None
        audio = None
        try:
            self.telemetry = Telemetry(self.args.stats_window, self.args.jsonl, self.args.session_json)
            self.telemetry.started = self.started
            sampler = RuntimeSampler()
            self.telemetry.add_sample(sampler.sample())
            next_sample = self.started + self.args.sample_interval
            next_save = self.started + self.args.save_interval
            if self.args.save_dir:
                Path(self.args.save_dir).mkdir(parents=True, exist_ok=True)
            if self.source is None:
                self.source = make_source(self.args.source, self.args.cam_w, self.args.cam_h,
                                          self.args.replay_fps)
            if self.engine is None:
                self.engine = RKNNEngine(self.args.model, self.mw, self.mh)
            model_path = Path(self.args.model)
            if model_path.is_file():
                with model_path.open('rb') as stream:
                    digest = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                    self.model_sha256 = digest.hexdigest()
            audio = AudioAlarm(timeout=self.args.audio_timeout,
                               event_callback=self.event_callback) if self.args.alarm_beep else None
            self.capture_thread = threading.Thread(target=self.capture_loop, name='helmet-capture', daemon=True)
            self.detect_thread = threading.Thread(target=self.detect_loop, name='helmet-detect', daemon=True)
            self.capture_thread.start()
            self.detect_thread.start()
            self._drain_events()
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self.args.duration and now - self.started >= self.args.duration:
                    break
                self._check_expiry()
                self._drain_events()
                if now >= next_sample:
                    self.telemetry.add_sample(sampler.sample())
                    next_sample = now + self.args.sample_interval
                if audio:
                    audio.poll()
                result = self.results.take(self.stop_event)
                if result is None:
                    if not self.args.no_display:
                        if self.latest_output is not None and not self.status_snapshot()['valid']:
                            image = self.latest_output.original_bgr.copy()
                            cv2.putText(image, self.state.upper(), (8, 22), cv2.FONT_HERSHEY_SIMPLEX,
                                        0.6, (0, 200, 200), 2)
                            cv2.imshow('Helmet Detection', image)
                        key = cv2.waitKey(1) & 0xff
                        if self._handle_key(key, self.latest_output):
                            break
                    continue
                packet, triggered = self._output(result)
                packet = self._presentation_packet(packet)
                if triggered and packet.metadata['valid']:
                    if audio:
                        packet = self._presentation_packet(packet)
                        if packet.metadata['valid']:
                            audio.trigger()
                    if self.args.save_dir:
                        self._save(packet, 'alarm')
                if (self.args.save_dir and self.args.save_interval
                        and packet.metadata['valid'] and now >= next_save):
                    if self._save(packet, 'periodic'):
                        next_save = time.monotonic() + self.args.save_interval
                if not self.args.no_display:
                    packet = self._presentation_packet(packet)
                    cv2.imshow('Helmet Detection', packet.annotated_bgr)
                    self.telemetry.increment('displayed')
                    key = cv2.waitKey(1) & 0xff
                    if self._handle_key(key, packet):
                        break
        except KeyboardInterrupt:
            pass
        except BaseException as error:
            failure = error if isinstance(error, PipelineError) else PipelineError(str(error))
            self._fail('pipeline', failure)
        finally:
            self.stop_event.set()
            self.frames.wake()
            self.results.wake()
            deadline = time.monotonic() + self.args.shutdown_timeout
            for thread in (self.capture_thread, self.detect_thread):
                if thread is not None and thread.ident is not None:
                    thread.join(max(0.0, deadline - time.monotonic()))
                    if thread.is_alive():
                        self._fail('shutdown', 'worker did not stop: ' + thread.name)
            for thread, resource in ((self.capture_thread, self.source), (self.detect_thread, self.engine)):
                if resource is not None and (thread is None or thread.ident is None):
                    try:
                        resource.release()
                    except BaseException as error:
                        self._fail('startup release', error)
            if audio:
                try:
                    audio.close()
                except BaseException as error:
                    self._fail('audio shutdown', error)
            if not self.args.no_display:
                try:
                    cv2.destroyAllWindows()
                except BaseException as error:
                    self._fail('display shutdown', error)
            if not self.errors.empty():
                failure = self.errors.get()
            if failure is None:
                self._set_state('stop', 'session stopped')
            try:
                self._drain_events()
            except BaseException as error:
                if failure is None:
                    failure = PipelineError('event callback: ' + str(error))
                    self._set_state('fault', str(failure))
            summary = self.telemetry.summary() if self.telemetry else {'counters': {}}
            summary['counters']['pending_capture'] = self.frames.pending()
            summary['counters']['pending_output'] = self.results.pending()
            summary.update(state=self.state, reason=self.reason, model=self.args.model,
                           model_sha256=self.model_sha256, source=self.args.source,
                           class_order=list(CLASSES), input_size=[self.mw, self.mh],
                           thresholds={'conf': self.args.conf, 'nms': self.args.nms},
                           replay_fps=self.args.replay_fps, result_ttl=self.args.result_ttl,
                           runtime={'python': __import__('sys').version, 'opencv': cv2.__version__,
                                    'numpy': np.__version__})
            try:
                if self.telemetry:
                    self.telemetry.close()
                write_session(self.args.session_json, summary)
            except BaseException as error:
                failure = failure or PipelineError('telemetry save: ' + str(error))
                self._set_state('fault', str(failure))
                summary.update(state=self.state, reason=self.reason)
                try:
                    self._drain_events()
                except BaseException:
                    # Preserve the storage failure even if its error consumer fails.
                    pass
        if failure is not None:
            raise failure
        return summary
