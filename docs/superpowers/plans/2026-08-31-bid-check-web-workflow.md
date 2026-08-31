# 新版标书检查 Web 工作流第一版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个可实际上传两份 DOCX、创建持久化任务、并发运行两个模拟前置步骤，并展示结构化合规性检查要求的独立 Web 应用。

**Architecture:** 使用 FastAPI + Jinja2 提供服务端页面和 JSON API，SQLite 仓储持久化任务及阶段状态，`BidCheckWorkflow` 通过 `ThreadPoolExecutor` 并发调用两个可注入的模拟服务。浏览器轮询任务 API，并在同一详情页中从工作流状态切换到结果展示。

**Tech Stack:** Python 3.14、FastAPI、Jinja2、python-multipart、SQLite（标准库）、pytest、FastAPI TestClient、原生 HTML/CSS/JavaScript、uv

**Spec:** `docs/superpowers/specs/2026-08-31-bid-check-web-workflow-design.md`

## Global Constraints

- 项目目录为 `/Users/wenjuhao/code/python/bid_check`，相邻 `contract_risk_review` 只读参考，不修改、不导入其业务代码。
- 只允许 Git `main` 分支，不创建其他分支，不创建 Git worktree。
- 第一版只支持 `.docx`，且只有 `check_mode=compliance` 可执行。
- 数据模型必须预留 `compliance`、`evaluation`、`full` 三个校验方式。
- 阶段状态统一使用 `pending`、`running`、`complete`、`failed`。
- 不调用 MinerU、LLM、VL、OCR、embedding、rerank，不实现 RAG、真实解析、真实规则提取、真实合规判断或缓存。
- 结果页只展示提取出的合规性检查要求，不展示通过、不通过、得分或废标风险。
- 所有实现任务遵循测试驱动：先写失败测试，确认失败，再写最小实现，确认通过。
- 每个任务只提交该任务列出的文件；提交发生在 `main`，不得切换或新建分支。

## Planned File Structure

```text
bid_check/
├── .gitignore                         # 忽略虚拟环境、缓存、运行数据
├── pyproject.toml                     # Python 版本、运行依赖、测试配置
├── main.py                            # ASGI 应用入口，只导入 create_app
├── README.md                          # 启动方式、路由和第一版边界
├── app/
│   ├── __init__.py                    # 包标识
│   ├── api.py                         # 应用工厂、页面路由、上传和查询 API
│   ├── config.py                      # 数据目录、数据库和模拟延迟配置
│   ├── models.py                      # 枚举、文件元数据和任务记录
│   ├── repository.py                  # SQLite 任务持久化
│   ├── mock_services.py               # 三个可替换的模拟业务函数
│   ├── workflow.py                    # 并发编排与状态流转
│   ├── static/
│   │   ├── bid-check.css              # 共享视觉样式和响应式布局
│   │   ├── upload.js                  # 双文件选择、删除、模式与提交交互
│   │   └── task.js                    # 状态轮询及完成后刷新
│   └── templates/
│       ├── base.html                  # 顶栏、内容容器、静态资源块
│       ├── bid_check.html             # 上传与模式选择页
│       └── bid_check_task.html        # 工作流、失败和结果视图
└── tests/
    ├── conftest.py                    # 临时 Settings、仓储、应用客户端工厂
    ├── test_repository.py             # 仓储创建、状态和结果持久化
    ├── test_mock_services.py          # 模拟数据结构与无外部依赖约束
    ├── test_workflow.py               # 真并发、汇合、成功和失败状态
    ├── test_api.py                    # 上传、模式校验和任务查询协议
    ├── test_pages.py                  # 页面文案、状态结构和结果展示
    └── test_end_to_end.py             # 上传到完成结果的完整测试流程
```

---

### Task 1: 项目骨架、领域模型与 SQLite 仓储

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/models.py`
- Create: `app/repository.py`
- Create: `tests/conftest.py`
- Create: `tests/test_repository.py`

**Interfaces:**
- Consumes: 无。
- Produces: `Settings`、`FileMetadata`、`BidCheckTask`、`BidCheckRepository.create()`、`get()`、`count()`、`update_stage()`、`complete()`、`fail()`，供后续工作流和 API 使用。

- [ ] **Step 1: 写仓储失败测试**

在 `tests/test_repository.py` 中写出任务创建、阶段更新、完成结果和失败信息四组测试。测试使用 `tmp_path / "bid_check.db"`，不要访问项目运行数据目录。

```python
from app.models import FileMetadata
from app.repository import BidCheckRepository


def make_repository(tmp_path):
    return BidCheckRepository(tmp_path / "bid_check.db")


def make_files():
    return (
        FileMetadata(filename="招标文件.docx", size=12, storage_path="tasks/tender.docx"),
        FileMetadata(filename="投标文件.docx", size=34, storage_path="tasks/bid.docx"),
    )


def test_create_persists_required_task_fields(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()

    task = repository.create(
        task_id="task-001",
        tender_file=tender_file,
        bid_file=bid_file,
        check_mode="compliance",
    )

    assert task.task_id == "task-001"
    assert task.status == "pending"
    assert task.requirements_status == "pending"
    assert task.bid_parse_status == "pending"
    assert task.review_status == "pending"
    assert task.tender_file.filename == "招标文件.docx"
    assert repository.get("task-001") == task


def test_update_stage_preserves_other_stage_states(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")

    updated = repository.update_stage("task-001", "requirements", "running")

    assert updated.status == "running"
    assert updated.requirements_status == "running"
    assert updated.bid_parse_status == "pending"
    assert updated.review_status == "pending"


def test_complete_persists_structured_result(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")
    result = {
        "requirements": [{"id": "compliance_001"}],
        "bid_parse": {"status": "success"},
        "review_result": {
            "mode": "mock",
            "message": "当前版本尚未执行真实合规性检查",
        },
    }

    completed = repository.complete("task-001", result)

    assert completed.status == "complete"
    assert completed.review_status == "complete"
    assert completed.result == result
    assert repository.get("task-001").result == result


def test_fail_records_failed_stage_and_message(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")

    failed = repository.fail("task-001", "bid_parse", "模拟投标文件解析失败")

    assert failed.status == "failed"
    assert failed.bid_parse_status == "failed"
    assert failed.failed_stage == "bid_parse"
    assert failed.error_message == "模拟投标文件解析失败"
```

- [ ] **Step 2: 运行仓储测试并确认因模块缺失而失败**

Run: `uv run pytest tests/test_repository.py -v`

Expected: collection 失败，包含 `ModuleNotFoundError: No module named 'app.models'`。

- [ ] **Step 3: 创建项目配置和领域模型**

`pyproject.toml` 固定 Python 3.14，并仅加入第一版需要的依赖：

```toml
[project]
name = "bid-check"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "fastapi>=0.115.0",
    "jinja2>=3.1.0",
    "python-multipart>=0.0.9",
    "uvicorn>=0.34.0",
]

[dependency-groups]
dev = [
    "httpx>=0.28.1",
    "pytest>=8.3.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore` 包含：

```gitignore
.venv/
__pycache__/
.pytest_cache/
*.pyc
data/
```

`app/config.py` 定义：

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    data_dir: Path
    database_path: Path
    tasks_dir: Path
    mock_delay_seconds: float = 0.35


def load_settings(base_dir: Path | None = None) -> Settings:
    project_dir = base_dir or Path(__file__).resolve().parent.parent
    data_dir = project_dir / "data"
    return Settings(
        project_dir=project_dir,
        data_dir=data_dir,
        database_path=data_dir / "bid_check.db",
        tasks_dir=data_dir / "tasks",
    )
```

`app/models.py` 使用冻结 dataclass，并验证枚举值：

```python
from dataclasses import dataclass
from typing import Any, Literal

CheckMode = Literal["compliance", "evaluation", "full"]
TaskStatus = Literal["pending", "running", "complete", "failed"]
StageName = Literal["requirements", "bid_parse", "review"]


@dataclass(frozen=True)
class FileMetadata:
    filename: str
    size: int
    storage_path: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "filename": self.filename,
            "size": self.size,
            "storage_path": self.storage_path,
        }


@dataclass(frozen=True)
class BidCheckTask:
    task_id: str
    tender_file: FileMetadata
    bid_file: FileMetadata
    check_mode: CheckMode
    status: TaskStatus
    requirements_status: TaskStatus
    bid_parse_status: TaskStatus
    review_status: TaskStatus
    failed_stage: str | None
    error_message: str | None
    result: dict[str, Any] | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "task_id": self.task_id,
            "tender_file": self.tender_file.to_dict(),
            "bid_file": self.bid_file.to_dict(),
            "check_mode": self.check_mode,
            "status": self.status,
            "requirements_status": self.requirements_status,
            "bid_parse_status": self.bid_parse_status,
            "review_status": self.review_status,
            "failed_stage": self.failed_stage,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.result:
            payload.update(self.result)
        return payload
```

- [ ] **Step 4: 实现 SQLite 仓储的最小闭环**

`app/repository.py` 创建单表 `bid_check_tasks`，文件元数据和结果以 JSON 文本存储。每次方法调用建立短连接，写操作由 `threading.RLock` 保护。实现以下完整接口；SQL 建表语句中的三个阶段字段均使用与 `status` 相同的 `CHECK (value IN ('pending', 'running', 'complete', 'failed'))`，`check_mode` 使用 `CHECK (check_mode IN ('compliance', 'evaluation', 'full'))`。

```python
TASK_STATUSES = {"pending", "running", "complete", "failed"}
STAGE_COLUMNS = {
    "requirements": "requirements_status",
    "bid_parse": "bid_parse_status",
    "review": "review_status",
}


class BidCheckRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def create(
        self,
        task_id: str,
        tender_file: FileMetadata,
        bid_file: FileMetadata,
        check_mode: CheckMode,
    ) -> BidCheckTask:
        if check_mode not in {"compliance", "evaluation", "full"}:
            raise ValueError(f"unsupported check mode: {check_mode}")
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bid_check_tasks (
                    task_id, tender_file_json, bid_file_json, check_mode,
                    status, requirements_status, bid_parse_status,
                    review_status, failed_stage, error_message, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 'pending', 'pending',
                          'pending', NULL, NULL, NULL, ?, ?)
                """,
                (
                    task_id,
                    json.dumps(tender_file.to_dict(), ensure_ascii=False),
                    json.dumps(bid_file.to_dict(), ensure_ascii=False),
                    check_mode,
                    timestamp,
                    timestamp,
                ),
            )
        return self._get_required(task_id)

    def get(self, task_id: str) -> BidCheckTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bid_check_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._record_from_row(row)

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM bid_check_tasks"
            ).fetchone()
        return int(row["count"])

    def update_stage(
        self,
        task_id: str,
        stage: StageName,
        status: TaskStatus,
    ) -> BidCheckTask:
        # status=running 时将总状态同步为 running；禁止动态 SQL 使用未校验列名。
        if stage not in STAGE_COLUMNS or status not in TASK_STATUSES:
            raise ValueError("unsupported stage or status")
        column = STAGE_COLUMNS[stage]
        total_status = "running" if status == "running" else None
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                f"""UPDATE bid_check_tasks
                    SET {column} = ?, status = COALESCE(?, status), updated_at = ?
                    WHERE task_id = ?""",
                (status, total_status, timestamp, task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self._get_required(task_id)

    def complete(self, task_id: str, result: dict[str, Any]) -> BidCheckTask:
        # 原子写入 result_json、review_status=complete、status=complete。
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE bid_check_tasks
                   SET status = 'complete', review_status = 'complete',
                       result_json = ?, failed_stage = NULL,
                       error_message = NULL, updated_at = ?
                   WHERE task_id = ?""",
                (json.dumps(result, ensure_ascii=False), timestamp, task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self._get_required(task_id)

    def fail(self, task_id: str, stage: StageName, message: str) -> BidCheckTask:
        # 原子写入对应阶段 failed、总状态 failed、failed_stage、error_message。
        if stage not in STAGE_COLUMNS:
            raise ValueError(f"unsupported stage: {stage}")
        column = STAGE_COLUMNS[stage]
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                f"""UPDATE bid_check_tasks
                    SET {column} = 'failed', status = 'failed',
                        failed_stage = COALESCE(failed_stage, ?),
                        error_message = COALESCE(error_message, ?),
                        updated_at = ?
                    WHERE task_id = ?""",
                (stage, message, timestamp, task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self._get_required(task_id)
```

文件顶部导入 `json`、`sqlite3`、`threading`、`datetime/timezone`、`Path`、`cast` 和模型类型。辅助方法必须按以下职责实现：`_connect()` 设置 `row_factory=sqlite3.Row`；`_initialize()` 执行上述建表；`_record_from_row()` 通过 `json.loads()` 还原两个 `FileMetadata` 和可空 `result_json`；`_get_required()` 调用 `get()` 并在空值时抛出 `KeyError(task_id)`。SQLite context manager 的正常退出负责 commit，异常退出负责 rollback。所有不存在的 `task_id` 更新操作抛出 `KeyError(task_id)`。

- [ ] **Step 5: 运行仓储测试并确认通过**

Run: `uv run pytest tests/test_repository.py -v`

Expected: 4 tests passed。

- [ ] **Step 6: 提交任务 1**

```bash
git add .gitignore pyproject.toml uv.lock app/__init__.py app/config.py app/models.py app/repository.py tests/conftest.py tests/test_repository.py
git commit -m "feat: add bid check task repository"
```

---

### Task 2: 可替换的模拟服务与结构化测试数据

**Files:**
- Create: `app/mock_services.py`
- Create: `tests/test_mock_services.py`

**Interfaces:**
- Consumes: `pathlib.Path`。
- Produces: `extract_compliance_requirements(tender_file: FileMetadata, *, delay_seconds: float = 0.35) -> list[dict[str, Any]]`、`parse_bid_document(bid_file: FileMetadata, *, delay_seconds: float = 0.35) -> dict[str, Any]`、`run_compliance_review(requirements: list[dict[str, Any]], parsed_bid: dict[str, Any]) -> dict[str, str]`。

- [ ] **Step 1: 写模拟服务失败测试**

```python
from app.mock_services import (
    extract_compliance_requirements,
    parse_bid_document,
    run_compliance_review,
)
from app.models import FileMetadata


def test_extract_returns_five_structured_requirements(tmp_path):
    tender_file = tmp_path / "tender.docx"
    tender_file.write_bytes(b"docx")

    metadata = FileMetadata("招标文件.docx", 4, str(tender_file))
    requirements = extract_compliance_requirements(metadata, delay_seconds=0)

    assert [item["id"] for item in requirements] == [
        "compliance_001", "compliance_002", "compliance_003",
        "compliance_004", "compliance_005",
    ]
    assert [len(item["checks"]) for item in requirements] == [2, 3, 5, 3, 4]
    assert all(item["source"]["section"] for item in requirements)


def test_parse_returns_document_name_and_mock_counts(tmp_path):
    bid_file = tmp_path / "投标文件.docx"
    bid_file.write_bytes(b"docx")

    metadata = FileMetadata("投标文件.docx", 4, str(bid_file))
    result = parse_bid_document(metadata, delay_seconds=0)

    assert result == {
        "status": "success",
        "document_name": "投标文件.docx",
        "section_count": 22,
        "block_count": 405,
        "table_count": 11,
        "image_count": 70,
    }


def test_review_returns_disclaimer_without_fake_judgement():
    result = run_compliance_review([{"id": "compliance_001"}], {"status": "success"})

    assert result["mode"] == "mock"
    assert result["message"] == "当前版本尚未执行真实合规性检查"
    assert "passed" not in result
    assert "risk" not in result
```

- [ ] **Step 2: 运行测试并确认因模块缺失而失败**

Run: `uv run pytest tests/test_mock_services.py -v`

Expected: collection 失败，包含 `ModuleNotFoundError: No module named 'app.mock_services'`。

- [ ] **Step 3: 实现五项模拟要求常量**

`app/mock_services.py` 定义 `MOCK_COMPLIANCE_REQUIREMENTS`。每个条目必须包含 `id`、`name`、`category`、`target`、`checks`、`applicability` 和 `source`。数据矩阵如下，逐项写入常量，不把数据写进模板或工作流：

| ID | 名称 | 对象 | 检查要求 |
|---|---|---|---|
| compliance_001 | 商务投标文件封面完整性 | 商务投标文件封面 | 投标人名称应填写完整；日期应填写完整 |
| compliance_002 | 法定代表人身份证明完整性 | 法定代表人/负责人身份证明 | 法定代表人基本信息应填写完整；应提供法定代表人身份证明；居民身份证应包含人像面和国徽面 |
| compliance_003 | 授权委托书完整性 | 法定代表人/负责人授权委托书 | 委托代理人信息应填写完整；授权内容应填写完整；日期应填写完整；要求的签字不得缺失；要求的盖章不得缺失 |
| compliance_004 | 函件及承诺书完整性 | 函件及承诺书 | 投标人名称不得缺失；日期不得缺失；模板占位符不得残留 |
| compliance_005 | 人员材料完整性 | 项目人员材料 | 人员基本信息应填写完整；要求提供的身份证明不得缺失；要求提供的社保证明不得缺失；要求提供的资格证书不得缺失 |

`check_type` 与 `evidence_type` 按设计需求使用 `required_field`、`date`、`attachment_exists`、`attachment_content`、`signature`、`seal`、`placeholder` 和 `text`、`structure`、`vision`。`source.section`、`source_text`、`target.scope` 与原始需求示例保持一致。

- [ ] **Step 4: 实现三个模拟函数**

```python
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.models import FileMetadata


def extract_compliance_requirements(
    tender_file: FileMetadata,
    *,
    delay_seconds: float = 0.35,
) -> list[dict[str, Any]]:
    source_path = Path(tender_file.storage_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    time.sleep(delay_seconds)
    return deepcopy(MOCK_COMPLIANCE_REQUIREMENTS)


def parse_bid_document(
    bid_file: FileMetadata,
    *,
    delay_seconds: float = 0.35,
) -> dict[str, Any]:
    source_path = Path(bid_file.storage_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    time.sleep(delay_seconds)
    return {
        "status": "success",
        "document_name": bid_file.filename,
        "section_count": 22,
        "block_count": 405,
        "table_count": 11,
        "image_count": 70,
    }


def run_compliance_review(
    requirements: list[dict[str, Any]],
    parsed_bid: dict[str, Any],
) -> dict[str, str]:
    if not requirements or parsed_bid.get("status") != "success":
        raise ValueError("模拟审查输入不完整")
    return {
        "mode": "mock",
        "message": "当前版本尚未执行真实合规性检查",
    }
```

统计数字只出现在 `parse_bid_document()` 的模拟返回值中，不进入模型、仓储、工作流或模板条件分支。

- [ ] **Step 5: 运行模拟服务测试并确认通过**

Run: `uv run pytest tests/test_mock_services.py -v`

Expected: 3 tests passed。

- [ ] **Step 6: 提交任务 2**

```bash
git add app/mock_services.py tests/test_mock_services.py
git commit -m "feat: add mock bid check services"
```

---

### Task 3: 真并发工作流与失败阶段状态

**Files:**
- Create: `app/workflow.py`
- Create: `tests/test_workflow.py`

**Interfaces:**
- Consumes: `BidCheckRepository`、任务中的 `FileMetadata`、Task 2 的三个服务函数。
- Produces: `BidCheckServices` 可注入服务集合；`BidCheckWorkflow.run(task_id: str) -> None`；`shutdown(wait: bool = True) -> None`。

- [ ] **Step 1: 写证明并发重叠的失败测试**

使用 `threading.Barrier(2)` 证明两个前置服务都在任一服务返回前进入。不要使用“总耗时小于某阈值”作为并发证据。

```python
from threading import Barrier

from app.workflow import BidCheckServices, BidCheckWorkflow


def test_requirements_and_parse_enter_concurrently(task_repository):
    barrier = Barrier(2, timeout=2)
    entered = []

    def extract(file_metadata):
        entered.append("requirements")
        barrier.wait()
        return [{"id": "compliance_001"}]

    def parse(file_metadata):
        entered.append("bid_parse")
        barrier.wait()
        return {"status": "success"}

    workflow = BidCheckWorkflow(
        task_repository,
        BidCheckServices(extract=extract, parse=parse, review=lambda reqs, parsed: {
            "mode": "mock", "message": "当前版本尚未执行真实合规性检查"
        }),
    )

    workflow.run("task-001")

    assert set(entered) == {"requirements", "bid_parse"}
    task = task_repository.get("task-001")
    assert task.status == "complete"
    assert task.requirements_status == "complete"
    assert task.bid_parse_status == "complete"
    assert task.review_status == "complete"
```

`tests/conftest.py` 增加以下 fixture，为每个工作流测试创建全新的仓储和任务：

```python
@pytest.fixture
def task_repository(tmp_path):
    repository = BidCheckRepository(tmp_path / "bid_check.db")
    task_dir = tmp_path / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"docx-tender")
    bid_path.write_bytes(b"docx-bid")
    repository.create(
        "task-001",
        FileMetadata("招标文件.docx", tender_path.stat().st_size, str(tender_path)),
        FileMetadata("投标文件.docx", bid_path.stat().st_size, str(bid_path)),
        "compliance",
    )
    return repository
```

- [ ] **Step 2: 写三个失败分支测试**

```python
import pytest


def make_workflow(repository, extract, parse, review_calls):
    def review(requirements, parsed):
        review_calls.append((requirements, parsed))
        return {"mode": "mock", "message": "当前版本尚未执行真实合规性检查"}

    return BidCheckWorkflow(
        repository,
        BidCheckServices(extract=extract, parse=parse, review=review),
    )


@pytest.mark.parametrize(
    ("failing_service", "failed_stage", "message"),
    [
        ("extract", "requirements", "模拟合规性要求提取失败"),
        ("parse", "bid_parse", "模拟投标文件解析失败"),
    ],
)
def test_parallel_stage_failure_is_persisted(
    task_repository, failing_service, failed_stage, message
):
    def extract(file_metadata):
        if failing_service == "extract":
            raise RuntimeError(message)
        return [{"id": "compliance_001"}]

    def parse(file_metadata):
        if failing_service == "parse":
            raise RuntimeError(message)
        return {"status": "success"}

    review_calls = []
    workflow = make_workflow(task_repository, extract, parse, review_calls)
    workflow.run("task-001")

    task = task_repository.get("task-001")
    assert task.status == "failed"
    assert task.failed_stage == failed_stage
    assert task.error_message == message
    assert task.review_status == "pending"
    assert review_calls == []


def test_review_failure_is_persisted(task_repository):
    services = BidCheckServices(
        extract=lambda file_metadata: [{"id": "compliance_001"}],
        parse=lambda file_metadata: {"status": "success"},
        review=lambda requirements, parsed: (_ for _ in ()).throw(
            RuntimeError("模拟合规性检查失败")
        ),
    )
    BidCheckWorkflow(task_repository, services).run("task-001")

    task = task_repository.get("task-001")
    assert task.status == "failed"
    assert task.review_status == "failed"
    assert task.failed_stage == "review"
```

- [ ] **Step 3: 运行工作流测试并确认失败**

Run: `uv run pytest tests/test_workflow.py -v`

Expected: collection 失败，包含 `ModuleNotFoundError: No module named 'app.workflow'`。

- [ ] **Step 4: 实现可注入服务集合和并发编排器**

```python
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from app.models import FileMetadata
from app.repository import BidCheckRepository


@dataclass(frozen=True)
class BidCheckServices:
    extract: Callable[[FileMetadata], list[dict[str, Any]]]
    parse: Callable[[FileMetadata], dict[str, Any]]
    review: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, str]]


class BidCheckWorkflow:
    def __init__(
        self,
        repository: BidCheckRepository,
        services: BidCheckServices,
        *,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.repository = repository
        self.services = services
        self.executor = executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="bid-check",
        )

    def run(self, task_id: str) -> None:
        task = self.repository.get(task_id)
        if task is None:
            raise KeyError(task_id)
        self.repository.update_stage(task_id, "requirements", "running")
        self.repository.update_stage(task_id, "bid_parse", "running")
        futures = {
            self.executor.submit(self.services.extract, task.tender_file):
                "requirements",
            self.executor.submit(self.services.parse, task.bid_file):
                "bid_parse",
        }
        outputs: dict[str, Any] = {}
        failures: list[tuple[str, Exception]] = []
        for future in as_completed(futures):
            stage = futures[future]
            try:
                outputs[stage] = future.result()
                self.repository.update_stage(task_id, stage, "complete")
            except Exception as exc:
                failures.append((stage, exc))
                self.repository.fail(task_id, stage, str(exc))
        if failures:
            return
        try:
            self.repository.update_stage(task_id, "review", "running")
            review_result = self.services.review(
                outputs["requirements"], outputs["bid_parse"]
            )
            self.repository.complete(task_id, {
                "requirements": outputs["requirements"],
                "bid_parse": outputs["bid_parse"],
                "review_result": review_result,
            })
        except Exception as exc:
            self.repository.fail(task_id, "review", str(exc))

    def shutdown(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait)
```

若两个前置步骤都失败，以第一个由 `as_completed()` 返回的失败作为任务级 `failed_stage`，但两个阶段字段都必须最终为 `failed`。为避免第二次 `fail()` 覆盖任务级首个错误，仓储增加 `fail_stage_preserving_primary()`，或让 `fail()` 仅在 `failed_stage IS NULL` 时写任务级错误；计划采用后者，并新增对应测试。

- [ ] **Step 5: 运行工作流与仓储回归测试**

Run: `uv run pytest tests/test_workflow.py tests/test_repository.py -v`

Expected: 所有测试通过；并发测试不触发 Barrier timeout。

- [ ] **Step 6: 提交任务 3**

```bash
git add app/workflow.py app/repository.py tests/conftest.py tests/test_workflow.py tests/test_repository.py
git commit -m "feat: orchestrate parallel bid check stages"
```

---

### Task 4: 上传、任务创建与状态查询 API

**Files:**
- Create: `app/api.py`
- Create: `main.py`
- Create: `tests/test_api.py`

**Interfaces:**
- Consumes: `Settings`、`BidCheckRepository`、`BidCheckWorkflow`、`BidCheckServices`。
- Produces: `create_app(settings: Settings | None = None, repository: BidCheckRepository | None = None, workflow: BidCheckWorkflow | None = None) -> FastAPI` 和公开 API 路由。

- [ ] **Step 1: 写上传校验失败测试**

```python
import pytest


def docx_files():
    return {
        "tender_file": (
            "招标文件.docx",
            b"PK\x03\x04tender",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        "bid_file": (
            "投标文件.docx",
            b"PK\x03\x04bid",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    }


def test_create_task_requires_both_files(client):
    response = client.post(
        "/api/bid-check/tasks",
        files={"tender_file": docx_files()["tender_file"]},
        data={"check_mode": "compliance"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("field", ["tender_file", "bid_file"])
def test_create_task_rejects_non_docx(client, field):
    files = docx_files()
    files[field] = ("不支持.txt", b"text", "text/plain")
    response = client.post(
        "/api/bid-check/tasks", files=files, data={"check_mode": "compliance"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "当前仅支持 .docx 文件。"


@pytest.mark.parametrize("mode", ["evaluation", "full"])
def test_development_modes_do_not_create_tasks(client, repository, mode):
    response = client.post(
        "/api/bid-check/tasks", files=docx_files(), data={"check_mode": mode}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "该校验方式正在开发中。"
    assert repository.count() == 0
```

在同一文件加入空文件、未知模式和不存在任务测试：

```python
def test_create_task_rejects_empty_file(client):
    files = docx_files()
    files["bid_file"] = (files["bid_file"][0], b"", files["bid_file"][2])
    response = client.post(
        "/api/bid-check/tasks", files=files, data={"check_mode": "compliance"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "上传文件不能为空。"


def test_create_task_rejects_unknown_mode(client):
    response = client.post(
        "/api/bid-check/tasks", files=docx_files(), data={"check_mode": "unknown"}
    )
    assert response.status_code == 422


def test_get_unknown_task_returns_404(client):
    response = client.get("/api/bid-check/tasks/not-found")
    assert response.status_code == 404
    assert response.json()["detail"] == "标书检查任务不存在。"
```

再加入上传保存失败和数据库创建失败测试，断言不会留下有效任务或本次任务目录：

```python
from unittest.mock import patch


def test_upload_write_failure_returns_500_without_task(client, repository, settings):
    with patch("app.api.Path.write_bytes", side_effect=OSError("disk full")):
        response = client.post(
            "/api/bid-check/tasks",
            files=docx_files(),
            data={"check_mode": "compliance"},
        )
    assert response.status_code == 500
    assert response.json()["detail"] == "创建任务失败，请稍后重试。"
    assert repository.count() == 0
    assert not settings.tasks_dir.exists() or list(settings.tasks_dir.iterdir()) == []


def test_repository_create_failure_cleans_saved_files(client, repository, settings):
    with patch.object(repository, "create", side_effect=RuntimeError("db unavailable")):
        response = client.post(
            "/api/bid-check/tasks",
            files=docx_files(),
            data={"check_mode": "compliance"},
        )
    assert response.status_code == 500
    assert repository.count() == 0
    assert not settings.tasks_dir.exists() or list(settings.tasks_dir.iterdir()) == []
```

- [ ] **Step 2: 写成功创建与查询协议测试**

向应用注入 `RecordingWorkflow`，其 `run(task_id)` 只记录 ID，避免 API 测试执行真实后台流程。

在 `tests/conftest.py` 中加入以下 fixture；`stored_task` 供本任务及后续页面测试复用：

```python
class RecordingWorkflow:
    def __init__(self):
        self.task_ids: list[str] = []

    def run(self, task_id: str) -> None:
        self.task_ids.append(task_id)


@pytest.fixture
def settings(tmp_path):
    data_dir = tmp_path / "data"
    return Settings(
        project_dir=tmp_path,
        data_dir=data_dir,
        database_path=data_dir / "bid_check.db",
        tasks_dir=data_dir / "tasks",
        mock_delay_seconds=0,
    )


@pytest.fixture
def repository(settings):
    return BidCheckRepository(settings.database_path)


@pytest.fixture
def workflow():
    return RecordingWorkflow()


@pytest.fixture
def client(settings, repository, workflow):
    return TestClient(
        create_app(settings=settings, repository=repository, workflow=workflow)
    )


@pytest.fixture
def stored_task(settings, repository):
    task_dir = settings.tasks_dir / "stored-task"
    task_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    return repository.create(
        "stored-task",
        FileMetadata("招标文件.docx", 6, str(tender_path)),
        FileMetadata("投标文件.docx", 3, str(bid_path)),
        "compliance",
    )
```

```python
def test_create_task_stores_files_and_schedules_workflow(client, settings, workflow):
    response = client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": "compliance"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["check_mode"] == "compliance"
    assert payload["tender_file"]["filename"] == "招标文件.docx"
    assert payload["bid_file"]["filename"] == "投标文件.docx"
    assert workflow.task_ids == [payload["task_id"]]
    task_dir = settings.tasks_dir / payload["task_id"]
    assert (task_dir / "tender.docx").read_bytes() == b"PK\x03\x04tender"
    assert (task_dir / "bid.docx").read_bytes() == b"PK\x03\x04bid"


def test_get_task_returns_stable_payload(client, repository, stored_task):
    response = client.get(f"/api/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert set(response.json()) >= {
        "task_id", "tender_file", "bid_file", "check_mode", "status",
        "requirements_status", "bid_parse_status", "review_status",
        "failed_stage", "error_message", "created_at", "updated_at",
    }
```

- [ ] **Step 3: 运行 API 测试并确认失败**

Run: `uv run pytest tests/test_api.py -v`

Expected: collection 失败，包含 `ModuleNotFoundError: No module named 'app.api'`。

- [ ] **Step 4: 实现应用工厂和默认依赖**

本任务只在 `app/api.py` 实现 JSON API；Jinja2 模板和静态目录挂载留在 Task 5 与对应页面一起完成。默认服务通过 `functools.partial` 注入模拟延迟：

```python
def build_default_workflow(settings: Settings, repository: BidCheckRepository):
    services = BidCheckServices(
        extract=partial(
            extract_compliance_requirements,
            delay_seconds=settings.mock_delay_seconds,
        ),
        parse=partial(
            parse_bid_document,
            delay_seconds=settings.mock_delay_seconds,
        ),
        review=run_compliance_review,
    )
    return BidCheckWorkflow(repository, services)
```

应用工厂必须接受注入，测试不得 monkeypatch 全局对象：

```python
def create_app(
    *,
    settings: Settings | None = None,
    repository: BidCheckRepository | None = None,
    workflow: BidCheckWorkflow | None = None,
) -> FastAPI:
    active_settings = settings or load_settings()
    active_repository = repository or BidCheckRepository(
        active_settings.database_path
    )
    active_workflow = workflow or build_default_workflow(
        active_settings, active_repository
    )
    application = FastAPI(title="标书检查")
    application.state.settings = active_settings
    application.state.repository = active_repository
    application.state.workflow = active_workflow
    return application
```

`main.py` 只包含：

```python
from app.api import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
```

- [ ] **Step 5: 实现上传保存、回滚和任务查询**

`POST /api/bid-check/tasks` 的顺序必须固定：

1. 校验 `check_mode`；
2. 校验两个扩展名和非空内容；
3. 生成 `task_id = str(uuid.uuid4())`；
4. 创建 `tasks_dir/task_id`；
5. 保存为 `tender.docx` 与 `bid.docx`；
6. 使用原始文件名、字节大小和绝对存储路径创建仓储记录；
7. `background_tasks.add_task(active_workflow.run, task_id)`；
8. 返回 `JSONResponse(task.to_dict(), status_code=202)`。

若步骤 4–6 失败，用 `shutil.rmtree(task_dir, ignore_errors=True)` 清理本次新目录，并返回 `500` 和“创建任务失败，请稍后重试。”。不得删除 `tasks_dir` 或其他任务目录。

```python
@application.get("/api/bid-check/tasks/{task_id}")
def get_bid_check_task(task_id: str):
    task = active_repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="标书检查任务不存在。")
    return task.to_dict()
```

- [ ] **Step 6: 运行 API 和既有测试**

Run: `uv run pytest tests/test_api.py tests/test_repository.py tests/test_workflow.py -v`

Expected: 所有测试通过。

- [ ] **Step 7: 提交任务 4**

```bash
git add app/api.py main.py tests/conftest.py tests/test_api.py
git commit -m "feat: add bid check task api"
```

---

### Task 5: 双文件上传与校验方式选择页面

**Files:**
- Create: `app/templates/base.html`
- Create: `app/templates/bid_check.html`
- Create: `app/static/bid-check.css`
- Create: `app/static/upload.js`
- Create: `tests/test_pages.py`
- Modify: `app/api.py`

**Interfaces:**
- Consumes: `POST /api/bid-check/tasks`。
- Produces: `GET /` 重定向、`GET /bid-check` 页面、上传页 DOM 协议。

- [ ] **Step 1: 写上传页失败测试**

```python
def test_root_redirects_to_bid_check(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/bid-check"


def test_bid_check_page_has_two_docx_uploads_and_official_modes(client):
    response = client.get("/bid-check")

    assert response.status_code == 200
    assert 'name="tender_file"' in response.text
    assert 'name="bid_file"' in response.text
    assert response.text.count('accept=".docx') == 2
    assert "标书合规性校验" in response.text
    assert "评标规则校验" in response.text
    assert "全面校验" in response.text
    assert response.text.count("开发中") >= 2
    assert "评分+废标检查" not in response.text
    assert 'id="start-check"' in response.text
    assert 'disabled' in response.text
```

- [ ] **Step 2: 运行页面测试并确认路由缺失**

Run: `uv run pytest tests/test_pages.py::test_root_redirects_to_bid_check tests/test_pages.py::test_bid_check_page_has_two_docx_uploads_and_official_modes -v`

Expected: 两个测试均失败，当前路由返回 `404`。

- [ ] **Step 3: 实现共享模板和页面结构**

`base.html` 负责 `<head>`、深蓝渐变顶栏、主内容容器、CSS 链接及页面脚本 block。顶栏品牌只显示“标书检查”。

`bid_check.html` 包含：

```html
<form id="bid-check-form" enctype="multipart/form-data">
  <section class="upload-grid" aria-label="上传文件">
    <article class="upload-card" data-upload-card="tender_file">
      <h2>招标文件</h2>
      <input name="tender_file" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" required>
      <div class="file-summary" aria-live="polite"></div>
      <button type="button" data-clear-file>删除</button>
    </article>
    <article class="upload-card" data-upload-card="bid_file">
      <h2>投标文件</h2>
      <input name="bid_file" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" required>
      <div class="file-summary" aria-live="polite"></div>
      <button type="button" data-clear-file>删除</button>
    </article>
  </section>
  <section class="mode-grid" aria-label="选择校验方式">
    <!-- compliance radio 可选；evaluation/full radio disabled 并显示开发中 -->
  </section>
  <div id="form-error" class="error" role="alert" hidden></div>
  <button id="start-check" class="primary" type="submit" disabled>开始校验</button>
</form>
```

模式卡片完整说明严格使用需求文案。`compliance` 标签显示“本轮可执行”；另两个 input 使用 `disabled`，徽标显示“开发中”。

- [ ] **Step 4: 实现上传页样式**

`bid-check.css` 采用参考项目的视觉标记：

```css
:root {
  color: #172033;
  background: linear-gradient(180deg, #f7fafc 0%, #eef3f8 100%);
  font: 15px/1.6 "Avenir Next", "Segoe UI", sans-serif;
}
.topbar { background: linear-gradient(90deg, #172033 0%, #25344f 100%); }
.shell { width: min(1080px, calc(100% - 40px)); margin: 30px auto 64px; }
.card, .upload-card, .mode-card {
  border: 1px solid #d9e2ec;
  border-radius: 14px;
  background: white;
  box-shadow: 0 8px 24px #1020380a;
}
.upload-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.mode-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
@media (max-width: 760px) {
  .upload-grid, .mode-grid { grid-template-columns: 1fr; }
}
```

补齐按钮、虚线上传区、选择态、成功态、开发中徽标、错误提示和 `:focus-visible` 样式。不得从参考项目复制业务文案。

- [ ] **Step 5: 实现双文件前端交互**

`upload.js` 必须完成：

- 选择后验证 `.docx`，显示文件名、格式化大小和“已选择”；
- “删除”通过 `input.value = ""` 清除对应文件；
- 再次点击文件 input 可重新选择；
- 仅当两个文件存在且 `compliance` 被选择时启用按钮；
- 提交使用 `fetch('/api/bid-check/tasks', {method: 'POST', body: new FormData(form)})`；
- 成功后跳转 `/bid-check/tasks/${encodeURIComponent(payload.task_id)}`；
- 失败时读取 `detail` 并显示在 `#form-error`，恢复按钮可用状态。

文件大小使用 `Intl.NumberFormat` 或明确的 B/KiB/MiB 转换，不改变原始 `File`。

- [ ] **Step 6: 增加页面路由并运行测试**

在 `app/api.py` 挂载 `/static`，配置 `Jinja2Templates`，实现：

```python
@application.get("/")
def root():
    return RedirectResponse("/bid-check")


@application.get("/bid-check", response_class=HTMLResponse)
def bid_check_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="bid_check.html",
        context={},
    )
```

Run: `uv run pytest tests/test_pages.py tests/test_api.py -v`

Expected: 上传页与 API 测试全部通过。

- [ ] **Step 7: 提交任务 5**

```bash
git add app/api.py app/templates/base.html app/templates/bid_check.html app/static/bid-check.css app/static/upload.js tests/test_pages.py
git commit -m "feat: add bid check upload workflow page"
```

---

### Task 6: 工作流、失败状态和结构化结果页面

**Files:**
- Create: `app/templates/bid_check_task.html`
- Create: `app/static/task.js`
- Modify: `app/static/bid-check.css`
- Modify: `app/api.py`
- Modify: `tests/test_pages.py`

**Interfaces:**
- Consumes: `BidCheckTask.to_dict()` 与 `GET /api/bid-check/tasks/{task_id}`。
- Produces: `GET /bid-check/tasks/{task_id}` 页面；固定 DOM 标识 `data-task-status`、`data-task-id`、`data-parallel-stages`。

- [ ] **Step 1: 写运行中页面失败测试**

```python
def test_running_task_page_shows_parallel_workflow(client, repository, stored_task):
    repository.update_stage(stored_task.task_id, "requirements", "running")
    repository.update_stage(stored_task.task_id, "bid_parse", "running")

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "上传文件" in response.text
    assert "提取合规性检查要求" in response.text
    assert "解析投标文件" in response.text
    assert "执行合规性检查" in response.text
    assert "检查结果" in response.text
    assert 'data-parallel-stages' in response.text
    assert response.text.count("运行中") >= 2
    assert f'data-task-id="{stored_task.task_id}"' in response.text
```

- [ ] **Step 2: 写失败与完成页面测试**

```python
def test_failed_task_page_names_failed_stage(client, repository, stored_task):
    repository.fail(stored_task.task_id, "requirements", "模拟合规性要求提取失败")
    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "失败阶段：提取合规性检查要求" in response.text
    assert "模拟合规性要求提取失败" in response.text


def test_complete_page_renders_requirements_without_fake_verdict(
    client, repository, stored_task, mock_complete_result
):
    repository.complete(stored_task.task_id, mock_complete_result)
    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert "标书合规性校验结果" in response.text
    assert "本次共提取 5 项合规性检查要求" in response.text
    assert "商务投标文件封面完整性" in response.text
    assert "项目人员材料" in response.text
    assert "当前版本仅展示提取出的合规性检查要求" in response.text
    assert "尚未执行真实投标文件内容校验" in response.text
    assert "section_count" not in response.text
    assert "章节数" in response.text
    assert "检查通过" not in response.text
    assert "检查不通过" not in response.text
```

`mock_complete_result` fixture 调用 Task 2 的三个函数生成结果，避免在测试模板中复制五项规则：

```python
@pytest.fixture
def mock_complete_result(stored_task):
    requirements = extract_compliance_requirements(
        stored_task.tender_file,
        delay_seconds=0,
    )
    bid_parse = parse_bid_document(
        stored_task.bid_file,
        delay_seconds=0,
    )
    return {
        "requirements": requirements,
        "bid_parse": bid_parse,
        "review_result": run_compliance_review(requirements, bid_parse),
    }
```

- [ ] **Step 3: 运行页面测试并确认详情路由缺失**

Run: `uv run pytest tests/test_pages.py -v`

Expected: 新增测试失败，详情路由返回 `404`。

- [ ] **Step 4: 实现任务详情路由和模板上下文**

```python
STAGE_LABELS = {
    "requirements": "提取合规性检查要求",
    "bid_parse": "解析投标文件",
    "review": "执行合规性检查",
}


@application.get("/bid-check/tasks/{task_id}", response_class=HTMLResponse)
def bid_check_task_page(request: Request, task_id: str):
    task = active_repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="标书检查任务不存在。")
    return templates.TemplateResponse(
        request=request,
        name="bid_check_task.html",
        context={"task": task, "failed_stage_label": STAGE_LABELS.get(task.failed_stage)},
    )
```

- [ ] **Step 5: 实现工作流和结果模板**

模板顶层输出：

```html
<body data-task-id="{{ task.task_id }}" data-task-status="{{ task.status }}">
```

运行视图的步骤 2 和 3 必须位于同一 `.parallel-stages` 容器，并带 `data-parallel-stages`。状态标签由 Jinja 宏把 `pending/running/complete/failed` 映射为“等待中/运行中/已完成/失败”。步骤 1 固定为“已完成”；步骤 5 只有总状态完成时为“已完成”。

完成视图遍历 `task.result.requirements`：

```html
{% for requirement in task.result.requirements %}
  <article class="requirement-card">
    <h3>{{ requirement.name }}</h3>
    <div class="field"><strong>检查对象</strong><span>{{ requirement.target.name }}</span></div>
    <div class="field">
      <strong>检查要求</strong>
      <ul>
        {% for check in requirement.checks %}
          <li>{{ check.requirement }}</li>
        {% endfor %}
      </ul>
    </div>
    <div class="field"><strong>来源</strong><span>{{ requirement.source.section }}</span></div>
  </article>
{% endfor %}
```

解析摘要将字段映射为中文：章节数、内容块数、表格数、图片数。页面顶部使用警示说明：

```text
当前版本仅展示提取出的合规性检查要求，尚未执行真实投标文件内容校验。
```

要求列表使用普通 `<li>` 项目符号，不使用对勾图标。

- [ ] **Step 6: 实现轮询与工作流样式**

`task.js` 只在 `pending/running` 状态轮询：

```javascript
const taskId = document.body.dataset.taskId;
const status = document.body.dataset.taskStatus;

async function pollTask() {
  try {
    const response = await fetch(
      `/api/bid-check/tasks/${encodeURIComponent(taskId)}`,
      { cache: 'no-store' },
    );
    if (!response.ok) throw new Error('任务状态查询失败');
    const payload = await response.json();
    if (payload.status === 'complete' || payload.status === 'failed') {
      window.location.reload();
      return;
    }
    updateStageLabels(payload);
    window.setTimeout(pollTask, 700);
  } catch (error) {
    showPollingWarning('暂时无法获取最新任务状态，正在重试。');
    window.setTimeout(pollTask, 1800);
  }
}

if (taskId && ['pending', 'running'].includes(status)) {
  window.setTimeout(pollTask, 400);
}
```

`updateStageLabels()` 只更新三个阶段的 `data-stage` 节点，不根据状态生成任何结果数据。CSS 用两列网格和汇合连接线表达并行关系；窄屏改为单列，但仍保留“并行执行”文字标签。

- [ ] **Step 7: 运行页面和完整回归测试**

Run: `uv run pytest tests/test_pages.py tests/test_api.py tests/test_workflow.py -v`

Expected: 所有测试通过。

- [ ] **Step 8: 提交任务 6**

```bash
git add app/api.py app/templates/bid_check_task.html app/static/bid-check.css app/static/task.js tests/conftest.py tests/test_pages.py
git commit -m "feat: add bid check workflow results page"
```

---

### Task 7: 完整流程、运行说明与禁用能力审计

**Files:**
- Create: `tests/test_end_to_end.py`
- Create: `README.md`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: 完整应用工厂、API、工作流和页面。
- Produces: 可重复的端到端验证、项目启动说明和第一版能力边界。

- [ ] **Step 1: 写完整流程失败测试**

构造真实仓储和真实模拟工作流，模拟延迟设为 `0`。使用 `TestClient` 上传两个最小 DOCX 字节，随后查询任务和结果页。

```python
def docx_files():
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return {
        "tender_file": ("招标文件.docx", b"PK\x03\x04tender", mime),
        "bid_file": ("投标文件.docx", b"PK\x03\x04bid", mime),
    }


def test_upload_to_completed_requirements_result(live_client):
    create_response = live_client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": "compliance"},
    )
    assert create_response.status_code == 202
    task_id = create_response.json()["task_id"]

    task_response = live_client.get(f"/api/bid-check/tasks/{task_id}")
    payload = task_response.json()
    assert payload["status"] == "complete"
    assert payload["requirements_status"] == "complete"
    assert payload["bid_parse_status"] == "complete"
    assert payload["review_status"] == "complete"
    assert len(payload["requirements"]) == 5
    assert payload["review_result"] == {
        "mode": "mock",
        "message": "当前版本尚未执行真实合规性检查",
    }

    page_response = live_client.get(f"/bid-check/tasks/{task_id}")
    assert page_response.status_code == 200
    assert "本次共提取 5 项合规性检查要求" in page_response.text
    assert "尚未执行真实投标文件内容校验" in page_response.text
```

FastAPI `BackgroundTasks` 在当前依赖版本的 `TestClient` 请求结束前完成，因此该测试不使用 sleep 或轮询；测试若观察到任务仍在运行，应让 fixture 注入一个调用 `workflow.run(task_id)` 的同步后台执行器，并继续断言最终状态。

- [ ] **Step 2: 运行完整流程测试并确认初始失败原因**

Run: `uv run pytest tests/test_end_to_end.py -v`

Expected: 失败于 `fixture 'live_client' not found`；不得先实现 fixture 再运行此步骤。

- [ ] **Step 3: 实现端到端 fixture 并使测试通过**

`tests/conftest.py` 中 `live_client` 使用临时目录构造 `Settings(mock_delay_seconds=0)`、真实 `BidCheckRepository` 和默认 `BidCheckWorkflow`，并传入 `create_app(settings=settings, repository=repository, workflow=workflow)`。fixture 退出时调用 Task 3 已定义的 `workflow.shutdown()`，确保线程池被回收：

```python
@pytest.fixture
def live_client(tmp_path):
    settings = Settings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "bid_check.db",
        tasks_dir=tmp_path / "data" / "tasks",
        mock_delay_seconds=0,
    )
    repository = BidCheckRepository(settings.database_path)
    workflow = build_default_workflow(settings, repository)
    with TestClient(
        create_app(settings=settings, repository=repository, workflow=workflow)
    ) as client:
        yield client
    workflow.shutdown()
```

Run: `uv run pytest tests/test_end_to_end.py -v`

Expected: 1 test passed。

- [ ] **Step 4: 编写运行说明**

`README.md` 必须包含以下准确命令和信息：

````markdown
# 标书检查

## 启动

```bash
uv sync --dev
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/bid-check`。

## 第一版范围

- 仅支持招标文件和投标文件 `.docx` 上传；
- 仅“标书合规性校验”可执行；
- 要求提取、投标文件解析和合规性检查均为模拟实现；
- 不调用 MinerU、LLM、VL、OCR、embedding、rerank。
````

README 还需列出四个路由及 `uv run pytest -v` 测试命令。

- [ ] **Step 5: 运行全量自动化验证**

Run: `uv run pytest -v`

Expected: 全部测试通过，无 skipped、failed 或 error。

- [ ] **Step 6: 执行禁用能力静态审计**

Run:

```bash
rg -n -i "mineru|openai|langchain|llama|ocr|embedding|rerank|vision|vl" app main.py pyproject.toml
```

Expected: 仅允许 `mock_services.py` 的数据字段值 `evidence_type: "vision"` 命中；不得出现对应 SDK、客户端、环境变量、HTTP 请求或导入。`vision` 是测试规则的数据标签，不代表 VL 调用。

Run:

```bash
rg -n "检查通过|检查不通过|预计得分|废标风险|评分\+废标检查" app/templates app/static
```

Expected: 无输出。

- [ ] **Step 7: 启动服务并进行浏览器验收**

Run: `uv run uvicorn main:app --host 127.0.0.1 --port 8000`

在浏览器逐项验证：

1. `/` 跳转 `/bid-check`；
2. 两个上传卡片都支持选择、删除和重新选择；
3. 未选择两个文件时按钮禁用；
4. 三个正式模式名称均出现，后两个显示“开发中”；
5. 提交两个 `.docx` 后进入任务页；
6. 两个前置步骤在并行区域中显示运行状态；
7. 汇合后审查阶段完成；
8. 最终页面显示五项要求和模拟解析摘要；
9. 页面明确说明没有执行真实投标文件内容校验；
10. 浏览器窄屏下上传卡片、模式卡片和并行步骤不横向溢出。

停止服务后确认 `data/` 被 `.gitignore` 忽略。

- [ ] **Step 8: 核对 Git 操作边界并提交任务 7**

Run:

```bash
git branch --show-current
git worktree list
git status --short
```

Expected: 当前分支为 `main`；worktree 列表只有当前项目目录；只有本任务预期文件未提交。

```bash
git add README.md app/workflow.py tests/conftest.py tests/test_end_to_end.py
git commit -m "test: verify bid check workflow end to end"
```

- [ ] **Step 9: 完成前最终验证**

在声称完成前调用 `superpowers:verification-before-completion`，重新执行：

```bash
uv run pytest -v
git status --short
git branch --show-current
git worktree list
```

Expected: 测试全部通过；工作区干净；当前且唯一开发分支为 `main`；没有额外 worktree。

最终汇报严格只包含：修改文件、新增页面和接口、状态流转、并行实现、结果页内容、是否存在真实模型/解析器调用、实际 Git 分支/worktree/commit 操作。
