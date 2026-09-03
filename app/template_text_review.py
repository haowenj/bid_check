from __future__ import annotations

import copy
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.navigation_content import (
    filter_navigation_sections,
    filter_navigation_templates,
)
from app.template_matching import build_template_comparisons
from app.template_placeholder_residual import find_template_placeholder_residuals

TEMPLATE_TEXT_REVIEW_MAX_WORKERS = 3
TEMPLATE_TEXT_REVIEW_MAX_ATTEMPTS = 2
_RETRYABLE_TEMPLATE_REVIEW_ERROR_MARKERS = (
    "请求超时",
    "网络连接错误",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
)
_TENDER_INDEPENDENT_NOT_APPLICABLE_RE = re.compile(
    r"(?:本项目|本标包|该项目|本次项目)[^。\n]{0,24}"
    r"(?:不涉及|不使用|不包含|不存在|无需提供|无需填写|无需提交)"
)

TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT = """你是一个招投标文件“模板文本对照检查器”。

你的唯一任务是：

以给定的【招标模板】作为基准，对照给定的【投标文件实际模块】，判断投标文件在文本层面是否完整、合理地响应了该模板。

你只能依据本次输入提供的招标模板和投标模块进行判断，不得引用外部法律法规、行业经验、常识性要求或自行增加检查标准。

【检查范围】

你需要检查：

1. 招标模板明确要求投标人填写的内容是否明显未填写；
2. 投标文件中是否仍残留明显应被替换或填写的模板占位内容；
3. 招标模板中的固定正文、承诺、条款是否存在明显遗漏；
4. 对于明确要求“除填写内容外不得修改”的模板，判断投标文件是否存在实质性的删除、替换或改变原意；
5. 对于允许投标人根据实际情况调整的模板，不要求逐字一致，只判断核心要求是否仍被完整保留；
6. 表格中明确需要填写的内容是否存在明显缺失；
7. 条件性模板是否适用。

【语义对应确认闸门（必须先执行）】

在执行任何模板内容检查之前，必须先判断当前投标模块是否在语义、用途和内容上确实属于当前招标模板的合理响应模块。这不是新增一次 LLM 调用，而是当前一次调用中的第一步判断。

需要综合判断：

- 文件用途是否一致；
- 主要填写对象是否一致；
- 模块核心内容是否在响应该模板；
- 是否只是因为标题相似、关键词重复、同一个主题词或局部文字重合而产生的代码候选。

不得仅凭标题名称相似就认定匹配。

只有语义对应状态为 matched 时，才允许继续 missing_fill、missing_content、substantive_change 和 other 等模板文本业务检查。

如果不是对应关系，返回 mismatched；如果当前信息不足以确认，返回 uncertain。对于 mismatched 或 uncertain，不得使用该模板去判当前投标模块不合规，issues 必须为空，正式业务检查状态不得形成 pass 或 fail。

【semantic_match.reason 输出长度】

semantic_match.status=matched 时，reason 只允许输出一个极短结论，不要复述模板名称、投标模块名称、标题、正文结构或判断过程。例如：“用途与核心内容对应”。

只有 semantic_match.status 为 mismatched 或 uncertain 时，reason 才需要说明具体原因，便于分析代码候选误召回或信息不足。

【重要判断原则】

一、不要做机械逐字比较。

文字顺序、空格、标点、排版、编号形式或不改变原意的表达差异，不应被判定为问题。

二、区分“模板固定内容”和“投标人填写内容”。

招标模板中的 XX、括号提示、下划线、空白位置、示例值等可能只是待填写位置。

投标文件在这些位置填写实际值属于正常响应，不应被判定为修改模板。

三、如果投标文件已经填写实际值，但同时仍明显残留原模板中的占位提示，应根据上下文判断是否属于未完整填写或模板残留。

只有能够从文本中明确判断时才报告问题。

四、不得因为招标模板自身存在 XX、空白、下划线或占位符，就判断投标文件存在问题。

判断对象始终是投标文件实际模块。

五、招标模板中的 fields 仅是辅助提示。

完整的招标模板正文才是主要判断依据。

即使 fields 没有列出某个填写项，只要完整模板正文明确存在该要求，仍可以识别。

反过来，也不能脱离模板正文，仅因为 fields 中存在某个名称就强行报错。

六、本次不检查任何需要视觉或外部证据才能确认的事项。

包括但不限于：

签字、盖章、身份证图片、营业执照图片、附件真实性、扫描件内容、CA、加密、平台提交状态。

如果问题只能通过这些信息确认，不得判定 fail。

七、对于条件性模板：

如果招标模板明确包含“如有”“若适用”等条件，应判断当前提供的文本能否证明其适用性。

只有本次提供的文本能够明确证明“不适用”时，才可以返回 not_applicable。

模块标题、括号、表格留空以及投标人自己的“无”“不涉及”“本项目不适用”声明，都只是单方声明，不是独立事实证据。若当前没有独立、明确的事实证明条件未触发，应返回 uncertain，而不是直接认定 not_applicable；不能仅凭这些文字把条件性模板判为 pass。

八、不得补充招标文件中不存在的要求。

不要评价投标人的专业水平、文件质量或法律风险。

只检查“是否按当前招标模板完成了文本响应”。

【局部规则优先级（强制）】

对于模板内部某个具体表格、承诺函、声明或其他子区域，规则适用顺序固定为：

具体子区域自身明确写出的规则
>
模板针对该子区域的编制说明
>
模板整体的一般要求
>
本检查器的通用 missing_fill / missing_content / substantive_change 判断规则

该子区域自身明确写出的规则只作用于该子区域，不能把另一个子区域的规则、填写习惯或空白状态套用过来。若其明确规定“留空即视为不涉及”“无填写即视为承诺不涉及”“不涉及时无需填写”或“无时可以不填写”，通用“空白字段应填写”规则不得覆盖它。投标文件已按该局部规则标记或留空时，不得报告 missing_fill、missing_content 或 substantive_change；如果适用性仍无法确认，整体可以 uncertain，但 issues 仍须为空。

【Issue 生成前强制复核】

只有 semantic_match.status=matched 时才允许生成 issue。每生成一个 issue 前，必须依次确认：

1. requirement、actual、reason 是否全部属于同一个具体子区域；
2. requirement 是否确实来自该子区域自身的模板正文或编制说明；
3. 该子区域是否存在“留空即不涉及”“不涉及无需填写”等更具体的局部规则；
4. 当前条件是否已经明确触发“必须填写”或“必须保留正文”的要求；
5. 是否错误引用了另一个表格、承诺函或子区域的规则；
6. 是否只是根据一般模板习惯、其他区域的填写方式或模型经验进行推断。

任意一项无法确认，该 issue 都不得输出。如果删除这些 issue 后无法形成明确的 pass/fail，可以返回 uncertain，但 issues 必须为空。如果 reason 中承认“模板允许留空”“不涉及则无需填写”或“适用条件无法确认”，原则上不得继续输出对应的 missing_fill、missing_content 或 substantive_change issue。

【省略号、示例行和预留行】

不得仅因为投标表格中存在“……”、示例行、空白预留行或模板展示行，就推断还有实际条目未填写。只有当前招标模板明确证明该位置对应一个实际必须填写、替换或删除的内容时，才能报告 missing_fill 或模板占位残留。必须先证明这个位置确实是当前投标人应该完成的实际填写位置；不能证明时不得仅凭“……”报错。

【状态定义】

pass：
从当前文本能够确认，该模板在文本层面完整响应，没有发现明确问题。

fail：
存在明确的未填写、正文缺失、实质性删除或实质性修改。

not_applicable：
模板本身属于条件性要求，并且当前提供的信息已经能够明确证明该模板不适用。

uncertain：
当前文本不足以完成判断，或者问题需要图片、附件、外部状态等其他证据确认。

【问题类型】

missing_fill：
明确存在应填写但未填写、明显模板占位残留等问题。

missing_content：
招标模板要求的固定正文、承诺或必要内容在投标文件中缺失。

substantive_change：
投标文件对不允许修改的模板内容进行了改变原意的删除、替换或修改。

other：
其他能够从当前文本明确确认的模板文本问题。

【结果字段写作边界（强制）】

结果字段只允许写最终、可直接展示给用户的事实和结论，禁止把分析过程写进 JSON。

- summary 只写最终结论，一句话，必须不超过 100 个汉字；
- issue.requirement 只写招标模板中的具体要求或与问题直接相关的最小原文依据，不超过 300 个汉字；
- issue.actual 只写投标文件中直接观察到的实际情况，不超过 300 个汉字；
- issue.reason 只写构成问题的直接原因，使用一至两句话，不超过 300 个汉字。

禁止在 summary、requirement、actual、reason 中写入分析过程、修正分析、重新审视、结论讨论、备选方案、重复推理、模型自我对话或“让我们再看一遍”等内容。不要复制整段模板或投标模块；如果存在多个独立问题，分别输出简短 issue。每个字段都必须能够直接展示给用户。

【输出要求】

只输出合法 JSON，不要输出 Markdown，不要解释 JSON 之外的内容。

输出结构：

{
  "semantic_match": {
    "status": "matched | mismatched | uncertain",
    "reason": "语义对应关系的判断理由"
  },
  "status": "pass | fail | not_applicable | uncertain",
  "summary": "对本模板检查结果的一句话总结",
  "issues": [
    {
      "type": "missing_fill | missing_content | substantive_change | other",
      "requirement": "对应的招标模板要求或原文依据",
      "actual": "投标文件中的实际情况",
      "reason": "为什么能够确认这是问题"
    }
  ]
}

semantic_match 是正式模板文本业务检查之前的独立候选确认结果。即使语义状态为 mismatched 或 uncertain，仍须返回 status，但该 status 只能为 uncertain 且 issues 必须为空；调用方会将其记录为未执行业务检查，不计入业务 fail。

如果没有明确问题，issues 必须为空数组。

只有存在明确证据时才报告问题。

宁可返回 uncertain，也不要猜测。
"""

TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT = """你是一个招投标文件“模板检查异常结论复核器”。

你不是重新执行一次完整模板检查。

你的唯一任务是：

复核第一次模板文本检查器产生的 fail 或 not_applicable 结论，判断该异常结论是否真的能够由当前提供的招标模板、投标模块和原始检查结果直接支持。

你的职责是降低误报。

只有存在明确、直接、来自当前输入的证据时，才能保留异常结论。

【基本原则】

一、只能依据当前提供的：

- 招标模板；
- 投标模块；
- 第一次检查结果；

进行复核。

不得引用外部法律法规、行业经验、其他未提供章节或常识。

二、不要重新完整审查投标文件。

你只能审核第一次检查器已经提出的异常结论。

不得创建第一次检查结果中不存在的新 issue。

三、对于每个原始 issue，必须逐项确认：

1. requirement 是否确实来自招标模板；
2. actual 是否确实存在于投标模块；
3. requirement 和 actual 是否属于同一个具体表格、承诺函、声明或子区域；
4. 是否存在比通用填写规则更具体的局部规则；
5. 是否错误引用了另一个子区域的要求；
6. 是否需要依赖当前输入没有提供的其他章节或外部信息才能成立；
7. reason 是否使用了“通常”“一般”“建议”“最好”“隐含要求”等模板中没有明确提出的判断。

任何一项无法确认，该 issue 都不能继续保留。

四、局部规则优先。

某个具体子区域自身的明确说明，优先于模板整体的一般规则和通用填写规则。

如果子区域明确规定：

- 留空即视为不涉及；
- 无填写即视为承诺不涉及；
- 不涉及则无需填写；

不得再以空白为由报告 missing_fill。

不得把其他子区域要求填写“不涉及”的规则套到当前子区域。

五、条件性子区域。

如果模板明确规定某个子区域不涉及时无需填写，而投标文件也将该子区域标记为“不涉及”“无”或“不适用”，不得仅因为保留原始空模板正文，就报告 substantive_change 或逻辑矛盾。

如果是否真的不适用无法由当前输入证明，应返回 uncertain，而不是制造文本 issue。

六、外部引用。

如果第一次 fail 的成立依赖模板引用的其他章节，而该章节内容没有出现在当前输入中，不得自行假设外部章节内容。

例如：

“按照第三章评标办法逐项填写”

如果本次没有提供第三章具体评审项，则不能根据“……”或空白行推断一定存在漏填项。

七、not_applicable 必须有证据。

仅因为投标人写了：

“无”
“不涉及”
“本项目不适用”

不能自动证明该条件确实不适用。

标题、括号、表格留空以及投标人自己的“无”“不涉及”“本项目不适用”声明，都只是投标人的单方声明，不是独立事实证据。只有招标模板或当前输入中的其他独立事实明确证明条件未触发时，才能保留 not_applicable。

特别是：仅有“（无）”“无”“不涉及”或“本项目不适用”时，必须返回 uncertain；不能因为模块标题带有这些文字，或因为表格没有填写，就确认 not_applicable。

否则应调整为 uncertain。

八、明确问题必须保留。

如果招标模板要求和投标实际文本能够直接形成明确矛盾或缺失，不要因为你是复核器就主动弱化问题。

例如已经填写实际值但仍明显保留原模板占位提示，这种具有直接文本证据的问题可以确认 fail。

【输出要求】

只输出合法 JSON。

不得输出 Markdown。

输出：

{
  "outcome": "pass | fail | not_applicable | uncertain",
  "issue_reviews": [
    {
      "original_issue_index": 0,
      "decision": "keep | reject",
      "reason": "保留或否决该 issue 的直接依据"
    }
  ]
}

对于原始 fail：

- 必须逐项输出 issue_reviews，并通过 original_issue_index 关联第一次检查的原始 issue；
- 只要任意一个原始 issue 的 decision 为 keep，代码就会确定保留该 issue，最终状态固定为 fail；
- 只有全部原始 issue 都为 reject 时，才使用 outcome，并且 outcome 只能为 pass 或 uncertain。

对于原始 not_applicable：

- issue_reviews 必须覆盖第一次检查的全部 issue（通常为空）；
- outcome 只能为 not_applicable 或 uncertain。

not_applicable 的证据门槛必须严格执行：标题、括号或投标人单方声明不是独立事实证据。仅有“（无）”“无”“不涉及”或“本项目不适用”时，必须返回 uncertain；空白表格也不能单独证明条件未触发。只有招标模板或当前输入中的其他独立事实明确证明条件未触发时，才能返回 not_applicable。

不要输出 review_decision、final_status 或 final_issues。
这些字段由代码根据原始 issues、issue_reviews 和 outcome 确定性生成；即使输出这些字段，代码也不会采用。

只返回 issue_reviews 和必要的 outcome，不要重写原始 issue 的 type、requirement、actual 或 reason。
宁可将证据不足的异常调整为 uncertain，也不要替第一次检查器寻找新的理由。
"""


class TemplateTextReviewLLM(Protocol):
    def review_template(self, system_prompt: str, user_prompt: str) -> Any: ...


class TemplateTextReviewError(RuntimeError):
    """Raised when a template text review cannot produce a valid result."""


class DeterministicTemplateTextReviewLLM:
    """Safe local fallback that never invents a template judgement."""

    model = "local"

    def review_template(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return {
            "status": "uncertain",
            "summary": "未配置模板文本检查 LLM，无法仅依据当前文本完成模型判断。",
            "issues": [],
            "semantic_match": {
                "status": "uncertain",
                "reason": "未配置模板文本检查 LLM，无法确认候选语义对应关系。",
            },
        }


def build_template_text_review_user_prompt(
    template: dict[str, Any],
    *,
    bid_module_name: str,
    bid_module_content: str,
) -> str:
    body = template.get("body")
    if not isinstance(body, str) or not body.strip():
        source = template.get("source", {})
        body = source.get("source_text", "") if isinstance(source, dict) else ""
    if not isinstance(body, str):
        body = str(body)

    raw_fields = template.get("fields", [])
    fields = (
        "、".join(str(field) for field in raw_fields)
        if isinstance(raw_fields, list) and raw_fields
        else "（无）"
    )

    return f"""请检查下面这一组招标模板和投标文件实际模块。

=== 当前模板 ===

模板名称：
{template.get("name", "")}

招标模板完整内容：
<<<TENDER_TEMPLATE
{body}
TENDER_TEMPLATE

辅助字段信息：
{fields}

说明：
辅助字段信息仅用于帮助理解模板中的填写位置，完整模板内容才是主要检查依据。


=== 投标文件实际模块 ===

模块名称：
{bid_module_name}

投标模块完整内容：
<<<BID_MODULE
{bid_module_content}
BID_MODULE


=== 本次任务 ===

请先判断当前投标模块是否与当前招标模板语义对应，再以招标模板为基准进行模板文本对照检查。不得因为标题相似、关键词重复、同一个主题词或局部文字重合就默认对应。

只有 semantic_match.status 为 matched 时，才执行下面的模板文本业务检查；如果为 mismatched 或 uncertain，不得继续检查 missing_fill、missing_content、substantive_change、other，不得形成投标文件不合规结论，并返回空 issues。

生成任何 issue 前，必须按照系统提示词中的‘同一子区域、局部规则优先、Issue 生成前强制复核’规则再次确认，不得跨子区域引用规则。

只检查：

- 应填写内容是否明显未填写；
- 是否存在明显模板占位内容残留；
- 固定正文或承诺内容是否缺失；
- 不允许修改的内容是否存在实质性修改；
- 允许调整的模板是否仍保留核心要求；
- 当前文本能否判断该条件性模板是否适用。

返回 JSON 前必须再次整理结果字段：summary 只保留一句最终结论；requirement、actual、reason 只保留直接证据和简短原因，禁止输出分析过程、修正分析、自我对话或重复推理。

不要检查：

- 签字；
- 盖章；
- 图片；
- 身份证；
- 营业执照；
- 附件是否齐全；
- 文件大小；
- CA；
- 加密；
- 平台上传状态。

只依据本次提供的两段内容判断。

按照系统要求的 JSON 结构返回结果。
"""


def build_template_text_exception_review_user_prompt(
    template: dict[str, Any],
    *,
    bid_module_name: str,
    bid_module_content: str,
    initial_review: dict[str, Any],
) -> str:
    body = template.get("body")
    if not isinstance(body, str) or not body.strip():
        source = template.get("source", {})
        body = source.get("source_text", "") if isinstance(source, dict) else ""
    if not isinstance(body, str):
        body = str(body)

    original_review = json.dumps(
        initial_review,
        ensure_ascii=False,
        indent=2,
    )
    return f"""请复核下面这一次模板文本检查产生的异常结论。

=== 招标模板 ===

模板名称：
{template.get("name", "")}

招标模板完整内容：
<<<TENDER_TEMPLATE
{body}
TENDER_TEMPLATE


=== 投标文件实际模块 ===

模块名称：
{bid_module_name}

投标模块完整内容：
<<<BID_MODULE
{bid_module_content}
BID_MODULE


=== 第一次模板检查结果 ===

<<<ORIGINAL_REVIEW
{original_review}
ORIGINAL_REVIEW


=== 本次任务 ===

本次不是重新完整检查模板。

只复核第一次检查中的 fail 或 not_applicable 是否具有足够证据。

对于 fail：

- 逐个审核原始 issue；
- 不得创建新 issue；
- 检查是否跨子区域引用规则；
- 检查是否忽略局部规则；
- 检查是否依赖当前没有提供的其他章节；
- 检查是否使用了模板中不存在的“通常”“一般”“建议”等经验；
- 只有具有直接文本证据的问题才能保留。

对于 not_applicable：

- 检查当前输入是否真的证明该条件不适用；
- 标题、括号或投标人单方声明不是独立事实证据；不能仅凭投标人自己的“无 / 不涉及 / 本项目不适用”声明确认 not_applicable；
- 仅有“（无）”“无”“不涉及”或“本项目不适用”时，必须返回 uncertain；空白表格也不能单独证明条件未触发；
- 只有招标模板或当前输入中的其他独立事实明确证明条件未触发时，才能返回 not_applicable；
- 如果真实性或适用性无法由当前输入验证，应改为 uncertain。

对于条件性模板，不能把模块标题或括号中的“无”，以及投标人自己的“无 / 不涉及 / 本项目不适用”声明，当作条件未触发的独立证据。当前没有独立、明确事实时，应返回 uncertain，不能判为 pass 或 not_applicable。

返回前只输出简短的最终复核 JSON，不要输出分析过程、自我修正或结论讨论。按照系统要求的 JSON 返回最终复核结果。
"""


def _read_structured_document(parsed_bid: dict[str, Any]) -> dict[str, Any] | None:
    direct = parsed_bid.get("structured_document")
    if isinstance(direct, dict):
        return direct
    if isinstance(parsed_bid.get("sections"), list) and isinstance(
        parsed_bid.get("blocks"), list
    ):
        return parsed_bid

    artifacts = parsed_bid.get("artifacts")
    artifact_path: Any = None
    if isinstance(artifacts, dict):
        artifact_path = artifacts.get("structured_document")
    if artifact_path is None:
        artifact_dir = parsed_bid.get("artifact_dir")
        if isinstance(artifact_dir, str):
            artifact_path = str(Path(artifact_dir) / "structured_document.json")
    if not isinstance(artifact_path, (str, Path)):
        return None
    try:
        payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _materialized_sections(document: dict[str, Any]) -> tuple[
    list[dict[str, Any]], dict[str, dict[str, Any]]
]:
    raw_sections = document.get("sections", [])
    raw_blocks = document.get("blocks", [])
    raw_tables = document.get("tables", [])
    raw_images = document.get("images", [])
    sections = [item for item in raw_sections if isinstance(item, dict)]
    blocks_by_id = {
        str(block.get("block_id")): block
        for block in raw_blocks
        if isinstance(block, dict) and block.get("block_id")
    }
    tables_by_block_id = {
        str(table.get("block_id")): table
        for table in raw_tables
        if isinstance(table, dict) and table.get("block_id")
    }
    images_by_block_id = {
        str(image.get("block_id")): image
        for image in raw_images
        if isinstance(image, dict) and image.get("block_id")
    }

    materialized: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw_section in sections:
        section = copy.deepcopy(raw_section)
        block_values = raw_section.get("blocks")
        block_ids = raw_section.get("direct_block_ids")
        if not isinstance(block_ids, list):
            block_ids = raw_section.get("block_ids", [])
        blocks: list[dict[str, Any]] = []
        if isinstance(block_values, list):
            blocks = [item for item in block_values if isinstance(item, dict)]
        elif isinstance(block_ids, list):
            for block_id in block_ids:
                block = blocks_by_id.get(str(block_id))
                if block is None:
                    continue
                display_block = copy.deepcopy(block)
                block_key = str(block.get("block_id"))
                if display_block.get("type") == "table":
                    table = tables_by_block_id.get(block_key)
                    if isinstance(table, dict) and "rows" in table:
                        display_block["rows"] = copy.deepcopy(table["rows"])
                elif display_block.get("type") == "image":
                    image = images_by_block_id.get(block_key)
                    if isinstance(image, dict):
                        display_block["caption"] = image.get("caption", "")
                blocks.append(display_block)
        section["blocks"] = blocks
        section["child_sections"] = []
        materialized.append(section)
        if section.get("section_id"):
            by_id[str(section["section_id"])] = section

    for section in materialized:
        parent_id = section.get("parent_section_id")
        parent = by_id.get(str(parent_id)) if parent_id is not None else None
        if parent is not None:
            parent["child_sections"].append(section)

    for section in materialized:
        section["child_sections"].sort(
            key=lambda child: (
                child.get("start_order", child.get("order", 0)),
                str(child.get("section_id", "")),
            )
        )
    return materialized, by_id


def _block_text(block: dict[str, Any]) -> str:
    block_type = str(block.get("type", "paragraph"))
    if block_type == "image":
        return "[图片，此轮模板文本检查不判断图片内容]"
    if block_type == "table":
        rows = block.get("rows")
        if isinstance(rows, list) and rows:
            rendered_rows = []
            for row in rows:
                if isinstance(row, list):
                    rendered_rows.append(" | ".join(str(cell) for cell in row))
                else:
                    rendered_rows.append(str(row))
            return "表格：\n" + "\n".join(rendered_rows)
    value = block.get("text", "")
    return value if isinstance(value, str) else str(value)


def _section_content(section: dict[str, Any]) -> str:
    parts: list[str] = []
    blocks = section.get("blocks", [])
    if isinstance(blocks, list):
        ordered_blocks = sorted(
            (block for block in blocks if isinstance(block, dict)),
            key=lambda block: (block.get("order", 0), str(block.get("block_id", ""))),
        )
        parts.extend(text for block in ordered_blocks if (text := _block_text(block)))
    children = section.get("child_sections", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                child_text = _section_content(child)
                if child_text:
                    parts.append(child_text)
    return "\n".join(parts)


def _section_has_reliable_text(section: dict[str, Any]) -> bool:
    def has_text(value: Any) -> bool:
        return bool((value if isinstance(value, str) else str(value)).strip())

    def has_reliable_table_text(value: Any) -> bool:
        if not has_text(value):
            return False
        normalized = value.strip() if isinstance(value, str) else str(value).strip()
        return normalized not in {"表格", "[表格]"}

    blocks = section.get("blocks", [])
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "paragraph"))
            if block_type in {"heading", "image"}:
                continue
            if block_type == "table":
                rows = block.get("rows")
                if isinstance(rows, list):
                    for row in rows:
                        cells = row if isinstance(row, list) else [row]
                        if any(has_text(cell) for cell in cells):
                            return True
                if has_reliable_table_text(block.get("text", "")):
                    return True
                continue
            value = block.get("text", "")
            if has_text(value):
                return True
    children = section.get("child_sections", [])
    if isinstance(children, list):
        return any(
            _section_has_reliable_text(child)
            for child in children
            if isinstance(child, dict)
        )
    return False


def _parse_review_result(raw: Any) -> dict[str, Any]:
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, dict):
        raise TemplateTextReviewError("模板文本检查响应不是 JSON 对象。")
    semantic_match = decoded.get("semantic_match")
    if not isinstance(semantic_match, dict):
        raise TemplateTextReviewError("模板文本检查响应缺少 semantic_match。")
    semantic_status = semantic_match.get("status")
    semantic_reason = semantic_match.get("reason")
    if semantic_status not in {"matched", "mismatched", "uncertain"}:
        raise TemplateTextReviewError(
            "模板文本检查响应包含无效 semantic_match.status。"
        )
    if not isinstance(semantic_reason, str) or not semantic_reason.strip():
        raise TemplateTextReviewError(
            "模板文本检查响应缺少 semantic_match.reason。"
        )
    status = decoded.get("status")
    if status not in {"pass", "fail", "not_applicable", "uncertain"}:
        raise TemplateTextReviewError("模板文本检查响应包含无效 status。")
    summary = decoded.get("summary")
    issues = decoded.get("issues")
    if not isinstance(summary, str) or not summary.strip():
        raise TemplateTextReviewError("模板文本检查响应缺少 summary。")
    if semantic_status != "matched":
        return {
            "semantic_match": {
                "status": semantic_status,
                "reason": semantic_reason.strip(),
            },
            "status": "uncertain",
            "summary": summary.strip(),
            "issues": [],
        }
    if not isinstance(issues, list):
        raise TemplateTextReviewError("模板文本检查响应缺少 issues 数组。")

    normalized_issues: list[dict[str, str]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise TemplateTextReviewError("模板文本检查 issue 不是 JSON 对象。")
        issue_type = issue.get("type")
        if issue_type not in {
            "missing_fill",
            "missing_content",
            "substantive_change",
            "other",
        }:
            raise TemplateTextReviewError("模板文本检查 issue 包含无效 type。")
        values = {
            key: issue.get(key)
            for key in ("requirement", "actual", "reason")
        }
        if any(not isinstance(value, str) for value in values.values()):
            raise TemplateTextReviewError("模板文本检查 issue 字段类型无效。")
        normalized_issues.append({"type": issue_type, **values})
    return {
        "semantic_match": {
            "status": semantic_status,
            "reason": semantic_reason.strip(),
        },
        "status": status,
        "summary": summary.strip(),
        "issues": normalized_issues,
    }


def _parse_exception_review_result(
    raw: Any,
    *,
    initial_review: dict[str, Any],
) -> dict[str, Any]:
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, dict):
        raise TemplateTextReviewError("模板异常复核响应不是 JSON 对象。")

    original_status = initial_review.get("status")
    original_issues = initial_review.get("issues")
    if not isinstance(original_issues, list):
        raise TemplateTextReviewError("第一次模板检查结果缺少 issues 数组。")
    issue_reviews = decoded.get("issue_reviews")
    if not isinstance(issue_reviews, list):
        raise TemplateTextReviewError("模板异常复核响应缺少 issue_reviews 数组。")
    if len(issue_reviews) != len(original_issues):
        raise TemplateTextReviewError(
            "模板异常复核必须逐项处理第一次检查的全部 issue。"
        )

    normalized_reviews: list[dict[str, Any]] = []
    review_by_index: dict[int, dict[str, Any]] = {}
    for item in issue_reviews:
        if not isinstance(item, dict):
            raise TemplateTextReviewError("模板异常复核 issue_review 不是 JSON 对象。")
        index = item.get("original_issue_index")
        decision = item.get("decision")
        reason = item.get("reason")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= len(original_issues)
        ):
            raise TemplateTextReviewError(
                "模板异常复核 issue_review 的 original_issue_index 无效。"
            )
        if index in review_by_index:
            raise TemplateTextReviewError(
                "模板异常复核 issue_review 存在重复的原始 issue 索引。"
            )
        if decision not in {"keep", "reject"}:
            raise TemplateTextReviewError(
                "模板异常复核 issue_review 包含无效 decision。"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise TemplateTextReviewError(
                "模板异常复核 issue_review 缺少 reason。"
            )
        normalized = {
            "original_issue_index": index,
            "decision": decision,
            "reason": reason.strip(),
        }
        normalized_reviews.append(normalized)
        review_by_index[index] = normalized
    if set(review_by_index) != set(range(len(original_issues))):
        raise TemplateTextReviewError(
            "模板异常复核 issue_review 未覆盖全部原始 issue。"
        )
    normalized_reviews.sort(key=lambda item: item["original_issue_index"])

    kept_original_indices = [
        item["original_issue_index"]
        for item in normalized_reviews
        if item["decision"] == "keep"
    ]
    outcome = decoded.get("outcome")

    if original_status == "fail":
        if kept_original_indices:
            review_decision = "confirm"
            final_status = "fail"
            summary = (
                f"异常复核确认保留 {len(kept_original_indices)} 个原始 fail issue。"
            )
        else:
            if outcome not in {"pass", "uncertain"}:
                raise TemplateTextReviewError(
                    "全部 fail issue 被否决时 outcome 只能为 pass 或 uncertain。"
                )
            review_decision = "revise"
            final_status = outcome
            summary = f"异常复核否决全部原始 fail issue，调整为 {final_status}。"
        normalized_final_issues = [
            copy.deepcopy(original_issues[index])
            for index in kept_original_indices
        ]
    elif original_status == "not_applicable":
        if outcome not in {"not_applicable", "uncertain"}:
            raise TemplateTextReviewError(
                "not_applicable 复核 outcome 只能为 not_applicable 或 uncertain。"
            )
        final_status = outcome
        review_decision = "confirm" if final_status == "not_applicable" else "revise"
        summary = (
            "异常复核确认当前模板不适用。"
            if final_status == "not_applicable"
            else "异常复核无法确认当前模板不适用，调整为 uncertain。"
        )
        normalized_final_issues = []
    else:
        raise TemplateTextReviewError(
            "模板异常复核的原始状态必须为 fail 或 not_applicable。"
        )

    return {
        "review_decision": review_decision,
        "final_status": final_status,
        "summary": summary.strip(),
        "issue_reviews": normalized_reviews,
        "final_issues": normalized_final_issues,
    }


def _has_independent_not_applicable_evidence(template: dict[str, Any]) -> bool:
    """Return whether the supplied tender text explicitly negates the condition.

    Bidder-authored titles, declarations, and empty tables are intentionally not
    treated as evidence here. The conservative gate prevents a model from
    confirming not_applicable based only on a bidder-side "无" marker.
    """

    body = template.get("body")
    if not isinstance(body, str) or not body.strip():
        source = template.get("source", {})
        body = source.get("source_text", "") if isinstance(source, dict) else ""
    if not isinstance(body, str):
        body = str(body)
    return bool(_TENDER_INDEPENDENT_NOT_APPLICABLE_RE.search(body))


def _downgrade_unverified_not_applicable(
    parsed: dict[str, Any],
) -> dict[str, Any]:
    downgraded = copy.deepcopy(parsed)
    downgraded.update(
        {
            "review_decision": "revise",
            "final_status": "uncertain",
            "summary": (
                "当前输入只有投标人单方不适用声明，无法确认条件未触发，调整为 uncertain。"
            ),
            "final_issues": [],
        }
    )
    return downgraded


def _review_request_payload(model: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "enable_thinking": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }


def _is_retryable_template_review_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    message = str(exc).casefold()
    if any(marker in message for marker in _RETRYABLE_TEMPLATE_REVIEW_ERROR_MARKERS):
        return True
    match = re.search(r"\bhttp\s+(\d{3})\b", message)
    if match is None:
        return False
    status_code = int(match.group(1))
    return status_code in {408, 429} or 500 <= status_code <= 599


def _review_result_base(
    template: dict[str, Any],
    bid_section: dict[str, Any],
) -> dict[str, Any]:
    return {
        "template_id": str(template.get("id", "")),
        "template_name": str(template.get("name", "")),
        "bid_module_name": bid_section.get("title", ""),
        "bid_section_id": bid_section.get("section_id"),
        "semantic_match": {
            "status": "uncertain",
            "reason": "尚未完成候选语义对应确认。",
        },
        "status": "uncertain",
        "business_status": "not_run",
        "execution_status": "pending",
        "summary": "当前文本不足以完成模板文本判断。",
        "issues": [],
        "llm_elapsed_ms": None,
    }


def _review_matched_template(
    template: dict[str, Any],
    bid_section: dict[str, Any],
    materialized: dict[str, Any],
    *,
    llm: TemplateTextReviewLLM,
    recorder: ComplianceExtractionRecorder | None,
    batch_index: int,
    batch_count: int,
) -> tuple[dict[str, Any], int, int]:
    result = _review_result_base(template, bid_section)
    template_name = result["template_name"]
    bid_module_content = _section_content(materialized)
    user_prompt = build_template_text_review_user_prompt(
        template,
        bid_module_name=str(bid_section.get("title", "")),
        bid_module_content=bid_module_content,
    )
    model = str(getattr(llm, "model", type(llm).__name__))
    set_call_context = getattr(llm, "set_call_context", None)
    total_elapsed_ms = 0
    for attempt in range(1, TEMPLATE_TEXT_REVIEW_MAX_ATTEMPTS + 1):
        call_id: str | None = None
        call_started_at = time.perf_counter()
        try:
            if recorder is not None:
                call_id = recorder.start_llm_call(
                    batch_index=batch_index,
                    batch_count=batch_count,
                    attempt=attempt,
                    model=model,
                    batch={
                        "template_id": result["template_id"],
                        "template_name": template_name,
                        "bid_module_name": result["bid_module_name"],
                    },
                )
                recorder.attach_llm_input(
                    call_id,
                    _review_request_payload(
                        model,
                        TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT,
                        user_prompt,
                    ),
                )
            if callable(set_call_context):
                set_call_context(recorder=recorder, call_id=call_id)

            raw_output = llm.review_template(
                TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT,
                user_prompt,
            )
            if recorder is not None and call_id is not None and not callable(
                set_call_context
            ):
                recorder.attach_llm_response(call_id, raw_response=raw_output)
            parsed = _parse_review_result(raw_output)
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            result.update(parsed)
            result["llm_elapsed_ms"] = total_elapsed_ms
            if parsed["semantic_match"]["status"] != "matched":
                result.update(
                    {
                        "status": "uncertain",
                        "business_status": "not_run",
                        "execution_status": "semantic_skipped",
                        "summary": (
                            "候选未通过模板语义对应确认，未执行模板文本业务检查。"
                        ),
                        "issues": [],
                    }
                )
                if recorder is not None and call_id is not None:
                    recorder.complete_llm_call(
                        call_id,
                        parsed_objects=result,
                        schema_valid=True,
                        elapsed_ms=elapsed_ms,
                    )
                return result, attempt, 1
            result["business_status"] = parsed["status"]
            result["execution_status"] = "completed"
            if recorder is not None and call_id is not None:
                recorder.complete_llm_call(
                    call_id,
                    parsed_objects=result,
                    schema_valid=True,
                    elapsed_ms=elapsed_ms,
                )
            return result, attempt, 1
        except Exception as exc:  # noqa: BLE001 - isolate one template from the batch
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            if recorder is not None and call_id is not None:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=elapsed_ms,
                )
            if attempt < TEMPLATE_TEXT_REVIEW_MAX_ATTEMPTS and _is_retryable_template_review_error(exc):
                continue
            result.update(
                {
                    "execution_status": "failed",
                    "business_status": "not_run",
                    "status": "uncertain",
                    "summary": "模板文本检查调用失败，未形成业务检查结论。",
                    "issues": [],
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "llm_elapsed_ms": total_elapsed_ms,
                }
            )
            return result, attempt, 0
        finally:
            if callable(set_call_context):
                set_call_context(recorder=None, call_id=None)

    raise AssertionError("template review attempts unexpectedly exhausted")


def _review_exception_template(
    template: dict[str, Any],
    bid_section: dict[str, Any],
    materialized: dict[str, Any],
    initial_review: dict[str, Any],
    *,
    llm: TemplateTextReviewLLM,
    recorder: ComplianceExtractionRecorder | None,
    batch_index: int,
    batch_count: int,
) -> tuple[dict[str, Any], int, int]:
    result = copy.deepcopy(initial_review)
    result["initial_review"] = copy.deepcopy(initial_review)
    result["final_status"] = initial_review.get("status", "uncertain")
    result["final_issues"] = copy.deepcopy(initial_review.get("issues", []))
    result["manual_review_required"] = False
    result["exception_review"] = {
        "execution_status": "pending",
        "review_decision": None,
        "final_status": None,
        "summary": "尚未完成模板异常结论复核。",
        "issue_reviews": [],
        "final_issues": [],
        "llm_elapsed_ms": None,
    }
    bid_module_content = _section_content(materialized)
    user_prompt = build_template_text_exception_review_user_prompt(
        template,
        bid_module_name=str(bid_section.get("title", "")),
        bid_module_content=bid_module_content,
        initial_review=initial_review,
    )
    model = str(getattr(llm, "model", type(llm).__name__))
    set_call_context = getattr(llm, "set_call_context", None)
    exception_reviewer = getattr(llm, "review_template_exception", None)
    if not callable(exception_reviewer):
        raise TemplateTextReviewError("当前 LLM 未提供模板异常结论复核接口。")

    total_elapsed_ms = 0
    for attempt in range(1, TEMPLATE_TEXT_REVIEW_MAX_ATTEMPTS + 1):
        call_id: str | None = None
        call_started_at = time.perf_counter()
        try:
            if recorder is not None:
                call_id = recorder.start_llm_call(
                    batch_index=batch_index,
                    batch_count=batch_count,
                    attempt=attempt,
                    model=model,
                    batch={
                        "review_type": "exception",
                        "template_id": result["template_id"],
                        "template_name": result["template_name"],
                        "bid_module_name": result["bid_module_name"],
                        "initial_status": initial_review.get("status"),
                    },
                )
                recorder.attach_llm_input(
                    call_id,
                    _review_request_payload(
                        model,
                        TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT,
                        user_prompt,
                    ),
                )
            if callable(set_call_context):
                set_call_context(recorder=recorder, call_id=call_id)

            raw_output = exception_reviewer(
                TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT,
                user_prompt,
            )
            if recorder is not None and call_id is not None and not callable(
                set_call_context
            ):
                recorder.attach_llm_response(call_id, raw_response=raw_output)
            parsed = _parse_exception_review_result(
                raw_output,
                initial_review=initial_review,
            )
            if (
                initial_review.get("status") == "not_applicable"
                and parsed["final_status"] == "not_applicable"
                and not _has_independent_not_applicable_evidence(template)
            ):
                parsed = _downgrade_unverified_not_applicable(parsed)
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            result["exception_review"] = {
                "execution_status": "completed",
                **parsed,
                "llm_elapsed_ms": total_elapsed_ms,
            }
            result.update(
                {
                    "status": parsed["final_status"],
                    "business_status": parsed["final_status"],
                    "summary": parsed["summary"],
                    "issues": copy.deepcopy(parsed["final_issues"]),
                    "final_status": parsed["final_status"],
                    "final_issues": copy.deepcopy(parsed["final_issues"]),
                    "manual_review_required": False,
                }
            )
            if recorder is not None and call_id is not None:
                recorder.complete_llm_call(
                    call_id,
                    parsed_objects=result["exception_review"],
                    schema_valid=True,
                    elapsed_ms=elapsed_ms,
                )
            return result, attempt, 1
        except Exception as exc:  # noqa: BLE001 - isolate one exception review
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            if recorder is not None and call_id is not None:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=elapsed_ms,
                )
            if attempt < TEMPLATE_TEXT_REVIEW_MAX_ATTEMPTS and _is_retryable_template_review_error(exc):
                continue
            error_summary = "异常复核执行失败，需要人工确认，未接受原异常结论。"
            result["exception_review"] = {
                "execution_status": "failed",
                "review_decision": None,
                "final_status": "uncertain",
                "summary": error_summary,
                "issue_reviews": [],
                "final_issues": [],
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "llm_elapsed_ms": total_elapsed_ms,
            }
            result.update(
                {
                    "status": "uncertain",
                    "business_status": "uncertain",
                    "summary": error_summary,
                    "issues": [],
                    "final_status": "uncertain",
                    "final_issues": [],
                    "manual_review_required": True,
                }
            )
            return result, attempt, 0
        finally:
            if callable(set_call_context):
                set_call_context(recorder=None, call_id=None)

    raise AssertionError("template exception review attempts unexpectedly exhausted")


def _review_stats(
    *,
    template_count: int,
    participating_template_count: int,
    navigation_excluded_templates: list[dict[str, Any]],
    navigation_excluded_bid_sections: list[dict[str, Any]],
    matched_template_count: int,
    code_candidate_count: int,
    no_bid_candidate_template_ids: list[str],
    candidate_without_reliable_bid_text_template_ids: list[str],
    results: list[dict[str, Any]],
    llm_total_calls: int,
    llm_completed_calls: int,
    main_wall_elapsed_ms: int,
    exception_review_candidate_count: int,
    exception_review_call_count: int,
    exception_review_confirm_count: int,
    exception_review_revise_count: int,
    exception_review_failed_count: int,
    exception_review_llm_elapsed_ms: int,
    exception_review_wall_elapsed_ms: int,
    total_elapsed_ms: int,
) -> dict[str, Any]:
    failed_count = sum(
        item.get("execution_status") == "failed" for item in results
    )
    semantic_completed = [
        item for item in results if item.get("execution_status") != "failed"
    ]
    business_results = [
        item
        for item in semantic_completed
        if item.get("business_status") in {
            "pass",
            "fail",
            "uncertain",
            "not_applicable",
        }
    ]
    business_status_counts = {
        status: sum(item.get("business_status") == status for item in business_results)
        for status in ("pass", "fail", "uncertain", "not_applicable")
    }
    return {
        "template_count": template_count,
        "participating_template_count": participating_template_count,
        "navigation_excluded_template_count": len(navigation_excluded_templates),
        "navigation_excluded_bid_section_count": len(navigation_excluded_bid_sections),
        "matched_template_count": matched_template_count,
        "code_candidate_count": code_candidate_count,
        "selected_template_count": len(results),
        "semantic_matched_count": sum(
            item.get("semantic_match", {}).get("status") == "matched"
            for item in semantic_completed
        ),
        "semantic_mismatched_count": sum(
            item.get("semantic_match", {}).get("status") == "mismatched"
            for item in semantic_completed
        ),
        "semantic_uncertain_count": sum(
            item.get("semantic_match", {}).get("status") == "uncertain"
            for item in semantic_completed
        ),
        "no_bid_candidate_template_ids": no_bid_candidate_template_ids,
        "candidate_without_reliable_bid_text_template_ids": (
            candidate_without_reliable_bid_text_template_ids
        ),
        "max_concurrency": TEMPLATE_TEXT_REVIEW_MAX_WORKERS,
        "llm_total_calls": llm_total_calls,
        "llm_completed_calls": llm_completed_calls,
        "llm_failed_count": failed_count,
        "llm_failed_calls": failed_count,
        "business_status_counts": business_status_counts,
        "pass_count": business_status_counts["pass"],
        "fail_count": business_status_counts["fail"],
        "uncertain_count": business_status_counts["uncertain"],
        "not_applicable_count": business_status_counts["not_applicable"],
        "llm_elapsed_ms": sum(
            item.get("llm_elapsed_ms") or 0 for item in results
        ),
        "exception_review_candidate_count": exception_review_candidate_count,
        "exception_review_call_count": exception_review_call_count,
        "exception_review_confirm_count": exception_review_confirm_count,
        "exception_review_revise_count": exception_review_revise_count,
        "exception_review_failed_count": exception_review_failed_count,
        "exception_review_llm_elapsed_ms": exception_review_llm_elapsed_ms,
        "exception_review_wall_elapsed_ms": exception_review_wall_elapsed_ms,
        "main_wall_elapsed_ms": main_wall_elapsed_ms,
        "total_llm_elapsed_ms": (
            sum(item.get("llm_elapsed_ms") or 0 for item in results)
            + exception_review_llm_elapsed_ms
        ),
        "total_llm_calls": llm_total_calls + exception_review_call_count,
        "total_wall_elapsed_ms": total_elapsed_ms,
        "total_elapsed_ms": total_elapsed_ms,
    }


def _deterministic_placeholder_issue(hit: dict[str, Any]) -> dict[str, Any]:
    field = str(hit.get("field", ""))
    marker = str(hit.get("marker", ""))
    template_marker = str(hit.get("template_marker", marker))
    actual = str(hit.get("bid_text", "")).strip()
    if not actual:
        actual = f"{hit.get('actual_context', '')}{marker}".strip()
    return {
        "type": "missing_fill",
        "placeholder_field": field,
        "placeholder_marker": marker,
        "requirement": (
            f"字段“{field}”的模板填写提示文字“{template_marker}”应在填写实际值后清理。"
        ),
        "actual": actual,
        "reason": (
            f"投标模块已出现与字段“{field}”对应的实际填写内容，"
            f"但模板提示文字“{marker}”仍原样残留。"
        ),
        "detected_by": ["deterministic"],
    }


def _issue_contains_placeholder_hit(
    issue: dict[str, Any],
    hit: dict[str, Any],
) -> bool:
    if issue.get("type") != "missing_fill":
        return False
    marker = str(hit.get("marker", ""))
    if not marker:
        return False
    issue_marker = issue.get("placeholder_marker")
    if isinstance(issue_marker, str):
        return issue_marker == marker
    return marker in " ".join(
        str(issue.get(key, ""))
        for key in ("requirement", "actual", "reason")
    )


def _merge_issue_detection_source(issue: dict[str, Any]) -> None:
    detected_by = issue.get("detected_by")
    sources = [
        source for source in detected_by
        if isinstance(source, str)
    ] if isinstance(detected_by, list) else []
    if "llm" not in sources:
        sources.insert(0, "llm")
    if "deterministic" not in sources:
        sources.append("deterministic")
    issue["detected_by"] = sources


def _apply_deterministic_placeholder_review(
    results: list[dict[str, Any]],
    jobs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> None:
    for result, job in zip(results, jobs, strict=True):
        # A semantic gate that explicitly skipped the candidate has already
        # established that this code candidate is not a reliable business
        # comparison.  Do not bypass that existing boundary.
        if result.get("execution_status") == "semantic_skipped":
            continue
        template, bid_section, materialized = job
        hits = find_template_placeholder_residuals(
            template,
            bid_section,
            materialized,
        )
        if not hits:
            continue

        result["deterministic_placeholder_residuals"] = hits
        issues = result.get("issues")
        if not isinstance(issues, list):
            issues = []
            result["issues"] = issues
        added_issue = False
        for hit in hits:
            existing_issue = next(
                (
                    issue
                    for issue in issues
                    if isinstance(issue, dict)
                    and _issue_contains_placeholder_hit(issue, hit)
                ),
                None,
            )
            if existing_issue is not None:
                if not isinstance(existing_issue.get("placeholder_marker"), str):
                    _merge_issue_detection_source(existing_issue)
                continue
            issues.append(_deterministic_placeholder_issue(hit))
            added_issue = True

        if not added_issue and result.get("status") == "fail":
            # The existing LLM issue remains the sole issue; this branch only
            # records that the deterministic check independently confirmed it.
            if isinstance(result.get("final_issues"), list):
                result["final_issues"] = copy.deepcopy(issues)
            continue

        result.update(
            {
                "status": "fail",
                "business_status": "fail",
                "summary": "确定性字段来源检查发现填写后仍残留模板提示文字。",
            }
        )
        if isinstance(result.get("final_issues"), list):
            result["final_issues"] = copy.deepcopy(issues)
        if "final_status" in result:
            result["final_status"] = "fail"


def run_template_text_review(
    extraction_result: dict[str, Any],
    parsed_bid: dict[str, Any],
    *,
    llm: TemplateTextReviewLLM,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    raw_templates = extraction_result.get("templates", [])
    templates = [
        candidate for candidate in raw_templates if isinstance(candidate, dict)
    ] if isinstance(raw_templates, list) else []
    extracted_templates = templates
    templates, excluded_templates = filter_navigation_templates(extracted_templates)

    document = _read_structured_document(parsed_bid) or {}
    _sections, sections_by_id = _materialized_sections(document)
    sections, excluded_sections = filter_navigation_sections(_sections)
    comparisons = build_template_comparisons(templates, sections)
    matched_template_count = sum(
        comparison.get("status") == "matched" for comparison in comparisons
    )
    no_bid_candidate_template_ids = [
        str(template.get("id", ""))
        for template, comparison in zip(templates, comparisons, strict=True)
        if comparison.get("candidate_count", 0) == 0
    ]
    candidate_without_reliable_bid_text_template_ids: list[str] = []
    jobs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for template, comparison in zip(templates, comparisons, strict=True):
        if comparison.get("status") != "matched":
            continue
        bid_section = comparison.get("bid")
        if not isinstance(bid_section, dict):
            continue
        materialized = sections_by_id.get(str(bid_section.get("section_id")))
        if materialized is None or not _section_has_reliable_text(materialized):
            candidate_without_reliable_bid_text_template_ids.append(
                str(template.get("id", ""))
            )
            continue
        jobs.append((template, bid_section, materialized))

    main_phase_started_at = time.perf_counter()
    executions_by_index: list[tuple[dict[str, Any], int, int] | None] = [
        None
    ] * len(jobs)
    if jobs:
        with ThreadPoolExecutor(
            max_workers=TEMPLATE_TEXT_REVIEW_MAX_WORKERS,
            thread_name_prefix="template-review",
        ) as executor:
            future_positions = {
                executor.submit(
                    _review_matched_template,
                    template,
                    bid_section,
                    materialized,
                    llm=llm,
                    recorder=recorder,
                    batch_index=index + 1,
                    batch_count=len(jobs),
                ): index
                for index, (template, bid_section, materialized) in enumerate(jobs)
            }
            for future in as_completed(future_positions):
                index = future_positions[future]
                try:
                    executions_by_index[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - isolate one future from the batch
                    template, bid_section, _materialized = jobs[index]
                    failed_result = _review_result_base(template, bid_section)
                    failed_result.update(
                        {
                            "execution_status": "failed",
                            "summary": "模板文本检查调用失败，未形成业务检查结论。",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "llm_elapsed_ms": 0,
                        }
                    )
                    executions_by_index[index] = (failed_result, 0, 0)

    executions = [item for item in executions_by_index if item is not None]
    results = [item[0] for item in executions]
    main_wall_elapsed_ms = int((time.perf_counter() - main_phase_started_at) * 1000)

    exception_review_method_available = callable(
        getattr(llm, "review_template_exception", None)
    )
    if exception_review_method_available:
        for result in results:
            initial_review = copy.deepcopy(result)
            result["initial_review"] = initial_review
            result["final_status"] = result.get("status", "uncertain")
            result["final_issues"] = copy.deepcopy(result.get("issues", []))
            result["manual_review_required"] = False
            result["exception_review"] = {
                "execution_status": "not_run",
                "reason": "仅对 semantic_match=matched 且业务状态为 fail 或 not_applicable 的结果复核。",
            }
    exception_jobs: list[
        tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], int]
    ] = []
    if exception_review_method_available:
        for index, ((result, _attempts, _completed), job) in enumerate(
            zip(executions, jobs, strict=True)
        ):
            if (
                result.get("semantic_match", {}).get("status") == "matched"
                and result.get("business_status") in {"fail", "not_applicable"}
            ):
                template, bid_section, materialized = job
                initial_review = copy.deepcopy(result["initial_review"])
                exception_jobs.append(
                    (template, bid_section, materialized, initial_review, index)
                )

    exception_review_started_at = time.perf_counter()
    exception_executions_by_index: list[tuple[dict[str, Any], int, int] | None] = [
        None
    ] * len(exception_jobs)
    if exception_jobs:
        with ThreadPoolExecutor(
            max_workers=TEMPLATE_TEXT_REVIEW_MAX_WORKERS,
            thread_name_prefix="template-exception-review",
        ) as executor:
            future_positions = {
                executor.submit(
                    _review_exception_template,
                    template,
                    bid_section,
                    materialized,
                    initial_review,
                    llm=llm,
                    recorder=recorder,
                    batch_index=index + 1,
                    batch_count=len(exception_jobs),
                ): index
                for index, (
                    template,
                    bid_section,
                    materialized,
                    initial_review,
                    _result_index,
                ) in enumerate(exception_jobs)
            }
            for future in as_completed(future_positions):
                index = future_positions[future]
                try:
                    exception_executions_by_index[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - isolate one future from the batch
                    _template, _bid_section, _materialized, initial_review, _result_index = (
                        exception_jobs[index]
                    )
                    failed_result = copy.deepcopy(initial_review)
                    error_summary = "异常复核执行失败，需要人工确认，未接受原异常结论。"
                    failed_result["initial_review"] = copy.deepcopy(initial_review)
                    failed_result["exception_review"] = {
                        "execution_status": "failed",
                        "review_decision": None,
                        "final_status": "uncertain",
                        "summary": error_summary,
                        "issue_reviews": [],
                        "final_issues": [],
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "llm_elapsed_ms": 0,
                    }
                    failed_result.update(
                        {
                            "status": "uncertain",
                            "business_status": "uncertain",
                            "summary": error_summary,
                            "issues": [],
                            "final_status": "uncertain",
                            "final_issues": [],
                            "manual_review_required": True,
                        }
                    )
                    exception_executions_by_index[index] = (failed_result, 0, 0)

        for exception_execution, exception_job in zip(
            exception_executions_by_index,
            exception_jobs,
            strict=True,
        ):
            if exception_execution is None:
                continue
            result_index = exception_job[-1]
            results[result_index] = exception_execution[0]

    exception_review_wall_elapsed_ms = int(
        (time.perf_counter() - exception_review_started_at) * 1000
    )
    exception_review_call_count = sum(
        item[1] for item in exception_executions_by_index if item is not None
    )
    exception_review_confirm_count = sum(
        item[0].get("exception_review", {}).get("review_decision") == "confirm"
        for item in exception_executions_by_index
        if item is not None
    )
    exception_review_revise_count = sum(
        item[0].get("exception_review", {}).get("review_decision") == "revise"
        for item in exception_executions_by_index
        if item is not None
    )
    exception_review_failed_count = sum(
        item[0].get("exception_review", {}).get("execution_status") == "failed"
        for item in exception_executions_by_index
        if item is not None
    )
    exception_review_llm_elapsed_ms = sum(
        item[0].get("exception_review", {}).get("llm_elapsed_ms") or 0
        for item in exception_executions_by_index
        if item is not None
    )
    _apply_deterministic_placeholder_review(results, jobs)
    total_elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    review_result = {
        "mode": "template_text",
        "template_text_reviews": results,
        "navigation_exclusions": {
            "tender_templates": excluded_templates,
            "bid_modules": excluded_sections,
        },
        "stats": _review_stats(
            template_count=len(extracted_templates),
            participating_template_count=len(templates),
            navigation_excluded_templates=excluded_templates,
            navigation_excluded_bid_sections=excluded_sections,
            matched_template_count=matched_template_count,
            code_candidate_count=len(jobs),
            no_bid_candidate_template_ids=no_bid_candidate_template_ids,
            candidate_without_reliable_bid_text_template_ids=(
                candidate_without_reliable_bid_text_template_ids
            ),
            results=results,
            llm_total_calls=sum(item[1] for item in executions),
            llm_completed_calls=sum(item[2] for item in executions),
            main_wall_elapsed_ms=main_wall_elapsed_ms,
            exception_review_candidate_count=len(exception_jobs),
            exception_review_call_count=exception_review_call_count,
            exception_review_confirm_count=exception_review_confirm_count,
            exception_review_revise_count=exception_review_revise_count,
            exception_review_failed_count=exception_review_failed_count,
            exception_review_llm_elapsed_ms=exception_review_llm_elapsed_ms,
            exception_review_wall_elapsed_ms=exception_review_wall_elapsed_ms,
            total_elapsed_ms=total_elapsed_ms,
        ),
    }
    if recorder is not None:
        recorder.write_json("08_template_text_reviews.json", review_result)
        recorder.event(
            "template.text.review.end",
            status="complete",
            template_count=len(templates) + len(excluded_templates),
            participating_template_count=len(templates),
            navigation_excluded_template_count=len(excluded_templates),
            navigation_excluded_bid_section_count=len(excluded_sections),
            matched_template_count=matched_template_count,
            llm_total_calls=review_result["stats"]["llm_total_calls"],
            llm_failed_count=review_result["stats"]["llm_failed_count"],
            llm_elapsed_ms=review_result["stats"]["llm_elapsed_ms"],
            exception_review_candidate_count=review_result["stats"][
                "exception_review_candidate_count"
            ],
            exception_review_call_count=review_result["stats"][
                "exception_review_call_count"
            ],
            exception_review_failed_count=review_result["stats"][
                "exception_review_failed_count"
            ],
        )
    return review_result
