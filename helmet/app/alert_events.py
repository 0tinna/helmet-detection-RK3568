"""Anonymous no-hat tracking and durable event evidence."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import threading


EVENT_ID = re.compile(r'^\d{8}T\d{12}Z-[0-9a-f]{8}$')
SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class EventCandidate:
    captured_at: float
    captured_wall_time: str
    frame_id: int
    source_epoch: int
    generation: str
    trigger_track_ids: list
    visible_no_hat_track_ids: list
    detections: list


@dataclass
class _Track:
    track_id: int
    box: tuple
    last_seen: float
    hits: int = 1
    confirmed: bool = False


def _valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(type(value) in (int, float) and math.isfinite(value)
                    for value in box)
            and box[2] > box[0] and box[3] > box[1])


def _iou(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0:
        return 0.0
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (first_area + second_area - intersection + 1e-9)


class NoHatTrackTracker:
    """Confirm anonymous no-hat tracks without claiming person identity."""
    def __init__(self, iou_threshold=.30, max_gap=2.0, min_hits=2):
        if (type(iou_threshold) not in (int, float)
                or not math.isfinite(iou_threshold) or not 0 < iou_threshold <= 1):
            raise ValueError('IoU threshold must be within 0..1')
        if (type(max_gap) not in (int, float)
                or not math.isfinite(max_gap) or max_gap <= 0):
            raise ValueError('track gap must be positive')
        if type(min_hits) is not int or min_hits < 1:
            raise ValueError('minimum hits must be a positive integer')
        self.iou_threshold = float(iou_threshold)
        self.max_gap = float(max_gap)
        self.min_hits = min_hits
        self._generation = None
        self._source_epoch = None
        self._tracks = {}
        self._next_id = 1

    def reset(self):
        self._generation = None
        self._source_epoch = None
        self._tracks.clear()
        self._next_id = 1

    def _reset_identity(self, generation, source_epoch):
        self.reset()
        self._generation, self._source_epoch = generation, source_epoch

    def observe(self, frame_id, captured_at, source_epoch, generation, detections):
        if type(frame_id) is not int or frame_id < 0:
            raise ValueError('frame id must be nonnegative')
        if (type(captured_at) not in (int, float)
                or not math.isfinite(captured_at) or captured_at < 0):
            raise ValueError('capture time must be finite and nonnegative')
        if not isinstance(generation, str) or not generation:
            raise ValueError('generation is required')
        if source_epoch is None or type(source_epoch) is bool:
            raise ValueError('source epoch is required')
        if (generation != self._generation or source_epoch != self._source_epoch):
            self._reset_identity(generation, source_epoch)

        for track_id, track in list(self._tracks.items()):
            if captured_at - track.last_seen > self.max_gap:
                self._tracks.pop(track_id, None)

        current = []
        for item in detections:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise ValueError('invalid detection row')
            box, wearing, score = item
            # Pipeline detections carry NumPy coordinates; persist and track
            # plain Python numbers instead of silently dropping every box.
            if not isinstance(box, (list, tuple)) and callable(getattr(box, 'tolist', None)):
                try:
                    box = box.tolist()
                except (TypeError, ValueError):
                    continue
            if wearing or not _valid_box(box):
                continue
            if type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError('invalid detection score')
            current.append((tuple(float(value) for value in box), float(score)))

        pairs = []
        for track_id, track in self._tracks.items():
            for detection_index, (box, _) in enumerate(current):
                overlap = _iou(track.box, box)
                if overlap >= self.iou_threshold:
                    pairs.append((overlap, track_id, detection_index))
        pairs.sort(key=lambda row: (-row[0], row[1], row[2]))

        matched_tracks, matched_detections = set(), set()
        detection_tracks = {}
        for _, track_id, detection_index in pairs:
            if track_id in matched_tracks or detection_index in matched_detections:
                continue
            track = self._tracks[track_id]
            track.box = current[detection_index][0]
            track.last_seen = float(captured_at)
            track.hits += 1
            matched_tracks.add(track_id)
            matched_detections.add(detection_index)
            detection_tracks[detection_index] = track_id

        for detection_index, (box, _) in enumerate(current):
            if detection_index in matched_detections:
                continue
            track_id = self._next_id
            self._next_id += 1
            self._tracks[track_id] = _Track(track_id, box, float(captured_at))
            detection_tracks[detection_index] = track_id

        triggered = []
        for track in self._tracks.values():
            if not track.confirmed and track.hits >= self.min_hits:
                track.confirmed = True
                if track.last_seen == captured_at:
                    triggered.append(track.track_id)
        if not triggered:
            return None

        visible_ids = sorted({track_id for track_id in detection_tracks.values()})
        rows = []
        for detection_index, (box, score) in enumerate(current):
            track_id = detection_tracks[detection_index]
            rows.append({'track_id': track_id, 'box': list(box),
                         'score': score, 'class': 'person'})
        return EventCandidate(
            captured_at=float(captured_at),
            captured_wall_time=datetime.now(timezone.utc).astimezone(
                SHANGHAI).isoformat(timespec='milliseconds'),
            frame_id=frame_id,
            source_epoch=source_epoch,
            generation=generation,
            trigger_track_ids=sorted(triggered),
            visible_no_hat_track_ids=visible_ids,
            detections=rows,
        )


class AlertEventStore:
    """Atomic event evidence files with a bounded persistent index."""
    def __init__(self, root, retention_days=7, max_bytes=268435456,
                 max_events=5000, free_space_floor=134217728):
        if type(retention_days) is not int or retention_days <= 0:
            raise ValueError('retention days must be a positive integer')
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('event byte limit must be positive')
        if type(max_events) is not int or max_events <= 0:
            raise ValueError('event count limit must be positive')
        if type(free_space_floor) is not int or free_space_floor < 0:
            raise ValueError('free-space floor must be nonnegative')
        self.root = Path(root).resolve()
        self.directory = (self.root / 'results' / 'helmet-events').resolve()
        try:
            self.directory.relative_to(self.root)
        except ValueError as error:
            raise ValueError('event directory must remain inside application root') from error
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.max_events = max_events
        self.free_space_floor = free_space_floor
        self._records = {}
        self._corrupt_records = 0
        self._closed = False
        self._lock = threading.RLock()
        self._reload_index()
        self.cleanup()

    def _reload_index(self):
        records, corrupt = {}, 0
        for metadata_path in self.directory.rglob('*.json'):
            try:
                if metadata_path.is_symlink() or not metadata_path.is_file():
                    corrupt += 1
                    continue
                raw = json.loads(metadata_path.read_text(encoding='utf-8'))
                if not isinstance(raw, dict):
                    raise ValueError('event metadata must be an object')
                event_id = raw.get('event_id')
                if (not isinstance(event_id, str)
                        or EVENT_ID.fullmatch(event_id) is None
                        or metadata_path.stem != event_id):
                    raise ValueError('invalid event metadata identity')
                image_name = raw.get('image')
                if (not isinstance(image_name, str) or not image_name
                        or Path(image_name).name != image_name):
                    raise ValueError('invalid event image identity')
                image_path = (metadata_path.parent / image_name).resolve()
                try:
                    image_path.relative_to(self.directory)
                except ValueError as error:
                    raise ValueError('event image escaped its root') from error
                if image_path.is_symlink() or not image_path.is_file():
                    raise ValueError('event image is unavailable')
                raw['_media_path'] = image_path
                raw['_metadata_path'] = metadata_path.resolve()
                raw['_bytes'] = image_path.stat().st_size + metadata_path.stat().st_size
                records[event_id] = raw
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                corrupt += 1
        self._records, self._corrupt_records = records, corrupt

    def _new_event_id(self):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        return stamp + '-' + secrets.token_hex(4)

    @staticmethod
    def _atomic_write(path, data):
        temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(6) + '.tmp')
        try:
            with open(temporary, 'xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _wall_time(value):
        if not isinstance(value, str):
            raise ValueError('captured wall time is required')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def publish(self, candidate, jpeg, metadata):
        if self._closed:
            raise RuntimeError('event store is closed')
        if not isinstance(candidate, EventCandidate):
            raise TypeError('event candidate is required')
        if not isinstance(jpeg, (bytes, bytearray)) or not jpeg:
            raise ValueError('event JPEG is required')
        if not isinstance(metadata, dict):
            raise ValueError('event metadata must be an object')
        jpeg = bytes(jpeg)
        wall_time = self._wall_time(candidate.captured_wall_time)
        free = shutil.disk_usage(self.root).free
        if free - len(jpeg) < self.free_space_floor:
            raise OSError('insufficient free space for event evidence')
        with self._lock:
            for _ in range(8):
                event_id = self._new_event_id()
                if EVENT_ID.fullmatch(event_id) and event_id not in self._records:
                    break
            else:
                raise RuntimeError('could not allocate a unique event id')
            event_dir = self.directory / ('%04d' % wall_time.year) / (
                '%02d' % wall_time.month) / ('%02d' % wall_time.day)
            event_dir.mkdir(parents=True, exist_ok=True)
            image_path = event_dir / (event_id + '.jpg')
            metadata_path = event_dir / (event_id + '.json')
            if image_path.exists() or metadata_path.exists():
                raise FileExistsError('event evidence path already exists')
            record = {
                'schema': 1,
                'event_id': event_id,
                'captured_wall_time': candidate.captured_wall_time,
                'captured_at': candidate.captured_at,
                'frame_id': candidate.frame_id,
                'source_epoch': candidate.source_epoch,
                'generation': candidate.generation,
                'trigger_track_ids': list(candidate.trigger_track_ids),
                'visible_no_hat_track_ids': list(candidate.visible_no_hat_track_ids),
                'count': len(candidate.visible_no_hat_track_ids),
                'detections': list(candidate.detections),
                'image': image_path.name,
                'retention_days': self.retention_days,
                'model': metadata.get('model'),
                'selection': metadata.get('selection'),
                'email': metadata.get('email', {'state': 'pending'}),
            }
            payload = (json.dumps(record, ensure_ascii=False, allow_nan=False,
                                  indent=2) + '\n').encode('utf-8')
            self._atomic_write(image_path, jpeg)
            try:
                self._atomic_write(metadata_path, payload)
            except BaseException:
                try:
                    image_path.unlink()
                except OSError:
                    pass
                raise
            record.update({
                '_media_path': image_path.resolve(),
                '_metadata_path': metadata_path.resolve(),
                '_bytes': image_path.stat().st_size + metadata_path.stat().st_size,
            })
            record['media_path'] = str(image_path.resolve())
            self._records[event_id] = record
            return dict(record)

    def _ordered(self):
        return sorted(self._records.values(),
                      key=lambda row: (str(row.get('captured_wall_time', '')),
                                       row.get('event_id', '')), reverse=True)

    @staticmethod
    def _public_record(record):
        keys = ('schema', 'event_id', 'captured_wall_time', 'captured_at',
                'frame_id', 'source_epoch', 'trigger_track_ids',
                'visible_no_hat_track_ids', 'count', 'detections', 'image',
                'retention_days', 'model', 'selection', 'email')
        return {key: record[key] for key in keys if key in record}

    def list_events(self, limit=50, before=None, day=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('event page limit must be within 1..100')
        if day is not None:
            try:
                datetime.strptime(day, '%Y-%m-%d')
            except (TypeError, ValueError) as error:
                raise ValueError('event day must use YYYY-MM-DD') from error
        with self._lock:
            ordered = self._ordered()
            if day is not None:
                ordered = [row for row in ordered
                           if str(row.get('captured_wall_time', '')).startswith(day)]
            if before is not None:
                if not isinstance(before, str) or EVENT_ID.fullmatch(before) is None:
                    raise ValueError('event cursor is invalid')
                matching = [index for index, row in enumerate(ordered)
                            if row.get('event_id') == before]
                ordered = ordered[matching[0] + 1:] if matching else []
            return [self._public_record(row) for row in ordered[:limit]]

    def media_path(self, event_id):
        if not isinstance(event_id, str) or EVENT_ID.fullmatch(event_id) is None:
            return None
        with self._lock:
            record = self._records.get(event_id)
            if record is None:
                return None
            path = Path(record['_media_path']).resolve()
            try:
                path.relative_to(self.directory)
            except ValueError:
                return None
            if path.is_symlink() or not path.is_file():
                return None
            return path

    def _delete(self, record):
        removed = True
        for path in (record['_media_path'], record['_metadata_path']):
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                removed = False
        if removed:
            self._records.pop(record['event_id'], None)
        return removed

    def cleanup(self, now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        elif not isinstance(now, datetime):
            raise ValueError('cleanup time must be a datetime')
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(days=self.retention_days)
        removed = 0
        with self._lock:
            for record in list(self._ordered()):
                try:
                    expired = self._wall_time(record.get('captured_wall_time')) < cutoff
                except ValueError:
                    expired = False
                if expired and self._delete(record):
                    removed += 1
            ordered = self._ordered()
            total_bytes = sum(int(row.get('_bytes', 0)) for row in ordered)
            while ordered and (len(ordered) > self.max_events or total_bytes > self.max_bytes):
                record = ordered.pop()
                if self._delete(record):
                    removed += 1
                    total_bytes -= int(record.get('_bytes', 0))
            return {'removed': removed, 'remaining': len(self._records),
                    'bytes': sum(int(row.get('_bytes', 0))
                                 for row in self._records.values()),
                    'corrupt_records': self._corrupt_records}

    def status(self):
        with self._lock:
            return {'records': len(self._records),
                    'bytes': sum(int(row.get('_bytes', 0))
                                 for row in self._records.values()),
                    'corrupt_records': self._corrupt_records,
                    'retention_days': self.retention_days,
                    'max_bytes': self.max_bytes,
                    'max_events': self.max_events,
                    'closed': self._closed}

    def close(self):
        self._closed = True
