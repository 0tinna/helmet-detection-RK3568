"""Validate the fixed six-model, 50-loop RK3568 comparison experiment."""
import argparse
import json
import math
import sys


EXPECTED_MODELS = {
    'helmet-640-640.rknn', 'helmet_416x416.rknn', 'helmet_512x288.rknn',
    'helmet_512x512.rknn', 'helmet_640x384.rknn', 'helmet_640x640.rknn',
}


def validate_results(path):
    with open(path, encoding='utf-8') as stream:
        results = json.load(stream)
    if not isinstance(results, list) or len(results) != len(EXPECTED_MODELS):
        raise ValueError('comparison must contain exactly six successful models')
    names = []
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get('model'), str):
            raise ValueError('each result must identify its model')
        names.append(result['model'])
        if result.get('loops') != 50:
            raise ValueError('each model must have 50 measured loops')
        for field in ('infer_ms', 'post_ms', 'e2e_ms', 'fps'):
            value = result.get(field)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError('%s must be finite and positive' % field)
    if set(names) != EXPECTED_MODELS:
        raise ValueError('comparison model names are incomplete or duplicated')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', help='benchmark JSON for the fixed model matrix')
    args = parser.parse_args()
    try:
        validate_results(args.path)
    except (OSError, ValueError) as error:
        print('Invalid comparison: %s' % error, file=sys.stderr)
        return 1
    print('Verified six successful model results.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
