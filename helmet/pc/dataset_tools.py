#!/usr/bin/env python3
"""Immutable annotated-data preparation; canonical boxes are zero-based xywh.

Model semantics: person(0) = bare head, hat(1) = helmeted head, not full bodies.
Scene groups must be supplied by the source curator; filenames do not prove
scene independence. Partitions are evaluation/reserve, never training/tuning.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import xml.etree.ElementTree as ET

CLASSES = ('person', 'hat')
DS_TO_MODEL = {'no-hardhat': 0, 'hardhat': 1}
SEMANTICS = {'person': 'bare head (no helmet)', 'hat': 'head wearing a helmet'}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def safe_name(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or '\x00' in value:
        raise ValueError('invalid relative image filename: %r' % (value,))
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('unsafe image filename: %r' % value)
    return value


def image_path(root, filename):
    base = Path(root).resolve()
    path = (base / safe_name(filename)).resolve()
    if not path.is_relative_to(base):
        raise ValueError('image path escapes dataset root')
    return path


def finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('%s must be a finite number' % label)
    return float(value)


def integer_id(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s must be an integer' % label)
    return value


def validate_coco(data, require_dimensions=False):
    """Validate all records, retaining all images; unsupported crowd/ignore fail.

    Legacy metadata-only prediction evaluation may omit image dimensions and
    annotation IDs. Missing dimensions are reported; real inference requires
    them. Unknown category names are counted, never interpreted as bare heads.
    A dataset without any recognized annotated head is unusable and rejected.
    """
    if not isinstance(data, dict) or any(not isinstance(data.get(k), list)
                                        for k in ('images', 'annotations', 'categories')):
        raise ValueError('COCO requires images/annotations/categories arrays')
    images, names, categories, ann_ids = {}, set(), {}, set()
    for item in data['images']:
        if not isinstance(item, dict):
            raise ValueError('invalid image record')
        key = integer_id(item.get('id'), 'image id')
        name = safe_name(item.get('file_name'))
        if key in images or name.casefold() in names:
            raise ValueError('duplicate image id or filename')
        if ('width' in item) != ('height' in item):
            raise ValueError('image dimensions must be supplied together')
        if require_dimensions and 'width' not in item:
            raise ValueError('image dimensions required for inference/manifest')
        if 'width' in item:
            for dimension in ('width', 'height'):
                if integer_id(item[dimension], dimension) <= 0:
                    raise ValueError('image dimensions must be positive')
        images[key] = dict(item)
        names.add(name.casefold())
    if not images:
        raise ValueError('dataset has no images')
    for item in data['categories']:
        if not isinstance(item, dict):
            raise ValueError('invalid category record')
        key = integer_id(item.get('id'), 'category id')
        if key in categories or not isinstance(item.get('name'), str):
            raise ValueError('duplicate or invalid category')
        categories[key] = item['name']
    gt = {key: [] for key in images}
    recognized, ignored = 0, 0
    for index, item in enumerate(data['annotations']):
        if not isinstance(item, dict):
            raise ValueError('invalid annotation')
        if 'id' in item:
            aid = integer_id(item['id'], 'annotation id')
            if aid in ann_ids:
                raise ValueError('duplicate annotation id')
            ann_ids.add(aid)
        iid = integer_id(item.get('image_id'), 'annotation image id')
        cid = integer_id(item.get('category_id'), 'annotation category id')
        if iid not in images or cid not in categories:
            raise ValueError('annotation references unknown image/category')
        if item.get('iscrowd', 0) != 0 or item.get('ignore', 0) != 0:
            raise ValueError('crowd/ignore annotations unsupported by this evaluator')
        box = item.get('bbox')
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError('COCO bbox requires [x,y,w,h]')
        x, y, w, h = [finite_number(v, 'bbox') for v in box]
        if x < 0 or y < 0 or w <= 0 or h <= 0 or not math.isfinite(w * h) or not math.isfinite(x + w + y + h):
            raise ValueError('invalid annotation geometry')
        im = images[iid]
        if 'width' in im and (x + w > im['width'] + 1e-6 or y + h > im['height'] + 1e-6):
            raise ValueError('annotation bbox outside image')
        if 'area' in item and abs(finite_number(item['area'], 'area') - w * h) > max(1e-5, w * h * 1e-5):
            raise ValueError('annotation area differs from rectangle; segmentation area unsupported')
        cls = DS_TO_MODEL.get(categories[cid])
        if cls is None:
            ignored += 1
        else:
            gt[iid].append((cls, x, y, x + w, y + h))
            recognized += 1
    if recognized == 0:
        raise ValueError('unusable dataset: no hardhat/no-hardhat head annotations')
    metadata = {'class_mapping': DS_TO_MODEL, 'model_semantics': SEMANTICS,
                'recognized_annotations': recognized, 'ignored_annotations': ignored,
                'unknown_categories': sorted({n for n in categories.values() if n not in DS_TO_MODEL}),
                'dimensions_available': all('width' in i for i in images.values()),
                'evaluation_rules': data.get('info', {}).get('evaluation_rules', {})}
    return sorted(images.values(), key=lambda i: i['id']), gt, metadata


def source_image_index(root):
    """Index actual relative image names; no rename/copy or inferred scene IDs."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('source image root must be an existing directory')
    by_name, by_stem = {}, {}
    extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'}
    for path in sorted(root.rglob('*'), key=lambda p: p.as_posix()):
        if not path.is_file() or path.suffix.casefold() not in extensions:
            continue
        name = safe_name(path.relative_to(root).as_posix())
        # Reject an image symlink escaping this declared source root.
        image_path(root, name)
        by_name.setdefault(name.casefold(), []).append(name)
        by_stem.setdefault(path.stem.casefold(), []).append(name)
    return by_name, by_stem


def resolve_source_filename(original, annotation_path, index, policy):
    by_name, by_stem = index
    candidates = by_name.get(original.casefold(), [])
    method = 'exact' if original in candidates else 'case-insensitive'
    if not candidates and policy == 'unique-stem':
        # Original archive identity is the XML stem, not a guessed extension.
        candidates = by_stem.get(Path(annotation_path).stem.casefold(), [])
        method = 'unique-stem'
    if not candidates:
        raise ValueError('no source image for XML filename %r (%s); explicit unique-stem policy required for a mismatched extension'
                         % (original, annotation_path))
    if len(candidates) != 1:
        raise ValueError('ambiguous source images for %s: %s' % (annotation_path, ', '.join(candidates)))
    return candidates[0], method


def voc_to_coco(xml_paths, class_map, coordinate_mode='voc-inclusive', ignored_labels=None,
                difficult_policy='reject', image_root=None, filename_policy='strict'):
    """Convert declared head labels; preserve raw XML paths and hashes in info.

    voc-inclusive: one-based inclusive [xmin,ymin,xmax,ymax] -> zero-based
    half-open xyxy by subtracting 1 from minima only. zero-based: xyxy already
    half-open. The caller must establish source semantics before class mapping.
    Optional image_root verifies declared names against actual source names.
    Strict policy permits only a unique case-insensitive declared path match;
    unique-stem explicitly permits fallback to the XML stem. Both preserve the
    declared filename and original hashes, and reject ambiguous source images.
    """
    if coordinate_mode not in ('voc-inclusive', 'zero-based'):
        raise ValueError('unsupported VOC coordinate convention')
    if difficult_policy not in ('reject', 'include'):
        raise ValueError('difficult_policy must be reject/include; no silent deletion')
    if filename_policy not in ('strict', 'unique-stem'):
        raise ValueError('filename_policy must be strict/unique-stem')
    if image_root is None and filename_policy != 'strict':
        raise ValueError('filename resolution requires explicit image_root')
    source_index = source_image_index(image_root) if image_root is not None else None
    resolution_counts = {}
    if not class_map or any(not isinstance(k, str) or isinstance(v, bool) or v not in (0, 1)
                            for k, v in class_map.items()):
        raise ValueError('explicit source label -> model 0/1 class map required')
    ignored_labels = set(ignored_labels or [])
    if any(not isinstance(label, str) for label in ignored_labels) or ignored_labels.intersection(class_map):
        raise ValueError('ignored labels must be strings and cannot also be mapped')
    ignored_counts = {name: 0 for name in sorted(ignored_labels)}
    raw_object_count = 0
    ignored_objects = []
    difficult_counts = {}
    images, annotations, raw_sources, seen = [], [], [], set()
    paths = sorted((Path(p) for p in xml_paths), key=lambda p: p.as_posix())
    if not paths:
        raise ValueError('no VOC XML annotations')
    for path in paths:
        node = ET.parse(path).getroot()
        original_filename = safe_name(node.findtext('filename'))
        filename, resolution = (resolve_source_filename(original_filename, path, source_index, filename_policy)
                                if source_index is not None else (original_filename, 'declared'))
        resolution_counts[resolution] = resolution_counts.get(resolution, 0) + 1
        if filename.casefold() in seen:
            raise ValueError('duplicate VOC filename')
        seen.add(filename.casefold())
        try:
            width, height = int(node.findtext('size/width')), int(node.findtext('size/height'))
        except (TypeError, ValueError) as error:
            raise ValueError('invalid VOC dimensions: %s' % path) from error
        if width <= 0 or height <= 0:
            raise ValueError('invalid VOC dimensions')
        iid = len(images) + 1
        image = {'id': iid, 'file_name': filename, 'width': width, 'height': height,
                 'original_filename': original_filename, 'source_filename': filename,
                 'filename_resolution': resolution}
        raw_source = {'file_name': filename, 'path': path.as_posix(), 'sha256': sha256_file(path),
                      'original_filename': original_filename, 'source_filename': filename,
                      'filename_resolution': resolution}
        if image_root is not None:
            image['source_image_sha256'] = sha256_file(image_path(image_root, filename))
            raw_source['source_image_sha256'] = image['source_image_sha256']
        images.append(image)
        raw_sources.append(raw_source)
        for obj in node.findall('object'):
            name = obj.findtext('name')
            raw_object_count += 1
            if name not in class_map and name not in ignored_labels:
                raise ValueError('unknown VOC object label: %r' % name)
            flag = obj.findtext('difficult', '0').strip()
            if flag not in ('0', '1'):
                raise ValueError('VOC difficult flag must be 0/1')
            difficult = int(flag)
            if difficult:
                difficult_counts[name] = difficult_counts.get(name, 0) + 1
                if difficult_policy == 'reject':
                    raise ValueError('difficult VOC annotation requires explicit difficult_policy=include; preserve raw data')
            try:
                x1, y1, x2, y2 = [float(obj.findtext('bndbox/' + field))
                                  for field in ('xmin', 'ymin', 'xmax', 'ymax')]
            except (TypeError, ValueError) as error:
                raise ValueError('invalid VOC bbox') from error
            if coordinate_mode == 'voc-inclusive':
                x1 -= 1
                y1 -= 1
            box = [x1, y1, x2 - x1, y2 - y1]
            if (not all(math.isfinite(v) for v in box) or x1 < 0 or y1 < 0 or box[2] <= 0
                    or box[3] <= 0 or x2 > width or y2 > height):
                raise ValueError('invalid VOC bbox geometry, including ignored objects')
            if name in ignored_labels:
                ignored_counts[name] += 1
                ignored_objects.append({'file_name': filename, 'source_label': name, 'bbox': box,
                                        'source_difficult': difficult})
                continue
            annotations.append({'id': len(annotations) + 1, 'image_id': iid,
                                'category_id': class_map[name], 'bbox': box,
                                'area': box[2] * box[3], 'iscrowd': 0, 'source_label': name,
                                'source_difficult': difficult})
    data = {'images': images, 'annotations': annotations,
            'categories': [{'id': 0, 'name': 'no-hardhat'}, {'id': 1, 'name': 'hardhat'}],
            'info': {'coordinate_mode': coordinate_mode, 'source_class_map': class_map,
                     'model_semantics': SEMANTICS, 'raw_annotations': raw_sources,
                     'filename_policy': filename_policy, 'filename_resolution_counts': resolution_counts,
                     'ignored_labels': sorted(ignored_labels), 'ignored_label_counts': ignored_counts,
                     'raw_object_count': raw_object_count, 'ignored_objects': ignored_objects,
                     'difficult_label_counts': difficult_counts,
                     'evaluation_rules': {'difficult_policy': 'include-as-normal' if difficult_policy == 'include' else 'reject',
                                          'difficult_definition': 'explicit diagnostic normalization: mapped difficult objects remain normal GT and may be TP/FN; original flags preserved; not standard VOC difficult-ignore' if difficult_policy == 'include'
                                                                  else 'reject any difficult object until explicit policy supplied'}}}
    validate_coco(data, require_dimensions=True)
    if image_root is not None:
        data['info']['source_image_root'] = Path(image_root).resolve().as_posix()
    return data


def create_manifest(root, coco, provenance, groups):
    images, gt, metadata = validate_coco(coco, require_dimensions=True)
    if not isinstance(provenance, dict) or any(not provenance.get(k)
                                              for k in ('source_url', 'license', 'origin', 'independence')):
        raise ValueError('source_url/license/origin/independence required (unknown permitted)')
    if provenance['independence'] not in ('unknown', 'verified', 'overlap'):
        raise ValueError('independence must be unknown/verified/overlap')
    if provenance['independence'] == 'verified' and not provenance.get('independence_evidence'):
        raise ValueError('verified independence requires evidence')
    records = []
    raw = {r['file_name']: r for r in coco.get('info', {}).get('raw_annotations', [])}
    for im in images:
        name = im['file_name']
        if not groups.get(name) or not isinstance(groups[name], str):
            raise ValueError('explicit scene group required for %s' % name)
        anns = sorted((a for a in coco['annotations'] if a['image_id'] == im['id']),
                      key=lambda a: json.dumps(a, sort_keys=True))
        row = {'file_name': name, 'id': im['id'], 'width': im['width'], 'height': im['height'],
               'group': groups[name], 'image_sha256': sha256_file(image_path(root, name)),
               'annotation_sha256': canonical_hash(anns), 'annotation_count': len(anns),
               'class_counts': {c: sum(g[0] == i for g in gt[im['id']]) for i, c in enumerate(CLASSES)}}
        for key in ('original_filename', 'source_filename', 'source_image_sha256', 'filename_resolution'):
            if key in im:
                row[key] = im[key]
        if name in raw:
            row['raw_annotation'] = raw[name]
        records.append(row)
    return {'schema_version': 1, 'provenance': dict(provenance), 'validation': metadata,
            'annotation_document_sha256': canonical_hash(coco), 'records': records,
            'counts': {'images': len(images), 'annotations': len(coco['annotations']),
                       'raw_objects': coco.get('info', {}).get('raw_object_count', len(coco['annotations'])),
                       'ignored_labels': coco.get('info', {}).get('ignored_label_counts', {}),
                       'difficult_labels': coco.get('info', {}).get('difficult_label_counts', {}),
                       'difficult_included_annotations': sum(a.get('source_difficult', 0) for a in coco['annotations']),
                       'groups': len({r['group'] for r in records}),
                       'classes': {c: sum(r['class_counts'][c] for r in records) for c in CLASSES}}}


def split_records(records, eval_fraction=.8, seed='helmet-eval-v1'):
    """Hash-stable scene split, merging connected scenes sharing image hashes."""
    if not 0 < eval_fraction < 1 or not records:
        raise ValueError('nonempty records and 0 < eval_fraction < 1 required')
    parent, hashes, names = {}, {}, set()

    def find(group):
        while parent[group] != group:
            parent[group] = parent[parent[group]]
            group = parent[group]
        return group

    for row in sorted(records, key=lambda r: r['file_name']):
        name, group, digest = safe_name(row['file_name']), row.get('group'), row.get('image_sha256')
        if name in names or not group or not digest:
            raise ValueError('unique filenames, explicit groups and image hashes required')
        names.add(name)
        parent.setdefault(group, group)
        if digest in hashes:
            a, b = find(group), find(hashes[digest])
            parent[max(a, b)] = min(a, b)
        else:
            hashes[digest] = group
    components = {}
    for group in parent:
        components.setdefault(find(group), []).append(group)
    ordered = sorted(components, key=lambda g: hashlib.sha256(
        (str(seed) + '\0' + '\0'.join(sorted(components[g]))).encode()).hexdigest())
    if len(ordered) < 2:
        raise ValueError('at least two independent scene/hash components required for a split')
    eval_count = max(1, min(len(ordered) - 1, round(len(ordered) * eval_fraction)))
    chosen = set(ordered[:eval_count])
    return {r['file_name']: 'eval' if find(r['group']) in chosen else 'reserve'
            for r in sorted(records, key=lambda r: r['file_name'])}


def verify_images(root, images):
    import cv2
    for im in images:
        raw = cv2.imread(str(image_path(root, im['file_name'])))
        if raw is None or raw.shape[1] != im['width'] or raw.shape[0] != im['height']:
            raise ValueError('unreadable image or dimensions differ: %s' % im['file_name'])


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    voc = subs.add_parser('voc')
    voc.add_argument('--xml-dir', required=True)
    voc.add_argument('--class-map', required=True, help='JSON source label -> 0/1; establish head semantics first')
    voc.add_argument('--coordinate-mode', choices=['voc-inclusive', 'zero-based'], default='voc-inclusive')
    voc.add_argument('--ignored-labels', nargs='*', default=[], help='explicit out-of-scope source labels, e.g. person full body')
    voc.add_argument('--difficult-policy', choices=['reject', 'include'], default='reject',
                     help='include explicitly treats mapped difficult targets as normal GT, retaining flags/counts')
    voc.add_argument('--filename-policy', choices=['strict', 'unique-stem'], default='strict',
                     help='resolve actual --images names; unique-stem explicitly permits XML-stem fallback')
    for name in ('voc', 'manifest'):
        sub = voc if name == 'voc' else subs.add_parser(name)
        sub.add_argument('--images', required=True)
        sub.add_argument('--out', required=True)
        sub.add_argument('--provenance', required=True, help='JSON with source/license/origin/independence')
        sub.add_argument('--groups', required=True, help='JSON filename -> scene group (no filename inference)')
        if name == 'manifest':
            sub.add_argument('--coco', required=True)
    split = subs.add_parser('split')
    split.add_argument('--manifest', required=True)
    split.add_argument('--out', required=True)
    split.add_argument('--eval-fraction', type=float, default=.8)
    split.add_argument('--seed', default='helmet-eval-v1')
    args = parser.parse_args()
    try:
        if args.command == 'split':
            manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
            assignment = split_records(manifest['records'], args.eval_fraction, args.seed)
            write_json(args.out, {'schema_version': 1, 'seed': args.seed, 'eval_fraction': args.eval_fraction,
                                  'manifest_sha256': sha256_file(args.manifest), 'assignments': assignment,
                                  'purpose': 'evaluation/reserve; no training or threshold tuning'})
            return
        if args.command == 'voc':
            coco = voc_to_coco(Path(args.xml_dir).rglob('*.xml'),
                               json.loads(Path(args.class_map).read_text(encoding='utf-8')), args.coordinate_mode,
                               args.ignored_labels, args.difficult_policy, args.images, args.filename_policy)
        else:
            coco = json.loads(Path(args.coco).read_text(encoding='utf-8'))
        verify_images(args.images, coco['images'])
        provenance = json.loads(Path(args.provenance).read_text(encoding='utf-8'))
        groups = json.loads(Path(args.groups).read_text(encoding='utf-8'))
        manifest = create_manifest(args.images, coco, provenance, groups)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for im in coco['images']:
            src, dest = image_path(args.images, im['file_name']), image_path(out, im['file_name'])
            if src != dest:
                if dest.exists():
                    raise ValueError('refuse to overwrite image: %s' % dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        write_json(out / '_annotations.coco.json', coco)
        write_json(out / 'manifest.json', manifest)
    except (ValueError, OSError, ET.ParseError, KeyError) as error:
        parser.exit(1, 'dataset error: %s\n' % error)


if __name__ == '__main__':
    main()
