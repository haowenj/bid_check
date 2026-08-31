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
