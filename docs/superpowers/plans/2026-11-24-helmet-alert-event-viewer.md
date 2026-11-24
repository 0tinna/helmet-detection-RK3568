# Helmet Alert Event Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add track-aware no-hat evidence snapshots, SMTP alerts, and an authenticated public event viewer without changing the existing detector, Qt app, or `8091` live preview.

**Architecture:** A new AlertHub observes only already-validated detector packets. It confirms anonymous no-hat tracks with IoU, persists annotated JPEG/metadata pairs, batches SMTP notifications, and exposes an event-only `8092` HTTP site protected by a persistent access key.

**Tech Stack:** Python 3 standard library, OpenCV, existing unittest suite, Node for browser-script tests, HTML/CSS/vanilla JavaScript, SMTP over SSL/STARTTLS.

**Spec:** `docs/superpowers/specs/2026-11-24-helmet-alert-event-viewer-design.md`

## Global Constraints

- Keep `person` as bare-head/no-hat and `hat` as helmet-wearing head.
- Keep `conf=0.35`, `NMS=0.45`, `skip=0`, and result `TTL=1.0s`.
- Do not modify Pipeline decoding, `yolo_post.py`, alarm thresholds, model class order, or Qt double-buffer behavior.
- Generate events only after `DetectionController._fresh()` and JPEG encoding both succeed.
- Keep one detector instance and preserve resident, show/hide/quit, `8091`, boot and icon behavior.
- Public `8092` serves stored events only; it must never serve live frames or controls.
- Store real SMTP credentials only in board-local `app-state/mail.json`; never add them to Git or a release bundle.
- Default event retention is 7 days, 256 MiB, 5000 records, and a 128 MiB free-space floor.
- Default new-track confirmation is 2 frames, IoU 0.30, and a 2.0-second disappearance grace period.
- Public access links use a 30-day HMAC invitation and HttpOnly SameSite=Strict cookies.
- No commits, tags, releases, or board deployment occur until the user explicitly requests them.

## Review Focus

- A no-hat head shifts or briefly misses: the user expects one event, not repeated emails; Task 1 tests jitter and a short gap.
- Two no-hat heads overlap: the user expects both evidence rows without an identity claim; Task 1 tests simultaneous and overlapping boxes.
- Router forwarding is absent or the public IP changes: the user expects truthful, non-broken behavior; Tasks 2 and 3 test cached/unknown addresses and unreachable previews.
- Event storage fills or contains malformed files: the user expects bounded disk use and preserved valid evidence; Task 1 tests retention, quota and corruption.
- SMTP or the public listener fails while detection continues: the user expects no detector crash and no false success; Tasks 2 and 4 test isolated failures and close receipts.

---

### Task 1: Track-Aware Event Capture and Bounded Storage

**Files:**
- Create: `helmet/app/alert_events.py`
- Test: `tests/test_alert_events.py`

**Interfaces:**
- Produces: `NoHatTrackTracker(iou_threshold=0.30, max_gap=2.0, min_hits=2)`
- Produces: `NoHatTrackTracker.reset() -> None`
- Produces: `NoHatTrackTracker.observe(frame_id, captured_at, source_epoch, generation, detections) -> EventCandidate | None`, where `detections` is the packet list of `(box, wearing, score)`.
- Produces: `AlertEventStore(root, retention_days=7, max_bytes=268435456, max_events=5000, free_space_floor=134217728)`
- Produces: `AlertEventStore.publish(candidate, jpeg, metadata) -> dict`
- Produces: `AlertEventStore.list_events(limit=50, before=None, day=None) -> list[dict]`
- Produces: `AlertEventStore.media_path(event_id) -> Path | None`
- Produces: `AlertEventStore.cleanup(now=None) -> dict`
- Produces: `AlertEventStore.close() -> None`
- Produces: `EventCandidate` fields `captured_at`, `captured_wall_time`, `frame_id`, `source_epoch`, `generation`, `trigger_track_ids`, `visible_no_hat_track_ids`, `detections`.

- [x] **Step 1: Write failing tracker tests**

Create `tests/test_alert_events.py` with unittest cases asserting:

```python
tracker.observe(1, 10.0, 0, "run-a", [([10, 10, 50, 50], False, .9)]) is None
event = tracker.observe(2, 10.1, 0, "run-a",
                        [([11, 10, 51, 50], False, .9)])
self.assertEqual(event.trigger_track_ids, [1])
self.assertIsNone(tracker.observe(3, 10.2, 0, "run-a",
                                  [([12, 11, 52, 50], False, .9)]))
```

Add tests for simultaneous new heads, a new head entering while an existing head remains, a 1.5s missed track, a 2.1s re-entry, generation reset, and source-epoch reset.

- [x] **Step 2: Run tracker tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_events.NoHatTrackTrackerTests -v`

Expected: import failure because `helmet.app.alert_events` does not exist.

- [x] **Step 3: Implement `NoHatTrackTracker` and `EventCandidate`**

Use only `wearing=False` detections. Match detections to active tracks by descending IoU and consume each box/track at most once. A new track remains pending until `min_hits`; expired tracks are removed after `max_gap`. `observe()` returns one candidate when any pending tracks are confirmed on the current frame and includes all confirmed trigger IDs plus all currently active no-hat IDs.

- [x] **Step 4: Write failing store tests**

In `tests/test_alert_events.py`, create a temporary app root and test:

- JPEG/JSON pair is written under `results/helmet-events/YYYY/MM/DD/`.
- `list_events()` is newest-first and returns no absolute paths or private generation values.
- Invalid event IDs, traversal, symlinks and out-of-root media requests return `None`.
- Cleanup removes records older than 7 days.
- Cleanup removes oldest records until byte and count limits pass.
- Invalid JSON is skipped and counted.
- Existing files are not overwritten by an event-ID collision.

- [x] **Step 5: Run store tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_events.AlertEventStoreTests -v`

Expected: FAIL because `AlertEventStore` is not implemented.

- [x] **Step 6: Implement `AlertEventStore`**

Encode metadata with UTF-8 and `allow_nan=False`. Publish JPEG then JSON through same-directory temporary files, `flush`, `fsync`, and `os.replace`. Rebuild an in-memory newest-first index on startup. Use UTC event IDs and ISO-8601 wall times with `+08:00` for board-local display. Cleanup runs at construction and through `cleanup()`. A free-space check failure refuses publication without deleting valid evidence.

- [x] **Step 7: Run Task 1 tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_events -v`

Expected: all Task 1 tests pass.

- [x] **Step 8: Checkpoint without committing**

Run: `git diff --check -- helmet/app/alert_events.py tests/test_alert_events.py`

Expected: no output.

---

### Task 2: SMTP Notification and Dynamic Public Address

**Files:**
- Create: `helmet/app/alert_mail.py`
- Create: `helmet/app/mail.example.json`
- Test: `tests/test_alert_mail.py`

**Interfaces:**
- Consumes: event dictionaries returned by `AlertEventStore.publish()`.
- Produces: `MailSettings.load(path) -> MailSettings`
- Produces: `PublicAddressResolver(url="https://ifconfig.me/ip", timeout=3.0, cache_seconds=1800)`
- Produces: `PublicAddressResolver.resolve(now=None) -> dict` with `address`, `source`, `checked_at` and `stale`.
- Produces: `SmtpAlertMailer(settings, access_url_builder, resolver, smtp_factory=None)`
- Produces: `SmtpAlertMailer.enqueue(events) -> bool`
- Produces: `SmtpAlertMailer.status() -> dict`
- Produces: `SmtpAlertMailer.start() -> None`
- Produces: `SmtpAlertMailer.close(timeout=3.0) -> bool`

- [x] **Step 1: Write failing configuration and resolver tests**

Test valid `ssl`, `starttls`, and `none` configurations; reject unknown security, invalid email, missing host/recipient, oversized files, and non-file paths. Test resolver accepts only a single validated IP address, uses the cached address after network failure, marks cache entries over 30 minutes as stale, and never includes exception text in public status.

- [x] **Step 2: Run configuration/resolver tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_mail.MailSettingsTests tests.test_alert_mail.PublicAddressResolverTests -v`

Expected: import failure.

- [x] **Step 3: Implement settings and resolver**

Read at most 64 KiB, require a regular non-symlink file, validate exact field types and address syntax, and expose no password in `repr`, `status()` or errors. Query the public IP with `urllib.request` and validate with `ipaddress.ip_address()`. Keep the last valid result in memory.

- [x] **Step 4: Write failing SMTP tests**

Use a fake SMTP factory to assert:

- First enqueue opens one 5-second aggregation window.
- Two events in the same window produce one message with count 2 and one JPEG attachment.
- Subject includes the no-hat danger marker and recipient is exactly `230010044@mail.szpu.edu.cn`.
- The body contains event time, dynamic address, and access URL.
- Failures retry 3 times with 30s, 120s, and 600s delays.
- More than 8 pending batches drops the oldest and reports the count.
- `close()` joins the sender and leaves no worker alive.
- Password and SMTP response text never enter status.

- [x] **Step 5: Run SMTP tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_mail.SmtpAlertMailerTests -v`

Expected: FAIL because `SmtpAlertMailer` is not implemented.

- [x] **Step 6: Implement `SmtpAlertMailer`**

Use one non-daemon sender worker, condition-based wakeups, bounded queue, `email.message.EmailMessage`, and `smtplib.SMTP_SSL`/`SMTP`+STARTTLS. Read the attachment from the published event path only through `AlertEventStore`; pass attachment bytes into the queue so retention cannot remove a queued file before sending. Sanitize all public status.

- [x] **Step 7: Run Task 2 tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_mail -v`

Expected: all Task 2 tests pass.

- [x] **Step 8: Checkpoint without committing**

Run: `git diff --check -- helmet/app/alert_mail.py helmet/app/mail.example.json tests/test_alert_mail.py`

Expected: no output.

---

### Task 3: Authenticated Public Event Viewer

**Files:**
- Create: `helmet/app/alert_viewer.py`
- Create: `tests/test_alert_viewer.py`
- Create: `tests/test_alert_viewer_browser.py`

**Interfaces:**
- Consumes: `AlertEventStore.list_events()`, `media_path()`, and `cleanup()`.
- Produces: `AlertAccessKey.load_or_create(path) -> AlertAccessKey`
- Produces: `AlertAccessKey.make_invite(now=None, ttl=2592000) -> str`
- Produces: `AlertAccessKey.verify_invite(token, now=None) -> bool`
- Produces: `AlertAccessKey.cookie_value(now=None) -> str`
- Produces: `AlertAccessKey.verify_cookie(value, now=None) -> bool`
- Produces: `PublicEventServer(store, access_key, status_provider, host="0.0.0.0", port=8092, max_clients=8)`
- Produces: `PublicEventServer.start() -> None`
- Produces: `PublicEventServer.close() -> None`
- Produces: `PublicEventServer.url_path(public_host, public_port=8092, scheme="http", now=None) -> str`

- [x] **Step 1: Write failing access-key tests**

Assert:

- Key file is 32+ random bytes, mode `0600`, and never overwritten.
- Invite token rejects tampering, expiration and a different key.
- Cookie value uses constant-time HMAC verification.
- `url_path()` percent-encodes the token and uses configured host/port.

- [x] **Step 2: Run access-key tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_viewer.AlertAccessKeyTests -v`

Expected: import failure.

- [x] **Step 3: Implement `AlertAccessKey`**

Base64url-encode an HMAC over version, expiry and purpose. Compare with `hmac.compare_digest`. Create the private parent/key with restrictive umask and exclusive file creation.

- [x] **Step 4: Write failing HTTP tests**

Start a real server on `127.0.0.1`, port 0, with a temporary store and fake status provider. Assert:

- `/events`, `/api/events`, `/api/status` and media return 403 without cookies.
- `/alerts?access=...` sets HttpOnly, SameSite=Strict, Path=/ cookie and redirects to `/events`.
- `/events` and `/api/events?limit=2&before=...&day=...` return paginated, path-sanitized events.
- Media uses event IDs and never accepts traversal or arbitrary paths.
- POST/PUT/DELETE, live frame and MJPEG routes return 404/405.
- Security headers and `Cache-Control: no-store` are present.
- Concurrent clients beyond `max_clients` get 503 for media.
- Port is released after close.

- [x] **Step 5: Run HTTP tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_viewer.PublicEventServerTests -v`

Expected: FAIL because `PublicEventServer` is not implemented.

- [x] **Step 6: Implement server routes and page**

Use `ThreadingHTTPServer`. Keep all HTML/CSS/JS inline. Implement responsive cyber danger/safe cards, semantic elements, accessible focus states, reduced-motion handling, explicit image dimensions, lazy thumbnails, `content-visibility`, empty states and `Intl` date/number formatting. Use CSP without `unsafe-eval`.

- [x] **Step 7: Add failing browser-script tests**

Execute page JavaScript in Node with timer/fetch boundaries. Test pagination, date filtering, empty state, auth redirect handling, expired-public-IP warning, image error state and retry.

- [x] **Step 8: Run browser tests to verify failure, then implement page script**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_viewer_browser -v`

Expected before implementation: failing assertions. After implementing the page script: PASS.

- [x] **Step 9: Run Task 3 tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_viewer tests.test_alert_viewer_browser -v`

Expected: all Task 3 tests pass; browser tests skip only when Node is unavailable.

- [x] **Step 10: Checkpoint without committing**

Run: `git diff --check -- helmet/app/alert_viewer.py tests/test_alert_viewer.py tests/test_alert_viewer_browser.py`

Expected: no output.

---

### Task 4: AlertHub and Native Service Integration

**Files:**
- Create: `helmet/app/alert_hub.py`
- Modify: `helmet/app/service.py`
- Modify: `helmet/app/launch.py`
- Modify: `helmet/app/resident.py`
- Test: `tests/test_app_service.py`
- Test: `tests/test_app_lan.py`
- Test: `tests/test_app_resident.py`
- Test: `tests/test_alert_hub.py`

**Interfaces:**
- Consumes: `NoHatTrackTracker`, `AlertEventStore`, `SmtpAlertMailer`, `PublicAddressResolver`, `AlertAccessKey`, and `PublicEventServer`.
- Produces: `AlertHub.from_root(root, port=8092, host="0.0.0.0") -> AlertHub`
- Produces: `AlertHub.start() -> None`
- Produces: `AlertHub.reset(generation) -> None`
- Produces: `AlertHub.observe(packet, jpeg, generation) -> bool`
- Produces: `AlertHub.status() -> dict`
- Produces: `AlertHub.close(timeout=3.0) -> bool`
- Detector receives `DetectionController(..., alert_hub=None)` and calls `alert_hub.observe()` only after fresh JPEG validation.

- [x] **Step 1: Write failing AlertHub tests**

Test that a fake tracker returning a candidate causes one store publish, one mail enqueue and one status update. Test store failure, mail configuration absence and mail failure without a raised detector error. Test generation reset, close ordering and idempotent close.

- [x] **Step 2: Run AlertHub tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_alert_hub -v`

Expected: import failure.

- [x] **Step 3: Implement `AlertHub`**

Start store cleanup, mail worker, access key and public viewer in that order. `observe()` accepts only a still-fresh packet and builds the tracker boundary outside disk/network work. Publish first, enqueue mail second. Keep SMTP and viewer failures out of detector faults.

- [x] **Step 4: Write failing service integration tests**

Extend the existing controlled-pipeline tests to assert:

- `alert_hub.observe()` is called once per successfully encoded fresh packet.
- Expired, stale-epoch, stopping and failed JPEG packets never reach AlertHub.
- `--alert-port 0` disables the public server.
- A port conflict makes service startup fail rather than silently disabling alerts.
- Service close waits for AlertHub and reports `alert_viewer_alive`, `alert_port` and mail state.
- Existing `/api/start`, `/api/stop`, `8091`, WiFi and application lifecycle results are unchanged.

- [x] **Step 5: Run integration tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_app_service tests.test_app_lan tests.test_alert_hub -v
```

Expected: failures for the missing `alert_hub` argument and `alert-port` parsing.

- [x] **Step 6: Integrate AlertHub with DetectionController and LocalService**

Inject the hub before `start()`. On `DetectionController.on_packet()`, perform the existing fresh checks and JPEG encoding first, then call `observe()` with the exact packet, bytes and run UUID. Do not hold network or SMTP work under the controller lock. `LocalService.start()` starts the hub and fails truthfully on a bind error; `close()` stops detection, closes the hub, then releases local HTTP.

- [x] **Step 7: Add resident and one-shot lifecycle tests**

Assert resident service args enable `8092`, one-shot service args disable it with `--alert-port 0`, readiness includes the actual event port, progress reads `alert_viewer_alive`, and quit/exit receipts include actual cleanup. Do not relax the existing forced-terminal receipt rules.

- [x] **Step 8: Run Task 4 tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_alert_hub tests.test_app_service tests.test_app_lan tests.test_app_resident -v
```

Expected: all pass.

- [x] **Step 9: Checkpoint without committing**

Run: `git diff --check -- helmet/app/alert_hub.py helmet/app/service.py helmet/app/launch.py helmet/app/resident.py tests/test_alert_hub.py tests/test_app_service.py tests/test_app_lan.py tests/test_app_resident.py`

Expected: no output.

---

### Task 5: Versioned Bundle, Documentation and Release Evidence

**Files:**
- Create: `docs/tools/build_native_app_bundle_v7_r1.py`
- Create: `tests/test_native_app_v7_release_builder.py`
- Create: `docs/app/未戴帽告警、邮件与公网事件站-v7.md`
- Modify: `docs/README.md`
- Modify: `docs/项目交付说明.md`
- Modify: `docs/项目蓝图与完整手册.md`
- Modify: `docs/版本管理.md`

**Interfaces:**
- Consumes: all runtime files from Tasks 1-4.
- Produces: bundle version `20261124-v7-r1`, release tag `native-app-v7-r1`, and deployment root `/userdata/helmet-validation/20261124-native-app-v7-r1`.
- Produces: existing five-file `code_identity` plus `alert_feature_identity`.
- Produces: `mail.example.json`, but never `mail.json` or `public-access.key`.

- [x] **Step 1: Write failing builder tests**

Test exact version/root/wrapper paths, inclusion of all alert modules and the example config, exclusion of live secrets, manifest file hashes, five-file identity stability, and deterministic archive inspection.

- [x] **Step 2: Run builder tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_native_app_v7_release_builder -v`

Expected: file-not-found failure.

- [x] **Step 3: Implement the v7-r1 builder**

Copy the v6-r2 builder pattern with a new immutable root. Include all `helmet/app` files. Compute `alert_feature_identity` over `alert_events.py`, `alert_mail.py`, `alert_viewer.py`, `alert_hub.py`, `mail.example.json`, and integration diffs identified by their file SHA. Refuse existing output/manifest paths.

- [x] **Step 4: Write deployment and operator documentation**

Document SMTP configuration without real credentials, `8092` port forwarding, dynamic IP caveat, access-key rotation, retention settings, log/status inspection, HTTPS/VPN requirement, start/stop, and isolation-root deployment. Include a warning that `ifconfig.me` proves outbound access only.

- [x] **Step 5: Run Task 5 tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_native_app_v7_release_builder -v`

Expected: all pass.

- [x] **Step 6: Run complete host regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m unittest discover -s helmet/tests -v
```

Expected: both suites pass; NPU-gated tests may skip only with an explicit reason.

- [x] **Step 7: Build and verify the candidate

Run:

```powershell
.\.venv\Scripts\python.exe docs/tools/build_native_app_bundle_v7_r1.py
```

Expected: new ZIP and manifest are created; all internal SHA checks pass; no old release is replaced.

- [x] **Step 8: Checkpoint without committing or tagging**

Run: `git diff --check`

Expected: no output. Record changed paths and hashes for the user without creating a commit.

---

### Task 6: Board End-to-End Acceptance

**Files:**
- Create: `docs/experiments/2026-11-24/native-app-v7-r1/README.md`
- Create: `docs/experiments/2026-11-24/native-app-v7-r1/acceptance.json`
- Create: `docs/experiments/2026-11-24/native-app-v7-r1/report.md`

**Interfaces:**
- Consumes: the v7-r1 ZIP, manifest, board model and physical UVC camera.
- Produces: raw evidence for one person, new later entrants, repeat entry, unplug suppression, resident continuation, e-mail delivery and external public viewing.

**Board and operator inputs:**
- Board SSH target.
- A physical UVC camera exposed as a capture node.
- SMTP sender credentials placed only in board-local `app-state/mail.json`.
- Router port forwarding from a public TCP port to board `8092`.
- Access to `230010044@mail.szpu.edu.cn`.
- A phone with WiFi disabled for the external access test.

- [ ] **Step 1: Preflight board runtime and storage**

Record board identity, Python/OpenCV/RKNN versions, camera nodes/capabilities, free space, current governor and existing v6 tag. Do not run NPU workloads concurrently.

- [ ] **Step 2: Deploy as a fresh isolated root**

Extract the complete v7-r1 bundle to `/userdata/helmet-validation/20261124-native-app-v7-r1`, verify every manifest entry, then start the app. Do not copy individual files into an existing root.

- [ ] **Step 3: Configure private SMTP and public access**

Create `app-state/mail.json` with mode `0600`; do not export its content, SMTP response, or `public-access.key` into evidence. Confirm the configured external port and current dynamic public IP without placing the access token in logs.

- [ ] **Step 4: Run physical event scenarios**

Run the 12 board scenarios in the design spec: helmet only, one no-hat, sustained presence, later entrants, repeat entry, camera unplug, hidden app continuation, public phone access, e-mail content, 180-second run and complete exit. Capture JSON receipts plus manually observed timestamps.

- [ ] **Step 5: Verify storage and cleanup**

Create fixture-only events in a temporary store to verify 7-day, byte and count cleanup; never wait days or mutate the real board clock. Report actual event bytes and free-space change separately.

- [ ] **Step 6: Publish truthful acceptance report**

For each gate record pass/fail/not-run, command or observation, timestamp, PID/session, frame range, event IDs, image hashes, e-mail receipt time and public-view result. Keep failed and untested items explicit.

- [ ] **Step 7: Request final integration approval**

Ask the user before any Git commit, tag, release upload, icon migration or `systemui` restart.

---

## Plan Self-Review

- Spec coverage: tasks cover tracking, freshness, storage, retention, SMTP, public IP, authenticated viewer, accessibility, service lifecycle, packaging, docs, and board evidence.
- Type consistency: `NoHatTrackTracker.observe`, `AlertEventStore.publish/list_events/media_path/cleanup`, `SmtpAlertMailer.enqueue/status/start/close`, `AlertAccessKey.make_invite/verify_invite`, `PublicEventServer.start/close/url_path`, and `AlertHub.observe/status/close` are introduced before later consumers.
- Failure coverage: stale frames, duplicate tracks, overlaps, storage limits, corrupt files, SMTP failure, missing forwarding, auth failure, port conflict and partial cleanup each have an owning test.
- Proportion: each task produces one testable subsystem; exact algorithms are left to implementation where the interfaces and assertions settle behavior.
- Repository policy: no task commits, tags, deploys, restarts the vendor UI or exposes credentials without a later explicit instruction.
