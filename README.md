# 标书检查

新版“标书检查”Web 工作流第一版。当前版本先搭建双文件上传、任务状态、并行步骤和结构化结果展示，不执行真实文档解析或合规判断。

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
- 要求提取、投标文件解析和合规性检查均为模拟实现；
- 结果页仅展示模拟提取出的合规性检查要求；
- 不调用 MinerU、LLM、VL、OCR、embedding、rerank；
- 不实现 RAG、缓存、真实解析、真实规则提取或真实合规判断。

## 测试

```bash
uv run pytest -v
```
