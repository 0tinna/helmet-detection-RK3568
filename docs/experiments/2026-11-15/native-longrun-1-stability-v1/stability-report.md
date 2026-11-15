# Native stability analysis

This is descriptive analysis of a terminal recorded run, not an independent acceptance audit.

Run: `native-longrun-1`; manifest: `completed`; exit: `0`.

## Separate acceptance gates

| Gate | Recorded strict-verifier result |
| --- | --- |
| strict_integrity_passed | True |
| sampled_http_policy_passed | True |
| automatic_native_gui_verified | True |
| thirty_minute_app_passed | True |
| fixed_10_fps_met | True |
| rss_no_leak_gate | None |
| continuous_display_gate | None |
| physical_camera_accepted | False |
| human_display_touch_accepted | False |
| actual_wifi_switching_accepted | False |
| independent_85_accuracy_accepted | False |
| final_model_configuration_accepted | False |

## Conditions, coverage and original counts

Requested duration: 1800 seconds; observed active: 1800.05 seconds.
Windows: full active, after the first 60 seconds, and requested last 300 seconds (truncated for a shorter run).
Model input: [416, 416]; decoded source: [640, 480]; source kind: VideoSource.
Replay pacer: 20 FPS; file header: 10 FPS; fixed public target: 10 FPS.
Operating thresholds: {'conf': 0.35, 'nms': 0.45, 'skip': 0}; result TTL: 1 seconds.

| Stream | Raw row count |
| --- | ---: |
| frame_rows | 39611 |
| detection_rows | 19806 |
| output_rows | 19805 |
| resource_rows | 1732 |
| pipeline_resource_rows | 1746 |
| event_rows | 64 |
| http_rows | 1087 |
| gui_observation_rows | 3604 |

Original counters: `{"active_skip": 0, "captured": 34496, "consumed": 19806, "detected": 19806, "detection_failure": 0, "displayed": 0, "output": 19805, "output_overwritten": 0, "overwritten": 14689, "pending_capture": 1, "pending_output": 1, "read_failure": 0}`.
Actual engine calls: 19806; source reads: 34497; source read failures: 0.
Strict-verifier observed rates: `{"captured_fps": 19.163913224081043, "detected_fps": 11.00302833128911, "displayed_fps": 0.0, "output_fps": 11.002472791133032}`.
The displayed_fps counter belongs to the OpenCV display path; actual Qt display FPS is not measured. Model/input/code/runtime hashes, versions, governor read-backs and direct cleanup receipts are in stability-results.json.

## RSS by actual process and window

| Window/process | Samples | Start MiB | End MiB | Peak MiB | Growth MiB | OLS MiB/min |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| active/python | 1728 | 124.215 | 137.73 | 140.02 | 13.5156 | 0.135554 |
| active/qt | 1728 | 159.316 | 166.055 | 183.043 | 6.73828 | 0.448865 |
| after_warmup/python | 1670 | 134.324 | 137.73 | 140.02 | 3.40625 | 0.130122 |
| after_warmup/qt | 1670 | 167.605 | 166.055 | 183.043 | -1.55078 | 0.455617 |
| tail/python | 288 | 137.957 | 137.73 | 140.02 | -0.226562 | -0.00261081 |
| tail/qt | 288 | 178.344 | 166.055 | 183.043 | -12.2891 | -0.09792 |

RSS slopes are unweighted OLS against actual monotonic sample times, with no leak pass/fail threshold.

| Window/process | Mean MiB | p50 MiB | p95 MiB | p99 MiB |
| --- | ---: | ---: | ---: | ---: |
| active/python | 136.541 | 136.426 | 138.471 | 138.672 |
| active/qt | 172.718 | 169.613 | 178.68 | 178.68 |
| after_warmup/python | 136.635 | 136.527 | 138.475 | 138.673 |
| after_warmup/qt | 172.912 | 169.613 | 178.68 | 178.68 |
| tail/python | 137.932 | 137.836 | 138.668 | 138.822 |
| tail/qt | 178.396 | 178.672 | 178.68 | 179.215 |

## Exact timing distributions

| Window/metric | Count | Mean ms | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| all_logged/prep_ms | 19806 | 15.1128 | 14.3413 | 23.7737 | 28.7147 |
| all_logged/infer_ms | 19806 | 47.7617 | 46.8319 | 64.407 | 72.4229 |
| all_logged/post_ms | 19806 | 24.7696 | 19.4694 | 50.79 | 67.5192 |
| all_logged/capture_to_output_ms | 19805 | 137.024 | 133.463 | 191.801 | 254.998 |
| all_logged/sink_ms | 19805 | 44.2283 | 40.5967 | 75.0997 | 90.1982 |
| all_logged/capture_to_sink_complete_ms | 19805 | 181.465 | 176.604 | 244.683 | 323.664 |
| active/prep_ms | 19806 | 15.1128 | 14.3413 | 23.7737 | 28.7147 |
| active/infer_ms | 19806 | 47.7617 | 46.8319 | 64.407 | 72.4229 |
| active/post_ms | 19806 | 24.7696 | 19.4694 | 50.79 | 67.5192 |
| active/capture_to_output_ms | 19805 | 137.024 | 133.463 | 191.801 | 254.998 |
| active/sink_ms | 19805 | 44.2283 | 40.5967 | 75.0997 | 90.1982 |
| active/capture_to_sink_complete_ms | 19805 | 181.465 | 176.604 | 244.683 | 323.664 |
| after_warmup/prep_ms | 19147 | 15.1149 | 14.3387 | 23.7829 | 28.7203 |
| after_warmup/infer_ms | 19147 | 47.7669 | 46.8618 | 64.3921 | 72.3591 |
| after_warmup/post_ms | 19147 | 24.7538 | 19.4599 | 50.7656 | 67.6859 |
| after_warmup/capture_to_output_ms | 19147 | 136.997 | 133.462 | 191.8 | 255.87 |
| after_warmup/sink_ms | 19147 | 44.2606 | 40.6118 | 75.1591 | 90.2166 |
| after_warmup/capture_to_sink_complete_ms | 19147 | 181.469 | 176.579 | 244.699 | 323.687 |
| tail/prep_ms | 3306 | 15.1633 | 14.3442 | 24.1681 | 29.3787 |
| tail/infer_ms | 3306 | 47.6866 | 46.7539 | 64.0451 | 71.2126 |
| tail/post_ms | 3306 | 24.7622 | 19.1283 | 51.6327 | 66.6786 |
| tail/capture_to_output_ms | 3306 | 136.708 | 133.521 | 192.078 | 261.984 |
| tail/sink_ms | 3306 | 44.4635 | 40.9207 | 75.0861 | 90.5556 |
| tail/capture_to_sink_complete_ms | 3306 | 181.38 | 176.99 | 243.454 | 325.128 |

Quantiles use linear interpolation at (n-1)*q; JSON preserves full numerical precision.

## Sampled GUI availability

- all_samples: 3443/3604 ready/visible inline JPEG samples; ratio 0.955327.
- control_phase_running: 3443/3601 ready/visible inline JPEG samples; ratio 0.956123.

## Interpretation limits

- RSS is reported separately for Python and Qt. Shared mappings can appear in both; no sum is unique physical memory.
- RSS growth and ordinary least-squares slopes describe this run; zero or positive slopes do not prove absence or presence of a leak.
- GUI ratios count observations, not rendered frames. They do not establish continuous visibility or actual display FPS.
- Callback dispatch and sink completion exclude later HTTP transfer, Qt image decoding and physical presentation.
- Replay supply 20 FPS and public detection/output target 10 FPS are distinct. File-header FPS and camera capture are separate.
- Historical pure-Pipeline, old-service and native runs differ in pacing, GUI/JPEG, logging, snapshots, code and thermal conditions; changes are not a causal pure speedup.
- Physical camera, human viewing/touch, WiFi switching/authentication/DHCP, independent 85% accuracy and final model/configuration acceptance remain separate.
- This script implements analysis and does not independently audit its own conclusions. A separate reviewer must check raw evidence and formulae.
