"""A matching JSON report alone is insufficient architecture evidence."""
import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest


def test_comparison_checks_actual_bytes_and_selection(tmp_path):
    path = Path(__file__).parents[1] / 'deploy/oracle/compare-golden.py'
    spec = importlib.util.spec_from_file_location('compare_golden', path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    roots = [tmp_path / 'arm', tmp_path / 'x86']
    data = b'fixture bytes'
    sha = hashlib.sha256(data).hexdigest()
    cases = []
    for media in sorted(script.MEDIA_IDS):
        renders = [{'preset_ref': ref, 'evidence_file': f'{media}-{slot}.webp',
                    'byte_size': len(data), 'content_sha256': sha}
                   for slot, ref in enumerate(script.PRESETS)]
        cases.append({'media_id': media, 'normalized_artifacts': [], 'renders': renders})
    for root in roots:
        root.mkdir()
        for case in cases:
            for item in case['renders']:
                (root / item['evidence_file']).write_bytes(data)
    arm = {'presets': script.PRESETS, 'cases': cases,
           'environment': {'architecture': 'aarch64'},
           'renderer_revision': 'a' * 64, 'source_recipe_revision': 'b' * 64}
    x86 = copy.deepcopy(arm)
    x86['environment']['architecture'] = 'x86_64'
    assert script.compare(arm, x86, *roots)['verified_files'] == [12, 12]
    x86['cases'][0]['selection'] = 'different artwork'
    with pytest.raises(ValueError, match='projections differ'):
        script.compare(arm, x86, *roots)
    del x86['cases'][0]['selection']
    damaged = roots[1] / cases[0]['renders'][0]['evidence_file']
    damaged.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='digest/size mismatch'):
        script.compare(arm, x86, *roots)
