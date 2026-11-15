# RK3568 Native App Usability And Resident v4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the native board application show a continuous fresh preview in a true 720x1280 full-screen window, then prove a minimal resident v4 lifecycle on the RK3568 without changing inference behavior.

**Architecture:** Qt/QML owns only presentation and user input. The Python service owns inference independently. The resident manager, when used, keeps the service and one Qt instance in the same process session while a small Unix-socket client handles repeated icon launches.

**Tech Stack:** Python 3.10, Qt Quick/QML, Wayland/Weston, OpenCV, RKNNLite, BusyBox shell over SSH.

**Spec:** User-supplied task in the active conversation dated 2026-11-15.

**Completion:** 2026-11-15. Phase 1 evidence is in `docs/experiments/2026-11-15/native-app-usability-v4-phase1-1/report.md`; Phase 2 and icon migration evidence is in `docs/experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md`. The 180-second resident gate passed. The vendor `systemui` reload and human finger tap remain separately unverified, as recorded in the final report.

## Global Constraints

- Preserve model, class order, confidence 0.35, NMS 0.45, skip 0, 1-second TTL, and responsive profile.
- Preserve `generation`, `source_epoch`, `frameExpires`, and `minimumCaptureAt` checks.
- Use MJPG and `/dev/video10` for the USB camera.
- Do not rewrite the resident architecture.
- Keep the real Linux implementation in `helmet/app`; board copies are verification deployments.
- Record every board-only modification as a SHA deviation.
- A failed or unverified acceptance item remains failed or unverified.

## Review Focus

- A stale frame must disappear within the 1-second TTL after camera loss.
- A source replacement must not reveal the placeholder during normal detection.
- Full-screen geometry must cover the 720x1280 Weston output.
- Closing/hiding the window must not terminate inference.
- A second icon launch must reuse the same resident session and input generation.

---

### Task 1: Phase 0 Baseline And Capability Audit

**Files:**
- Read: `helmet/app/qml/Main.qml`
- Read: `helmet/app/service.py`
- Read: `helmet/app/resident.py`
- Read: `helmet/app/launch.py`
- Record: `docs/experiments/2026-11-15/native-app-usability-v4-phase1-1/report.md`

**Interfaces:**
- Produces: before-change SHA256 values, camera enumeration facts, and the resident control/lifecycle summary.

- [x] Confirm `_camera_sources()` filters `rkisp`, filters metadata by sysfs index, and prefixes real cameras with `实时摄像头`.
- [x] Record the four requested baseline hashes.
- [x] Summarize resident control messages, visibility behavior, identity checks, and lease checks in no more than 20 lines.

### Task 2: Continuous Preview And Full-Screen Regression Tests

**Files:**
- Modify: `tests/test_app_resident.py`
- Test: QML source contract plus shipped JavaScript function extraction

**Interfaces:**
- Consumes: current QML ids and functions.
- Produces: failing tests for no hidden-to-visible frame swap, detection-time placeholder suppression, 125 ms refresh, and full-screen window setup.

- [x] Add tests that fail against the current QML.
- [x] Run the focused tests and record the expected RED failure.

### Task 3: Phase 1 QML Fix

**Files:**
- Modify: `helmet/app/qml/Main.qml`

**Interfaces:**
- Produces: continuous preview and true full-screen presentation while preserving all freshness bindings.

- [x] Remove the pre-decode `preview.visible = false`.
- [x] Keep the placeholder hidden while detection is running.
- [x] Change the preview timer from 200 ms to 125 ms.
- [x] Import `QtQuick.Window`, apply `Screen.width` and `Screen.height`, and request `Window.FullScreen`.
- [x] Use `showFullScreen()` when a resident show intent becomes visible.
- [x] Run focused and full unit tests for GREEN.

### Task 4: Phase 1 Board Acceptance

**Files:**
- Copy to: `/userdata/helmet-validation/20261115-native-app-v2/src/app/qml/Main.qml`
- Record: `docs/experiments/2026-11-15/native-app-usability-v4-phase1-1/report.md`

**Interfaces:**
- Produces: real display evidence for continuous preview, idle placeholder, camera-loss TTL, and full-screen edge behavior.

- [x] Stop the existing v2 one-shot normally and wait for its three processes to exit.
- [x] Deploy only the changed QML to the v2 verification copy and record its board SHA.
- [x] Launch the real camera with `/dev/video10`, responsive, 0.35/0.45, skip 0, TTL 1.
- [x] Observe/capture the display and verify no placeholder flicker during running detection.
- [x] Stop detection and verify the idle prompt returns.
- [x] Unplug or invalidate the camera and verify the preview disappears within one second.
- [x] Verify full-screen coverage and that edge taps cannot reveal the vendor desktop.

### Task 5: Phase 2 Minimal Resident Deployment And Acceptance

**Files:**
- Deploy: complete `helmet/app` source under the final `/userdata/helmet-validation/20261115-native-app-v4-r2-local-2/src/app`
- Record: process identities, sessions, frame IDs, and any board deviations

**Interfaces:**
- Consumes: the accepted Phase 1 QML and existing resident/launch/service interfaces.
- Produces: evidence for icon show, hide, same-session re-show, tab switching, quit, and 180-second continuity.

- [x] Build a complete new v4 root from the local source and frozen model/replay assets.
- [x] Verify five-file `code_identity` consistency before launch.
- [x] Launch detection tab and record session, process identities, and initial frame ID.
- [x] Hide via the native app control, confirm all processes stay alive and frame ID increases for at least 30 seconds.
- [x] Show through the WiFi icon, confirm the same session and a later frame ID.
- [x] Show through the detection icon again and confirm tab switching and same-session reuse.
- [x] Quit and verify service/Qt termination, lock release, and successful restart.
- [x] Run 180 continuous seconds and verify monotonic frame progression and no duplicate process owners.

### Task 6: Icon Migration And Final Report

**Files:**
- Use: `docs/experiments/2026-11-15/switch_native_v4_icons.py`
- Record: before/after hashes and rollback copies

**Interfaces:**
- Produces: normal desktop icons for the accepted v4 root and a human-readable acceptance report.

- [x] Back up current desktop entries and app links.
- [x] Run the approved migration tool without restarting the vendor desktop automatically.
- [x] Verify icon files, wrapper arguments, config hash, and launcher behavior.
- [x] Add before/after hashes, exact commands, pass/fail/not-run evidence, and downgraded limitations to the report.
