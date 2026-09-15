#!/usr/bin/env python3
"""Verify paired offline golden manifests and their actual exported bytes."""
import argparse
import hashlib
import json
from pathlib import Path


PRESETS = ['clean-notch@4', 'prestige@3', 'minimalist@4']
MEDIA_IDS = {9258, 1344, 1402, 371985}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def projection(value):
    if isinstance(value, dict):
        return {key: projection(item) for key, item in value.items()
                if key not in {'environment', 'evidence_file'}}
    if isinstance(value, list):
        return [projection(item) for item in value]
    return value


def verify_files(document, root):
    root = Path(root).resolve(strict=True)
    if document['presets'] != PRESETS or len(document['cases']) != 4:
        raise ValueError('Expected four titles and the three current presets')
    if {case['media_id'] for case in document['cases']} != MEDIA_IDS:
        raise ValueError('Golden title identities differ')
    files = 0
    for case in document['cases']:
        if [item['preset_ref'] for item in case['renders']] != PRESETS:
            raise ValueError('Incomplete preset renders')
        for item in case['normalized_artifacts'] + case['renders']:
            relative = Path(item['evidence_file'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unsafe golden evidence path')
            path = root / relative
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                raise ValueError('Golden evidence escapes artifact root')
            data = path.read_bytes()
            expected = item.get('content_sha256', item.get('sha256'))
            if len(data) != item['byte_size'] or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('Golden file digest/size mismatch: ' + str(relative))
            files += 1
    return files


def compare(arm, x86, arm_root, x86_root):
    counts = [verify_files(arm, arm_root), verify_files(x86, x86_root)]
    if arm['environment']['architecture'] not in {'aarch64', 'arm64'}:
        raise ValueError('ARM execution evidence required')
    if x86['environment']['architecture'] not in {'x86_64', 'amd64'}:
        raise ValueError('x86 execution evidence required')
    if projection(arm) != projection(x86):
        raise ValueError('Architecture enrichment/render projections differ')
    return {'schema': 'posterplus-golden-comparison-v1', 'titles': 4, 'webps': 12,
            'verified_files': counts, 'arm_digest': digest(arm), 'x86_digest': digest(x86),
            'aggregate_digest': digest(projection(arm)),
            'renderer_revision': arm['renderer_revision'],
            'source_recipe_revision': arm['source_recipe_revision']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('arm', 'x86', 'arm-root', 'x86-root', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = compare(json.loads(args.arm.read_text()), json.loads(args.x86.read_text()),
                     args.arm_root, args.x86_root)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    print(json.dumps(result, sort_keys=True))
