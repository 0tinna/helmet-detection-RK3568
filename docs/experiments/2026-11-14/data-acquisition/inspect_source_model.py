import hashlib
import json
from pathlib import Path
import onnx

commit = json.loads(Path("datasets/provenance/community-model-commit.json").read_text(encoding="utf-8-sig"))["sha"]
model = onnx.load("models/source/helmet.onnx")
member = "onnx_to_rknn/examples/onnx/yolov5s/helmet.onnx"
digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
result = {
    "repository": "https://github.com/harperjuanl/helmet_yolov5_rknn_deploy",
    "commit": commit,
    "onnx_source_member": member,
    "onnx_url": "https://raw.githubusercontent.com/harperjuanl/helmet_yolov5_rknn_deploy/" + commit + "/" + member,
    "onnx_local": "models/source/helmet.onnx",
    "onnx_bytes": Path("models/source/helmet.onnx").stat().st_size,
    "onnx_sha256": digest("models/source/helmet.onnx"),
    "producer": [model.producer_name, model.producer_version],
    "inputs": [(n.name, [v.dim_value for v in n.type.tensor_type.shape.dim]) for n in model.graph.input],
    "outputs": [(n.name, [v.dim_value for v in n.type.tensor_type.shape.dim]) for n in model.graph.output],
    "class_order": ["person", "hat"],
    "class_order_evidence": "Upstream test.py CLASSES=(person,hat), retained as plain text; not executed",
    "upstream_rknn_sha256": digest("models/source/upstream-helmet-640-640.rknn"),
    "local_baseline_sha256": digest("models/helmet-640-640.rknn"),
    "license": "GitHub repository license metadata null; no root license in inspected tree. Model redistribution grant not established. Local analysis only.",
    "training_data": "Training dataset and overlap unknown; no upstream training manifest establishing independence.",
    "lineage_limit": "Pinned upstream RKNN bytes equal local baseline. This links the downloaded ONNX to the same upstream deployment tree, but does not establish byte-exact conversion/calibration lineage for the five existing converted variants."
}
Path("datasets/provenance/community-model-provenance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
