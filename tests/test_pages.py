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
    assert 'href="/bid-check/tasks"' in response.text


def test_bid_check_page_contains_full_mode_descriptions(client):
    response = client.get("/bid-check")

    assert "检查模板填写、必填字段、附件完整性" in response.text
    assert "签字盖章、日期及材料完整性等问题" in response.text
    assert "评分项与否决投标风险" in response.text
    assert "根据招标文件中的评分办法、初步评审标准" in response.text
    assert "同时执行标书合规性校验和评标规则校验" in response.text


def test_task_list_page_shows_tasks_and_links_to_results(
    client,
    repository,
    stored_task,
    mock_complete_result,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(stored_task.task_id, mock_complete_result)

    response = client.get("/bid-check/tasks")

    assert response.status_code == 200
    assert "任务列表" in response.text
    assert "投标文件.docx" in response.text
    assert "已完成" in response.text
    assert "模板规范" in response.text
    assert "附件" in response.text
    assert "业绩合同" in response.text
    assert 'href="/bid-check/tasks/stored-task"' in response.text
    assert "查看检查结果" in response.text
    assert 'data-delete-task="stored-task"' in response.text
    assert 'data-delete-dialog' in response.text
    assert "确认删除任务" in response.text


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
    assert "以下按模板规范、附件、业绩合同和文件自身四个检查范围展示合规性结果。" in response.text
    assert "模板规范检查" in response.text
    assert "附件检查" in response.text
    assert "业绩合同检查" in response.text
    assert "未发现需处理的模板规范问题" in response.text
    assert "未发现需处理的附件问题" in response.text
    assert "未发现需处理的业绩合同问题" in response.text
    assert "最终检查结果" in response.text
    assert "section_count" not in response.text
    assert "解析摘要" not in response.text
    assert "检查通过" not in response.text
    assert "检查不通过" not in response.text


def test_complete_page_reads_and_renders_file_requirement_artifact(
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
            "bid_parse": {"status": "success"},
            "review_result": {"mode": "template_text", "template_text_reviews": []},
        },
    )
    artifact_dir = Path(stored_task.tender_file.storage_path).parent / "compliance_extraction"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "10_file_requirement_reviews.json").write_text(
        json.dumps(
            {
                "source": "original_uploaded_file",
                "original_file": {
                    "filename": "投标文件.docx",
                    "extension": ".docx",
                    "size_bytes": 3,
                    "size_display": "3 B",
                },
                "requirements": [
                    {
                        "name": "文件大小限制",
                        "requirement": "电子投标文件不得超过 2B",
                        "requirement_type": "size",
                        "actual": {"size_bytes": 3, "size_display": "3 B"},
                        "expected": {"max_bytes": 2, "max_display": "2 B"},
                        "status": "fail",
                        "status_label": "不合规",
                        "message": "原始文件大小超过要求。",
                        "source": {
                            "section": "投标人须知前附表",
                            "block_ids": ["t1"],
                            "source_text": "电子投标文件不得超过 2B",
                        },
                    }
                ],
                "stats": {"requirement_count": 1, "fail_count": 1, "not_supported_count": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "文件自身检查" in response.text
    assert "电子投标文件不得超过 2B" in response.text
    assert "原始文件大小为" in response.text or "原始文件大小超过要求" in response.text
    assert "不合规" in response.text
    assert "投标人须知前附表" in response.text

    list_response = client.get("/bid-check/tasks")
    assert list_response.status_code == 200
    assert "文件自身" in list_response.text


def test_complete_page_offers_three_switchable_result_styles(
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
    assert 'data-result-style-switcher' in response.text
    assert 'A · 审阅工作台' in response.text
    assert 'B · 风险驾驶舱' in response.text
    assert 'C · 结论报告页' in response.text
    assert 'data-result-style="workbench"' in response.text
    assert 'data-result-style="dashboard"' in response.text
    assert 'data-result-style="report"' in response.text
    assert 'data-result-style-view="workbench"' in response.text
    assert 'data-result-style-view="dashboard"' in response.text
    assert 'data-result-style-view="report"' in response.text
    assert 'localStorage' in response.text


def test_complete_page_hides_debug_bid_parse_stats(
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
    assert "章节数" not in response.text
    assert "内容块数" not in response.text
    assert "表格数" not in response.text
    assert "图片数" not in response.text
    assert "实际模块内容在对比表的对应行内折叠查看" not in response.text


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
    assert "模板规范检查" in response.text
    assert "未发现需处理的模板规范问题" in response.text
    assert "投标函文本完整响应。" not in response.text
    assert "检查通过" not in response.text
    assert "321 ms" not in response.text


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
    assert "模板规范检查" in response.text
    assert "对应模块：2 授权委托书" in response.text
    assert "检查通过" not in response.text
    assert "检查不通过" in response.text
    assert "调用失败" in response.text
    assert "对应关系待确认" in response.text
    assert "未匹配" in response.text
    assert "模板正文未保留。" in response.text
    assert "模拟请求失败" in response.text
    assert "查看招标模板原文" not in response.text
    assert "查看投标模块内容" not in response.text


def test_complete_page_separates_template_semantic_skip_from_business_fail(
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
                        "section_id": "s-semantic",
                        "parent_section_id": None,
                        "title": "1 候选模板",
                        "path": ["1 候选模板"],
                        "start_order": 1,
                        "direct_block_ids": ["b-semantic"],
                    }
                ],
                "blocks": [
                    {
                        "block_id": "b-semantic",
                        "type": "paragraph",
                        "text": "这是另一个文件用途的实际模块内容。",
                    }
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
                {
                    "id": "tpl-semantic",
                    "name": "候选模板",
                    "section": "格式",
                    "body": "候选模板正文",
                    "source": {"source_text": "候选模板正文"},
                }
            ],
            "project_requirements": [],
            "supplemental_materials": [],
            "bid_parse": {"status": "success", "stats": {"section_count": 1}},
            "review_result": {
                "mode": "template_text",
                "template_text_reviews": [
                    {
                        "template_id": "tpl-semantic",
                        "template_name": "候选模板",
                        "bid_module_name": "1 候选模板",
                        "semantic_match": {
                            "status": "mismatched",
                            "reason": "标题相似，但文件用途和核心内容不一致。",
                        },
                        "status": "uncertain",
                        "business_status": "not_run",
                        "execution_status": "semantic_skipped",
                        "summary": "候选未通过语义对应确认。",
                        "issues": [],
                        "llm_elapsed_ms": 10,
                    }
                ],
                "stats": {"llm_total_calls": 1, "llm_elapsed_ms": 10},
            },
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "语义不匹配" in response.text
    assert "标题相似，但文件用途和核心内容不一致。" in response.text
    assert "未执行业务检查" not in response.text
    assert "检查不通过" not in response.text


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


def test_complete_page_hides_template_comparison_debug_details(
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
    assert 'class="comparison-detail"' not in response.text
    assert "<details open" not in response.text
    assert "模板对比表" not in response.text
    assert "模板规范检查" in response.text
    assert "未匹配" in response.text
    assert "存在候选但不能确定" not in response.text
    assert "查看招标模板原文" not in response.text


def test_complete_page_hides_collapsed_bid_document_debug_sections(
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
    assert "模板对比表" not in response.text
    assert 'data-comparison-status="matched"' not in response.text
    assert "查看招标模板原文" not in response.text
    assert "查看投标模块内容" not in response.text
    assert "投标函模板原文" not in response.text
    assert "投标函\n投标人名称：____" not in response.text
    assert "投标人名称：示例公司" not in response.text
    assert "联系人" not in response.text
    assert "张三" not in response.text
    assert "4 个内容块" not in response.text
    assert "1 个表格" not in response.text
    assert "0 张图片" not in response.text
    assert "1 个子章节" not in response.text
    assert "1.1 投标函附表" not in response.text
    assert "附表内容" not in response.text


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
    assert "附件检查" in response.text
    assert "应提供营业执照复印件。" in response.text
    assert "只检查法定代表人/负责人身份证明、授权委托书和基本开户银行情况" not in response.text
    assert "调用失败" not in response.text


def test_complete_page_renders_performance_contract_review_results(
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
                    "section_count": 33,
                    "structured_block_count": 452,
                    "table_count": 11,
                    "image_count": 70,
                },
            },
            "review_result": {
                "mode": "template_text_and_performance",
                "template_text_reviews": [],
                "performance_reviews": [
                    {
                        "case_type": "21.1 信达旺大厦云平台运营及维护服务",
                        "template_id": "tender_template_021",
                        "template_name": "业绩情况表",
                        "bid_module_name": "21.1 信达旺大厦云平台运营及维护服务",
                        "bid_section_id": "s0027",
                        "image_ids": ["i0009", "i0021"],
                        "status": "uncertain",
                        "summary": "合同金额证据不清晰。",
                        "framework_contract": {
                            "status": "uncertain",
                            "reason": "无法可靠确认合同类型。",
                            "evidence_image_ids": [],
                        },
                        "materials": [
                            {
                                "material_type": "服务合同",
                                "image_ids": ["i0009", "i0021"],
                                "facts": [],
                            }
                        ],
                        "checks": [
                            {
                                "key": "contract_amount",
                                "requirement": "合同金额",
                                "status": "uncertain",
                                "reason": "图片文字无法可靠识别。",
                                "evidence_image_ids": [],
                            },
                            {
                                "key": "signature_date",
                                "requirement": "合同签署日期",
                                "status": "fail",
                                "reason": "签署页日期栏为空。",
                                "evidence_image_ids": ["i0021"],
                            },
                        ],
                        "llm_elapsed_ms": 100,
                    }
                ],
                "performance_stats": {
                    "selected_case_count": 1,
                    "image_count": 13,
                    "llm_total_calls": 1,
                    "llm_elapsed_ms": 100,
                },
                "stats": {"llm_total_calls": 1, "llm_elapsed_ms": 100},
            },
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "业绩合同检查" in response.text
    assert "21.1 信达旺大厦云平台运营及维护服务" in response.text
    assert "合同类型：待确认" in response.text
    assert "合同金额" in response.text
    assert "合同签署日期" in response.text
    assert "证据图片：i0021" in response.text
    assert "送模图片" not in response.text
    assert "OCR：" not in response.text
    assert "100 ms" not in response.text
