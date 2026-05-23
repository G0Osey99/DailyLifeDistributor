# agent/tests/test_db_shim.py
import pytest
from agent import db_shim

def test_record_image_use_emits_image_used():
    emitted = []
    shim = db_shim.Shim(emit=emitted.append)
    shim.record_image_use(photo_id="p1", source="unsplash", topic="joy",
                          used_on_date="2026-05-22",
                          photographer="Jane", photo_url="https://u/p1")
    assert emitted == [{
        "type": "image_used",
        "photo_id": "p1", "source": "unsplash", "topic": "joy",
        "used_on_date": "2026-05-22",
        "photographer": "Jane", "photo_url": "https://u/p1",
    }]

def test_any_other_attr_raises_not_implemented():
    shim = db_shim.Shim(emit=lambda _f: None)
    with pytest.raises(NotImplementedError) as e:
        shim.has_successful_upload("S1", "2026-05-22", "Rock")
    assert "agent does not implement" in str(e.value)
