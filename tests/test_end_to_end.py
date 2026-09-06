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
    assert {
        key: value
        for key, value in payload["review_result"].items()
        if key
        not in {
            "file_requirement_reviews",
            "file_requirement_stats",
            "file_requirement_original_file",
        }
    } == {
        "mode": "template_text",
        "template_text_reviews": [],
        "navigation_exclusions": {
            "tender_templates": [],
            "bid_modules": [],
        },
        "stats": {
            "template_count": 0,
            "participating_template_count": 0,
            "navigation_excluded_template_count": 0,
            "navigation_excluded_bid_section_count": 0,
            "matched_template_count": 0,
            "code_candidate_count": 0,
            "selected_template_count": 0,
            "semantic_matched_count": 0,
            "semantic_mismatched_count": 0,
            "semantic_uncertain_count": 0,
            "no_bid_candidate_template_ids": [],
            "candidate_without_reliable_bid_text_template_ids": [],
            "max_concurrency": 3,
            "llm_total_calls": 0,
            "llm_completed_calls": 0,
            "llm_failed_count": 0,
            "llm_failed_calls": 0,
            "pass_count": 0,
            "fail_count": 0,
            "uncertain_count": 0,
            "not_applicable_count": 0,
            "business_status_counts": {
                "pass": 0,
                "fail": 0,
                "uncertain": 0,
                "not_applicable": 0,
            },
            "exception_review_candidate_count": 0,
            "exception_review_call_count": 0,
            "exception_review_confirm_count": 0,
            "exception_review_revise_count": 0,
            "exception_review_failed_count": 0,
            "exception_review_llm_elapsed_ms": 0,
            "exception_review_wall_elapsed_ms": 0,
            "main_wall_elapsed_ms": 0,
            "total_llm_elapsed_ms": 0,
            "total_llm_calls": 0,
            "total_wall_elapsed_ms": 0,
            "llm_elapsed_ms": 0,
            "total_elapsed_ms": 0,
        },
    }
    assert payload["review_result"]["file_requirement_reviews"] == []
    assert payload["review_result"]["file_requirement_stats"]["requirement_count"] == 0
    assert payload["review_result"]["file_requirement_original_file"]["filename"] == "投标文件.docx"

    page_response = client.get(f"/bid-check/tasks/{task_id}")
    assert page_response.status_code == 200
    assert "以下按模板规范、附件、业绩合同和文件自身四个检查范围展示合规性结果。" in page_response.text
    assert "模板规范检查" in page_response.text
    assert "未发现需处理的模板规范问题" in page_response.text
    assert "不判断签字、盖章、图片、附件真实性或外部状态" not in page_response.text
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
    assert payload["review_result"]["mode"] == "template_text"
    assert payload["review_result"]["template_text_reviews"] == []
    assert payload["review_result"]["stats"]["template_count"] == 2
    assert payload["review_result"]["stats"]["matched_template_count"] == 0
    assert payload["review_result"]["stats"]["llm_total_calls"] == 0
    artifact_dir = settings.tasks_dir / task_id / "compliance_extraction"
    assert (artifact_dir / "summary.json").is_file()
    assert (artifact_dir / "execution.jsonl").is_file()
