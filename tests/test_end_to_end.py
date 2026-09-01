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
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
        "<w:r><w:t>投标函</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>投标人名称：____</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>法定代表人应签字并加盖公章。</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
        "<w:r><w:t>法定代表人身份证明</w:t></w:r></w:p>"
        '<w:p><w:r><w:t>姓名：____，身份证明附国徽面和人像面。</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
        "<w:r><w:t>投标人须知前附表</w:t></w:r></w:p>"
        '<w:p><w:r><w:t>投标有效期 | 90 天</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
        "<w:r><w:t>投标人资格要求</w:t></w:r></w:p>"
        '<w:p><w:r><w:t>须随投标文件提供营业执照或事业单位法人证书。</w:t></w:r></w:p>'
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


def test_upload_to_completed_tender_objects_result(client):
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
    assert payload["templates"] == []
    assert payload["project_requirements"] == []
    assert payload["supplemental_materials"] == []
    assert payload["bid_parse"]["document_name"] == "投标文件.docx"
    assert payload["review_result"] == {
        "mode": "mock",
        "message": "当前版本尚未执行真实合规性检查",
    }

    page_response = client.get(f"/bid-check/tasks/{task_id}")
    assert page_response.status_code == 200
    assert "本次识别 0 个模板、0 条项目专用编制要求和 0 项补充证明材料" in page_response.text
    assert "尚未执行真实投标文件内容校验" in page_response.text
    assert "检查通过" not in page_response.text
    assert "检查不通过" not in page_response.text


def test_upload_valid_tender_extracts_tender_objects_from_document_text(
    client,
    settings,
):
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
    task_id = create_response.json()["task_id"]
    payload = client.get(f"/api/bid-check/tasks/{task_id}").json()

    assert payload["status"] == "complete"
    assert [template["name"] for template in payload["templates"]] == [
        "投标函",
        "法定代表人身份证明",
    ]
    source_text = "\n".join(
        template["source"]["source_text"] for template in payload["templates"]
    )
    assert "投标人名称：____" in source_text
    assert "法定代表人应签字并加盖公章" in source_text
    assert "商务评分" not in source_text
    assert payload["project_requirements"][0]["value"] == "90 天"
    assert payload["supplemental_materials"][0]["name"] == "营业执照"
    artifact_dir = settings.tasks_dir / task_id / "compliance_extraction"
    assert (artifact_dir / "summary.json").is_file()
    assert (artifact_dir / "execution.jsonl").is_file()
