# 主观评标评分执行设计

## 目标

在已有 `11_evaluation_rules.json` 和投标文件结构化解析产物的基础上，独立执行主观评分项，生成可审计的 `subjective_scores.json`。本阶段只处理 `evaluation_type = subjective` 的评分项，不重复执行客观评分，不执行否决规则，不计算总分、排名或中标结果。

当前真实样例中的 6 个主观评分项为：

- `score_item_001`：投标文件编写质量的情况；
- `score_item_003`：项目需求的分析及理解程度；
- `score_item_004`：云网产品开发服务、大模型技术支持服务、云网产品测试服务支撑方案；
- `score_item_005`：云网产品预集成服务、移动整合开发服务、项目研发管理平台迭代优化服务支撑方案；
- `score_item_006`：研发效益管理平台迭代优化服务、全栈信创适配服务、信创集成测试服务支撑方案；
- `score_item_009`：质量服务保障措施。

在当前仅有商务投标文件的验收场景下，只有能够从现有商务结构化内容定位到的项目才允许进入模型判断；需要技术标、服务方案而当前文件范围没有对应内容的项目必须返回 `file_scope_missing`，不得调用模型，也不得按 0 分处理。

## 非目标

- 不修改评标规则抽取逻辑、规则 Schema 或 `11_evaluation_rules.json`；
- 不筛选或执行 `objective`、`mixed` 评分项；
- 不读取其他投标人的报价或评分数据；
- 不执行 `veto_rules`；
- 不把 `recommended_score` 当成确定性客观分；
- 不启动 MinerU、OCR 或新的整份投标文件解析；
- 不对缺失文件范围作“未提供 0 分”的推断；
- 不输出评分项汇总分、投标人总分、排名或中标候选人。

## 设计选择

采用独立执行入口，而不是把主观评分追加到现有默认评标工作流。原因是本阶段的边界要求明确禁止重复执行客观评分和执行否决规则；独立入口可以只消费既有规则和既有投标证据，并将结果写成独立产物。

不采用六项合并一次模型调用。每个能够评分的项目独立调用一次模型，便于保持评分项边界、记录每项耗时和审计输入；主观项之间通过线程池并发执行。缺少范围或匹配证据的项目在模型调用之前结束。

## 组件与接口

### `app/subjective_scoring.py`

新增模块只负责主观评分执行，包含以下边界清晰的接口：

```python
def select_subjective_items(
    evaluation_rules: Mapping[str, Any],
) -> list[dict[str, Any]]: ...

def match_subjective_bid_content(
    item: Mapping[str, Any],
    bid_document: Mapping[str, Any] | None,
    bid_filename: str,
) -> dict[str, Any]: ...

def run_subjective_scoring(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    subjective_llm: SubjectiveScoreLLM,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]: ...
```

`SubjectiveScoreLLM` 接口接收一个已经裁剪的规则和投标内容上下文，返回一个 JSON 对象。实现包括：

- `OpenAICompatibleSubjectiveScoreLLM`：使用现有 OpenAI-compatible 配置，温度为 0，要求 JSON 输出，并通过 recorder 保存脱敏请求、原始响应、usage 和耗时；
- `DeterministicSubjectiveScoreLLM`：未配置模型时只返回无法完成 AI 判断的结构，不生成建议分，不伪造证据。

### `app/workflow.py` 与 `app/api.py`

`BidCheckServices` 新增可选的 `score_subjective_with_recorder` 回调。`BidCheckWorkflow.run_subjective(task_id)` 是独立执行方法：

1. 读取任务中已有的评标规则结果，或读取任务目录的 `11_evaluation_rules.json`；
2. 调用主观评分服务；
3. 不调用 `extract`、`parse`、`score_objective_with_recorder` 或 `execute_veto_with_recorder`；
4. 将 `subjective_scores` 合并回任务结果，但不改变已有阶段状态；
5. 由 API 提供独立触发和查询结果入口。

既有 `BidCheckWorkflow.run()` 的评标模式行为保持不变，避免本阶段改动改变历史评标任务的客观/否决执行语义。

## 投标内容复用与范围判定

执行器首先调用现有 `load_reusable_bid_evidence`，从真实投标文件路径旁的 `bid_document_cleaning/structured_document.json` 读取结构化文档，并校验 `source.sha256` 与原始投标文件一致。可复用的 `08/09/10` 检查产物只作为已经存在的补充证据，不启动新的检查。

以下约束必须成立：

- `bid_document` 存在且哈希通过时，直接使用其中的 `blocks`、`sections`、`tables` 和必要的 OCR 文字；
- 不调用 `MinerUBidDocumentParser`、OCR 或其他解析器；
- 不把 `bid_document` 全量序列化到模型请求；
- 送模上下文只包含匹配的 block 及其 `block_id`、章节、类型和原文；
- 索引表、目录、对应页码等导航块不能作为主观评分内容证据；
- 评分项没有匹配证据时返回 `evidence_insufficient`，不调用模型；
- 评分项要求技术标、技术响应、技术方案、服务方案等当前文件范围没有的内容时返回 `file_scope_missing`，不调用模型。

范围判定优先使用结构化章节标题和文件名的保守信号。文件名包含“商务”且结构化章节没有技术标、技术响应、服务方案等章节时，视为商务文件范围；商务文件中的招标文件引用、索引表或业绩正文中的“技术服务”文字不能证明技术标存在。

当前样例中，`score_item_001` 应定位到商务标 `13.5 评审要求承诺函` 的实际正文 block；`score_item_003` 至 `score_item_006` 需要技术方案或技术响应；`score_item_009` 需要服务质量保障措施。后五项在只有商务标且没有对应正文时返回 `file_scope_missing`。

## 模型输入与评分约束

每个可评分项目独立发送一次请求。模型输入包含：

- `score_item_id`、评分项名称、满分；
- `original_rule`、`conditions`、`scoring_method`、`evidence_requirements`；
- 从投标结构化文档裁剪出的带 block 标识内容；
- 明确指令：只能依据该评分项原始标准判断，不得增加招标文件没有的评价维度，不得把其他评分项或否决条件带入判断；
- 必须先选择原始规则中的评分档，再在该档范围内给 `recommended_score`；
- 必须给出与具体 block 对应的直接证据摘录；无法由输入确认时返回不确定，不得猜测。

模型输出的 `score_band` 只允许是评分标准中可还原的档位。对于 `[4,5]`、`[2,4)`、`(0,2)` 和未提供 0 分等标准，执行器验证区间边界和建议分；对于 `score_item_001` 这类“每具备一项扣 1 分，扣完为止”的规则，`score_band` 固定表达为“扣分规则”，模型必须列出实际判断的扣分项，建议分不得低于 0 或高于满分。

模型结果落盘前必须校验：

- `recommended_score` 是有限数字，且在 `0..max_score` 内；
- 显式区间项的建议分满足左闭右开/闭区间边界；
- `score_band` 与规则中可识别的评分档一致；
- `reason` 非空；
- `evidence` 非空，且每条证据的 `block_id` 属于本次送模 block；
- 证据引用不能来自未送模的整份文档；
- 校验失败时该项为 `llm_error` 或 `evidence_insufficient`，不写入建议分。

## 产物契约

使用现有 `ComplianceExtractionRecorder` 在招标文件任务目录的 `compliance_extraction/subjective_scores.json` 写入：

```json
{
  "schema_version": "subjective-score-v1",
  "source": {
    "evaluation_rules_artifact": "11_evaluation_rules.json",
    "bid_document_artifact": ".../structured_document.json",
    "bid_document_hash_verified": true,
    "bid_filename": "商务投标文件部分.docx"
  },
  "score_items": [
    {
      "score_item_id": "score_item_001",
      "rule_name": "投标文件编写质量的情况",
      "max_score": 5.0,
      "status": "ai_scored",
      "score_band": "扣分规则",
      "recommended_score": 5.0,
      "reason": "...",
      "matched_bid_content": [
        {
          "block_id": "b0218",
          "section": "13.5 评审要求承诺函",
          "type": "paragraph",
          "text": "..."
        }
      ],
      "evidence": [
        {
          "block_id": "b0218",
          "quote": "...",
          "relation": "对应评分标准中的具体判断"
        }
      ],
      "block_ids": ["b0218"],
      "uncertainty": {"level": "low", "notes": []}
    }
  ],
  "stats": {
    "subjective_item_count": 6,
    "ai_scored_count": 1,
    "file_scope_missing_count": 5,
    "llm_total_calls": 1,
    "llm_completed_calls": 1,
    "llm_failed_calls": 0,
    "llm_elapsed_ms": 0,
    "llm_call_elapsed_ms": [],
    "bid_parse_reused": true,
    "new_parse_calls": 0,
    "new_ocr_calls": 0,
    "new_mineru_calls": 0,
    "duplicate_parse": false,
    "total_score_computed": false,
    "ranking_computed": false,
    "veto_executed": false
  }
}
```

实际 `llm_call_elapsed_ms` 和 `llm_elapsed_ms` 由执行器写入；示例中的 0/空数组只是契约形状示意。每个评分项都必须有一条记录，包含 `score_item_id`、`rule_name`、`max_score`、`status`、`score_band`、`recommended_score`、`reason`、`matched_bid_content`、`evidence`、`block_ids` 和 `uncertainty`。非 `ai_scored` 状态的 `recommended_score` 为 `null`。

执行器不会写入 `score_sum`，也不会在独立产物中生成排名或中标字段。

## 并发、调用与故障处理

使用最多 5 个并发 worker 执行可评分项目。每项最多一次必要的模型调用，不做自动重试；因此 `llm_total_calls` 等于实际送出的项目数，而不是主观项总数。每次调用记录项目索引、模型、开始/结束时间、耗时、usage、响应和 Schema 校验结果。

单项模型失败不影响其他项目：该项返回 `llm_error`，建议分为空，并在 `uncertainty` 中记录失败原因；其他项继续完成。结构化文档不存在、哈希校验失败或没有匹配内容时不触发解析补偿，返回可审计的 `evidence_insufficient` 或 `file_scope_missing`。

## 测试与验收

新增单元测试和集成测试覆盖：

1. 只选择 6 个 `subjective` 项，排除 8 个 `objective` 项、`mixed` 项和全部否决规则；
2. 导航索引不被当作评分内容，商务 `score_item_001` 能定位到正文 block；
3. 技术/服务范围缺失时 5 项均为 `file_scope_missing`，模型调用数为 0；
4. 模型请求只包含匹配 block，不包含整份结构化文档；
5. 可评分项并发执行且每项仅调用一次；
6. `[4,5]`、`[2,4)`、`(0,2)`、0 分和扣分规则的档位/边界校验；
7. 非法建议分、非法档位、无 block 证据的响应不会生成 AI 建议分；
8. 产物包含 6 条逐项记录、证据 block 可回溯，并明确无总分/排名/否决；
9. `run_subjective` 和 API 独立入口不会调用客观评分、否决、OCR、MinerU 或投标文件重解析；
10. 现有全量测试保持通过。

真实验收使用当前已有的 `11_evaluation_rules.json` 和商务投标文件结构化产物，逐项展示 6 条结果、成功 AI 评分项、`file_scope_missing` 项、评分档、建议分、理由、证据 block、LLM 调用次数/耗时及零次新增 OCR/MinerU/整份解析的统计。
