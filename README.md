# 标书检查

新版“标书检查”Web 工作流。招标文件提取链路以投标文件模板为主要检查对象，并保留少量项目专用编制要求和模板外证明材料；投标文件解析与真实合规审查仍为模拟实现。

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
- 招标文件要求提取默认调用项目现有 MinerU `/tasks` 服务链路（默认地址 `http://127.0.0.1:7100`）；DOCX XML fallback 只允许在开发/测试代码中显式传入开关；
- 配置 `LLM_API_KEY` 后，仅在模板边界、模板命名或前附表行存在歧义时使用 OpenAI-compatible Chat Completions；否则使用确定性的结构提取；
- MinerU/结构解析结果和三类提取结果分别按招标文件内容哈希缓存；
- 结果页展示完整模板、项目专用编制要求和模板外补充证明材料；
- 投标文件解析和合规性检查仍为模拟实现，不判断通过、不通过、得分或废标风险。

## 真实要求提取配置

MinerU 使用项目既有本地服务配置：

- MinerU 服务地址默认是 `http://127.0.0.1:7100`，后端默认 `hybrid-engine`，不新增新的 `.env` 配置项。
- `MINERU_COMMAND`：现有的显式 MinerU 命令适配器；配置后可覆盖默认 MinerU 服务调用。
- `LLM_API_KEY`：启用 OpenAI-compatible LLM；同时可设置 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_MAX_TOKENS`（默认 8192）、`LLM_TIMEOUT_SECONDS`。
- 项目根目录可放置本地 `.env`，配置项命名与合同审查项目一致；当前真实运行配置使用 DashScope、`LLM_MODEL=qwen3.8-27b` 和 `LLM_ENABLE_THINKING=false`。`.env` 不纳入 Git。
- `COMPLIANCE_MAX_BATCHES`：候选批次数，范围 1–10，默认 8。

MinerU 调用失败或结果无法解析时，任务会失败并保留错误原因；不会静默切换到 DOCX XML。未配置 LLM 时，系统使用确定性、来源受限的本地对象提取器。解析摘要和执行日志会记录实际解析器（`mineru` 或 `docx_fallback`）、是否真实调用 MinerU、耗时和结构统计。

## 处理日志

工作流和对象提取链路使用 Python `logging` 输出阶段日志。默认启动命令会在终端显示 `start`、`end`、`retry` 和 `error` 事件，包括文件名、实际解析器、是否调用 MinerU、对象数量、模型名和耗时；不会记录 API Key。重点事件前缀包括 `workflow.*`、`document.parse.*`、`functional.region.*`、`llm.call.*` 和 `compliance.extract.*`。

每个任务的招标文件目录下会生成 `compliance_extraction/`：`execution.jsonl` 保存结构化执行事件，`01_parsed_blocks.json`、`02_functional_regions.json`、`03_templates.json`、`04_project_requirements.json`、`05_supplemental_materials.json`、`06_filter_report.json` 和 `07_result.json` 保存阶段产物，`llm/call_NNN_input.json` 和 `llm/call_NNN_output.json` 保存每次调用的批次、脱敏请求、原始响应、对象解析结果、耗时、finish reason、usage 和错误信息，`summary.json` 保存实际解析器、解析统计、模板/项目要求/补充材料数量及 LLM 调用统计，`workflow_summary.json` 保存并行解析、复核和整个任务耗时。阶段文件在成功后立即写入，后续失败不会清理已有文件。`data/mineru_cache/` 独立保存按解析器版本、传输方式和文件内容哈希得到的结构解析结果；MinerU 与显式 DOCX fallback 使用不同缓存命名空间，对象结果缓存也会区分解析器、LLM 模型和批次参数，避免旧结果遮蔽新的提取配置。

最终结果是三类对象：`templates[]` 保存完整模板名称、所属章节、连续 `block_ids`、模板正文、表格、填写项、附件说明和来源；`project_requirements[]` 保存直接影响投标文件组成、编制、容量、形式、有效期、保证金、备选方案和报价格式的项目专用要求及其具体值；`supplemental_materials[]` 保存模板之外明确要求随投标文件提交的证明材料。模板正文和来源原文直接来自 MinerU/DOCX 结构块，LLM 只辅助识别歧义边界、命名或筛选，不生成 `check_type`、`scope`、`evidence_type`、`checks` 等执行字段，也不把模板编译成自然语言规则。评标办法、评分、履约、终验、人员管理、知识产权归属和违约责任不会进入这三类核心结果；明确的项目专用条款优先过滤通用保证金或纸质递交模板。

## 测试

```bash
uv run pytest -v
```
