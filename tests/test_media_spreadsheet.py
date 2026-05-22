"""Spreadsheet upload + session column-mapping round-trip."""
import io

import openpyxl
import pytest

from core import auth


@pytest.fixture()
def client(temp_db, monkeypatch, tmp_path):
    # Keep cached spreadsheets off the real data volume.
    from core import media_session as ms
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "correct-horse"})
        yield c


def _xlsx_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["Date", "Title", "Transcript"])
    ws.append(["2025-05-21", "Hello", "A transcript"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_spreadsheet_upload_returns_sheets(client):
    resp = client.post(
        "/media/spreadsheet",
        data={"file": (_xlsx_bytes(), "plan.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert "Plan" in resp.get_json()["sheets"]


def test_spreadsheet_columns(client):
    client.post(
        "/media/spreadsheet",
        data={"file": (_xlsx_bytes(), "plan.xlsx")},
        content_type="multipart/form-data",
    )
    resp = client.get("/media/spreadsheet/columns?sheet=Plan")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["columns"] == ["Date", "Title", "Transcript"]
    # Preview surfaces the first data row keyed by column so the user can
    # tell which column holds what before mapping.
    assert data["preview"] == [
        {"Date": "2025-05-21", "Title": "Hello", "Transcript": "A transcript"}
    ]


def test_mapping_roundtrip(client):
    mapping = {"sheet_name": "Plan", "date_column": "Date", "transcript_column": "Transcript"}
    resp = client.post("/media/mapping", json=mapping)
    assert resp.status_code == 200
    assert resp.get_json()["mapping"] == mapping
    got = client.get("/media/mapping")
    assert got.get_json()["mapping"] == mapping


def test_non_xlsx_rejected(client):
    resp = client.post(
        "/media/spreadsheet",
        data={"file": (io.BytesIO(b"not a spreadsheet"), "junk.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_missing_file_rejected(client):
    resp = client.post("/media/spreadsheet", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
