"""Mirror existing convert.py config using same-source ONNX; fresh files only."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[name] = "1"
import onnx
from rknn.api import RKNN

parser = argparse.ArgumentParser()
parser.add_argument("--int8", action="store_true")
parser.add_argument("--dataset")
args = parser.parse_args()
source = Path("helmet.onnx")
expected = "7ea304dbfc05a549c3f0c10890a5d20fc9bbd4d34df2680265d78316e6b770a7"
assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, "Source ONNX hash differs"
graph = onnx.load(str(source)).graph
producers = {name: node for node in graph.node for name in node.output}
heads = []
for output in graph.output:
    node = producers[output.name]
    while node.op_type != "Conv":
        node = producers[node.input[0]]
    heads.append(node.output[0])
assert heads == ["350", "364", "378"], heads
config = dict(mean_values=[[0, 0, 0]], std_values=[[255, 255, 255]], target_platform="rk3568", quantized_algorithm="normal", quantized_method="channel", optimization_level=3)
load_config = dict(model=str(source), inputs=[graph.input[0].name], input_size_list=[[1, 3, 416, 416]], outputs=heads)
precision = "int8" if args.int8 else "fp16"
output = Path("helmet_416x416_same_source_" + precision + ".rknn")
record = {"source_sha256": expected, "config": config, "load_config": load_config, "do_quantization": args.int8, "dataset": args.dataset, "head_layout": "NCHW,21channels=3anchors*(xywh,obj,person,hat)", "started_at": time.time(), "expected_toolkit": "2.3.2", "output": str(output)}
Path("conversion-" + precision + ".json").write_text(json.dumps(record, indent=2))
rknn = RKNN(verbose=True)
try:
    assert rknn.config(**config) in (None, 0), "config failed"
    assert rknn.load_onnx(**load_config) == 0, "load_onnx failed"
    assert rknn.build(do_quantization=args.int8, dataset=args.dataset if args.int8 else None) == 0, "build failed"
    assert rknn.export_rknn(str(output)) == 0, "export failed"
    record.update(completed_at=time.time(), output_bytes=output.stat().st_size, output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(), success=True)
    print("CONVERSION_COMPLETE", precision, record["output_sha256"], flush=True)
finally:
    rknn.release()
    Path("conversion-" + precision + ".json").write_text(json.dumps(record, indent=2))
