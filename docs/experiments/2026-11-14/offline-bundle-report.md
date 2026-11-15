# Offline bundle task handoff and version record

Deliverables: `helmet/pc/build_offline_bundle.py`, root `requirements-board.txt` / `requirements-host.txt`, `tests/test_bundle.py`, `offline-bundle.md`, official wheel/source metadata in `dist/deps/`, immutable ZIPs and local verification evidence. No production module, handbook, raw dataset, model byte or default configuration was edited. This task used no SSH/NPU or subagents. The only network action was the authorized download from the pinned official airockchip repository.

## Versions actually built

| Artifact | Bytes | ZIP SHA256 | Payload files | Models |
| --- | --- | --- | --- | --- |
| `dist/helmet-offline-20261114.zip` | 39759444 | `ee3e446b85511b353cac101b9548c3edc53bdf0dab9c9535e66dd724b18a5c92` | 32 + manifest | 6 |
| `dist/helmet-offline-20261114-v2.zip` | 39759444 | `ee3e446b85511b353cac101b9548c3edc53bdf0dab9c9535e66dd724b18a5c92` | 32 + manifest | 6 |
| `dist/helmet-offline-20261114-final.zip` | 39764098 | `480e7b1cc2c2f37697890e80d44defbc084275610506817043e3bed46853fb2b` | 32 + manifest | 6 |

The original ZIP is preserved; v2 was built into a distinct path. At those two historical builds `helmet/output_sinks.py` was SHA256 `3b7ad99aef4170981fc8315ac90c21aae798f2678ce55d4f04a39afb4a32b664`; all payload bytes were identical, so deterministic ZIP construction correctly produced equal hashes. The coordinating owner later built the distinct final ZIP from frozen sources after bounded telemetry, model-head validation, benchmark cleanup and output validation changes. This task audited that existing final archive without changing source or generating another archive. Final output-sink SHA256 is `c2f65642d38f8994200503b242ca93c8f0af105dc5ad3b13650e9ff83c7003cf`; the historical3b7 revision is not the current final source identity.

Final archive manifest SHA256 is `96c0b592b72773b4392f3bbb664a533a5bfc0b8725e3c040cbd7d21482a29e6e`. All32payload hashes and14runtime/PC source hashes were checked against final bytes; all14 source files match the current frozen repository source. Six model identities are unchanged. Final local verify/compile/help commands all exited0 and are recorded in `offline-bundle-local-verification-final.json`. The historical original/v2 manifest remains `bfa210a69d6ebe8bcc18e8e9aad1116e24caac4385cdf65a5a8e2373f484dec3`, with full v2 records in `offline-bundle-local-verification-v2.json`. The original source-model provenance and sample-rights limits are bundled as local evidence rather than interpreted as redistribution permissions.

Final audit also checked all25repository-bound payload files against their source paths, including14code files,6models,the sample,requirements and provenance. Comparing old/v2 payload hashes against final identifies exactly five changed payloads: `src/benchmark.py`, `src/output_sinks.py`, `src/pipeline_runtime.py`, `src/telemetry.py`, `src/yolo_post.py`; six model hashes are identical. This exact comparison is recorded in final audit JSON under `all_repository_payload_comparison` and `history_comparison`, preserving both historical manifest identities. Final fresh packaging tests ran11,passed10,skipped1Windows source-symlink permission; final32-payload archive validation exited0. No ZIP/source mutation was needed for this verification.

Final source SHA256 inventory (every row matched current frozen repository bytes):

| Bundle source | SHA256 |
| --- | --- |
| `src/alarm.py` | `d3e6d68c7b17f5508f0d697e96998f556f224f9f5067169d66c35972b3a023d2` |
| `src/benchmark.py` | `15dcff515fc9c7332dd5eefd40502f38b0a02e6ab6a62e1d0ec50ce593ab4859` |
| `src/dump_dets.py` | `c21b4a1d1c4ee4e0428cb19d16b560c9c7dfdef2c34b52f62195b836fca90e3f` |
| `src/helmet_realtime.py` | `15794ebc40fdd1736d9c80d5504d17aa91e1de972bb92bb30a9eeb026309e46d` |
| `src/output_sinks.py` | `c2f65642d38f8994200503b242ca93c8f0af105dc5ad3b13650e9ff83c7003cf` |
| `src/pc/analyze_errors.py` | `863b7115d31d599eb72c5a74748852f39b116e629827a2b9091e16a5be02bc90` |
| `src/pc/convert.py` | `86d6fa0e865123995b122edec3d490ccf005467c28ca244cda0493621b718c7b` |
| `src/pc/dataset_tools.py` | `921281aa3e5bd9eb6dc441861bfb2d75c9ea6bb18969e8abf24fe5f122aade19` |
| `src/pc/eval_map.py` | `81ac033ef0dd61eaacd5c740fbc1e875aa3b43ce98ebfc63e5f59498a9bf1ec8` |
| `src/pipeline_runtime.py` | `dee30d6b71c9e8d3e697657fb309fb22e8fb47fafd366d60b555466cf56152e9` |
| `src/sources.py` | `ea89d66fdefb2bc429ca74efa0437a3c0a8aab672a856078fc15afccfe3b31c9` |
| `src/telemetry.py` | `da4522206053712484a21f80bb26d6424649a263f606f35fa16070be51a83b9e` |
| `src/validate_comparison.py` | `3b65f9e561084aa7f6199cc7cd413b6426f6ac962f39729ac0668e50350c2860` |
| `src/yolo_post.py` | `fae57bbfa1d47d5ab16ef444096fab4fe8e14701c5ec10d87b1ec518282b0505` |

## Verification

RED: `python -m unittest tests.test_bundle -v` first failed because the builder module did not exist. Implementation then passed meaningful boundaries: complete6 model round-trip, missing model/module rejected before archive creation, traversal/absolute/Windows unsafe names rejected, hash tamper rejected before destination creation, duplicate/unlisted archive members rejected, directory tamper detected, existing archive/destination refused, credentials/private.py excluded, wrong official-wheel hash rejected, ZIP symlink members rejected and byte-deterministic rebuild. Final test command ran11, passed10 and skipped1 Windows source-symlink creation permission; ZIP-symlink rejection passed.

Full regression trees passed: 132 tests in `tests/` with1 Windows source-symlink skip; 23 tests in `helmet/tests/` with1 NPU model gate skip. No hardware inference was performed by these tests.

Actual original, v2 and final packages were verified. Their extracted copies were verified with their standalone standard-library script, compiled and checked through actual CLI `--help`; all exits0. Final checks used the owner's extracted `dist/verified-offline-final/` and verified its final manifest identity. The earlier132/23 regression logs are historical software evidence from those runs, not relabeled as the final source suite; the owner runs final production checks separately. ZIP hashes are independently saved in adjacent `.sha256` files; those are integrity checks, not signatures.

## Dependency and hardware boundary

Lite2 official wheel is cp310 aarch642.5.0,574389bytes,SHA256 `57a5a1f967ff88a1f933abc15a64cf11db37fb406c00cb610f20a566ec231161`, from airockchip fixedcommit `2c723177a94fac65cbc036d722907ddbc91980cf`. METADATA requires NumPy,psutil,ruamel.yaml. Existing board values provided by coordinating owner: Python3.10.5,NumPy1.21.2,cv2 4.5.4 Buildroot without opencv-python distribution metadata,Lite2 2.5.0,psutil5.8.0,ruamel.yaml0.17.31. The checker does not demand nonexistent pip OpenCV metadata.

The ZIP does not contain Python/Buildroot NumPy/OpenCV/system codec/GUI/driver packages or all wheel dependencies. It explicitly says `offline_fresh_board_install_complete=false`. Optional ONNX conversion source is not included in the default six-model runtime package, and the newly paired quantization models are outside this runtime bundle's preserved-six scope. No full fresh-board install, camera/display/codec qualification or 30-minute acceptance is inferred.

The owner completed another actual NPU sample replay of the final32-file archive inside a temporary `CLONE_NEWNET` namespace, recorded as `net:[4026532925]` with only `lo DOWN` and unchanged parent network. This task directly audited `offline-network-isolated-final/manifest.json`, `session.json`, `frames.jsonl`, the complete resource sidecar and five snapshot JSON/images. All three commands exited0: final bundle verification, board import checks and real416 NPU replay. Runtime manifest SHA256 matches final ZIP manifest byte-for-byte and all14 sources match frozen source bytes. This is final's own actual run; historical old/v2 evidence is retained in its original paths rather than substituted for final.

Final's actual10.068886-second session contains79 captured/consumed/detected,78 outputs,0 read or detection failures,0 display and1 pending output at shutdown. JSONL has79detection/78output records with original capture time and frame ID preserved. Nine full resource-sidecar records exactly match session's resource records and have monotonic timestamps. Five snapshots (1alarm,4periodic) were saved within TTL and bind to actual detections. Full audit with manifest/session/frame/resource hashes, all14current-source comparisons, model/input hashes, final archive identity and snapshot hashes is in `offline-bundle-network-evidence-verification-final.json`. The historical10.048-second77detection/76output run remains in `offline-bundle-network-evidence-verification.json` and corresponds only to original/v2 bytes. Final proof establishes network-denied NPU sample replay on the prepared board, not fresh OS install, physical camera/display or longrun acceptance. A new final benchmark is a separate owner's experiment and is not invented or incorporated here while pending.
