# Native App Performance Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce native preview CPU and restore smooth display frame rate without changing detection accuracy or freshness contracts.

**Architecture:** Publish immutable JPEG packages with exact frame tokens, send only small metadata to QML, and load binary images through two alternating QML Image layers. Coalesce resident and service state writes so storage flushing no longer competes with inference and display.

**Tech Stack:** Python 3.10, Qt 5 QML/Wayland, OpenCV JPEG, loopback HTTP.

**Spec:** `docs/superpowers/specs/2026-11-17-performance-optimization-design.md`

## Global Constraints

- Preserve `person`/`hat` semantics, conf `0.35`, NMS `0.45`, skip `0`, TTL `1.0s`.
- Do not modify models, `pipeline_runtime.py`, `yolo_post.py`, alarm thresholds or source enumeration.
- Preserve generation, source epoch, frame expiry, minimum capture and running/valid checks.
- Keep the old base64 preview endpoint for compatibility while removing it from the native App hot path.
- Run all implementation work in this session; no subagents.

## Review Focus

- Token for a stale generation must never return a newer JPEG.
- Evicted token must fail rather than silently return latest.
- Camera removal must clear all cached preview tokens within TTL.
- Supervisor record coalescing must not delay a show/hide/quit state transition.
- Double Image must keep the previous valid layer visible while the next JPEG decodes.

### Task 1: Exact Tokenized Preview Packages

**Files:**
- Modify: `helmet/app/service.py`
- Test: `tests/test_app_service.py`
- Test: `tests/test_app_lan.py`

**Interfaces:**
- Produces: `DetectionController.preview_package(after_capture=None) -> dict|None` with `frame_token`.
- Produces: `DetectionController.preview_package(frame_token=None, after_capture=None)` exact lookup.
- Produces: private `/api/preview-meta`; exact `/api/frame?frame_token=...` and `/frame.jpg?frame_token=...`.

- [x] **Step 1: Write failing exact-token tests**
- [x] **Step 2: Run focused tests and confirm missing-token behavior fails**
- [x] **Step 3: Implement four-entry immutable preview ring and token lookup**
- [x] **Step 4: Run service and LAN focused tests**

### Task 2: Direct Binary QML Preview

**Files:**
- Modify: `helmet/app/qml/Main.qml`
- Test: `tests/test_native_app_usability.py`

**Interfaces:**
- Consumes: `/api/preview-meta` JSON and `/api/frame?frame_token=...`.
- Produces: QML pending token identity while retaining existing pending generation/epoch/expiry fields.

- [x] **Step 1: Write failing QML-source and JavaScript boundary tests**
- [x] **Step 2: Run focused tests and confirm base64 path still fails the new contract**
- [x] **Step 3: Replace JSON base64 source assignment with metadata request plus exact image URL**
- [x] **Step 4: Run native presentation tests and all QML contract tests**

### Task 3: LAN Binary Preview

**Files:**
- Modify: `helmet/app/lan.py`
- Test: `tests/test_app_lan.py`
- Test: `tests/test_app_lan_browser.py`

**Interfaces:**
- Consumes: latest `frame_token` in `snapshot()` metadata.
- Produces: browser image URL `/frame.jpg?frame_token=...` with a new-image decode gate.

- [x] **Step 1: Write failing browser-source tests for token loading and no base64 hot path**
- [x] **Step 2: Run focused tests and confirm current page fails**
- [x] **Step 3: Update page polling and image swap logic**
- [x] **Step 4: Run LAN and browser tests**

### Task 4: Coalesced Persistent State Writes

**Files:**
- Modify: `helmet/app/resident.py`
- Modify: `helmet/app/service.py`
- Test: `tests/test_app_resident.py`
- Test: `tests/test_app_service.py`

**Interfaces:**
- Produces: manager owner-record publication on state change or 5-second heartbeat.
- Produces: service lifecycle receipt interval of 1 second outside terminal cleanup.

- [x] **Step 1: Write failing tests that count unchanged record publications and receipt writes**
- [x] **Step 2: Run focused tests and confirm excessive writes fail the contract**
- [x] **Step 3: Implement change detection, heartbeat and slower idle receipts**
- [x] **Step 4: Run resident/service focused tests**

### Task 5: Board Performance and Safety Acceptance

**Files:**
- Create: `docs/experiments/2026-11-17/performance-baseline/report.md`
- Create: `docs/experiments/2026-11-17/performance-baseline/before-after.json`

- [x] **Step 1: Run the full host regression suites**
- [x] **Step 2: Deploy a fresh complete root; do not patch an old root file by file**
- [x] **Step 3: Run identical hidden/visible replay measurements and compare before/after**
- [ ] **Step 4: Run physical camera 180-second, screen continuity, TTL unplug and resident regressions**
- [x] **Step 5: Record every failed or unverified item and complete Git version tagging**

> **Final status:** Tasks 1–4 and release Steps 1–3/5 completed for v6-r1. Step 4 remains intentionally unchecked because the 2026-11-17 run used `1.jpg` replay without the physical USB camera or human display observation.
