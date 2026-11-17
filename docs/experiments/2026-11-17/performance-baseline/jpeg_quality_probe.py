#!/usr/bin/env python3
"""Compare board JPEG encode time and size on an existing annotated frame."""
import argparse
import json
from pathlib import Path
import statistics
import time

import cv2


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--iterations', type=int, default=30)
    args = parser.parse_args(argv)
    image = cv2.imread(args.image)
    if image is None:
        raise RuntimeError('image could not be decoded')
    height, width = image.shape[:2]
    cv2.rectangle(image, (20, 20), (width - 20, height - 20), (0, 200, 0), 2)
    cv2.putText(image, 'healthy | alarm: active | frame: 12345', (5, 20),
                cv2.FONT_HERSHEY_SIMPLEX, .45, (200, 200, 200), 1)
    results = {}
    for quality in (95, 90, 85, 80, 75, 70):
        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError('JPEG encoding failed')
        samples = []
        for _ in range(max(1, args.iterations)):
            began = time.perf_counter()
            ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
            samples.append((time.perf_counter() - began) * 1000)
            if not ok:
                raise RuntimeError('JPEG encoding failed')
        results[str(quality)] = {
            'bytes': len(encoded),
            'mean_ms': statistics.mean(samples),
            'p50_ms': statistics.median(samples),
            'p95_ms': sorted(samples)[int((len(samples) - 1) * .95)],
        }
    print(json.dumps({'image': str(Path(args.image).resolve()),
                      'shape': list(image.shape), 'results': results},
                     ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
