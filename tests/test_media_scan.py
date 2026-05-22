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
