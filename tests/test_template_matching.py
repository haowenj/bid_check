from app.template_matching import build_template_comparisons, normalize_module_title


def _section(section_id, title, *, level=1, path=None):
    return {
        "section_id": section_id,
        "title": title,
        "level": level,
        "path": path or [title],
        "block_count": 1,
        "table_count": 0,
        "image_count": 0,
        "blocks": [],
        "child_sections": [],
    }


def _template(name):
    return {
        "id": "tpl-1",
        "name": name,
        "section": "投标文件格式",
        "body": f"{name}\n模板正文",
        "source": {"source_text": f"来源：{name}"},
    }


def test_normalize_module_title_ignores_heading_number_symbols_and_qualifiers():
    assert normalize_module_title("18.1.5 投标函") == normalize_module_title("5 投标函")
    assert normalize_module_title("联合体协议书（如有）") == normalize_module_title(
        "6 联合体协议书（本项目不适用）"
    )
    assert normalize_module_title("★知识产权不侵权承诺函") == normalize_module_title(
        "22 ★知识产权不侵权承诺函"
    )


def test_exact_normalized_title_is_matched_without_changing_actual_bid_title():
    comparisons = build_template_comparisons(
        [_template("18.1.5 投标函")],
        [_section("s0005", "5 投标函")],
    )

    comparison = comparisons[0]
    assert comparison["status"] == "matched"
    assert comparison["status_label"] == "已匹配"
    assert comparison["match_basis"] == "归一化标题完全一致"
    assert comparison["bid"]["title"] == "5 投标函"
    assert comparison["candidate_count"] == 1


def test_qualifier_is_used_only_for_matching_and_actual_title_is_preserved():
    comparisons = build_template_comparisons(
        [_template("联合体协议书（如有）")],
        [_section("s0006", "6 联合体协议书（本项目不适用）")],
    )

    comparison = comparisons[0]
    assert comparison["status"] == "matched"
    assert comparison["bid"]["title"] == "6 联合体协议书（本项目不适用）"
    assert "本项目不适用" in comparison["bid"]["title"]


def test_fuzzy_candidate_is_not_promoted_to_a_confirmed_match():
    comparisons = build_template_comparisons(
        [_template("投标一览表")],
        [_section("s0001", "1 投标报价一览表")],
    )

    comparison = comparisons[0]
    assert comparison["status"] == "ambiguous"
    assert comparison["status_label"] == "存在候选但不能确定"
    assert comparison["bid"] is None
    assert comparison["candidates"][0]["title"] == "1 投标报价一览表"


def test_duplicate_exact_sections_are_ambiguous():
    comparisons = build_template_comparisons(
        [_template("投标函")],
        [
            _section("s0001", "1 投标函"),
            _section("s0002", "5 投标函"),
        ],
    )

    comparison = comparisons[0]
    assert comparison["status"] == "ambiguous"
    assert comparison["bid"] is None
    assert comparison["candidate_count"] == 2


def test_no_sufficient_candidate_is_left_unmatched():
    comparisons = build_template_comparisons(
        [_template("投标保函")],
        [_section("s0001", "1 商务投标文件封面")],
    )

    comparison = comparisons[0]
    assert comparison["status"] == "unmatched"
    assert comparison["status_label"] == "未匹配"
    assert comparison["bid"] is None
    assert comparison["candidate_count"] == 0
