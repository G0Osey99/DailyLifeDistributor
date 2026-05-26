"""/media/scan: filename→date matching + attached sheet metadata."""
import io

import openpyxl
import pytest

from core import auth, media_session as ms


@pytest.fixture()
def client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "pw"})
        yield c


def test_scan_groups_by_date_and_category(client):
    resp = client.post("/media/scan", json={"categories": {
        "youtube_video": ["vid_250521.mp4", "vid_250522.mp4", ".DS_Store"],
        "podcast": ["ep_250521.mp3"],
    }})
    assert resp.status_code == 200
    dates = resp.get_json()["dates"]
    assert dates["2025-05-21"]["categories"]["youtube_video"] == ["vid_250521.mp4"]
    assert dates["2025-05-21"]["categories"]["podcast"] == ["ep_250521.mp3"]
    assert dates["2025-05-22"]["categories"]["youtube_video"] == ["vid_250522.mp4"]
    # junk ignored
    assert ".DS_Store" not in str(dates)


def test_scan_attaches_sheet_metadata(client):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["Date", "Title", "Transcript"])
    ws.append(["2025-05-21", "Gratitude", "We talk about gratitude."])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    client.post("/media/spreadsheet", data={"file": (buf, "plan.xlsx")},
                content_type="multipart/form-data")
    client.post("/media/mapping", json={
        "sheet_name": "Plan", "date_column": "Date",
        "youtube_title_column": "Title", "transcript_column": "Transcript",
    })

    resp = client.post("/media/scan", json={"categories": {
        "youtube_video": ["vid_250521.mp4"],
    }})
    meta = resp.get_json()["dates"]["2025-05-21"]["metadata"]
    assert meta["youtube_title"] == "Gratitude"
    assert meta["transcript"] == "We talk about gratitude."


def test_scan_filters_to_sheet_dates_when_sheet_loaded(client):
    """When a spreadsheet is loaded with a date column, scan results
    are filtered to dates in the sheet. Eliminates noise from old
    archived files whose YYMMDD parses to a real-but-irrelevant year
    (the user reported 2022/23/24/25-06-XX entries showing up next
    to their 2026 schedule) and drops the DDMMYY-ambiguity arm of
    6-digit filenames when only one interpretation is in the sheet."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["Date"])
    ws.append(["2026-06-01"])
    ws.append(["2026-06-02"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    client.post("/media/spreadsheet", data={"file": (buf, "plan.xlsx")},
                content_type="multipart/form-data")
    client.post("/media/mapping", json={
        "sheet_name": "Plan", "date_column": "Date",
    })

    # Mix of files: some that parse to sheet dates, some that don't.
    resp = client.post("/media/scan", json={"categories": {
        "youtube_video": [
            "youtube 260601.mp4",   # → 2026-06-01 (in sheet)
            "youtube 260602.mp4",   # → 2026-06-02 (in sheet)
            "old 220601.mp4",       # → 2022-06-01 (NOT in sheet)
            "old 230626.mp4",       # → 2023-06-26 (NOT in sheet)
        ],
        "thumbnails": ["0601.jpg", "602.jpg"],  # MMDD + MDD, both → June
    }})
    dates = resp.get_json()["dates"]
    # Sheet dates are present.
    assert "2026-06-01" in dates
    assert "2026-06-02" in dates
    # Old archive dates are filtered out.
    assert "2022-06-01" not in dates
    assert "2023-06-26" not in dates
    # And the missing-leading-zero thumbnail (`602.jpg`) shows up under
    # June 2 — proves the 3-digit MDD parser is wired AND the filter
    # keeps it because June 2 is in the sheet.
    assert "602.jpg" in dates["2026-06-02"]["categories"]["thumbnails"]
    assert "0601.jpg" in dates["2026-06-01"]["categories"]["thumbnails"]


def test_scan_without_sheet_returns_full_parse(client):
    """No spreadsheet loaded → no filtering. Manual workflows where
    filenames are the source of truth keep working."""
    resp = client.post("/media/scan", json={"categories": {
        "youtube_video": ["vid_220601.mp4", "vid_260601.mp4"],
    }})
    dates = resp.get_json()["dates"]
    # Both parsed dates returned (no sheet to filter against).
    assert "2022-06-01" in dates
    assert "2026-06-01" in dates
