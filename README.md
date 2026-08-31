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
- 候选内容默认最多分成 8 个模型批次，硬上限为 10；结果按招标文件内容哈希缓存；
- 结果页仅展示从招标文件来源块恢复出的合规性检查要求；
- 投标文件解析和合规性检查仍为模拟实现，不判断通过、不通过、得分或废标风险。

## 真实要求提取配置

可选环境变量：

- `MINERU_COMMAND`：MinerU 命令模板。命令标准输出应为 JSON 数组，或包含 `blocks`/`content`/`items` 数组；可用 `{input}` 占位符接收输入路径。
- `LLM_API_KEY`：启用 OpenAI-compatible LLM；同时可设置 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_MAX_TOKENS`、`LLM_TIMEOUT_SECONDS`。
- `COMPLIANCE_MAX_BATCHES`：候选批次数，范围 1–10，默认 8。

未配置外部 MinerU 时，系统使用 DOCX XML 结构回退；未配置 LLM 时，系统使用确定性、来源受限的本地抽取器。对有效 DOCX 不会返回固定五条 mock 规则；历史非 DOCX 测试字节仅保留兼容回退。

## 处理日志

工作流和要求提取链路使用 Python `logging` 输出详细阶段日志。默认启动命令会在终端显示 `start`、`end`、`retry` 和 `error` 事件，包括文件名、批次、数量、模型名和耗时；不会记录 API Key、完整提示词或招标文件原文。重点事件前缀包括 `workflow.*`、`document.parse.*`、`candidate.filter.*`、`compliance.batch.*`、`llm.call.*` 和 `requirements.normalize.*`。

## 测试

```bash
uv run pytest -v
```
