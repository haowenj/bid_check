# 标书检查

新版“标书检查”Web 工作流。招标文件的合规性要求提取已接入真实的结构化文档解析、候选筛选、批量结构化抽取、来源回填和缓存；投标文件解析与真实合规审查仍为模拟实现。

## 启动

```bash
uv sync --dev
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/bid-check>。

## 页面与接口

- `GET /`：跳转到标书检查页面；
- `GET /bid-check`：上传两个 DOCX 并选择校验方式；
- `POST /api/bid-check/tasks`：创建标书检查任务；
- `GET /bid-check/tasks/{task_id}`：查看工作流状态或结构化结果；
- `GET /api/bid-check/tasks/{task_id}`：查询任务状态和结果 JSON。

## 第一版范围

- 仅支持招标文件和投标文件 `.docx` 上传；
- 仅“标书合规性校验”可执行；
- “评标规则校验”和“全面校验”仅展示开发中入口；
- 招标文件要求提取默认使用本地 DOCX 结构解析；配置 `MINERU_COMMAND` 后通过 MinerU 命令适配器解析；
- 配置 `LLM_API_KEY` 后使用 OpenAI-compatible Chat Completions 做结构化要求抽取，否则使用基于候选原文的确定性本地回退；
- 候选内容默认最多分成 8 个模型批次，硬上限为 10；MinerU/结构解析结果和最终要求结果分别按招标文件内容哈希缓存；
- 结果页仅展示从招标文件来源块恢复出的合规性检查要求；
- 投标文件解析和合规性检查仍为模拟实现，不判断通过、不通过、得分或废标风险。

## 真实要求提取配置

可选环境变量：

- `MINERU_COMMAND`：MinerU 命令模板。命令标准输出应为 JSON 数组，或包含 `blocks`/`content`/`items` 数组；可用 `{input}` 占位符接收输入路径。
- `LLM_API_KEY`：启用 OpenAI-compatible LLM；同时可设置 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_MAX_TOKENS`（默认 8192）、`LLM_TIMEOUT_SECONDS`。
- 项目根目录可放置本地 `.env`，配置项命名与合同审查项目一致；当前真实运行配置使用 DashScope、`LLM_MODEL=qwen3.8-27b` 和 `LLM_ENABLE_THINKING=false`。`.env` 不纳入 Git。
- `COMPLIANCE_MAX_BATCHES`：候选批次数，范围 1–10，默认 8。

未配置外部 MinerU 时，系统使用 DOCX XML 结构回退；未配置 LLM 时，系统使用确定性、来源受限的本地抽取器。对有效 DOCX 不会返回固定五条 mock 规则；历史非 DOCX 测试字节仅保留兼容回退。

## 处理日志

工作流和要求提取链路使用 Python `logging` 输出详细阶段日志。默认启动命令会在终端显示 `start`、`end`、`retry` 和 `error` 事件，包括文件名、批次、数量、模型名和耗时；不会记录 API Key、完整提示词或招标文件原文。重点事件前缀包括 `workflow.*`、`document.parse.*`、`candidate.filter.*`、`compliance.batch.*`、`llm.call.*` 和 `requirements.normalize.*`。

每个任务的招标文件目录下会生成 `compliance_extraction/`：`execution.jsonl` 保存结构化执行事件，`01_parsed_blocks.json`、`02_candidates.json`、`03_batches.json`、`04_raw_requirements.json`、`05_normalized_requirements.json` 保存阶段产物，`06_filter_report.json` 保存确定性边界过滤的条目和原因，`llm/call_NNN_input.json` 和 `llm/call_NNN_output.json` 保存每次调用的批次、脱敏请求、原始响应、解析结果、耗时、finish reason、usage 和错误信息，`summary.json` 保存提取统计，`workflow_summary.json` 保存并行解析、复核和整个任务耗时。阶段文件在成功后立即写入，后续失败不会清理已有文件。`data/mineru_cache/` 独立保存按解析器版本、配置和文件内容哈希得到的结构解析结果；要求结果缓存还会区分解析器、LLM 模型和批次参数，避免旧结果遮蔽新的提取配置。

最终结果使用简化的 `TenderRequirement`：`id`、`name`、`rule`、`condition` 和 `source(section/block_ids/source_text)`。LLM 只负责从候选原文中忠实发现、压缩要求、保留明确条件和返回来源 block_id；本阶段不生成执行类型、scope、evidence_type、category 或 checks。归一化阶段会在同一候选窗口内校验来源块是否支持 rule，发现模型误引时进行确定性修正；原始 LLM 返回仍保留在 `04_raw_requirements.json`。提取范围仅包括可从投标文件及其文件元数据直接检查的要求，外部系统状态、未明确要求随投标文件提交的合同履约要求，以及与项目专用条款冲突的联合体/备选方案规则会记录在过滤报告中。

## 测试

```bash
uv run pytest -v
```
