"""Transcript-column extraction via the arbitrary-path parse entrypoint."""
import openpyxl
from core.excel_parser import parse_spreadsheet


def test_transcript_column_extracted(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Transcript"])
    ws.append(["2025-05-21", "Today we talk about gratitude."])
    p = tmp_path / "sheet.xlsx"
    wb.save(p)
    mapping = {
        "sheet_name": ws.title,
        "date_column": "Date",
        "transcript_column": "Transcript",
    }
    rows = parse_spreadsheet(str(p), mapping)
    assert rows["2025-05-21"]["transcript"] == "Today we talk about gratitude."


def test_parse_spreadsheet_maps_title_and_description(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Title", "Desc"])
    ws.append(["2025-05-22", "My Title", "My description"])
    p = tmp_path / "sheet.xlsx"
    wb.save(p)
    mapping = {
        "sheet_name": ws.title,
        "date_column": "Date",
        "youtube_title_column": "Title",
        "description_column": "Desc",
    }
    rows = parse_spreadsheet(str(p), mapping)
    assert rows["2025-05-22"]["youtube_title"] == "My Title"
    assert rows["2025-05-22"]["description"] == "My description"
    # Unmapped transcript stays empty rather than raising.
    assert rows["2025-05-22"]["transcript"] == ""
