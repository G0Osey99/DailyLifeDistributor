import os
from agent import scan


def _touch(path):
    with open(path, "w") as f:
        f.write("x")


def test_scan_groups_files_by_date_and_category(tmp_path):
    vids = tmp_path / "vids"; shorts = tmp_path / "shorts"
    vids.mkdir(); shorts.mkdir()
    _touch(vids / "260115_sermon.mp4")
    _touch(vids / "260116_sermon.mp4")
    _touch(shorts / "260115_short.mp4")
    _touch(vids / "notes.txt")
    _touch(vids / "no_date_here.mp4")

    report = scan.scan_roots({"video": str(vids), "shorts": str(shorts)})

    assert report["dates"] == ["2026-01-15", "2026-01-16"]
    assert report["by_date"]["2026-01-15"] == {
        "video": ["260115_sermon.mp4"], "shorts": ["260115_short.mp4"]}
    assert report["by_date"]["2026-01-16"] == {"video": ["260116_sermon.mp4"]}
    assert report["errors"] == {}


def test_scan_reports_missing_dir_as_error(tmp_path):
    report = scan.scan_roots({"video": str(tmp_path / "does_not_exist")})
    assert report["dates"] == []
    assert report["by_date"] == {}
    assert "video" in report["errors"]


def test_scan_empty_roots():
    report = scan.scan_roots({})
    assert report == {"by_date": {}, "dates": [], "errors": {}}
