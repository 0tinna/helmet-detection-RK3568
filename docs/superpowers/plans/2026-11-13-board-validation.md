# RK3568 Board Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Restore SSH validation, fix three reproduced correctness problems, and measure existing models on the board.

**Architecture:** Keep existing Python/RKNN interfaces and model defaults. Upload validation copies to `/userdata/helmet-validation/20261113`, run built-in unittest regressions and the existing benchmark, and retain original board files.

**Tech Stack:** Windows OpenSSH, board Python 3.10, unittest, OpenCV, NumPy, RKNNLite.

**Spec:** `docs/performance/2026-11-13-review.md`; user authorization to continue optimization and resolve board access.

## Global Constraints

- Keep class order `('person', 'hat')`; do not change model decoding or thresholds in this correctness pass.
- Keep all evaluation images in the precision/recall denominator; missing detections become empty lists.
- Respect positive `--duration` even when no frames arrive or display is enabled.
- Run all board writes within the validation directory; restore the original NPU governor after benchmarking.
- No camera or held-out dataset is currently available; sample throughput cannot establish production accuracy or video QoS.

## Review Focus

- CLI help must work before any NPU initialization.
- Missing detection entries must count as missed objects.
- Empty detection files must retain the whole evaluation dataset.
- An input yielding no usable frame must still honor the deadline.
- A failed model must not be reported as a successful comparison; verify six successful result entries.

## Tasks

### Task 1: CLI startup and deadline

**Files:** Modify `helmet/helmet_realtime.py`; create `helmet/tests/test_regressions.py`.

- [x] Write `test_realtime_help_starts` to run the real CLI and assert exit status 0.
- [x] Run the test on the board and observe the existing SyntaxError.
- [x] Replace the illegal with/else branch with an explicit empty-frame sentinel; wait outside the lock.
- [x] Run the test and observe success.
- [x] Write `test_duration_exits_without_frames` using a temporary unreadable JPG, the real original RKNN model, a short duration, and a five-second subprocess deadline.
- [x] Observe timeout, move duration checking to the top of the main loop, and verify bounded exit.

### Task 2: Evaluation denominator

**Files:** Modify `helmet/pc/eval_map.py`; extend `helmet/tests/test_regressions.py`.

- [x] Write CLI tests with two hat-positive images, one perfect detection, and one missing entry: expect two evaluated images, recall 0.5, AP50 0.5, and an empty saved detection for the missing image.
- [x] Add an empty-detection-file case: expect two evaluated images and recall 0.
- [x] Observe the existing incorrect image counts and recall.
- [x] Initialize detection lists for every test image, retain all GT, and rerun the complete unittest suite.

### Task 3: Board comparison

**Files:** Create a run-specific report and JSON results under `docs/performance/`; keep uploaded model hashes and configuration.

- [x] Upload all five converted models; compare board SHA256 with local files.
- [x] Benchmark the original model and five converted models at confidence 0.25, warmup 5, loops 50, on the same sample.
- [x] Restore the original governor in a shell exit trap; check all six entries and SSH exit status.
- [x] Run a fixed-duration headless session and save actual output; distinguish pipeline smoke verification from long-run camera QoS.
- [x] Review changed code independently, rerun required checks, and report measured results and remaining limitations.

## Execution Record

- SSH access recovered after the execution environment changed from restricted to unrestricted.
- Original board contains `/userdata/ai` and only the baseline model; no active helmet process was found.
- Uploaded original local realtime script reproduces SyntaxError at line 320 on board Python.
- This checkout has no Git metadata; worktree and commit steps do not apply.
- Completed all three tasks: seven CLI regressions pass on board without skips; two six-model comparison runs succeed; a 10-second real NPU smoke session exits normally.
- Ruling: add a standalone fixed-matrix result validator instead of changing benchmark behavior — independent review identified silent partial success; CLI regression tests cover this gate.
- Original NPU governor was restored and read back as `rknpu_ondemand`; original board experiment files were retained. Raw logs, result JSON, hashes, and the review are archived under `docs/performance/`.
