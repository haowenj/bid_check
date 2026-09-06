# 否决性规则执行设计

## 目标与范围

本阶段在已有 `11_evaluation_rules.json` 基础上，逐条执行正式 `veto_rules`，生成独立、可审计的 `veto_rule_reviews.json`。执行器只消费招标文件中已经结构化的规则和当前已有投标文件检查产物，不新增整份投标文件的 LLM 审查，不执行主观评分、最终总分、排名或中标候选人推荐。

本阶段的核心安全边界是：只有当前证据明确证明某条否决规则的触发条件成立，才返回 `triggered`。普通检查失败、未找到材料、检查能力未覆盖、文件范围不全、单份文件无法比较、缺少外部事实或需要评标委员会认定，均不得被静默升级为否决。

## 非目标

- 不修改评标规则提取提示词、抽取 Schema、批次策略或已有 `11_evaluation_rules.json` 生成逻辑。
- 不重新 OCR 或重新解析已经存在且哈希匹配的投标文件结构化产物。
- 不新增外部网站、供应商系统或其他投标人数据访问能力。
- 不修改现有评标详情页；页面展示安排在后续阶段。
- 不生成一个笼统的最终“否决/不否决”结论；本阶段只输出每条规则的独立执行结果及统计。

## 现有系统接入点

当前系统已经有：

- `app/evaluation_rule_extraction.py`：生成 `11_evaluation_rules.json` 和 `12_evaluation_filter_report.json`；
- `app/objective_scoring.py`：读取已解析投标文件及 `08/09/10` 系列产物，并生成 `objective_scores.json`；
- `app/compliance_artifacts.py`：为任务目录提供原子 JSON 写入和执行事件记录；
- `app/workflow.py`：评标模式执行规则提取和客观评分后结束；
- `app/api.py`：构建默认服务并把评标结果写回任务结果。

新增执行器接在客观评分之后。旧的评标工作流测试中未配置否决执行回调时，仍保持原有结果结构；默认工作流配置回调后，会额外写入否决执行产物并将其放入任务结果。

## 架构

### 1. 独立执行模块

新增 `app/veto_rule_execution.py`，公开：

```python
def run_veto_rule_execution(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
    objective_scores: Mapping[str, Any] | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]: ...
```

该函数不调用 LLM、OCR 或文档解析器。它复用客观评分模块已经具备的哈希校验投标证据加载能力；无法读取某个普通检查产物时，保留缺失诊断并按规则依赖返回保守状态。

规则处理采用“规则原文信号 + 结构化事实适配器”的确定性执行方式，不把当前真实文件的规则编号或名称写死。适配器首先识别规则的证据依赖，再确认事实覆盖程度，最后才执行对应谓词。无法可靠识别的规则保留 `evidence_insufficient` 或 `manual_review_required`，不通过猜测补齐触发条件。

### 2. 工作流接入

评标模式顺序为：

```text
11_evaluation_rules.json
        ↓
objective_scores.json（若配置）
        ↓
veto_rule_reviews.json（若配置）
        ↓
任务完成
```

工作流为否决执行记录独立的 `veto_rule_execution` 起止事件和耗时。否决执行异常按评标结果阶段失败处理，不产生部分成功的否决结论。

默认 API 服务的回调复用既有投标文件产物：

1. 先按真实投标文件路径读取 `structured_document.json`，校验来源哈希；
2. 读取同一真实文件目录下的 `08_template_text_reviews.json`、`09_attachment_reviews.json`、`10_file_requirement_reviews.json`、`10_performance_reviews.json`；
3. 如果客观评分阶段已经完成一次必要的解析，否决执行只重新读取产物，不再解析；
4. 只有没有可用结构化投标产物时，沿用现有一次性解析回退，并在来源统计中明确记录。

## 执行状态

每条规则都包含 `status` 和 `triggered`。`triggered` 仅在 `status == "triggered"` 时为 `true`。

- `triggered`：直接证据完整，且明确满足规则触发条件。
- `not_triggered`：规则所需事实已经完整覆盖，并明确证明触发条件不成立。
- `evidence_insufficient`：规则可由投标文件判断，但当前事实缺失、不完整或冲突。
- `file_scope_missing`：当前上传文件范围不包含判断该规则所需部分，例如只有商务标而规则针对技术标 ★ 条款。
- `external_data_required`：需要信用、处罚、供应商管理等当前未接入的外部事实。
- `other_bidder_data_required`：需要其他投标文件、报价或多投标人横向比较。
- `manual_review_required`：需要评标委员会、解释说明、现场认定、后续评标过程或法律事实判断。

`not_triggered` 只在完整覆盖下使用；“没有发现问题”或某个模块没有返回结果不能作为充分证据。

## 事实与证据规则

执行器只读取具体事实和 issue，不直接把任何上游 `overall_status = fail` 映射为否决。每个事实记录其：

- 来源产物和条目索引；
- 结构化状态、值和事实说明；
- 投标证据 block id；
- 证据 image id 及可用的资源路径/描述；
- 覆盖状态和是否可以支撑当前谓词。

对于 `triggered`，输出必须能沿以下链路追溯：

```text
招标规则 source
 → 触发条件
 → 规则所需事实
 → 已确认事实
 → 上游产物条目
 → 投标 block/image
 → triggered
```

若上游 issue 只说明业绩表金额与合同金额不一致、普通模板缺项或附件检查未完成，则只能保留对应事实，不能自动推导“弄虚作假”或“否决投标”。

## 规则执行策略

执行器根据规则原文和 `trigger_condition` 的实际信号选择以下确定性策略；不按示例文本伪造规则。

### 初步评审汇总规则

只有当具体子规则已经明确触发，且该子规则确实属于正式初步评审项时，汇总规则才可触发，并通过 `triggered_by` / `parent_rule_ids` 记录上下位关系。若资格、形式、响应性等正式评审项尚未全部覆盖，则为 `evidence_insufficient`，不能返回 `not_triggered`。

### ★ 实质性条款

若规则针对技术标或其他当前不存在的文件范围，返回 `file_scope_missing`。存在对应文件但 ★ 条款没有完成逐项可靠检查时，返回 `evidence_insufficient`。只有逐项事实明确显示某项不满足，才可 `triggered`。

### 非实质性条款数量阈值

只累计明确确认的非实质性不满足项。`uncertain`、`file_scope_missing`、未检查和解析失败不计数。只有在完整条款覆盖下，超过原文数量阈值才 `triggered`；完整覆盖且未超过阈值才 `not_triggered`。

### 低于成本报价

报价偏低本身不构成触发。规则要求的异常识别、要求说明、说明/证明不足和评委认定等环节缺少任何一环时，返回 `manual_review_required` 或 `evidence_insufficient`，不自动否决。

### 串通投标或异常一致

单份投标文件不能完成横向比较，返回 `other_bidder_data_required`，不尝试推测关联或串通。

### 弄虚作假

材料不一致只作为一致性事实保留。没有直接、足够的虚假行为证据和规则对应关系时，返回 `evidence_insufficient` 或 `manual_review_required`，不从普通 fail 升级为“弄虚作假”。

### 算术修正拒绝接受

静态投标文件无法证明后续是否拒绝算术修正，返回 `manual_review_required`。

### 直接材料/签章/资格规则

只有规则原文明确把某项材料、签章或资格事实与否决后果绑定，并且现有附件、文件要求或模板检查产物有同一条件的明确 fail，才可 `triggered`。普通文件要求失败若无法和当前规则建立直接对应关系，只返回 `evidence_insufficient`，不触发。

## 产出物结构

`veto_rule_reviews.json` 顶层结构：

```json
{
  "schema_version": "veto-rule-review-v1",
  "source": {
    "evaluation_rules_artifact": "11_evaluation_rules.json",
    "bid_filename": "投标文件.docx",
    "bid_path": "...",
    "bid_document_artifact": ".../structured_document.json",
    "bid_document_hash_verified": true,
    "reused_artifacts": [],
    "missing_artifacts": [],
    "objective_scores_artifact": "objective_scores.json"
  },
  "veto_rule_reviews": [
    {
      "id": "veto_001",
      "name": "...",
      "original_rule": "...",
      "trigger_condition": "...",
      "consequence": "...",
      "additional_consequence": null,
      "evidence_requirements": [],
      "tender_rule_source": {
        "section": "...",
        "block_ids": ["..."],
        "source_text": "..."
      },
      "status": "evidence_insufficient",
      "triggered": false,
      "facts_required": [],
      "confirmed_facts": [],
      "reason": "...",
      "evidence": [],
      "bid_evidence": {
        "block_ids": [],
        "image_ids": []
      },
      "related_artifacts": [],
      "dependencies": {
        "external_data_required": false,
        "other_bidder_data_required": false,
        "manual_review_required": false
      },
      "parent_rule_ids": [],
      "triggered_by": []
    }
  ],
  "stats": {
    "formal_rule_count": 0,
    "triggered_count": 0,
    "status_counts": {},
    "llm_total_calls": 0,
    "ocr_reused": true,
    "duplicate_parse": false
  }
}
```

允许在不破坏上述字段的前提下增加诊断字段，但不得删除规则原文、来源、状态、事实、证据、产物关联或依赖标记。顶层不包含最终总分、排名或全局否决结论。

## 测试与验收

先按 TDD 添加失败测试，再实现以下行为：

1. 逐条保留 12 条正式规则，排除非正式不确定规则；
2. 规则来源 block、原始条件和后果完整保留；
3. 缺少材料不等于触发，普通 `fail` 不等于触发；
4. 商务文件范围不足时为 `file_scope_missing`，不默认满足或否决 ★ 条款；
5. 非实质性条款只累计明确 fail，阈值边界正确；
6. 低价、串通、弄虚作假、算术修正分别落到人工/其他投标人/证据不足状态；
7. 初步评审汇总规则只在正式子规则触发后关联触发，不重复计数；
8. 任意 `triggered` 都有直接证据、上游产物引用和 block/image 追溯；
9. 工作流在规则提取、客观评分后调用执行器并写入独立 JSON；
10. 评标模式未配置回调的旧行为保持兼容；
11. 执行器不新增 LLM/OCR 调用，并记录复用、缺失和解析回退统计；
12. 全量测试通过后，使用当前可获得的同一招标文件和商务投标文件核验 12 条规则、状态分布、触发证据链、复用产物情况、LLM/OCR/解析调用情况。
