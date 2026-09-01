from app.mock_services import (
    extract_tender_objects,
    parse_bid_document,
    run_compliance_review,
)
from app.models import FileMetadata


def test_extract_returns_three_tender_object_collections(tmp_path):
    tender_path = tmp_path / "tender.docx"
    tender_path.write_bytes(b"docx")
    tender_file = FileMetadata("招标文件.docx", 4, str(tender_path))

    result = extract_tender_objects(
        tender_file,
        delay_seconds=0,
    )

    assert set(result) == {
        "templates",
        "project_requirements",
        "supplemental_materials",
    }
    assert result == {"templates": [], "project_requirements": [], "supplemental_materials": []}


def test_parse_returns_original_document_name_and_mock_counts(tmp_path):
    bid_path = tmp_path / "bid.docx"
    bid_path.write_bytes(b"docx")
    bid_file = FileMetadata("投标文件.docx", 4, str(bid_path))

    result = parse_bid_document(bid_file, delay_seconds=0)

    assert result == {
        "status": "success",
        "document_name": "投标文件.docx",
        "section_count": 22,
        "block_count": 405,
        "table_count": 11,
        "image_count": 70,
    }


def test_review_returns_disclaimer_without_fake_judgement():
    result = run_compliance_review(
        {"templates": [], "project_requirements": [], "supplemental_materials": []},
        {"status": "success"},
    )

    assert result["mode"] == "mock"
    assert result["message"] == "当前版本尚未执行真实合规性检查"
    assert "passed" not in result
    assert "risk" not in result


def test_review_accepts_zero_extracted_requirements_without_fake_judgement():
    result = run_compliance_review(
        {"templates": [], "project_requirements": [], "supplemental_materials": []},
        {"status": "success"},
    )

    assert result["mode"] == "mock"
    assert "passed" not in result
