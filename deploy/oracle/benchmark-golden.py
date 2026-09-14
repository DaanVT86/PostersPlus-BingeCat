#!/usr/bin/env python3
"""Measure real OCR on frozen fixtures inside the pinned offline Core image."""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time

# Source checkout is mounted at /validation by run-offline-golden.sh.
sys.path.insert(0, str(Path.cwd()))
import offline_golden
import text_detect


def benchmark(fixtures):
    samples = []

    def measured(*args, **kwargs):
        started = time.perf_counter()
        try:
            return text_detect.poster_has_burned_in_text(*args, **kwargs)
        finally:
            samples.append(time.perf_counter() - started)

    started = time.perf_counter()
    result = offline_golden.export_fixture(
        str(fixtures), production_facts=str(fixtures / 'production-facts.json'),
        preserved_sources=str(fixtures / 'preserved_sources'), detector=measured)
    ordered = sorted(samples)
    if not ordered:
        raise RuntimeError('Fixture did not execute OCR; no benchmark evidence')
    return {'ocr_scans': len(samples), 'ocr_seconds': samples,
            'ocr_p50_seconds': statistics.median(samples),
            'ocr_p95_seconds': ordered[math.ceil(len(ordered) * .95) - 1],
            'pipeline_seconds': time.perf_counter() - started,
            'cases': len(result['cases']),
            'renders': sum(len(case['renders']) for case in result['cases']),
            'environment': result['environment'],
            'note': 'Small frozen sample; detector timings use the harness-warmed model. '
                    'Pipeline includes model startup, normalization and offline renders; '
                    'excludes provider/network latency.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=Path('/fixtures'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    evidence = benchmark(args.fixtures)
    args.output.write_text(json.dumps(evidence, indent=2, allow_nan=False) + '\n')
    print(json.dumps({key: value for key, value in evidence.items()
                      if key not in {'ocr_seconds', 'environment'}}))
