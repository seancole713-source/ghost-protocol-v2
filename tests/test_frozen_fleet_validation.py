import hashlib
import json

import pytest

from scripts.validate_frozen_fleet import family_lower_bound, load_snapshot


def test_family_correction_never_improves_bound():
    assert family_lower_bound(90, 100, 214) < family_lower_bound(90, 100, 1)


@pytest.mark.parametrize("counts", [(True, 10, 1), (11, 10, 1), (0, 0, 1), (5, 10, 0)])
def test_invalid_evidence_rejected(counts):
    with pytest.raises(ValueError):
        family_lower_bound(*counts)


def test_snapshot_is_complete_hash_bound_and_family_fixed(tmp_path):
    bars = tmp_path / "bars.json"
    bars.write_text('{"AAA": []}')
    spec = {"snapshot_status": "COMPLETE", "snapshot_sha256": hashlib.sha256(bars.read_bytes()).hexdigest(),
            "universe": ["AAA"], "directions": ["UP", "DOWN"], "family_size": 2}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(spec))
    assert load_snapshot(manifest, bars)[1] == {"AAA": []}
    spec["family_size"] = 1
    manifest.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="Family size"):
        load_snapshot(manifest, bars)
    spec["family_size"] = 2
    manifest.write_text(json.dumps(spec))
    bars.write_text('{"AAA": [{"close": 999}]}')
    with pytest.raises(ValueError, match="hash mismatch"):
        load_snapshot(manifest, bars)
