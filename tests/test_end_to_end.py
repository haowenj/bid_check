from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def real_tender_docx() -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>第六章 投标文件格式</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>投标人名称：____</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>法定代表人应签字并加盖公章。</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>商务评分满分 20 分。</w:t></w:r></w:p>"
        "</w:body></w:document>"
    ).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def docx_files():
    return {
        "tender_file": ("招标文件.docx", b"PK\x03\x04tender", DOCX_MIME),
        "bid_file": ("投标文件.docx", b"PK\x03\x04bid", DOCX_MIME),
    }


def test_upload_to_completed_requirements_result(client):
    create_response = client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": "compliance"},
    )
    assert create_response.status_code == 202
    task_id = create_response.json()["task_id"]

    task_response = client.get(f"/api/bid-check/tasks/{task_id}")
    payload = task_response.json()
    assert payload["status"] == "complete"
    assert payload["requirements_status"] == "complete"
    assert payload["bid_parse_status"] == "complete"
    assert payload["review_status"] == "complete"
    assert len(payload["requirements"]) == 5
    assert payload["bid_parse"]["document_name"] == "投标文件.docx"
    assert payload["review_result"] == {
        "mode": "mock",
        "message": "当前版本尚未执行真实合规性检查",
    }

    page_response = client.get(f"/bid-check/tasks/{task_id}")
    assert page_response.status_code == 200
    assert "本次共提取 5 项合规性检查要求" in page_response.text
    assert "尚未执行真实投标文件内容校验" in page_response.text
    assert "检查通过" not in page_response.text
    assert "检查不通过" not in page_response.text


def test_upload_valid_tender_extracts_requirements_from_document_text(client):
    files = {
        "tender_file": ("真实招标文件.docx", real_tender_docx(), DOCX_MIME),
        "bid_file": ("投标文件.docx", b"PK\\x03\\x04bid", DOCX_MIME),
    }
    create_response = client.post(
        "/api/bid-check/tasks",
        files=files,
        data={"check_mode": "compliance"},
    )
    assert create_response.status_code == 202
    payload = client.get(
        f"/api/bid-check/tasks/{create_response.json()['task_id']}"
    ).json()

    assert payload["status"] == "complete"
    assert payload["requirements"]
    source_text = "\n".join(
        item["source"]["source_text"] for item in payload["requirements"]
    )
    assert "投标人名称：____" in source_text
    assert "法定代表人应签字并加盖公章" in source_text
    assert "商务评分" not in source_text
