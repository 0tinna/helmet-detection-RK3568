# Corresponding source model and FP16 conversion prerequisites

Acquired `models/source/helmet.onnx` from pinned `harperjuanl/helmet_yolov5_rknn_deploy` commit `38eb6541ff71c3cf7160b27a16c2d8d2742500b1`, original member `onnx_to_rknn/examples/onnx/yolov5s/helmet.onnx`. SHA256 `7ea304dbfc05a549c3f0c10890a5d20fc9bbd4d34df2680265d78316e6b770a7`. ONNX producer PyTorch1.10, input `images` `[1,3,640,640]`, three outputs `[1,3,80,80,7]`, `[1,3,40,40,7]`, `[1,3,20,20,7]`. The original upstream `test.py` confirms class order `('person','hat')`; scripts are retained as text and were not executed.

Downloaded the same commit's `helmet-640-640.rknn` as `models/source/upstream-helmet-640-640.rknn`. Its SHA256 `23dac43065664447272b8a032f7c35a00269e5b7d63e210ae10d8181a7a3bb32` **equals the existing local `models/helmet-640-640.rknn`**. This directly links the ONNX to the original deployment project's local baseline. It does not establish exact calibration settings or source hash of the five subsequently converted local variants. That remaining lineage limit must be disclosed.

Community repository license field is null and no root license file was found in the inspected tree. Model redistribution permission and original training manifest remain unavailable. Retain local downloaded source and provenance for analysis; do not infer broad republication rights or training-set independence.

The real Rockchip Toolkit2v2.3.2 release was inspected. `toolkit2-2.3.2-tree.json` and tag metadata retain the exact source. The Python3.10x86 wheel is:

`rknn-toolkit2/packages/x86_64/rknn_toolkit2-2.3.2-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`

It requires Linuxx86_64, not native Windows Python. This WindowsAMD64 host's `wsl.exe --list --verbose` exits1 stating Windows Subsystem for Linux is not installed. `docker` and `podman` are absent from PATH and the standard DockerDesktop executable path is absent. See `conversion-host-probe.json`. No already available local Linuxx86 environment was found. No WSL OS was installed, no reboot was attempted, no downloaded third-party code was executed, no SSH/NPU was used by this acquisition task.

The project converter has a concrete FP16 route on an existing authorized Linuxx86 host with Python3.10 and `rknn-toolkit2==2.3.2`:

```bash
python3 helmet/pc/convert.py models/source/helmet.onnx --size 640 640 --fp -o helmet/models/helmet_640x640_fp16.rknn
```

The `--fp` branch calls `rknn.build(do_quantization=False, dataset=None)` while preserving preprocessing, platform and output-head selection. An appropriate paired experiment would freshly build INT8 and FP16 from this **same hashed ONNX**, with same640geometry/head settings and separately declared training/calibration inputs, then evaluate both on the same fixed subsets. INT8 calibration must not use these fixed evaluation images. An ONNX CPU result or another public model is not a measured FP16 RKNN result.

FP16 conversion was **not performed**: the precise missing prerequisite is an available Linuxx86_64 execution environment with Toolkit2v2.3.2. Installing a Linux OS/rebooting is outside authorized scope. The host prerequisite evidence is a limitation, not an FP16 comparison result or passing B criterion. No unrelated-model substitution is valid.
