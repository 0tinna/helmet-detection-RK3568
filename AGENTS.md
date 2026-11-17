# Repository Guidelines

## Current Scope and Evidence

This project implements RK3568 helmet detection with existing community models. The approved [graduation scope](docs/superpowers/plans/2026-11-14-graduation-scope.md) supersedes older proposal training objectives: no training, fine-tuning, pruning or distillation. Preserve original proposals and experiment records as history; do not restore training as an implied pending requirement.

The application code is frozen at the v6-r2 delivery. Use the [delivery statement](docs/项目交付说明.md), [current handbook](docs/项目蓝图与完整手册.md), [v6-r2 bugfix acceptance](docs/experiments/2026-11-17/native-app-v6-r2/report.md), [v6-r1 performance report](docs/experiments/2026-11-17/performance-baseline/report.md), and raw manifests to reconcile status. The 2026-11-15 and 2026-11-14 stage records remain historical evidence. Replay, software tests, real NPU inference, physical camera capture, display, long runs and codec/quantization comparisons have distinct acceptance gates. Do not infer physical-camera FPS, human-observed flicker or unplug TTL from the 2026-11-17 replay run. Documentation maintenance does not start new development or complete untested field gates.

## Project Structure & Module Organization

- `helmet/`: `helmet_realtime.py` CLI; `pipeline_runtime.py`, `sources.py`, `alarm.py`, `telemetry.py`, `output_sinks.py`; shared `yolo_post.py`; benchmark, checked deployment and board detection dump.
- `helmet/app/`: `launch.py`, `resident.py`, `service.py`, `wifi.py` and `qml/Main.qml` provide the native Qt application and resident lifecycle.
- `helmet/app/lan.py`: read-only `8091` status/LAN viewer using exact frame tokens and the same resident service.
- `helmet/pc/`: ONNX/RKNN conversion, `dataset_tools.py`, `eval_map.py`, `analyze_errors.py`.
- `helmet/models/`: existing variants named `helmet_<width>x<height>.rknn`; `models/` contains the original baseline, samples and source-model provenance.
- `tests/` and `helmet/tests/`: meaningful unittest regressions; `tools_ai/ref/test.py` is an upstream inference reference, not a test suite.
- `datasets/raw/`, `datasets/provenance/`, `datasets/evaluation/`: immutable raw sources, acquisition/semantic/overlap evidence and canonical fixed diagnostic subsets.
- `docs/experiments/`: raw measurements, manifests and reports; `.superpowers/sdd/` holds scoped implementation/review records.
- `docs/README.md`: current documentation index. `docs/app/`, `docs/proposal/` and `docs/deliverables/` hold maintained usage and academic materials; `docs/archive/` preserves superseded originals and their move/hash manifest. Keep raw experiments and historical plans at their original paths.
- `.runtime/` and `.venv/`: isolated host runtime/conversion infrastructure, not trained model deliverables.
- `.codebuddy/tools/`: serial fallback helpers; prefer SSH.

## Build, Test and Development Commands

No central build system or formatter is configured. Board inference needs Python 3.10, OpenCV, NumPy and RKNN-Toolkit-Lite2. Conversion uses Linux x86 with Toolkit2 2.3.2; record actual board/runtime versions and source-model hashes.

From the repository root:

- `.\helmet\deploy.ps1 -Board rk -RemoteRoot /userdata/helmet-validation/NEW_RUN`: checked copy of scripts, PC tools, tests, six models and sample; validates compile/help/model hashes. `deploy.cmd` wraps PowerShell; Linux uses `sh helmet/deploy.sh rk /userdata/helmet-validation/NEW_RUN`. Default isolation root is `/userdata/helmet-validation/deployment`.
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -v` and `... -m unittest discover -s helmet/tests -v`: run both regression trees. NPU-gated cases require `RK3568_TEST_MODEL`; report skips explicitly.
- `ssh rk "cd /userdata/helmet-validation/NEW_RUN/src && python3 helmet_realtime.py --model ../models/helmet_416x416.rknn --source ../input --conf 0.25 --no-display --duration 10"`: camera-independent replay, not physical-camera acceptance.
- `python3 helmet/dump_dets.py --model MODEL.rknn --data COCO_ROOT --out dump.json`: existing model on board, `.001` AP floor / `.25` operating confidence / `.45` NMS.
- `python3 helmet/pc/eval_map.py --data COCO_ROOT --dets dump.json --out metrics.json`: preserve all test images and reject failed/inconsistent dumps.
- `python3 helmet/pc/analyze_errors.py errors --data COCO_ROOT --dets dump.json --out ARTIFACT_DIR`: confidence-aware TP/FP/FN and area artifacts.

Use new result paths rather than overwriting prior evidence. Do not run NPU experiments concurrently; restore any governor change and retain its read-back record.

## Coding Style & Contracts

Use four-space Python indentation, snake_case modules/functions, PascalCase classes and uppercase constants. Keep shared YOLO decoding in `yolo_post.py`; document tensor layouts and preserve `('person','hat')`: model `person` means a bare head, not a full body. Keep serial helpers ASCII-compatible.

Results must remain bound to their original frame ID and capture time. TTL/source interruptions must suppress stale healthy previews, warnings, audio and saves. No detection is unknown evidence, not a guarantee that everyone wears a helmet. Report separate capture/detection/output/display rates and callback-dispatch versus sink-completion latency.

## Testing & Evaluation Guidelines

Verify changes with appropriate regressions, actual decoded replay, annotated outputs, held-out/provenance-qualified evaluation and board evidence. Record dimensions, thresholds, input/model/code hashes, runtime, latency quantiles, FPS and resources. Tests at injected engine or source boundaries are software evidence; identify what real hardware they do not exercise.

The current two 200-image sources have unknown training overlap and one confirmed shared photograph. They are diagnostic subsets, not certified independent tests. No scene IDs are available; preserve their one-group diagnostic provenance and fixed prespecified selection. Do not invent scene-independent splits, tune thresholds on them, or declare an 85% target passed. Project all-point AP50 at score floor `.001` differs from official COCO AP50:95 and standard SHWD difficult-ignore evaluation. Preserve 139 difficult hats as normal GT under the recorded policy, ignore 35 HardHat full-body persons explicitly, and retain the seven filename mappings/raw hashes.

## Commit & Pull Request Guidelines

This checkout is Git-backed and saved to `0tinna/helmet-detection-RK3568`. Use imperative commit titles and keep documentation maintenance separate from runtime changes. For documentation-only work, verify code, models, dependencies, original experiment records and release packages remain byte-identical; record archive moves and fix current links. For implementation changes, describe the concrete behavior, bottleneck, reference, validation, before/after metrics and material acceptance limits. Attach annotated images for detection changes. Final model choice requires combined quality, real source/display and stability gates, not speed alone. Preserve exact frame-token freshness, the double-Image no-blank invariant, forced terminal receipts and the distinction between replay measurements and physical camera/display acceptance.

## Experiment & Configuration Practices

Prefer established upstream approaches before custom optimization. Preserve all successful and failed raw runs; a failed dump is not empty successful inference. Keep credentials and raw-data redistribution rights separate from code. Board artifacts stay below `/userdata`, experiments below the explicit isolation root. Confirm camera devices with `v4l2-ctl --list-devices`; use serial only when SSH is unavailable. Documentation updates do not automatically complete plan or ledger checkboxes; the coordinating owner reconciles those from actual evidence.
