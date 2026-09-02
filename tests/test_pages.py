import json
from pathlib import Path


def test_root_redirects_to_bid_check(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/bid-check"


def test_bid_check_page_has_two_docx_uploads_and_official_modes(client):
    response = client.get("/bid-check")

    assert response.status_code == 200
    assert 'name="tender_file"' in response.text
    assert 'name="bid_file"' in response.text
    assert response.text.count('accept=".docx') == 2
    assert "标书合规性校验" in response.text
    assert "评标规则校验" in response.text
    assert "全面校验" in response.text
    assert response.text.count("开发中") >= 2
    assert "评分+废标检查" not in response.text
    assert 'id="start-check"' in response.text
    assert 'id="start-check" class="button primary" type="submit" disabled' in response.text


def test_bid_check_page_contains_full_mode_descriptions(client):
    response = client.get("/bid-check")

    assert "检查模板填写、必填字段、附件完整性" in response.text
    assert "签字盖章、日期及材料完整性等问题" in response.text
    assert "评分项与否决投标风险" in response.text
    assert "根据招标文件中的评分办法、初步评审标准" in response.text
    assert "同时执行标书合规性校验和评标规则校验" in response.text


def test_running_task_page_shows_parallel_workflow(
    client,
    repository,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "running")
    repository.update_stage(stored_task.task_id, "bid_parse", "running")

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "上传文件" in response.text
    assert "提取招标文件检查对象" in response.text
    assert "解析投标文件" in response.text
    assert "执行合规性检查" in response.text
    assert "检查对象结果" in response.text
    assert "并行执行" in response.text
    assert "data-parallel-stages" in response.text
    assert response.text.count("运行中") >= 2
    assert f'data-task-id="{stored_task.task_id}"' in response.text


def test_failed_task_page_names_failed_stage(client, repository, stored_task):
    repository.fail(
        stored_task.task_id,
        "requirements",
        "模拟招标文件检查对象提取失败",
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "失败阶段：提取招标文件检查对象" in response.text
    assert "模拟招标文件检查对象提取失败" in response.text


def test_complete_page_renders_requirements_without_fake_verdict(
    client,
    repository,
    stored_task,
    mock_complete_result,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(stored_task.task_id, mock_complete_result)

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "标书合规性校验结果" in response.text
    assert "本次识别 0 个模板、0 条项目专用编制要求和 0 项补充证明材料" in response.text
    assert "模板对比表" in response.text
    assert "没有可展示的招标模板" in response.text
    assert "当前版本仅展示招标模板与投标文件模块的匹配关系" in response.text
    assert "尚未执行真实内容对照或合规性判断" in response.text
    assert "section_count" not in response.text
    assert "章节数" in response.text
    assert "检查通过" not in response.text
    assert "检查不通过" not in response.text


def test_complete_page_renders_clean_bid_stats_from_nested_parser_result(
    client,
    repository,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(
        stored_task.task_id,
        {
            "templates": [],
            "project_requirements": [],
            "supplemental_materials": [],
            "bid_parse": {
                "status": "success",
                "document_name": "投标文件.docx",
                "stats": {
                    "section_count": 33,
                    "structured_block_count": 452,
                    "table_count": 11,
                    "image_count": 69,
                },
            },
            "review_result": {
                "mode": "mock",
                "message": "当前版本尚未执行真实合规性检查",
            },
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "<small>章节数</small><strong>33</strong>" in response.text
    assert "<small>内容块数</small><strong>452</strong>" in response.text
    assert "<small>表格数</small><strong>11</strong>" in response.text
    assert "<small>图片数</small><strong>69</strong>" in response.text
    assert "实际模块内容在对比表的对应行内折叠查看" in response.text


def test_complete_page_renders_template_text_review_results(
    client,
    repository,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(
        stored_task.task_id,
        {
            "templates": [],
            "project_requirements": [],
            "supplemental_materials": [],
            "bid_parse": {
                "status": "success",
                "stats": {
                    "section_count": 1,
                    "structured_block_count": 2,
                    "table_count": 0,
                    "image_count": 0,
                },
            },
            "review_result": {
                "mode": "template_text",
                "template_text_reviews": [
                    {
                        "template_id": "tender_template_005",
                        "template_name": "投标函",
                        "bid_module_name": "5 投标函",
                        "status": "pass",
                        "summary": "投标函文本完整响应。",
                        "issues": [],
                        "llm_elapsed_ms": 321,
                    }
                ],
                    "stats": {"llm_total_calls": 1, "llm_elapsed_ms": 321},
            },
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "已执行明确匹配模板的文本对照检查" in response.text
    assert "模板文本检查结果" in response.text
    assert "投标函文本完整响应。" in response.text
    assert "检查通过" in response.text
    assert "321 ms" in response.text


def test_complete_page_merges_template_match_and_text_review_statuses(
    client,
    repository,
    stored_task,
):
    artifact_dir = Path(stored_task.bid_file.storage_path).parent / "bid_document_cleaning"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "structured_document.json").write_text(
        json.dumps(
            {
                "sections": [
                    {
                        "section_id": "s-pass",
                        "parent_section_id": None,
                        "title": "1 投标函",
                        "path": ["1 投标函"],
                        "start_order": 1,
                        "direct_block_ids": ["b-pass"],
                    },
                    {
                        "section_id": "s-fail",
                        "parent_section_id": None,
                        "title": "2 授权委托书",
                        "path": ["2 授权委托书"],
                        "start_order": 2,
                        "direct_block_ids": ["b-fail"],
                    },
                    {
                        "section_id": "s-call-failed",
                        "parent_section_id": None,
                        "title": "3 失败模板",
                        "path": ["3 失败模板"],
                        "start_order": 3,
                        "direct_block_ids": ["b-call-failed"],
                    },
                    {
                        "section_id": "s-candidate",
                        "parent_section_id": None,
                        "title": "4 候选模板说明",
                        "path": ["4 候选模板说明"],
                        "start_order": 4,
                        "direct_block_ids": ["b-candidate"],
                    },
                ],
                "blocks": [
                    {"block_id": "b-pass", "type": "paragraph", "text": "投标函内容"},
                    {"block_id": "b-fail", "type": "paragraph", "text": "授权委托书内容"},
                    {"block_id": "b-call-failed", "type": "paragraph", "text": "失败模板内容"},
                    {"block_id": "b-candidate", "type": "paragraph", "text": "候选内容"},
                ],
                "tables": [],
                "images": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(
        stored_task.task_id,
        {
            "templates": [
                {"id": "tpl-pass", "name": "投标函", "section": "格式", "body": "投标函", "source": {"source_text": "投标函"}},
                {"id": "tpl-fail", "name": "授权委托书", "section": "格式", "body": "授权委托书", "source": {"source_text": "授权委托书"}},
                {"id": "tpl-call-failed", "name": "失败模板", "section": "格式", "body": "失败模板", "source": {"source_text": "失败模板"}},
                {"id": "tpl-candidate", "name": "候选模板", "section": "格式", "body": "候选模板", "source": {"source_text": "候选模板"}},
                {"id": "tpl-unmatched", "name": "未匹配模板", "section": "格式", "body": "未匹配模板", "source": {"source_text": "未匹配模板"}},
            ],
            "project_requirements": [],
            "supplemental_materials": [],
            "bid_parse": {"status": "success", "stats": {"section_count": 4}},
            "review_result": {
                "mode": "template_text",
                "template_text_reviews": [
                    {
                        "template_id": "tpl-pass",
                        "template_name": "投标函",
                        "bid_module_name": "1 投标函",
                        "status": "pass",
                        "summary": "文本完整。",
                        "issues": [],
                        "llm_elapsed_ms": 10,
                    },
                    {
                        "template_id": "tpl-fail",
                        "template_name": "授权委托书",
                        "bid_module_name": "2 授权委托书",
                        "status": "fail",
                        "summary": "存在固定正文缺失。",
                        "issues": [
                            {
                                "type": "missing_content",
                                "requirement": "保留固定正文。",
                                "actual": "正文缺失。",
                                "reason": "模板正文未保留。",
                            }
                        ],
                        "llm_elapsed_ms": 20,
                    },
                    {
                        "template_id": "tpl-call-failed",
                        "template_name": "失败模板",
                        "bid_module_name": "3 失败模板",
                        "status": "uncertain",
                        "execution_status": "failed",
                        "summary": "模板文本检查调用失败，未形成业务检查结论。",
                        "issues": [],
                        "error_type": "RuntimeError",
                        "error_message": "模拟请求失败",
                        "llm_elapsed_ms": 30,
                    },
                ],
                "stats": {
                    "template_count": 5,
                    "matched_template_count": 3,
                    "llm_total_calls": 3,
                    "llm_failed_count": 1,
                    "llm_elapsed_ms": 60,
                },
            },
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "招标模板" in response.text
    assert "对应投标文件模块" in response.text
    assert "匹配状态" in response.text
    assert "文本检查状态" in response.text
    assert "检查通过" in response.text
    assert "检查不通过" in response.text
    assert "调用失败" in response.text
    assert response.text.count("未执行") >= 2
    assert "存在候选但不能确定" in response.text
    assert "未匹配" in response.text
    assert "模板正文未保留。" in response.text
    assert "模拟请求失败" in response.text


def test_complete_page_includes_contract_style_back_to_top_control(
    client,
    repository,
    stored_task,
    mock_complete_result,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(stored_task.task_id, mock_complete_result)

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert 'id="back-to-top"' in response.text
    assert 'aria-label="回到顶部"' in response.text
    assert "position: fixed" in response.text
    assert "window.scrollTo" in response.text
    assert "prefers-reduced-motion" in response.text


def test_complete_page_renders_template_comparison_rows_collapsed_by_default(
    client,
    repository,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(
        stored_task.task_id,
        {
            "templates": [
                {
                    "name": "投标函",
                    "section": "投标文件格式",
                    "body": "投标人名称：____",
                    "fields": [],
                    "attachments": [],
                    "source": {"block_ids": ["b1"]},
                }
            ],
            "project_requirements": [
                {
                    "requirement": "投标有效期",
                    "value": "90天",
                    "source": {"section": "投标人须知", "source_text": "90天"},
                }
            ],
            "supplemental_materials": [
                {
                    "name": "营业执照",
                    "material": "提供扫描件",
                    "source": {"section": "资格要求", "source_text": "提供扫描件"},
                }
            ],
            "bid_parse": {
                "status": "success",
                "stats": {
                    "section_count": 1,
                    "structured_block_count": 1,
                    "table_count": 0,
                    "image_count": 0,
                },
            },
            "review_result": {"mode": "mock", "message": "未执行"},
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert response.text.count('class="comparison-detail"') == 1
    assert "<details open" not in response.text
    assert "模板对比表" in response.text
    assert "投标函" in response.text
    assert "存在候选但不能确定" not in response.text
    assert "未匹配" in response.text


def test_complete_page_renders_collapsed_bid_document_sections(
    client,
    repository,
    stored_task,
):
    artifact_dir = Path(stored_task.bid_file.storage_path).parent / "bid_document_cleaning"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "structured_document.json").write_text(
        json.dumps(
            {
                "source": {"filename": "投标文件.docx"},
                "stats": {
                    "section_count": 1,
                    "block_count": 3,
                    "table_count": 1,
                    "image_count": 0,
                },
                "sections": [
                    {
                        "section_id": "s0001",
                        "parent_section_id": None,
                        "level": 1,
                        "title": "1 投标函",
                        "path": ["1 投标函"],
                        "start_order": 1,
                        "end_order": 3,
                        "block_ids": ["b1", "b2", "b3"],
                        "direct_block_ids": ["b1", "b2", "b3"],
                    },
                    {
                        "section_id": "s0002",
                        "parent_section_id": "s0001",
                        "level": 2,
                        "title": "1.1 投标函附表",
                        "path": ["1 投标函", "1.1 投标函附表"],
                        "start_order": 4,
                        "end_order": 4,
                        "block_ids": ["b4"],
                        "direct_block_ids": ["b4"],
                    }
                ],
                "blocks": [
                    {"block_id": "b1", "type": "heading", "text": "1 投标函", "order": 1},
                    {"block_id": "b2", "type": "paragraph", "text": "投标人名称：示例公司", "order": 2},
                    {"block_id": "b3", "type": "table", "text": "表格", "order": 3},
                    {"block_id": "b4", "type": "paragraph", "text": "附表内容", "order": 4},
                ],
                "tables": [
                    {
                        "block_id": "b3",
                        "rows": [["字段", "内容"], ["联系人", "张三"]],
                    }
                ],
                "images": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(
        stored_task.task_id,
        {
            "templates": [
                {
                    "name": "投标函",
                    "section": "投标文件格式",
                    "body": "投标函\n投标人名称：____",
                    "source": {"source_text": "投标函模板原文"},
                }
            ],
            "project_requirements": [],
            "supplemental_materials": [],
            "bid_parse": {
                "status": "success",
                "document_name": "投标文件.docx",
                "stats": {
                    "section_count": 1,
                    "structured_block_count": 3,
                    "table_count": 1,
                    "image_count": 0,
                },
            },
            "review_result": {"mode": "mock", "message": "未执行"},
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "模板对比表" in response.text
    assert 'data-comparison-status="matched"' in response.text
    assert "1 投标函" in response.text
    assert "查看招标模板原文" in response.text
    assert "查看投标模块内容" in response.text
    assert "投标函模板原文" in response.text
    assert "投标函\n投标人名称：____" in response.text
    assert "投标人名称：示例公司" in response.text
    assert "联系人" in response.text
    assert "张三" in response.text
    assert "4 个内容块" in response.text
    assert "1 个表格" in response.text
    assert "0 张图片" in response.text
    assert "1 个子章节" in response.text
    assert "1.1 投标函附表" in response.text
    assert "附表内容" in response.text


def test_unknown_task_page_returns_404(client):
    response = client.get("/bid-check/tasks/not-found")

    assert response.status_code == 404


def test_complete_page_renders_generic_attachment_status_and_evidence_count(
    client,
    repository,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(
        stored_task.task_id,
        {
            "templates": [
                {
                    "id": "tpl-license",
                    "name": "营业执照材料",
                    "section": "投标文件格式",
                    "body": "应提供营业执照复印件。",
                    "source": {"source_text": "应提供营业执照复印件。"},
                    "attachments": [],
                },
                {
                    "id": "tpl-bid-letter",
                    "name": "投标函",
                    "section": "投标文件格式",
                    "body": "本页填写投标函正文。",
                    "source": {"source_text": "本页填写投标函正文。"},
                    "attachments": [],
                },
            ],
            "project_requirements": [],
            "supplemental_materials": [],
            "bid_parse": {
                "status": "success",
                "stats": {
                    "section_count": 2,
                    "structured_block_count": 2,
                    "table_count": 0,
                    "image_count": 0,
                },
            },
            "review_result": {
                "mode": "template_text_and_attachments",
                "template_text_reviews": [],
                "attachment_reviews": [
                    {
                        "template_id": "tpl-license",
                        "template_name": "营业执照材料",
                        "bid_module_name": "4 营业执照材料",
                        "status": "fail",
                        "summary": "营业执照材料缺失。",
                        "image_ids": [],
                        "materials": [],
                        "requirements": [
                            {
                                "requirement": "应提供营业执照复印件。",
                                "status": "fail",
                                "evidence_image_ids": [],
                                "reason": "当前没有关联证明图片。",
                            }
                        ],
                        "llm_elapsed_ms": 20,
                    }
                ],
                "attachment_stats": {
                    "selected_template_count": 1,
                    "llm_total_calls": 1,
                    "llm_elapsed_ms": 20,
                },
                "stats": {"llm_total_calls": 0, "llm_elapsed_ms": 0},
            },
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "营业执照材料" in response.text
    assert "检查不通过" in response.text
    assert "证据图片：0 张" in response.text
    assert "仅对已匹配且明确存在普通证明材料要求的模板执行附件检查" in response.text
    assert "只检查法定代表人/负责人身份证明、授权委托书和基本开户银行情况" not in response.text
    assert "调用失败" not in response.text
