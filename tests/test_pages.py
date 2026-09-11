import json
from pathlib import Path

from app.evaluation_summary import (
    _facts_evidence_view,
    _objective_item_view,
    _qualification_performance_view,
    _status_view,
    _subjective_item_view,
)
from app.models import FileMetadata


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
    assert "评分+废标检查" not in response.text
    assert 'id="start-check"' in response.text
    assert 'id="start-check" class="button primary" type="submit" disabled' in response.text
    assert 'href="/bid-check/tasks"' in response.text


def test_bid_check_page_contains_full_mode_descriptions(client):
    response = client.get("/bid-check")

    assert "检查模板填写、必填字段、附件完整性" in response.text
    assert "签字盖章、日期及材料完整性等问题" in response.text
    assert "评分项与否决投标风险" in response.text
    assert "提取招标文件中的评分项、评分条件、证明材料和否决性规则" in response.text
    assert "根据招标文件中的评分办法、初步评审标准" not in response.text
    assert "同时执行标书合规性校验和评标规则校验" in response.text


def test_bid_check_page_enables_evaluation_mode_without_promising_scoring(client):
    response = client.get("/bid-check")

    assert response.status_code == 200
    assert '<input name="check_mode" type="radio" value="evaluation" disabled>' not in response.text
    assert 'value="evaluation"' in response.text
    assert "本轮可执行" in response.text
    assert "预计得分" not in response.text
    assert "实际评分" not in response.text


def test_bid_check_page_enables_full_mode(client):
    response = client.get("/bid-check")

    assert response.status_code == 200
    assert 'input name="check_mode" type="radio" value="full" disabled' not in response.text
    assert 'value="full"' in response.text


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


def test_failed_task_list_has_single_retry_action(client, repository, stored_task):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.fail(stored_task.task_id, "review", "合规检查失败")

    response = client.get("/bid-check/tasks")

    assert response.status_code == 200
    assert 'data-retry-task="stored-task"' in response.text
    assert "重新执行全部检查" in response.text
    assert "从失败阶段开始" not in response.text


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
    assert "标书校验结果" in response.text
    assert "标书合规性校验结果" not in response.text
    assert response.text.count('data-result-mode="compliance"') == 1
    assert 'data-result-mode="evaluation"' not in response.text
    assert "以下按模板规范、附件、业绩合同和文件自身四个检查范围展示合规性结果。" in response.text
    assert "模板规范检查" in response.text
    assert "附件检查" in response.text
    assert "业绩合同检查" in response.text
    assert "未发现需处理的模板规范问题" in response.text
    assert "未发现需处理的附件问题" in response.text
    assert "未发现需处理的业绩合同问题" in response.text
    assert "最终检查结果" in response.text
    for internal_label in (
        "TEMPLATE COMPLIANCE",
        "ATTACHMENT COMPLIANCE",
        "PERFORMANCE CONTRACT",
        "UPLOADED FILE METADATA",
        "FINAL REVIEW",
        "COMPLIANCE REVIEW REPORT",
    ):
        assert internal_label not in response.text
    assert "section_count" not in response.text
    assert "解析摘要" not in response.text
    assert "检查通过" not in response.text
    assert "检查不通过" not in response.text


def test_complete_compliance_page_renders_merged_evaluation_results(
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
    artifacts = {
        "11_evaluation_rules.json": {
            "score_categories": [],
            "score_items": [
                {
                    "id": "score_item_001",
                    "name": "投标文件编写质量",
                    "full_score": 5,
                    "evaluation_type": "subjective",
                }
            ],
            "veto_rules": [],
            "uncertain_rules": [],
            "source_sections": [],
        },
        "objective_scores.json": {"score_items": []},
        "subjective_scores.json": {
            "score_items": [
                {
                    "score_item_id": "score_item_001",
                    "rule_name": "投标文件编写质量",
                    "max_score": 5,
                    "status": "evidence_insufficient",
                    "recommended_score": None,
                    "score_band": None,
                    "reason": "五类扣分项证据覆盖不足。",
                    "evidence": [],
                    "matched_bid_content": [],
                }
            ]
        },
        "veto_rule_reviews.json": {"veto_rule_reviews": []},
    }
    for filename, payload in artifacts.items():
        (artifact_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "标书校验结果" in response.text
    assert "标书合规性校验结果" not in response.text
    assert 'data-result-mode="compliance"' in response.text
    assert 'data-result-mode="evaluation"' in response.text
    assert 'data-result-mode-view="compliance"' in response.text
    assert 'data-result-mode-view="evaluation"' in response.text
    assert 'data-result-mode-view="compliance" data-active="true"' in response.text
    assert 'data-result-mode-view="evaluation" data-active="false"' in response.text
    assert response.text.count('data-result-style-view="workbench"') >= 2
    assert response.text.count('data-result-style-view="dashboard"') >= 2
    assert response.text.count('data-result-style-view="report"') >= 2
    assert 'evaluation-result-style-workbench' in response.text
    assert 'evaluation-result-style-dashboard' in response.text
    assert 'evaluation-result-style-report' in response.text
    assert "评标结果汇总" in response.text
    assert "评分结果概览" in response.text
    assert "独立评标产出物" not in response.text
    assert "主观评分 / AI辅助评分" in response.text
    assert "投标文件编写质量" in response.text
    assert "五类扣分项证据覆盖不足。" in response.text


def test_complete_evaluation_page_renders_rule_artifact_with_provenance(
    client,
    settings,
    repository,
):
    task_dir = settings.tasks_dir / "evaluation-task"
    task_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    repository.create(
        "evaluation-task",
        FileMetadata("评标规则招标文件.docx", 6, str(tender_path)),
        FileMetadata("未参与评分投标文件.docx", 3, str(bid_path)),
        "evaluation",
    )
    repository.update_stage("evaluation-task", "requirements", "complete")
    repository.update_stage("evaluation-task", "bid_parse", "complete")
    repository.update_stage("evaluation-task", "review", "complete")
    repository.complete(
        "evaluation-task",
        {
            "evaluation_rules": {
                "source_sections": [
                    {
                        "section": "第三章 评标办法",
                        "title": "评标办法前附表",
                        "block_ids": ["b100", "b101"],
                        "source_text": "商务部分：30分；企业业绩每个2分，最高10分。",
                    }
                ],
                "score_categories": [
                    {
                        "id": "category_001",
                        "name": "商务部分",
                        "parent_id": None,
                        "full_score": 30,
                        "original_rule": "商务部分满分30分。",
                        "conditions": {},
                        "structure_status": "complete",
                        "source": {
                            "section": "第三章 评标办法",
                            "block_ids": ["b100"],
                            "source_text": "商务部分：30分",
                        },
                    }
                ],
                "score_items": [
                    {
                        "id": "score_item_001",
                        "name": "企业业绩",
                        "category_id": "category_001",
                        "parent_item_id": None,
                        "original_rule": "投标人自2023年1月1日以来，每提供1个类似项目业绩得2分，最高10分，须提供合同关键页扫描件。",
                        "conditions": {
                            "time_range": "自2023年1月1日以来",
                            "per_unit_score": 2,
                            "quantity_unit": "个",
                            "max_score": 10,
                        },
                        "scoring_method": {"type": "per_unit", "formula": "有效业绩数量×2，最高10分"},
                        "full_score": 10,
                        "evidence_requirements": ["合同关键页扫描件"],
                        "evaluation_type": "objective",
                        "source": {
                            "section": "评标办法前附表",
                            "block_ids": ["b101"],
                            "source_text": "每提供1个类似项目业绩得2分，最高10分，须提供合同关键页扫描件。",
                        },
                    },
                    {
                        "id": "score_item_002",
                        "name": "技术方案",
                        "category_id": None,
                        "parent_item_id": None,
                        "original_rule": "技术方案内容完整、针对性强、措施合理的得8至10分。",
                        "conditions": {},
                        "scoring_method": {"type": "range", "range": "8至10分"},
                        "full_score": 10,
                        "evidence_requirements": [],
                        "evaluation_type": "subjective",
                        "source": {
                            "section": "详细评审",
                            "block_ids": ["b102"],
                            "source_text": "技术方案内容完整、针对性强、措施合理的得8至10分。",
                        },
                    },
                ],
                "veto_rules": [
                    {
                        "id": "veto_001",
                        "name": "资格审查不通过",
                        "trigger_condition": "未提供营业执照。",
                        "consequence": "否决其投标，不进入后续评审。",
                        "evidence_requirements": ["营业执照"],
                        "original_rule": "未提供营业执照的，否决其投标。",
                        "source": {
                            "section": "资格审查",
                            "block_ids": ["b103"],
                            "source_text": "未提供营业执照的，否决其投标。",
                        },
                    }
                ],
                "uncertain_rules": [],
                "stats": {
                    "candidate_count": 1,
                    "source_section_count": 1,
                    "score_category_count": 1,
                    "score_item_count": 2,
                    "veto_rule_count": 1,
                    "llm_total_calls": 1,
                    "llm_elapsed_ms": 1234,
                    "estimated_prompt_tokens": 800,
                },
            }
        },
    )

    response = client.get("/bid-check/tasks/evaluation-task")

    assert response.status_code == 200
    assert "评标规则提取结果" in response.text
    assert "评标办法前附表" in response.text
    assert "商务部分" in response.text
    assert "30 分" in response.text
    assert "企业业绩" in response.text
    assert "客观评分" in response.text
    assert "主观评分" in response.text
    assert "每提供1个类似项目业绩得2分，最高10分" in response.text
    assert "合同关键页扫描件" in response.text
    assert "资格审查不通过" in response.text
    assert "否决其投标，不进入后续评审。" in response.text
    assert "来源 block：b101" in response.text
    assert "来源 block：b103" in response.text
    assert "实际评分" not in response.text
    assert "最终得分" not in response.text
    assert "最终排名" not in response.text
    assert "comparison-status-fail" not in response.text


def test_complete_evaluation_page_summarizes_independent_result_artifacts(
    client,
    settings,
    repository,
):
    task_dir = settings.tasks_dir / "evaluation-summary-task"
    artifact_dir = task_dir / "compliance_extraction"
    artifact_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    repository.create(
        "evaluation-summary-task",
        FileMetadata("汇总测试招标文件.docx", 6, str(tender_path)),
        FileMetadata("汇总测试投标文件.docx", 3, str(bid_path)),
        "evaluation",
    )
    repository.update_stage("evaluation-summary-task", "requirements", "complete")
    repository.update_stage("evaluation-summary-task", "bid_parse", "complete")
    repository.update_stage("evaluation-summary-task", "review", "complete")
    repository.complete("evaluation-summary-task", {})

    rules = {
        "score_categories": [],
        "score_items": [
            {
                "id": "objective_pending",
                "name": "待补充客观项",
                "full_score": 5,
                "evaluation_type": "objective",
            },
            {
                "id": "objective_scored",
                "name": "已确定客观项",
                "full_score": 3,
                "evaluation_type": "objective",
            },
            {
                "id": "score_item_001",
                "name": "投标文件编写质量",
                "full_score": 5,
                "evaluation_type": "subjective",
            },
            {
                "id": "subjective_scored",
                "name": "技术方案完整性",
                "full_score": 5,
                "evaluation_type": "subjective",
            },
        ],
        "veto_rules": [
            {
                "id": "veto_not_applicable",
                "name": "项目不适用规则",
                "trigger_condition": "未递交投标保证金。",
                "consequence": "否决投标。",
                "original_rule": "未递交投标保证金的，否决投标。",
                "evidence_requirements": [],
                "source": {},
            }
        ],
        "uncertain_rules": [],
        "source_sections": [],
    }
    objective_scores = {
        "score_items": [
            {
                "id": "objective_pending",
                "name": "待补充客观项",
                "full_score": 5,
                "status": "evidence_insufficient",
                "score": 2,
                "reason": "缺少可核验的合同原件。",
                "evidence": [],
                "facts": {
                    "performance_summary": "uncertain",
                    "matched_terms": ["合同关键页"],
                    "performance_cases": [
                        {
                            "case_number": "1",
                            "project_name": "示例业绩合同",
                            "role": "qualification",
                            "role_label": "资格要求业绩",
                            "overall_status": "fail",
                            "final_user": "示例最终用户",
                            "raw_only_marker": "资格业绩原始JSON不应展示",
                            "checks": {
                                "signature_date": {
                                    "status": "fail",
                                    "reason": "签署日期栏为空，无法确认合同签署日期。",
                                    "evidence": [
                                        {
                                            "image_id": "i0021",
                                            "block_id": "b0376",
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                },
                "related_artifacts": ["10_performance_reviews.json"],
            },
            {
                "id": "objective_scored",
                "name": "已确定客观项",
                "full_score": 3,
                "status": "auto_scored",
                "score": 3,
                "reason": "已满足评分条件。",
                "evidence": [],
            },
        ]
    }
    subjective_scores = {
        "score_items": [
            {
                "score_item_id": "score_item_001",
                "rule_name": "投标文件编写质量",
                "max_score": 5,
                "status": "evidence_insufficient",
                "recommended_score": None,
                "score_band": None,
                "reason": "其余扣分项证据不足，最终分数无法确定。",
                "evidence": [],
                "matched_bid_content": [],
                "deduction_checks": [
                    {
                        "deduction_item": "文件内容错误",
                        "status": "confirmed_present",
                        "confirmed_exists": True,
                        "reason": "模板检查已确认存在残留占位文字。",
                        "evidence": [
                            {
                                "artifact": "08_template_text_reviews.json",
                                "quote": "致：【招标人名称】",
                                "block_ids": ["b0030"],
                            }
                        ],
                    }
                ],
            },
            {
                "score_item_id": "subjective_scored",
                "rule_name": "技术方案完整性",
                "max_score": 5,
                "status": "ai_scored",
                "recommended_score": 4,
                "score_band": "良好",
                "reason": "方案结构完整。",
                "evidence": [
                    {"quote": "技术方案包含实施计划。", "block_ids": ["b0100"]}
                ],
                "matched_bid_content": [],
            },
        ]
    }
    veto_reviews = {
        "veto_rule_reviews": [
            {
                "id": "veto_triggered",
                "name": "明确触发规则",
                "status": "triggered",
                "triggered": True,
                "reason": "已发现明确触发事实。",
                "evidence": [{"quote": "明确不响应实质性条款。"}],
                "dependencies": {},
            },
            {
                "id": "veto_not_triggered",
                "name": "明确未触发规则",
                "status": "not_triggered",
                "triggered": False,
                "reason": "证据确认未触发。",
                "evidence": [],
                "dependencies": {},
            },
            {
                "id": "veto_not_applicable",
                "name": "项目不适用规则",
                "status": "not_applicable",
                "triggered": False,
                "reason": "本项目不要求投标保证金。",
                "evidence": [],
                "dependencies": {},
            },
            {
                "id": "veto_009",
                "name": "否决投标情形（通用）",
                "status": "evidence_insufficient",
                "triggered": False,
                "reason": "16 个子条件仍有未完成判断的条件。",
                "evidence": [],
                "dependencies": {
                    "external_data_required": True,
                    "other_bidder_data_required": True,
                    "manual_review_required": True,
                },
                "sub_conditions": [
                    {
                        "id": f"veto_009_{index:02d}",
                        "index": index,
                        "name": f"第 {index} 个子条件",
                        "status": "file_scope_missing" if index == 1 else "not_applicable",
                        "triggered": False,
                        "reason": "文件范围不足" if index == 1 else "本项目不适用",
                        "evidence": [],
                        "dependencies": {},
                    }
                    for index in range(1, 17)
                ],
            },
        ]
    }
    for filename, payload in (
        ("11_evaluation_rules.json", rules),
        ("objective_scores.json", objective_scores),
        ("subjective_scores.json", subjective_scores),
        ("veto_rule_reviews.json", veto_reviews),
    ):
        (artifact_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    response = client.get("/bid-check/tasks/evaluation-summary-task")

    assert response.status_code == 200
    assert "评标结果汇总" in response.text
    assert "客观评分" in response.text
    assert "OBJECTIVE" not in response.text
    assert "QUALIFICATION" not in response.text
    assert "1 项已确定 / 1 项待补充" in response.text
    assert "主观评分 / AI辅助评分" in response.text
    assert "1 项已评分 / 0 项缺少文件 / 1 项其他待补充" in response.text
    assert "否决规则" in response.text
    assert "存在 1 项明确触发" in response.text
    assert "暂不具备计算完整评标总分的条件" in response.text
    assert "待补充客观项" in response.text
    assert "暂无法确定" in response.text
    assert "缺少可核验的合同原件。" in response.text
    assert "业绩核验结论" in response.text
    assert "影响资格判断的问题" in response.text
    assert "查看合同信息" in response.text
    assert response.text.index("影响资格判断的问题") < response.text.index("查看合同信息")
    assert '<section class="evaluation-result-card evaluation-qualification-section"' in response.text
    assert '<article class="evaluation-result-card evaluation-qualification-card">' in response.text
    assert '<div class="evaluation-issue-summary">' in response.text
    assert '<details class="evaluation-qualification-contract">' in response.text
    assert '<details class="evaluation-qualification-contract" open>' not in response.text
    assert "查看资格业绩证据" not in response.text
    assert "影响评分的问题" in response.text
    assert "合同签署日期" in response.text
    assert "签署日期栏为空，无法确认合同签署日期。" in response.text
    assert "查看检查问题（1项）" in response.text
    assert "合同签署日期" in response.text
    assert "签署日期栏为空，无法确认合同签署日期。" in response.text
    assert "证据图片：i0021；block：b0376" in response.text
    assert "performance_summary" in response.text
    raw_fact_index = response.text.index("performance_summary")
    raw_details_start = response.text.rfind(
        '<details class="evaluation-evidence-raw">', 0, raw_fact_index
    )
    assert raw_details_start >= 0
    assert response.text.find("</details>", raw_fact_index) > raw_fact_index
    assert "合同关键页" in response.text
    assert "10_performance_reviews.json" in response.text
    assert "投标文件编写质量" in response.text
    assert "AI辅助评分" in response.text
    assert "已确认扣分事实" in response.text
    assert "文件内容错误" in response.text
    assert "致：【招标人名称】" in response.text
    assert "项目不适用规则" in response.text
    assert "不适用" in response.text
    assert '<small>file_scope_missing</small>' not in response.text
    assert '<small>evidence_insufficient</small>' not in response.text
    assert "证据不足" in response.text
    assert 'data-result-mode="evaluation"' in response.text
    assert 'data-result-mode="compliance"' not in response.text
    assert 'data-result-style-switcher' in response.text
    assert response.text.count('data-result-style-view="workbench"') >= 1
    assert response.text.count('data-result-style-view="dashboard"') >= 1
    assert response.text.count('data-result-style-view="report"') >= 1
    assert 'data-veto-status="triggered"' in response.text
    assert response.text.count('data-veto-status="triggered"') == 3
    assert "comparison-status-fail" not in response.text
    assert 'data-veto-rule-id="veto_009"' in response.text
    assert response.text.count('data-veto-subcondition="veto_009_') == 48


def test_evaluation_performance_facts_are_presented_as_business_evidence():
    views = _facts_evidence_view(
        {
            "performance_cases": [
                {
                    "case_number": "1",
                    "project_name": "信达旺大厦云平台运营及维护服务",
                    "role_label": "资格要求业绩",
                    "overall_status": "fail",
                    "table_row": {
                        "最终用户": "广东双能低碳智慧城市运营管理有限公司",
                        "销售金额（万元）": "96",
                        "证明文件所在页码": "38~50",
                    },
                    "contract_amount_facts": [
                        {"value": "960000.00元", "image_id": "i0011"}
                    ],
                    "implementation_time_facts": [
                        {
                            "value": "2025年1月19日至2026年1月19日",
                            "image_id": "i0012",
                        }
                    ],
                    "source_blocks": ["b0355", "b0366"],
                }
            ]
        },
        source_artifact="10_performance_reviews.json",
    )

    assert len(views) == 1
    view = views[0]
    assert view["title"] == "业绩合同检查结果"
    assert view["subtitle"] == "案例 1"
    fields = {field["label"]: field["value"] for field in view["fields"]}
    assert fields["项目名称"] == "信达旺大厦云平台运营及维护服务"
    assert fields["检查状态"] == "未通过"
    assert fields["合同金额"] == "960000.00元"
    assert fields["服务期限"] == "2025年1月19日至2026年1月19日"
    assert view["meta"] == "block：b0355、b0366"
    assert "performance_cases" not in view["text"]
    assert view["source_artifact"] == "10_performance_reviews.json"


def test_evaluation_performance_case_shows_check_reasons_and_evidence():
    views = _facts_evidence_view(
        {
            "performance_cases": [
                {
                    "case_number": "1",
                    "project_name": "信达旺大厦云平台运营及维护服务",
                    "overall_status": "fail",
                    "source_blocks": ["b0376"],
                    "checks": {
                        "order_alignment": {
                            "status": "pass",
                            "reason": "业绩材料顺序一致。",
                            "evidence": [{"block_id": "b0355"}],
                        },
                        "signature_date": {
                            "status": "fail",
                            "reason": "甲乙双方下方的“年 月 日”日期栏均为空白，未实际填写任何日期。",
                            "evidence": [
                                {
                                    "image_id": "i0021",
                                    "block_id": "b0376",
                                }
                            ],
                        },
                        "table_amount_consistency": {
                            "status": "uncertain",
                            "reason": "合同金额与业绩表金额无法统一确认。",
                            "evidence": [
                                {
                                    "image_id": "i0011",
                                    "block_id": "b0366",
                                }
                            ],
                        },
                    },
                }
            ]
        },
        source_artifact="10_performance_reviews.json",
    )

    details = views[0]["check_details"]
    assert [detail["label"] for detail in details] == ["合同签署日期", "金额一致性"]
    assert views[0]["check_issue_count"] == 2
    assert details[0]["status_label"] == "未通过"
    assert "日期栏均为空白" in details[0]["reason"]
    assert details[0]["meta"] == "证据图片：i0021；block：b0376"
    assert details[1]["status_label"] == "待确认"
    assert "金额无法统一确认" in details[1]["reason"]
    assert all(detail["status_label"] != "通过" for detail in details)


def test_evaluation_performance_case_facts_replace_filename_only_duplicates():
    view = _objective_item_view(
        {
            "id": "performance-score",
            "name": "类似案例1",
            "status": "evidence_insufficient",
            "reason": "案例金额仍待确认。",
            "evidence": [
                {
                    "artifact": "10_performance_reviews.json",
                    "case_number": "1",
                    "overall_status": "fail",
                }
            ],
            "facts": {
                "performance_cases": [
                    {
                        "case_number": "1",
                        "project_name": "信达旺大厦云平台运营及维护服务",
                        "overall_status": "fail",
                        "source_blocks": ["b0355"],
                    }
                ]
            },
            "related_artifacts": ["10_performance_reviews.json"],
        },
        {"full_score": 5},
    )

    performance_evidence = [
        item for item in view["evidence_view"] if item["title"] == "业绩合同检查结果"
    ]
    assert len(performance_evidence) == 1
    assert performance_evidence[0]["fields"]
    assert all(item["title"] != "评分证据" for item in view["evidence_view"])


def test_subjective_deduction_shows_issue_reason_instead_of_bid_quote():
    view = _subjective_item_view(
        {
            "score_item_id": "score_item_001",
            "rule_name": "投标文件编写质量",
            "max_score": 5,
            "status": "evidence_insufficient",
            "reason": "其他扣分项证据不足。",
            "deduction_checks": [
                {
                    "deduction_item": "文件内容错误",
                    "status": "confirmed_present",
                    "confirmed_exists": True,
                    "reason": "发现模板提示文字残留。",
                    "evidence": [
                        {
                            "artifact": "08_template_text_reviews.json",
                            "quote": "投标人名称实际值（投标人名称）仍在正文中",
                            "requirement": (
                                "字段“投标人名称”的模板填写提示文字"
                                "“（投标人名称）”应在填写实际值后清理。"
                            ),
                            "block_ids": ["b0031"],
                        }
                    ],
                }
            ],
        },
        {"full_score": 5},
    )

    evidence = view["confirmed_deductions"][0]["evidence_view"][0]

    assert "仍残留在投标文件中" in evidence["text"]
    assert "投标人名称实际值（投标人名称）仍在正文中" not in evidence["text"]
    assert evidence["meta"] == "block：b0031"
    assert evidence["raw_json"]


def test_qualification_performance_is_separated_from_scoring_cases():
    cases = [
        {
            "case_number": "1",
            "project_name": "信达旺大厦云平台运营及维护服务",
            "role": "qualification",
            "role_label": "资格要求业绩",
            "overall_status": "fail",
            "source_blocks": ["b0355", "b0376"],
            "checks": {
                "order_alignment": {
                    "status": "pass",
                    "reason": "业绩材料顺序一致。",
                },
                "signature_date": {
                    "status": "fail",
                    "reason": "签字页的“年 月 日”均为空白，无法核实合同签订日期。",
                    "evidence": [{"image_id": "i0021", "block_id": "b0376"}],
                },
            },
        },
        {
            "case_number": "2",
            "project_name": "评分业绩合同",
            "role": "scoring",
            "role_label": "评分业绩",
            "overall_status": "pass",
            "source_blocks": ["b0400"],
            "checks": {},
        },
    ]

    views = _qualification_performance_view(
        [
            {
                "id": "score_item_010",
                "facts": {
                    "performance_cases": cases,
                    "case_evaluations": [
                        {
                            "case_number": "1",
                            "role": "qualification",
                            "current_rule_status": "excluded_by_role",
                        },
                        {
                            "case_number": "2",
                            "role": "scoring",
                            "current_rule_status": "valid",
                        },
                    ],
                },
            },
            {
                "id": "score_item_011",
                "facts": {
                    "performance_cases": cases,
                    "case_evaluations": [
                        {
                            "case_number": "1",
                            "role": "qualification",
                            "current_rule_status": "excluded_by_role",
                        }
                    ],
                },
            },
        ]
    )

    assert len(views) == 1
    assert views[0]["case_number"] == "1"
    assert views[0]["project_name"] == "信达旺大厦云平台运营及维护服务"
    assert views[0]["role_label"] == "资格要求业绩"
    assert views[0]["status_label"] == "未通过"
    assert [detail["label"] for detail in views[0]["check_details"]] == ["合同签署日期"]
    assert "无法核实合同签订日期" in views[0]["check_details"][0]["reason"]
    assert views[0]["check_details"][0]["meta"] == "证据图片：i0021；block：b0376"


def test_evaluation_performance_issues_are_scoped_to_the_score_item():
    cases = [
        {
            "case_number": "1",
            "project_name": "资格要求业绩",
            "overall_status": "fail",
            "checks": {
                "signature_date": {
                    "status": "fail",
                    "reason": "签署日期栏为空。",
                    "evidence": [{"image_id": "i0001", "block_id": "b0001"}],
                }
            },
        },
        {
            "case_number": "2",
            "project_name": "评分业绩",
            "overall_status": "uncertain",
            "checks": {
                "contract_amount": {
                    "status": "uncertain",
                    "reason": "未提供可直接核验的固定合同金额。",
                    "evidence": [{"image_id": "i0002", "block_id": "b0002"}],
                }
            },
        },
    ]

    count_item = _objective_item_view(
        {
            "id": "score_item_010",
            "name": "类似案例1",
            "status": "evidence_insufficient",
            "reason": "存在数量待确认的业绩。",
            "evidence": [],
            "facts": {
                "performance_cases": cases,
                "case_evaluations": [
                    {
                        "case_number": "1",
                        "role": "qualification",
                        "current_rule_status": "excluded_by_role",
                        "conditions": {},
                    },
                    {
                        "case_number": "2",
                        "role": "scoring",
                        "current_rule_status": "valid",
                        "included_in_scoring": True,
                        "conditions": {"proof_material": {"status": "pass"}},
                    },
                ],
            },
        },
        {"full_score": 5},
    )
    amount_item = _objective_item_view(
        {
            "id": "score_item_011",
            "name": "类似案例2",
            "status": "evidence_insufficient",
            "reason": "存在累计金额待确认的业绩。",
            "evidence": [],
            "facts": {
                "performance_cases": cases,
                "case_evaluations": [
                    {
                        "case_number": "1",
                        "role": "qualification",
                        "current_rule_status": "excluded_by_role",
                        "conditions": {},
                    },
                    {
                        "case_number": "2",
                        "role": "scoring",
                        "current_rule_status": "uncertain",
                        "included_in_scoring": False,
                        "conditions": {
                            "proof_material": {
                                "status": "uncertain",
                                "reason": "合同金额事实不是可直接累计的固定金额。",
                            }
                        },
                        "amount": {
                            "reason": "合同金额事实不是可直接累计的固定金额。"
                        },
                    },
                ],
            },
        },
        {"full_score": 5},
    )

    assert count_item["issue_summary"] == []
    count_case = [
        item for item in count_item["evidence_view"] if item["kind"] == "performance_case"
    ][0]
    assert count_case["check_details"] == []

    assert len(amount_item["issue_summary"]) == 1
    assert amount_item["issue_summary"][0]["label"] == "案例 2 · 评分业绩 · 累计金额"
    assert amount_item["issue_summary"][0]["status_label"] == "待确认"
    amount_case = [
        item
        for item in amount_item["evidence_view"]
        if item["kind"] == "performance_case" and item["case_number"] == "2"
    ][0]
    assert [detail["label"] for detail in amount_case["check_details"]] == ["累计金额"]


def test_evaluation_status_codes_are_presented_as_chinese_labels():
    assert _status_view("fail")[0] == "未通过"
    assert _status_view("uncertain")[0] == "待确认"
    assert _status_view("evidence_insufficient")[0] == "证据不足"


def test_complete_evaluation_page_marks_full_score_as_unavailable_without_totalling(
    client,
    settings,
    repository,
):
    task_dir = settings.tasks_dir / "evaluation-complete-scores"
    artifact_dir = task_dir / "compliance_extraction"
    artifact_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    repository.create(
        "evaluation-complete-scores",
        FileMetadata("招标文件.docx", 6, str(tender_path)),
        FileMetadata("投标文件.docx", 3, str(bid_path)),
        "evaluation",
    )
    repository.update_stage("evaluation-complete-scores", "requirements", "complete")
    repository.update_stage("evaluation-complete-scores", "bid_parse", "complete")
    repository.update_stage("evaluation-complete-scores", "review", "complete")
    repository.complete("evaluation-complete-scores", {})
    artifacts = {
        "11_evaluation_rules.json": {
            "score_items": [
                {"id": "obj", "name": "客观项", "full_score": 5, "evaluation_type": "objective"},
                {"id": "subj", "name": "主观项", "full_score": 5, "evaluation_type": "subjective"},
            ]
        },
        "objective_scores.json": {
            "score_items": [
                {"id": "obj", "name": "客观项", "full_score": 5, "status": "auto_scored", "score": 5, "reason": "已确定", "evidence": []}
            ]
        },
        "subjective_scores.json": {
            "score_items": [
                {"score_item_id": "subj", "rule_name": "主观项", "max_score": 5, "status": "ai_scored", "recommended_score": 4, "score_band": "良好", "reason": "理由", "evidence": []}
            ]
        },
        "veto_rule_reviews.json": {"veto_rule_reviews": []},
    }
    for filename, payload in artifacts.items():
        (artifact_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    response = client.get("/bid-check/tasks/evaluation-complete-scores")

    assert response.status_code == 200
    assert "已具备计算完整评标总分的条件" in response.text
    assert "最终总分" not in response.text


def test_partial_evaluation_artifacts_do_not_claim_veto_or_score_completeness(
    client,
    settings,
    repository,
):
    task_dir = settings.tasks_dir / "partial-evaluation-results"
    artifact_dir = task_dir / "compliance_extraction"
    artifact_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    repository.create(
        "partial-evaluation-results",
        FileMetadata("招标文件.docx", 6, str(tender_path)),
        FileMetadata("投标文件.docx", 3, str(bid_path)),
        "evaluation",
    )
    repository.update_stage("partial-evaluation-results", "requirements", "complete")
    repository.update_stage("partial-evaluation-results", "bid_parse", "complete")
    repository.update_stage("partial-evaluation-results", "review", "complete")
    repository.complete("partial-evaluation-results", {"veto_rule_reviews": {}})
    (artifact_dir / "11_evaluation_rules.json").write_text(
        json.dumps(
            {
                "score_items": [
                    {
                        "id": "mixed_item",
                        "name": "混合评分项",
                        "full_score": 10,
                        "evaluation_type": "mixed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "objective_scores.json").write_text(
        json.dumps({"score_items": []}), encoding="utf-8"
    )
    (artifact_dir / "subjective_scores.json").write_text(
        json.dumps({"score_items": []}), encoding="utf-8"
    )

    response = client.get("/bid-check/tasks/partial-evaluation-results")

    assert response.status_code == 200
    assert "评标结果汇总" in response.text
    assert "尚未生成否决规则结果，无法确认是否存在明确触发项" in response.text
    assert "未发现明确触发项" not in response.text
    assert "暂不具备计算完整评标总分的条件" in response.text


def test_evaluation_page_prefers_rule_artifact_over_stored_rule_result(
    client,
    settings,
    repository,
):
    task_dir = settings.tasks_dir / "artifact-first-evaluation"
    artifact_dir = task_dir / "compliance_extraction"
    artifact_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    repository.create(
        "artifact-first-evaluation",
        FileMetadata("招标文件.docx", 6, str(tender_path)),
        FileMetadata("投标文件.docx", 3, str(bid_path)),
        "evaluation",
    )
    repository.update_stage("artifact-first-evaluation", "requirements", "complete")
    repository.update_stage("artifact-first-evaluation", "bid_parse", "complete")
    repository.update_stage("artifact-first-evaluation", "review", "complete")
    repository.complete(
        "artifact-first-evaluation",
        {
            "evaluation_rules": {
                "score_categories": [],
                "score_items": [
                    {
                        "id": "stale",
                        "name": "数据库中的旧规则",
                        "category_id": None,
                        "evaluation_type": "objective",
                        "original_rule": "旧规则原文。",
                        "source": {},
                    }
                ],
                "veto_rules": [],
                "uncertain_rules": [],
                "source_sections": [],
            }
        },
    )
    (artifact_dir / "11_evaluation_rules.json").write_text(
        json.dumps(
            {
                "score_categories": [],
                "score_items": [
                    {
                        "id": "current",
                        "name": "文件中的当前规则",
                        "category_id": None,
                        "evaluation_type": "objective",
                        "original_rule": "当前规则原文。",
                        "source": {},
                    }
                ],
                "veto_rules": [],
                "uncertain_rules": [],
                "source_sections": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/bid-check/tasks/artifact-first-evaluation")

    assert response.status_code == 200
    assert "文件中的当前规则" in response.text
    assert "数据库中的旧规则" not in response.text


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
                            },
                            {
                                "type": "missing_fill",
                                "requirement": "清理模板占位提示。",
                                "actual": "实际内容后仍有占位提示。",
                                "reason": "填写内容后模板提示文字仍然残留。",
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
    assert "固定内容缺失" in response.text
    assert "未填写或占位残留" in response.text
    assert "<strong>missing_content</strong>" not in response.text
    assert "<strong>missing_fill</strong>" not in response.text
    assert '<details class="review-issue-details">' in response.text
    assert '<details class="review-issue-details" open>' not in response.text
    assert "查看要求与实际" in response.text
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
