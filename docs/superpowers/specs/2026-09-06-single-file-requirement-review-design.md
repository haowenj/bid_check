# 单份投标文件自身属性检查设计

**日期：** 2026-09-06
**状态：** 规划中，待用户确认后实现

## 目标

在现有招标文件提取、投标文件解析、合规检查、产出物和 Web 结果页主流程中，新增“单份投标文件自身属性检查”。本轮只处理能够直接依赖当前这一份投标文件的原始文件名、后缀和真实字节大小确定性判断的明确要求。

## 范围边界

正式 `file_requirements` 只允许以下约束对象：`single_bid_file`。

允许进入正式提取结果的类型：

- 单份电子投标文件大小上限、下限或明确精确值；
- 单份电子投标文件格式/后缀要求；
- 单份投标文件名称的明确命名规则；
- 明确针对单份文件自身元数据、但当前代码暂不支持确定性编译的其他规则。此类规则必须保留为 `auto_checkable=false` 和“无法自动检查”。

以下内容由 LLM 过滤，不进入正式 `file_requirements` 或最终合规汇总：多文件数量、商务/技术/报价分别提交、备份文件、多格式组合、U 盘/光盘、纸质正副本、密封包装、外部标记、文件之间的关系，以及“系统最大支持上传 500MB”这类没有形成投标约束的平台注册能力说明。

本轮保留现有 Web 的单文件 `.docx` 上传限制，不扩展多文件上传或新的解析格式。检查器仍基于 `FileMetadata.filename` 记录的原始上传名称和磁盘上原始上传字节流的 `stat().st_size` 工作；如果以后放开上传格式，后缀检查无需改变。

## 数据流

```text
招标文件
  → 现有 MinerU 结构化 blocks
  → 结构化完整候选章节/区域
  → 单次 file_requirements LLM 提取
  → 来源 block 校验、范围过滤和参数归一化
  → TenderExtractionResult.file_requirements
  → 现有投标文件 MinerU 解析并保留原始 FileMetadata
  → 单文件确定性检查
  → 10_file_requirement_reviews.json
  → review_result.file_requirement_reviews
  → 现有 Web 统一汇总与结果页
```

文件属性提取属于现有 requirements 阶段；文件属性执行属于现有 review 阶段，不新增孤立入口或独立任务状态。

## 招标候选区域

新增 `build_file_requirement_candidates(blocks)`，复用 `StructuredBlock`、heading level、`section` 和既有功能区域识别结果。

候选选择按结构优先、语义辅助的方式完成：

1. 以章节标题、heading 层级、`section` 边界和原文顺序组装完整区域，而不是对单个关键词命中的 block 做 RAG 召回。
2. 对“投标人须知前附表”保留从标题到同级章节结束的完整区域；表格及其一个单元格中的多个要求保持在同一候选上下文中。
3. 对“投标人须知”“投标文件组成/编制/制作/递交”“电子投标文件”“上传/加密/解密”“特别说明”“补充条款”等语义标题提高候选优先级；标题识别结合结构和同义语义，不依赖固定章节号。
4. 对模板正文、函件/承诺书模板、合同条款、技术需求、工程量清单、评标/评分等大块内容施加排除信号；若前附表与这些词在同一完整区域中出现，前附表结构优先保留。
5. 重叠候选按 block id 去重并按原始顺序合并，最终上下文一次性送入 LLM。记录候选数量、block 数量、候选文本字符数、完整请求字符数和选择原因。

## LLM 协议

复用现有 `OpenAICompatibleLLM`/确定性 fallback，新增独立方法 `extract_file_requirements(candidates)`，不改变旧的三类对象协议。生产 LLM 的一次请求只返回：

```json
{
  "file_requirements": [
    {
      "name": "电子投标文件大小限制",
      "requirement": "电子投标文件不得超过200MB",
      "target": "single_bid_file",
      "requirement_type": "size",
      "constraint_status": "explicit_constraint",
      "parameters": {"operator": "max", "value": 200, "unit": "MB"},
      "auto_checkable": true,
      "support_reason": "可用原始文件字节数比较",
      "source_block_ids": ["b0042"]
    }
  ]
}
```

LLM 不返回 id 和 `source_text`；后端只接受候选真实 block id，并根据 block 重建来源原文。`constraint_status` 必须为 `explicit_constraint` 才能进入正式结果；`platform_capability` 和 `uncertain` 记录为过滤事件但不进入正式产出物。`target` 不是 `single_bid_file` 的条目直接过滤。

参数规则：

- `size` 保存原始数值和单位，后端解析 B/KB/MB/GB 及二进制 MiB 等单位，并写入统一 `limit_bytes`；同时保留原始要求文字。
- `extension` 保存规范化后的允许后缀列表，例如 `[".pdf"]`，比较时大小写不敏感。
- `filename` 只将 LLM 明确识别出的字面量前缀、后缀、必含字符串或禁用字符串交给确定性检查；“应包含项目名称和投标人名称”但没有提供具体字面值时保留完整规则并标为不可自动检查，不把“项目名称”这种字段标签误当成真实文件名值。
- `other` 只有在明确针对单份文件自身属性时保留；如果没有安全的程序化比较器，标为不可自动检查。

确定性 fallback 也从完整候选区域提取明确规范句，但必须拒绝“系统/平台最大支持上传”“可上传容量”等能力描述，不能把它们编译为大小限制。

## 文件级检查产出物

新增 `app/file_requirement_review.py`，入口为：

```python
def run_file_requirement_review(
    extraction_result: dict[str, Any],
    parsed_bid: dict[str, Any],
    *,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]: ...
```

执行器从 workflow 注入的 `parsed_bid["original_file_metadata"]` 重建当前上传文件信息，并使用：

- 原始上传文件名；
- 原始上传文件后缀；
- 原始存储路径 `stat().st_size`；
- MIME 推断值和 SHA-256（审计信息）。

不会读取 `bid_document_cleaning` 下的 JSON、Markdown、PDF、OCR 图片或 MinerU ZIP 来代替原始文件大小/格式。

每条 review 至少包含：`requirement_id`、名称、要求原文、要求类型、约束对象、结构化参数、来源、`auto_checkable`、实际原始文件元数据、要求值、`status`（`pass`/`fail`/`not_supported`）、`status_label`（合规/不合规/无法自动检查）、实际值和问题说明。

所有规则都写入：

```text
<task_dir>/compliance_extraction/10_file_requirement_reviews.json
```

文件同时记录 schema 版本、候选文本字符数、LLM 提取统计、规则统计、原始文件元数据和执行状态。页面和统一问题计数通过 artifact reader 优先读取这份文件，缺失时才回退到任务 JSON 中的 `review_result.file_requirement_reviews`，确保最终汇总确实消费独立产出物。

## 现有流程集成

`TenderExtractionResult` 增加可兼容读取的 `file_requirements` 集合，继续写入现有 `07_result.json` 和缓存。`BidCheckWorkflow` 在交给 review 服务的投标解析字典中补充当前任务的 `original_file_metadata`，不改变已有解析 artifact。

`run_compliance_review_with_attachments` 在模板文本、普通附件、业绩合同检查之后调用文件属性检查，并把结果合并到 `review_result`。现有模板/附件/合同结果字段和调用顺序保持不变；文件属性结果为空时也生成空的独立 artifact，便于审计。

## Web 展示

结果页三种现有展示风格都增加“文件属性”分类和计数。默认工作台及详细结果区展示全部文件属性规则，每行包含：

- 检查项和类型；
- 招标要求及原文依据；
- 实际文件名、后缀和字节数；
- 合规、不合规或无法自动检查；
- 规则参数、来源 block id 和说明。

问题汇总计数将 `fail` 和 `not_supported` 计为需关注项，`pass` 不计入问题数但仍在文件属性明细表中展示。现有模板、附件、合同页面交互和视觉样式继续复用，不增加独立入口。

## 验收与测试

测试必须先写出失败断言，再实现最小代码。覆盖：

1. 200MB 上限与磁盘真实字节数比较，确认不使用转换产物大小；
2. `.PDF`/`.docx` 后缀大小写归一化和不允许后缀；
3. 明确字面文件名规则的通过/失败；动态“项目名称/投标人名称”规则保留但不可自动检查；
4. 同一前附表单元格拆分出大小、格式、文件名三条规则；
5. “系统最大支持上传500MB”被过滤；
6. 多文件、备份、纸质、U 盘、密封等被过滤；
7. 独立 artifact 完整写入、来源重建、原始 metadata 正确；
8. 统一 review、API、页面和三种展示风格可见；
9. 当前已有 275 个测试及基于现有真实任务目录的端到端运行结果不回归。
