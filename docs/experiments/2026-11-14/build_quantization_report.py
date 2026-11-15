"""Audit immutable paired measurements, recompute AP, and render report figures.

This script does not infer, convert, contact the board, or modify raw evidence.
"""
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
RUN = BASE / "quantization-pair-1"
BUILD = BASE / "quantization/build"
OUT = BASE / "quantization-figures"
OUT.mkdir(exist_ok=True)
AUDIT = {"checks": [], "limitations": [], "metrics": {}, "snapshots": {}}
SOURCES = ("shwd", "hardhat")
PRECISIONS = ("fp16", "int8")
CLASSES = ("person", "hat")
MEASURES = ("ap50", "precision", "recall", "f1")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check(name, value):
    AUDIT["checks"].append({"name": name, "pass": bool(value)})
    if not value:
        raise AssertionError(name)


def close_tree(a, b, label):
    if isinstance(a, dict):
        check(label + ":keys", set(a) == set(b))
        for key in a:
            close_tree(a[key], b[key], label + "." + key)
    elif isinstance(a, list):
        check(label, a == b)
    elif isinstance(a, (float, int)) and not isinstance(a, bool):
        check(label, isinstance(b, (int, float)) and math.isclose(a, b, abs_tol=1e-12, rel_tol=1e-12))
    else:
        check(label, a == b)


def dhash(image):
    pixels = np.asarray(image.convert("L").resize((9, 8)))
    return sum(int(v) << i for i, v in enumerate((pixels[:, 1:] > pixels[:, :-1]).ravel()))


build = load(BUILD / "paired-build-manifest.json")
run = load(RUN / "run-manifest.json")
check("build and board run complete", build["status"] == run["status"] == "complete")
check("hashed source ONNX", sha(ROOT / build["source_onnx"]) == build["source_onnx_sha256"])
for key in ("source_sha256", "config", "load_config", "head_layout", "expected_toolkit"):
    check("fresh pair equal " + key, build["builds"]["fp16"][key] == build["builds"]["int8"][key])
check("three matching Conv heads", build["output_conv_heads"] == ["350", "364", "378"])
check("same 416x416 and normalization", build["input_size"] == [1, 3, 416, 416] and build["normalization"] == {"mean": [0, 0, 0], "std": [255, 255, 255]})
check("toolkit wheel identity", sha(ROOT / ".runtime/linux-conversion/rknn_toolkit2-2.3.2-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl") == build["toolkit_wheel_sha256"])
check("conversion code snapshot", sha(BUILD / "convert_pair.py") == build["conversion_script_sha256"])
for precision in PRECISIONS:
    record = build["builds"][precision]
    model = ROOT / "helmet/models/quantization" / record["output"]
    check(precision + " actual model size/hash", model.stat().st_size == record["output_bytes"] and sha(model) == record["output_sha256"])
    check(precision + " board model matches built model", run["models"][precision]["sha256"] == record["output_sha256"] and run["models"][precision]["bytes"] == record["output_bytes"])
    check(precision + " successful build", record["success"] is True and record["expected_toolkit"] == "2.3.2")
check("precision branches", build["builds"]["fp16"]["do_quantization"] is False and build["builds"]["fp16"]["dataset"] is None and build["builds"]["int8"]["do_quantization"] is True)
check("original governor restored with readback", run["original_governor"] == run["restored_governor"] == "rknpu_ondemand")
for name, digest in run["sources"].items():
    path = RUN / "runtime-snapshot" / name
    check("runtime snapshot " + name, sha(path) == digest)
    AUDIT["snapshots"][name] = {"recorded_sha256": digest, "snapshot_matches": True, "current_sha256": sha(ROOT / "helmet" / name)}

calib = load(BUILD / "calibration-manifest.json")
calib_base = ROOT / ".runtime/linux-conversion"
check("calibration manifest identity", sha(BUILD / "calibration-manifest.json") == build["calibration"]["manifest_sha256"])
check("calibration fixed100, no exclusions", calib["retained_count"] == len(calib["files"]) == 100 and not calib["excluded"] and not calib["visual_candidates"])
train_path = calib_base / "calibration/original-train.txt"
check("original training-side split hash", sha(train_path) == build["calibration"]["source_split_sha256"])
check("source-defined prefix", [r["id"] for r in calib["files"]] == train_path.read_text().split()[:100])
check("calibration list hash", sha(calib_base / "calibration/dataset.txt") == build["calibration"]["dataset_file_sha256"])
ordered = [{"id": r["id"], "source_member": r["source_zip_member"], "original_sha256": r["original_sha256"], "prepared_sha256": r["prepared_sha256"]} for r in calib["files"]]
check("ordered calibration hash", hashlib.sha256(json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == build["calibration"]["ordered_file_identity_sha256"])
test_ids = set(load(ROOT / "datasets/provenance/shwd-selection.json")["ids"])
check("train calibration IDs exclude fixed SHWD test IDs", not test_ids.intersection(r["id"] for r in calib["files"]))
evaluation_identity = []
for source in SOURCES:
    dataset_root = ROOT / "datasets/evaluation" / source
    manifest = load(dataset_root / "manifest.json")
    annotation_document = load(dataset_root / "_annotations.coco.json")
    check(source + " preserved one unknown-scene group", len({r["group"] for r in manifest["records"]}) == 1)
    if source == "shwd":
        check("SHWD139difficult normalGT preserved", sum(bool(a["source_difficult"]) for a in annotation_document["annotations"]) == 139)
        check("seven original filename resolutions retained", sum(r["filename_resolution"] != "exact" for r in manifest["records"]) == 7)
    for record in manifest["records"]:
        path = dataset_root / record["file_name"]
        image = Image.open(path).convert("RGB")
        check(source + "/" + record["file_name"] + " original image hash", sha(path) == record["image_sha256"] == record["source_image_sha256"])
        evaluation_identity.append({"sha256": sha(path), "pixel_sha256": hashlib.sha256(image.tobytes()).hexdigest(), "dimensions": image.size, "dhash": dhash(image)})
duplicates, candidates = [], []
for row in calib["files"]:
    raw = calib_base / "calibration/raw" / Path(row["source_zip_member"]).name
    prepared = calib_base / row["prepared"]
    check("calibration image identity " + row["id"], sha(raw) == row["original_sha256"] and sha(prepared) == row["prepared_sha256"])
    image = Image.open(raw).convert("RGB")
    pixel = hashlib.sha256(image.tobytes()).hexdigest()
    if any(r["sha256"] == row["original_sha256"] or (r["dimensions"] == image.size and r["pixel_sha256"] == pixel) for r in evaluation_identity):
        duplicates.append(row["id"])
    code = dhash(image)
    if any((code ^ r["dhash"]).bit_count() <= 6 for r in evaluation_identity):
        candidates.append(row["id"])
check("recomputed calibration exact/pixel overlap zero", not duplicates)
check("recomputed calibration dHash<=6 candidates zero", not candidates)
AUDIT["calibration_overlap"] = {"calibration_images": 100, "evaluation_images": len(evaluation_identity), "exact_or_decoded_pixel_duplicates": duplicates, "dhash6_candidates": candidates, "limitation": "Input-only limited-subset scan does not certify full-source or training independence"}

benchmarks = {}
for precision in PRECISIONS:
    benchmark = load(RUN / ("bench-" + precision + ".json"))
    check(precision + " one successful100-loop benchmark", len(benchmark) == 1 and benchmark[0]["loops"] == 100 and benchmark[0]["size"] == "416x416")
    benchmarks[precision] = benchmark[0]
    text = (RUN / ("bench-" + precision + ".log")).read_text()
    check(precision + " benchmark recorded same input/10warmups", "input/1.jpg (1280x960)  Loops: 100  Warmup: 10" in text)
    check(precision + " board runtime versions", "librknnrt version: 2.3.2" in text and "Driver Information, version: 0.9.2" in text and "rknn-toolkit-lite2 version: 2.5.0" in text and "toolkit version: 2.3.2" in text)
    check(precision + " performance governor log", "[NPU governor -> performance]" in text)
AUDIT["benchmarks"] = benchmarks
AUDIT["benchmark_sample_host_sha256"] = sha(ROOT / "models/1.jpg")
AUDIT["benchmark_identity_limit"] = "Both logs record identicalboardpath1.jpgand1280x960. RunmanifestdoesnotstorebenchmarkinputSHA; hosthashalonecannotproveruntimebytes. Post-runboardidentitysidecarifprovidedislaterreadback."
post_run = load(RUN / "post-run-input-audit.json")
check("post-run benchmark input readback matches host sample", post_run["sha256"] == AUDIT["benchmark_sample_host_sha256"] and post_run["bytes"] == (ROOT / "models/1.jpg").stat().st_size)
check("post-run input audit labels timing limitation", "post-run" in post_run["scope"] and "not contemporaneous" in post_run["scope"])
AUDIT["benchmark_post_run_identity"] = post_run

for source in SOURCES:
    dumps = {}
    for precision in PRECISIONS:
        stem = source + "-" + precision
        dump_path = RUN / (stem + ".dump.json")
        metric_path = RUN / (stem + ".metrics.json")
        listed = next(r for r in run["runs"] if r["precision"] == precision and r["dataset"] == source)
        check(stem + " runartifact hashes", listed["dump_sha256"] == sha(dump_path) and listed["metrics_sha256"] == sha(metric_path))
        dump, metric = load(dump_path), load(metric_path)
        check(stem + " complete200images", dump["status"] == "complete" and dump["counts"]["requested"] == dump["counts"]["completed"] == len(dump["inputs"]) == metric["images"] == 200 and dump["counts"]["errors"] == 0 and all(r["status"] == "ok" for r in dump["inputs"]))
        check(stem + " dump model hash", dump["model"]["sha256"] == build["builds"][precision]["output_sha256"])
        check(stem + " dump decoderhash", dump["config"]["postprocess_sha256"] == run["sources"]["yolo_post.py"])
        check(stem + " fixed scoring configuration", metric["config"] == {"op_conf": .25, "iou": .5, "conf_min": .001, "nms_iou": .45, "limit": 0})
        check(stem + " correct diagnostic metric label", metric["metric_definition"]["metric_version"] == "helmet-all-point-ap50-v1" and metric["accuracy_target_pass"] is None)
        for evaluator_label, evaluator in (("current", ROOT / "helmet/pc/eval_map.py"), ("recorded", RUN / "runtime-snapshot/pc/eval_map.py")):
            target = OUT / (stem + "." + evaluator_label + ".metrics.json")
            command = [sys.executable, str(evaluator), "--data", str(ROOT / "datasets/evaluation" / source), "--dets", str(dump_path), "--out", str(target)]
            completed = subprocess.run(command, capture_output=True, text=True)
            check(stem + " " + evaluator_label + " evaluator exits0", completed.returncode == 0)
            (OUT / (stem + "." + evaluator_label + ".log")).write_text(completed.stdout + completed.stderr)
            recomputed = load(target)
            for key in (*CLASSES, "mAP50", "areas", "config", "ap_coverage", "images"):
                close_tree(metric[key], recomputed[key], stem + "." + evaluator_label + "." + key)
        dumps[precision] = dump
        AUDIT["metrics"][stem] = {"mAP50": metric["mAP50"], **{cls: metric[cls] for cls in CLASSES}, "dump_sha256": sha(dump_path), "metrics_sha256": sha(metric_path), "recomputed_current_and_recorded": True, "discarded_clipped_count": dump["counts"]["discarded_clipped_count"]}
    identity = lambda d: sorted((r["id"], r["file_name"], r["sha256"], tuple(r["dimensions"])) for r in d["inputs"])
    check(source + " exact paired200input identity", identity(dumps["fp16"]) == identity(dumps["int8"]))
    check(source + " matching full dump config and GT identity", dumps["fp16"]["config"] == dumps["int8"]["config"] and dumps["fp16"]["dataset"] == dumps["int8"]["dataset"] and dumps["fp16"]["runtime"] == dumps["int8"]["runtime"])

AUDIT["limitations"] = [
    "Unknowncommunitytrainingoverlap; twoprespecified200image diagnostic distributions shareoneknownphotoandhavenosceneIDs. No85%accuracytargetpass.",
    "Projectall-pointAP50IoU.5,confidencefloor.001; notofficialCOCOAP50:95orstandardSHWDdifficultignore.139difficulthatsnormalGT;35HardHatfullpersonsignoredbeforecanonicalGT.",
    "Two sequential single-image warmed benchmarks:100loops/10warmups perphase, FP16firstthenINT8; temperatures51.9/53.8C. Nofixedtemperature,CPUclocklock,randomizedorder,repeatedsessions,per-looptailrecordsorconfidenceintervals.",
    "Performance governorwrites/logpresent; originalgovernorrestorationreadbackverified. No intermediateperformance/frequencyreadbackstored in thisrun.",
    "Infermeans measureboardNPUAPIcalls. E2Emeans includeletterbox,colorconversion,inference,postprocess,excludecapture/display/audio/encode. NoCPUquantizationorphysicalcameraqualityclaim.",
    "Historicaldecoder/evaluatorhashsnapshotsmatch. Currentcomments/provenancechanged; bothcurrentandrecordedevaluatorsreproduceclass/areaAPandoperatingcountswithin1e-12."
]
AUDIT["status"] = "verified"
AUDIT["report_generator_sha256"] = sha(__file__)
(OUT / "quantization-verification.json").write_text(json.dumps(AUDIT, indent=2), encoding="utf8")

plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
COLORS = ("#365d8d", "#c87526")
labels = ("Bare head (person)", "Helmet (hat)")
fig, axes = plt.subplots(2, 4, figsize=(16, 7), sharey=True)
for i, source in enumerate(SOURCES):
    for j, measure in enumerate(MEASURES):
        ax = axes[i, j]
        for k, precision in enumerate(PRECISIONS):
            values = [AUDIT["metrics"][source + "-" + precision][cls][measure] * 100 for cls in CLASSES]
            bars = ax.bar(np.arange(2) + (k - .5) * .32, values, width=.32, label=precision.upper(), color=COLORS[k])
            ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=9)
        ax.set_xticks([0, 1], ["Bare head", "Helmet"])
        ax.set_ylim(0, 105)
        ax.set_title(source.upper() + " | " + ("AP50" if measure == "ap50" else measure.capitalize()))
        ax.grid(axis="y", alpha=.2)
        ax.set_axisbelow(True)
        if j == 0:
            ax.set_ylabel("Percent")
axes[0, 0].legend(loc="lower left")
fig.suptitle("Fresh same-source 416x416 pair | fixed 200-image diagnostic subsets", fontsize=15)
fig.text(.5, .015, "AP50: project all-point, score >=.001; P/R/F1: confidence >=.25; NMS IoU .45. Training overlap unknown.", ha="center", fontsize=11)
fig.tight_layout(rect=(0, .045, 1, .95))
for extension in ("png", "pdf"):
    fig.savefig(OUT / ("class-quality." + extension), dpi=170)
plt.close(fig)

fig, axes = plt.subplots(1, 2, figsize=(12, 5))
for i, source in enumerate(SOURCES):
    ax = axes[i]
    for k, cls in enumerate(CLASSES):
        values = [(AUDIT["metrics"][source + "-int8"][cls][measure] - AUDIT["metrics"][source + "-fp16"][cls][measure]) * 100 for measure in MEASURES]
        bars = ax.bar(np.arange(4) + (k - .5) * .32, values, width=.32, label=labels[k], color=COLORS[k])
        ax.bar_label(bars, labels=[f"{v:+.2f}" for v in values], padding=3, fontsize=9)
    ax.axhline(0, color="black", linewidth=.8)
    ax.set_xticks(np.arange(4), ["AP50", "Precision", "Recall", "F1"])
    ax.set_title(source.upper() + " | INT8 minus FP16")
    ax.set_ylabel("Percentage points")
    ax.set_ylim(-8, 3)
    ax.grid(axis="y", alpha=.2)
    ax.set_axisbelow(True)
axes[0].legend(loc="lower left")
fig.suptitle("Class changes can differ from the two-class mean", fontsize=14)
fig.tight_layout(rect=(0, 0, 1, .94))
for extension in ("png", "pdf"):
    fig.savefig(OUT / ("quality-deltas." + extension), dpi=170)
plt.close(fig)

fig, axes = plt.subplots(1, 2, figsize=(11, 5))
for k, precision in enumerate(PRECISIONS):
    ax = axes[0]
    values = [benchmarks[precision][name] for name in ("infer_ms", "e2e_ms")]
    bars = ax.bar(np.arange(2) + (k - .5) * .32, values, width=.32, color=COLORS[k], label=precision.upper())
    ax.bar_label(bars, fmt="%.2f", padding=3)
ax.set_xticks([0, 1], ["Inference API", "Image E2E"])
ax.set_ylabel("Mean milliseconds")
ax.set_ylim(0, 140)
ax.legend()
axes[1].bar([0, 1], [build["builds"][p]["output_bytes"] / 1e6 for p in PRECISIONS], color=COLORS, width=.55)
axes[1].set_xticks([0, 1], ["FP16", "INT8"])
axes[1].set_ylabel("RKNN file size (decimal MB)")
axes[1].set_ylim(0, 18)
for i, p in enumerate(PRECISIONS):
    axes[1].text(i, build["builds"][p]["output_bytes"] / 1e6 + .35, f"{build['builds'][p]['output_bytes'] / 1e6:.2f}", ha="center")
fig.suptitle("RK3568 measured pair | 416x416 | 100 loops, 10 warmups", fontsize=14)
fig.text(.5, .015, "FP16 measured first (51.9 C), INT8 later (53.8 C). Means only; temperatures and CPU clocks not locked.", ha="center", fontsize=10)
fig.tight_layout(rect=(0, .05, 1, .94))
for extension in ("png", "pdf"):
    fig.savefig(OUT / ("performance-size." + extension), dpi=170)
plt.close(fig)

percentage = lambda v: f"{v * 100:.2f}"
fp, quant = benchmarks["fp16"], benchmarks["int8"]
text = [
    "# 同源 416×416 FP16 / INT8 量化对照：实际结果与独立审计",
    "",
    f"本轮完成同源模型构建、实际 RK3568 推理速度和两套固定诊断子集的逐类质量对照。INT8 相比 FP16，平均推理 API 时间从 {fp['infer_ms']:.2f} ms 降至 {quant['infer_ms']:.2f} ms（{fp['infer_ms'] / quant['infer_ms']:.2f} 倍）；图像处理 E2E 从 {fp['e2e_ms']:.2f} ms 降至 {quant['e2e_ms']:.2f} ms（{fp['e2e_ms'] / quant['e2e_ms']:.2f} 倍）。SHWD / HardHat 的两类平均项目 AP50 分别下降 0.85 / 1.12 个百分点。此结论限定于本轮固定模型、子集和条件，不能宣布 85% 质量目标或物理摄像头、显示、长时稳定性验收通过。",
    "",
    "## 同源构建与校准",
    "",
    "这是新构建 FP16 与新构建 INT8 的成对实验，未混用历史六个 INT8 模型。源 ONNX SHA256 `" + build["source_onnx_sha256"] + "`，上游提交 `" + build["upstream_commit"] + "`。两者均用 Toolkit2 2.3.2、`images` `[1,3,416,416]`、Conv 输出 `350/364/378`、mean=0/std=255、RK3568、optimization=3；INT8 的 normal/channel 设置与 FP16 配置相同，量化开关与校准输入不同。配置和导出文件均重新核验。",
    "",
    "| 模型 | 文件大小（十进制 MB） | SHA256 | 构建耗时 |",
    "|---|---:|---|---:|",
]
for precision in PRECISIONS:
    r = build["builds"][precision]
    text.append(f"| {precision.upper()} | {r['output_bytes'] / 1e6:.6f} | `{r['output_sha256']}` | {r['completed_at'] - r['started_at']:.1f} s |")
text += [
    "",
    f"INT8 使用原 SHWD `train.txt` 顺序前 100 张图，只做校准，没有训练或微调。原图、预处理图、源拆分、列表和有序文件身份哈希全部验证；有序身份 SHA256 `{build['calibration']['ordered_file_identity_sha256']}`。按已有转换器做 416 letterbox、INTER_LINEAR、114 填充、JPEG 95。重新对比全部 400 张评估输入，训练侧 ID 与 SHWD 固定 test ID 无交集，字节/解码像素重复为 0，dHash 距离≤6候选为 0。这是有限输入检查，不能证明模型训练独立性。",
    "",
    "便携 Ubuntu 22.04 x86 QEMU 环境保留了两次 WHPX CPU 指令失败记录；Toolkit 原生 `vxorps` 需要 AVX，而该 WHPX guest 未暴露 AVX/XSAVE。换用 TCG `max` 后实际 CPUID 满足要求，两模型成功导出。构建进程 2 vCPU、2 GB RAM、4 GB swap，OMP/OpenBLAS/MKL/NumExpr 线程上限为 1。该环境说明转换可复现，不构成 CPU 量化推理测试。",
    "",
    "## 实际板端速度与大小",
    "",
    "| 指标 | FP16 | INT8 | INT8 相对变化 |",
    "|---|---:|---:|---:|",
]
for key, label in (("infer_ms", "推理 API 均值 / ms"), ("post_ms", "后处理均值 / ms"), ("e2e_ms", "图像 E2E 均值 / ms")):
    text.append(f"| {label} | {fp[key]:.2f} | {quant[key]:.2f} | {(quant[key] / fp[key] - 1) * 100:+.2f}% |")
text += [
    f"| 由 E2E 均值换算吞吐 / FPS | {1000 / fp['e2e_ms']:.2f} | {1000 / quant['e2e_ms']:.2f} | {fp['e2e_ms'] / quant['e2e_ms']:.2f} 倍 |",
    f"| 结束时温度 / °C | {fp['temp']:.1f} | {quant['temp']:.1f} | 顺序升温，未锁定 |",
    "",
    f"RKNN 文件从 {build['builds']['fp16']['output_bytes'] / 1e6:.2f} MB 降至 {build['builds']['int8']['output_bytes'] / 1e6:.2f} MB，减少 {(1 - build['builds']['int8']['output_bytes'] / build['builds']['fp16']['output_bytes']) * 100:.2f}%。",
    "",
    "![实际平均时间与模型大小](quantization-figures/performance-size.png)",
    "",
    "实际运行 `quantization-pair-1` 在 2026-11-14 09:06:14–09:09:13 UTC（17:06–17:09 北京时间）。两者日志都记录同路径 `input/1.jpg`、1280×960、100 loops、10 warmups；benchmark 代码显示推理、后处理和 E2E 三阶段各 100 次，预热为推理 10 次。FP16 先测，随后进行其两来源 dump，再测 INT8；不是随机交叉顺序。推理均值测 RKNN API；E2E 包含 resize/pad、颜色转换、推理和后处理，不含摄像头采集、显示、音频或编码。FPS 是 E2E 均值的倒数，不是显示 FPS。",
    "",
    "日志证实板端 RKNN-Toolkit-Lite2 **2.5.0**、librknnrt **2.3.2**、driver **0.9.2**、模型 compiler/toolkit **2.3.2**。静态 shape 的 dynamic-range query warning 已记录，模型实际推理与全部 dump 成功，不将 warning 改写成失败。NPU governor 写入 `performance` 的日志存在，原 `rknpu_ondemand` 在 finally 中恢复且读回一致；本轮未保存中间 performance/frequency 读回，未锁定 CPU 频率或温度。两个温度读数仅为各 benchmark 结束值。没有逐 loop 原始时间、p95/p99、置信区间或跨会话重复证据，故不声明尾延迟收益或固定热条件性能。",
    "",
    "benchmark 运行清单未记录采样图的运行时 SHA256。两日志相同路径和尺寸支持同图条件，主机 `models/1.jpg` SHA256 为 `" + AUDIT["benchmark_sample_host_sha256"] + "`。已保存 `post-run-input-audit.json`：板端相同路径事后读回 176,224 bytes，SHA 与主机一致，scope 明确标注 post-run、非 contemporaneous stamp；这是事后身份核验，不能升级为运行当时的字节时间戳。其 governor 读回 ondemand 也仅证明事后状态，不证明中间 performance 频率。",
    "",
    "## 两来源逐类质量与百分点差异",
    "",
    "AP 为 IoU=0.5、保留 score≥.001 的项目 all-point AP50；P/R/F1 在 confidence≥.25，NMS IoU=.45，最多 300 detections。未计算官方 COCO AP50:95。两来源各 200 张原预设输入完整保留，两精度模式的文件名、ID、维度、SHA、GT 元数据及 decoder 配置完全一致，四次均 200/200 成功、0 errors。",
    "",
    "| 来源 | FP16 mAP50 | INT8 mAP50 | INT8 − FP16 / pp |",
    "|---|---:|---:|---:|",
]
for source in SOURCES:
    a, b = AUDIT["metrics"][source + "-fp16"], AUDIT["metrics"][source + "-int8"]
    text.append(f"| {source.upper()}，200图 | {percentage(a['mAP50'])}% | {percentage(b['mAP50'])}% | {(b['mAP50'] - a['mAP50']) * 100:+.2f} |")
text += ["", "| 来源 / 类（GT 数） | FP16 AP50 / P / R / F1（%） | INT8 AP50 / P / R / F1（%） | INT8 − FP16 AP / P / R / F1（pp） |", "|---|---|---|---|"]
for source in SOURCES:
    for cls in CLASSES:
        a = AUDIT["metrics"][source + "-fp16"][cls]
        b = AUDIT["metrics"][source + "-int8"][cls]
        values_a = " / ".join(percentage(a[k]) for k in MEASURES)
        values_b = " / ".join(percentage(b[k]) for k in MEASURES)
        changes = " / ".join(f"{(b[k] - a[k]) * 100:+.2f}" for k in MEASURES)
        text.append(f"| {source.upper()} {cls}（{a['npos']}） | {values_a} | {values_b} | {changes} |")
text += [
    "",
    "![逐类 AP 与运行阈值质量](quantization-figures/class-quality.png)",
    "",
    "![INT8 减 FP16 的百分点变化](quantization-figures/quality-deltas.png)",
    "",
    "运行阈值的变化比 AP 均值更明显：SHWD 裸头召回提高 1.42 pp，但误检从 72 增至 92，precision 下降 5.22 pp；SHWD 安全帽 TP 从 634 降至 619、FP 从 133 增至 171。HardHat 裸头 TP 同为 217，FP 从 82 增至 96；安全帽 TP 从 504 降至 497，FP 从 58 增至 86。因此本轮 INT8 的更低平均时间伴随各类 precision/F1 下降，不能只用约 1 pp 的平均 AP 下降概括运行阈值代价。",
    "",
    "`person` 始终表示裸头，`hat` 表示戴安全帽的头部。SHWD 212 裸头 / 786 安全帽 GT；139 个 difficult hats 按既定诊断策略作为普通 GT，不等同标准 VOC difficult-ignore。HardHat 262 裸头 / 694 安全帽 GT，原 35 个全身 person 明确在规范化时忽略，绝不映射成裸头。所有面积 AP 与运行阈值 TP/FP/FN 均用当前 evaluator 和实际历史 snapshot 重新计算，数值在 1e-12 容差内吻合。",
    "",
    "规范 SHWD 输入的七个原 XML 文件名解析映射和原始哈希保持不变。共享后处理在图像边界裁剪后产生的 finite zero-area 候选按既定带 trace 的策略删除；SHWD FP16/INT8 各 7 个，HardHat 各 0 个。原 trace 与 counts 保留，未把失败推理处理为零检测。",
    "",
    "两套子集训练 overlap 未知，存在一张已确认共享原照片；原来源没有 scene ID，每套固定样本保留其 unknown-scene group。不把随机文件名或无字节重复解释为场景独立，也不给本轮 200 图结果加上完整数据集或认证独立测试标签。逐类变化和 2 类均值分别报告；没有对这两套诊断集调阈值，没有统计显著性或 85% 目标通过的结论。",
    "",
    "## 证据核验与复现",
    "",
    "实际运行 manifest 的五个源码 SHA（benchmark、dump、evaluator、dataset_tools、decoder）与 `quantization-pair-1/runtime-snapshot/` 全部一致。当前 evaluator/decoder 与历史哈希有差异，报告保留历史身份；重新评估同一 dump 后类/面积 AP、P/R/F1 与计数一致。原模型、raw dump、raw metrics、来源和手册/计划没有被本报告脚本修改。",
    "",
    "```powershell",
    ".\\.venv\\Scripts\\python.exe docs/experiments/2026-11-14/build_quantization_report.py",
    "```",
    "",
    "脚本只读原模型、构建清单、100 校准输入、400 规范评估输入及本轮 raw artifacts，写本报告和 `quantization-figures/`。其中 `quantization-verification.json` 保存每项检查和校准 overlap 重算；`*.current.metrics.json` / `*.recorded.metrics.json` 保存重算值，PNG 和 PDF 是独立图件。",
    "",
    "这支持 B1 同尺寸 FP16 构建、B2 同图短时平均 infer/E2E 比较、B3 同固定集合逐类 AP 对照的实际实验完成；计划勾选由协调 owner 依据这些证据执行。进一步固定热条件/CPU 频率/随机顺序/尾分布实验、独立有 scene provenance 的质量评估和真实摄像头最终型号选择仍是不同验收。",
]
(BASE / "quantization-results.md").write_text("\n".join(text) + "\n", encoding="utf8")
print("VERIFIED", len(AUDIT["checks"]), "checks; 4 current +4 recorded metric recomputations; 3 PNG +3 PDF figures")
print("mAP50", {s: {p: AUDIT["metrics"][s + "-" + p]["mAP50"] for p in PRECISIONS} for s in SOURCES})
