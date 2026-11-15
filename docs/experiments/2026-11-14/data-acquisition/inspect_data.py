"""Verify acquired original annotations/images, record hashes, render manual QA sheets.

No model scores or predictions are read; image selection follows acquisition manifests.
"""
import argparse
import binascii
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / "docs/experiments/2026-11-14/data-acquisition"
SOURCES = {
    "hardhat": ("hard-hat-workers", "annotations", "images", {"head": "person", "helmet": "hat", "person": None}),
    "shwd": ("shwd", "Annotations", "JPEGImages", {"person": "person", "hat": "hat"}),
}


def dhash(image):
    pixels = np.asarray(image.convert("L").resize((9, 8)))
    return sum(int(v) << i for i, v in enumerate((pixels[:, 1:] > pixels[:, :-1]).ravel()))


def inspect(source):
    directory, annotation_dir, image_dir, mapping = SOURCES[source]
    base = ROOT / "datasets/raw" / directory
    image_paths = {p.stem: p for p in (base / image_dir).iterdir() if p.is_file()}
    if source == "hardhat":
        selection = json.loads((ROOT / "datasets/provenance/hard-hat-selection.json").read_text())
        ids = [Path(p).stem for p in selection["annotation_names"]]
    else:
        ids = json.loads((ROOT / "datasets/provenance/shwd-selection.json").read_text())["ids"]
    inventory, problems, object_counts, image_class_counts = [], [], Counter(), Counter()
    sheets = []
    for image_id in ids:
        annotation_path = base / annotation_dir / (image_id + ".xml")
        image_path = image_paths.get(image_id)
        if not annotation_path.is_file() or image_path is None:
            problems.append({"image_id": image_id, "problem": "Missing original annotation or image"})
            continue
        root = ET.parse(annotation_path).getroot()
        image = Image.open(image_path).convert("RGB")
        width, height = image.size
        annotation_size = (int(root.findtext("size/width")), int(root.findtext("size/height")))
        if annotation_size != image.size:
            problems.append({"image_id": image_id, "problem": "XML and decoded dimensions differ", "xml": annotation_size, "image": image.size})
        objects = []
        for obj in root.findall("object"):
            name = obj.findtext("name").strip()
            box = tuple(float(obj.findtext("bndbox/" + key)) for key in ("xmin", "ymin", "xmax", "ymax"))
            object_counts[name] += 1
            if name not in mapping:
                problems.append({"image_id": image_id, "problem": "Unrecognized original class", "class": name})
            x1, y1, x2, y2 = box
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                problems.append({"image_id": image_id, "problem": "Invalid original box", "class": name, "box": box, "dimensions": image.size})
            objects.append({"name": name, "mapped": mapping.get(name), "box": box})
        image_class_counts.update(set(o["name"] for o in objects))
        inventory.append({"source": source, "id": image_id, "image": image_path.relative_to(ROOT).as_posix(), "annotation": annotation_path.relative_to(ROOT).as_posix(), "width": width, "height": height, "original_classes": ";".join(sorted(set(o["name"] for o in objects))), "objects": len(objects), "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(), "dhash": f"{dhash(image):016x}", "source_scene": "unknown"})
        if len(sheets) < 12:
            rendered = image.copy()
            draw = ImageDraw.Draw(rendered)
            for obj in objects:
                color = {"person": "red", "hat": "lime", None: "cyan"}[obj["mapped"]]
                draw.rectangle(obj["box"], outline=color, width=max(2, width // 250))
                draw.text((obj["box"][0], max(0, obj["box"][1] - 12)), obj["name"], fill=color, stroke_width=1, stroke_fill="black")
            tile = Image.new("RGB", (350, 290), "#f0f0f0")
            fitted = ImageOps.contain(rendered, (350, 255))
            tile.paste(fitted, ((350 - fitted.width) // 2, 0))
            ImageDraw.Draw(tile).text((5, 260), f"{image_id} {width}x{height}", fill="black")
            sheets.append(tile)
    summary = {"source": source, "images": len(inventory), "expected_images": len(ids), "original_object_counts": dict(object_counts), "images_with_class": dict(image_class_counts), "mapped_classes": mapping, "dimensions": dict(Counter(f"{r['width']}x{r['height']}" for r in inventory)), "problems": problems, "scene_grouping": "Original filenames and annotations lack camera/video/scene IDs. Exact hashes and dHash candidates are evidence only; singleton image groups do not establish scene independence."}
    (ROOT / f"datasets/provenance/{source}-inspection.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if inventory:
        with (ROOT / f"datasets/provenance/{source}-image-inventory.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(inventory[0]))
            writer.writeheader()
            writer.writerows(inventory)
    sheet = Image.new("RGB", (350 * 4, 290 * 3), "white")
    for i, tile in enumerate(sheets):
        sheet.paste(tile, (350 * (i % 4), 290 * (i // 4)))
    sheet.save(OUT / f"{source}-annotation-contact-sheet.jpg", quality=92)
    print(json.dumps(summary, ensure_ascii=False))
    if source == "shwd":
        manifest = json.loads((ROOT / "datasets/provenance/shwd-acquisition-manifest.json").read_text())
        bad = []
        for row in manifest["files"]:
            data = (ROOT / row["path"]).read_bytes()
            if binascii.crc32(data) != row["zip_crc32"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                bad.append(row["path"])
        (ROOT / "datasets/provenance/shwd-member-integrity.json").write_text(json.dumps({"checked_members": len(manifest["files"]), "failures": bad, "checks": ["original ZIP CRC32", "acquisition SHA256"]}, indent=2))
        if bad:
            raise ValueError("Original ZIP member integrity failed")
    return inventory


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*", default=list(SOURCES), choices=list(SOURCES))
    args = parser.parse_args()
    all_rows = [row for source in args.sources for row in inspect(source)]
    exact_duplicates, visual_candidates = [], []
    for i, row in enumerate(all_rows):
        for other in all_rows[:i]:
            if row["sha256"] == other["sha256"]:
                exact_duplicates.append([row["image"], other["image"]])
            distance = (int(row["dhash"], 16) ^ int(other["dhash"], 16)).bit_count()
            if distance <= 6:
                visual_candidates.append({"images": [row["image"], other["image"]], "dhash_distance": distance, "label": "candidate only; manually inspect before scene grouping"})
    (ROOT / "datasets/provenance/acquired-duplicate-review.json").write_text(json.dumps({"exact_duplicates": exact_duplicates, "dhash_candidates": visual_candidates, "limitation": "Absence of matches in these acquired subsets does not establish training independence or full-source disjointness."}, indent=2), encoding="utf-8")
