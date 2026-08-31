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
    assert "提取合规性检查要求" in response.text
    assert "解析投标文件" in response.text
    assert "执行合规性检查" in response.text
    assert "检查结果" in response.text
    assert "并行执行" in response.text
    assert "data-parallel-stages" in response.text
    assert response.text.count("运行中") >= 2
    assert f'data-task-id="{stored_task.task_id}"' in response.text


def test_failed_task_page_names_failed_stage(client, repository, stored_task):
    repository.fail(
        stored_task.task_id,
        "requirements",
        "模拟合规性要求提取失败",
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "失败阶段：提取合规性检查要求" in response.text
    assert "模拟合规性要求提取失败" in response.text


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
    assert "本次共提取 5 项合规性检查要求" in response.text
    assert "商务投标文件封面完整性" in response.text
    assert "投标人名称应填写完整" in response.text
    assert "项目人员材料" in response.text
    assert "当前版本仅展示提取出的合规性检查要求" in response.text
    assert "尚未执行真实投标文件内容校验" in response.text
    assert "section_count" not in response.text
    assert "章节数" in response.text
    assert "检查通过" not in response.text
    assert "检查不通过" not in response.text


def test_unknown_task_page_returns_404(client):
    response = client.get("/bid-check/tasks/not-found")

    assert response.status_code == 404
