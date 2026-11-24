# LAN-only Event Dashboard v7-r3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking. The user explicitly requested that the primary agent perform all
> work without subagents.

**Goal:** Replace the authenticated SMTP-backed v7-r2 event page with a
zero-configuration LAN-only cyberpunk dashboard for current alert state and
traceable no-hat evidence.

**Architecture:** Keep the existing detection/tracking/store path. Remove mail
and access-key composition, make `LanEventServer` a direct read-only API and
page, expose `alarm_state` in status, and update packaging and resident progress
contracts.

**Tech Stack:** Python 3 standard library, existing unittest suite, Node-based
browser-script harness, inline HTML/CSS/vanilla JavaScript, BusyBox/Linux board.

**Spec:**
`docs/superpowers/specs/2026-11-24-lan-only-event-dashboard-v7-r3-design.md`

## Global Constraints

- Keep LAN-only `8092`; keep private/loopback client rejection for public IPv4.
- Do not change model, class order, thresholds, NMS, skip, TTL, pipeline,
  post-processing, or alarm thresholds.
- Do not expose live frame, camera, detection, Wi-Fi, resident, or quit routes.
- Preserve exact event frame identity, timestamps, image paths, retention,
  atomic writes, startup rollback, and cleanup receipts.
- Preserve every historical release, tag, experiment, manifest, and user PPT.
- Keep generated sources and credentials out of release archives.

## Review Focus

- A public source must still be rejected even without a login page.
- An empty archive must not be described as a safe worksite.
- A stale/invalid detector must display unknown rather than a false clear.
- Old `/alerts` bookmarks must reach the page without requiring a token.
- Removing mail must not make resident cleanup report success prematurely.

---

### Task 1: Direct Event Viewer Contract

**Files:**
- Modify: `tests/test_alert_viewer.py`
- Modify: `tests/test_alert_viewer_browser.py`
- Modify: `helmet/app/alert_viewer.py`

**Interfaces:**
- Consumes: `AlertEventStore.list_events()`, `media_path()`, `status()`.
- Produces: `LanEventServer(store, status_provider, host='0.0.0.0',
  port=8092, max_clients=8)`.

- [ ] Write failing tests for direct `/`, `/events`, `/api/events`,
  `/api/status`, JPEG access, and `/alerts -> /`, including no `Set-Cookie`.
- [ ] Run the viewer tests and confirm failures are caused by invitation/auth
  and missing direct routes.
- [ ] Implement the direct routes, remove invite/key/cookie code, retain the
  LAN guard, security headers, pagination, media capacity, and read-only `405`.
- [ ] Run the targeted viewer tests until green.

### Task 2: Current Alert State and Event Hub

**Files:**
- Modify: `tests/test_alert_hub.py`
- Modify: `tests/test_alert_events.py`
- Modify: `helmet/app/alert_hub.py`
- Modify: `helmet/app/alert_events.py`

**Interfaces:**
- Consumes: existing `NoHatTrackTracker`, `AlertEventStore`, and packet
  generation/freshness checks.
- Produces: `AlertHub(root, *, store, tracker, host, port, viewer,
  viewer_factory, cleanup_interval)`, status detection keys `state`, `running`,
  `valid`, `generation`, `alarm_state`, `frame_id`, and `source_epoch`.

- [ ] Write failing tests for publish-only observation, zero-config `from_root`,
  direct viewer construction, no key/mail files, alarm-state exposure, and
  public event records without `email`.
- [ ] Run those tests and confirm they fail on removed composition fields.
- [ ] Remove mailer/resolver/access-key composition and the event `email`
  field; retain lifecycle rollback and cleanup behavior.
- [ ] Run hub/event tests until green.

### Task 3: Resident and Service Progress

**Files:**
- Modify: `tests/test_app_service.py`
- Modify: `tests/test_app_resident.py`
- Modify: `helmet/app/service.py`
- Modify: `helmet/app/resident.py`

**Interfaces:**
- Consumes: `AlertHub.status()`.
- Produces: progress without `mail`, retaining `alert_viewer_alive`,
  `alert_port`, `alert_stopped`, and `cleanup_completed`.

- [ ] Add failing assertions that progress/status contains no mail object while
  cleanup remains evidence-based.
- [ ] Run the focused tests and confirm only mail-schema assertions fail.
- [ ] Remove mail from service/resident field allowlists and unavailable
  fallbacks.
- [ ] Run the focused tests until green.

### Task 4: Zero-config Packaging

**Files:**
- Create: `docs/tools/build_native_app_bundle_v7_r3.py`
- Create: `tests/test_native_app_v7_r3_release_builder.py`
- Modify: `helmet/app/install.py`
- Modify: `helmet/app/deploy.ps1`
- Modify: `.gitignore`
- Delete: `helmet/app/alert_mail.py`
- Delete: `helmet/app/mail_probe.py`
- Delete: `helmet/app/mail.example.json`
- Delete: `tests/test_alert_mail.py`

**Interfaces:**
- Consumes: the v7-r2 deterministic builder pattern.
- Produces: `20261124-v7-r3`, immutable root
  `/userdata/helmet-validation/20261124-native-app-v7-r3`, and a payload with
  no SMTP/access-key entries.

- [ ] Add the failing v7-r3 builder test and assert removed files are absent.
- [ ] Run it and confirm failure because the builder does not exist.
- [ ] Add the v7-r3 builder, update checked deployment and install lists, and
  remove obsolete modules.
- [ ] Build twice in a temporary directory and verify deterministic hashes,
  exact member inventory, and no overwrite behavior.

### Task 5: Verification and Delivery Evidence

**Files:**
- Modify: current user documentation and v7-r3 evidence reports.
- Produce: `dist/helmet-native-app-20261124-v7-r3.zip` and a sibling SHA256.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: reproducible host evidence and a clean v7-r3 release artifact.

- [ ] Run both unittest trees, Python compilation, PowerShell AST parsing, and
  `git diff --check`.
- [ ] Build the release, inspect the manifest, and confirm absence of mail,
  key, runtime state, event photos, bytecode, and user PPT files.
- [ ] Quit the board's v7-r2 instance, deploy v7-r3 under its new root, and
  verify direct page/API/media access, no key/mail files, LAN rejection, process
  reuse, and truthful cleanup.
- [ ] Record every passed command and every not-run physical/phone gate.
- [ ] Commit and tag only after host and available board evidence are recorded.
