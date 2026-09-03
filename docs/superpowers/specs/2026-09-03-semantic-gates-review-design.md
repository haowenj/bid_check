# 两条检查链 LLM 语义确认闸门设计

## 目标

在模板文本检查和普通附件/证明材料检查的现有单次 LLM 调用中增加语义确认闸门，阻止标题、关键词或规则造成的错误候选直接形成业务检查结论，同时保留候选误召回数据和后续漏召回监控所需的统计信息。

## 范围与边界

- 每个模板文本候选仍只进行一次 LLM 调用；附件候选仍只进行一次多模态 LLM 调用。
- 语义确认必须发生在同一次调用的正式业务判断之前，由模型返回结构化语义状态和理由。
- 不修改现有标题匹配、关键词匹配、候选生成、图片提取、表格内嵌图片提取、结构化解析、图片路径、image_id、并发和重试机制。
- 不重新设计现有模板文本 issue 类型、模板状态、附件材料/事实/要求结构或条件适用性规则。
- 语义不匹配或不确定只能表示候选筛选状态，不能被转化为投标文件或附件业务不合规。

## 模板文本检查

模板文本提示词新增 `semantic_match` 对象：

```json
{
  "status": "matched | mismatched | uncertain",
  "reason": "语义、用途和内容对应关系的判断理由"
}
```

模型必须综合文件用途、主要填写对象、核心响应内容，排除仅由标题相似、关键词重复、主题词相同或局部文字重合产生的误匹配。只有 `matched` 才允许执行现有 `missing_fill`、`missing_content`、`substantive_change` 和 `other` 检查。

结果继续保留现有顶层 `status`、`summary` 和 `issues` 字段，同时增加 `business_status` 与 `execution_status`：

- 语义匹配时，`business_status` 等于现有业务状态，`execution_status` 为 `completed`；
- 语义不匹配或不确定时，`business_status` 为 `not_run`，`execution_status` 为 `semantic_skipped`，`issues` 必须为空；顶层 `status` 保留为兼容性的 `uncertain`，不计入业务状态统计；
- LLM 调用或响应解析失败时，语义状态为 `uncertain`，业务状态为 `not_run`，`execution_status` 为 `failed`，并保留错误信息。

## 普通附件检查

附件提示词新增同结构的 `semantic_match` 对象。模型必须先确认当前候选确实是招标文件明确要求投标时额外提交的独立证明材料。模板正文、表格填写、承诺函本身、签字盖章位置、未来履约义务和非独立附件内容不得进入正式附件检查。

只有 `matched` 才继续现有 materials、facts、requirements、evidence_image_ids、条件适用性和状态聚合逻辑。`mismatched` 或 `uncertain` 时保留候选记录，但不形成附件 `pass` 或 `fail`，也不因图片为空判定材料缺失。

## 统计与可追踪性

模板文本统计增加代码候选数、semantic matched/mismatched/uncertain 数、无投标候选模板 ID 和候选无可靠正文模板 ID；附件统计增加代码附件候选数、语义确认候选数、确认的 requirement 数、semantic mismatched/uncertain 数以及无投标候选模板 ID。现有业务 `pass_count`、`fail_count`、`uncertain_count` 和 LLM 耗时统计只统计实际执行正式业务检查的结果。

现有 recorder 继续保存完整提示词、原始响应、解析结果、错误和耗时。页面在模板对比表和明细中展示语义状态与理由，并将语义跳过显示为“未执行业务检查”，不显示为“不合规”。

## 验证

- 新增模板语义匹配、语义不匹配、语义不确定的先失败后通过测试。
- 新增附件候选边界语义闸门和业务状态隔离测试。
- 回归现有图片、表格内嵌图片、条件附件、并发、重试、recorder 和页面测试。
- 使用当前真实招标文件与投标文件重新执行两条检查链，记录代码候选数、语义分类、正式业务结果和两轮真实墙钟耗时。
