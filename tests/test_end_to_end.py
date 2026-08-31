DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


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
