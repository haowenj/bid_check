# MinerU DOCX → DocumentBlock 第一阶段设计

## 1. 目标

新建一个与既有合同风险审查项目相互独立的 Python 项目。第一阶段只完成一条数据链路：将 DOCX 提交给本机 MinerU 服务，完整保存 MinerU 原始产物，再把 MinerU 的 DOCX Office 输出转换为项目统一、稳定且可调试的 `DocumentBlock` 列表。

本阶段不实现 Agent、RAG、向量数据库、Web 页面或长文本切分。一个 MinerU 自然块最多生成一个 `DocumentBlock`；空且没有结构意义的 MinerU 块不生成 `DocumentBlock`。

## 2. 已确认环境

- MinerU API：`http://127.0.0.1:7100`
- MinerU 版本：`3.4.4`
- API 协议版本：`2`
- 同步接口：`POST /file_parse`
- 服务声明支持 DOCX，并支持返回 ZIP、middle JSON、content list、图片和原文件。
- 当前项目目录为空且尚未初始化 Git。
- 项目自行生成覆盖完整结构的测试 DOCX，不依赖用户业务标书。

实现和文档结论必须以该服务解析测试 DOCX 后的实际产物为准。不得把 PDF backend 的字段假设套用到 DOCX Office backend。

## 3. 范围

### 3.1 包含

- 初始化使用 `src/` 布局的 Python 项目。
- 封装 MinerU HTTP DOCX 解析调用。
- 原样保留 MinerU HTTP ZIP 响应和解压后的原始产物。
- 生成覆盖多级标题、列表、表格、图片、公式、普通段落和空段落的 DOCX fixture。
- 实测并记录 `middle.json`、`content_list.json`、`content_list_v2.json` 的 DOCX Office 数据结构及差异。
- 定义并序列化稳定的 `DocumentBlock` 数据模型。
- 将一个 MinerU 自然块转换为最多一个 `DocumentBlock`，保持自然阅读顺序。
- 根据标题层级维护标题栈并生成 `section_path`。
- 提供 CLI、解析报告、单元测试、契约测试和显式集成测试。

### 3.2 不包含

- Agent、工作流编排或智能体运行时。
- RAG、embedding、向量数据库或检索逻辑。
- Web 页面或 HTTP 业务服务。
- 长文本切分、chunk 合并或语义重排。
- OCR/PDF 通用标准化。
- 针对真实投标业务规则的审查逻辑。
- 对 MinerU 服务的安装、升级、启动或配置管理。

## 4. 方案选择

标准化以 `content_list_v2.json` 为首选输入，因为它提供按自然块组织的统一 `type + content` 结构。`middle.json` 和旧版 `content_list.json` 必须完整保存，用于结构分析、Debug 和在存在可靠关联键时补充缺失字段。

不得仅凭数组位置把三份 JSON 的块强行关联。若 `content_list_v2.json` 缺失或其顶层结构不受支持，转换器回退到 `content_list.json`。如果两者都不存在或无法识别，CLI 明确失败并保留所有已经收到的原始材料。

`middle.json` 不作为第一阶段的主要标准化输入，因为它是 MinerU 内部中间结构，信息丰富但耦合深、升级风险高。第一阶段也不实现三份 JSON 的全量对齐合并。

## 5. 架构与职责

数据流如下：

```text
DOCX
  → MinerUHttpClient
  → 原始 ZIP 和解压产物
  → DocxMineruNormalizer
  → DocumentBlock[]
  → document_blocks.json + report.json + CLI 摘要
```

组件职责：

- `mineru_client.py`：验证输入后缀，调用 MinerU `/file_parse`，保存原始响应，验证响应类型并安全解压。不理解 `DocumentBlock`。
- `models.py`：定义 `BlockType`、`TableContent`、`ImageContent`、`DocumentBlock` 等稳定模型及 JSON 序列化。
- `normalizer.py`：探测 DOCX Office JSON 形态、选择 v2 或 legacy 适配器、逐块标准化、维护标题栈、过滤无意义块并连接前后块。
- `reporting.py`：从标准化块生成统计报告，不读取 MinerU 原始 JSON。
- `scripts/parse_docx.py`：解析参数，串联客户端、标准化器和报告输出，并以进程退出码表示结果。
- `scripts/generate_test_docx.py`：可重复生成测试 DOCX fixture。

不引入插件框架、依赖注入容器、仓储层或抽象工厂。v2 与 legacy 的形态差异使用聚焦的小函数处理。

## 6. MinerU 请求和原始产物

CLI 默认向 `POST http://127.0.0.1:7100/file_parse` 发送单个 DOCX multipart 请求，并使用：

- `response_format_zip=true`
- `return_middle_json=true`
- `return_content_list=true`
- `return_images=true`
- `return_md=true`
- `return_model_output=false`
- `return_original_file=false`

Office DOCX 由 MinerU 自行选择 Office backend；项目不为 DOCX 强制套用 PDF backend 的字段或行为。HTTP 超时默认 1800 秒，可通过 CLI 修改。

每次运行创建不可覆盖的独立目录：

```text
outputs/<docx-stem>/<run-id>/
├── manifest.json
├── raw/
│   ├── response.zip
│   ├── *_middle.json
│   ├── *_content_list.json
│   ├── *_content_list_v2.json
│   ├── *.md
│   └── images/
├── document_blocks.json
└── report.json
```

`run-id` 使用 UTC 时间和输入 SHA-256 短前缀构成，以便定位且避免覆盖。ZIP 必须先作为 `raw/response.zip` 原样落盘，再使用解析后目标路径检查防止绝对路径和 `..` 路径穿越。

`manifest.json` 记录：输入文件名、输入 SHA-256、输入字节数、MinerU URL、MinerU `/health` 返回的版本与协议版本、请求参数、开始/结束时间、HTTP Content-Type、原始文件清单、标准化所选 JSON flavor 和 warning 汇总。manifest 不保存认证信息或其他秘密。

## 7. 测试 DOCX

生成器创建 `tests/fixtures/generated/mineru_docx_features.docx`，至少包含以下有唯一可断言文本的结构：

- 一级标题“第三章 技术要求”。
- 二级标题“3.1 总体要求”。
- 三级标题“3.1.2 人员要求”。
- 三级标题后的正文“项目经理应具有……”用于验证完整 `section_path`。
- 同级标题和返回上级标题，用于验证标题栈替换与深层清理。
- 普通段落。
- 项目符号列表和编号列表。
- 至少一个含表头与两行数据的表格。
- 至少一张项目生成的小型 PNG，并带图片附近的说明文字。
- 可由 DOCX 表示并被 MinerU 识别的行内公式或独立公式；若 MinerU 3.4.4 实际未输出 formula 块，分析文档必须记录差异，不能伪造 formula 映射。
- 空段落和仅含空白字符的段落。

生成脚本可重复运行；相同依赖版本下产生语义和样式等价的 fixture。fixture 作为集成测试输入保留在仓库中。

## 8. DocumentBlock schema

### 8.1 顶层字段

```json
{
  "id": "doc_a1b2c3d4e5f6_b000001",
  "block_type": "paragraph",
  "text": "项目经理应具有……",
  "title_level": null,
  "section_path": [
    "第三章 技术要求",
    "3.1 总体要求",
    "3.1.2 人员要求"
  ],
  "page_idx": null,
  "anchor": null,
  "source_object_index": null,
  "source_type": "paragraph",
  "table": null,
  "image": null,
  "prev_id": "doc_a1b2c3d4e5f6_b000000",
  "next_id": "doc_a1b2c3d4e5f6_b000002",
  "metadata": {
    "source_format": "docx",
    "source_json": "content_list_v2",
    "source_position": {
      "page_group_index": 0,
      "item_index": 4,
      "flat_index": 4
    },
    "normalization_warnings": [],
    "unmapped_fields": {}
  }
}
```

字段约束：

- `id: str`：`doc_<input_sha256前12位>_b<六位自然块序号>`。过滤无意义块之后按最终顺序编号。
- `block_type: str`：统一类型枚举。
- `text: str`：规范化的可检索文字；允许在表格或图片块中为空。
- `title_level: int | null`：仅可靠标题层级，正整数。
- `section_path: list[str]`：当前标题层级路径。
- `page_idx: int | null`：只保留 MinerU 明确给出的页索引。DOCX 分页语义不得由项目自行推断。
- `anchor: str | null`：只保留 MinerU 明确给出的 anchor。
- `source_object_index: int | str | null`：只保留 MinerU 明确给出的对象索引，不用转换器顺序冒充。
- `source_type: str`：MinerU 原始块类型。
- `table: TableContent | null`：仅表格块使用。
- `image: ImageContent | null`：图片或图表块使用。
- `prev_id: str | null`、`next_id: str | null`：最终块序列的双向相邻引用。
- `metadata: dict`：来源定位、warning 和未映射字段。

### 8.2 统一块类型

第一版支持：

- `title`
- `paragraph`
- `list`
- `table`
- `image`
- `formula`
- `code`
- `chart`
- `header`
- `footer`
- `page_number`
- `footnote`
- `aside`
- `unknown`

新的 MinerU 类型若无法可靠归类，映射为 `unknown` 并保留 `source_type`，不得静默删除。

### 8.3 TableContent

```json
{
  "html": null,
  "markdown": "| 角色 | 要求 |\n|---|---|\n| 项目经理 | ... |",
  "cells": null,
  "caption": ["人员配置表"],
  "footnote": [],
  "image_path": null
}
```

字段均可为空。只填入 MinerU 实际、明确提供的表示。不得从 HTML 猜测或重建 `cells`；如果第一阶段没有可靠二维单元格数据，`cells` 为 `null`。

### 8.4 ImageContent

```json
{
  "path": "images/example.png",
  "caption": ["系统架构示意图"],
  "footnote": [],
  "alt_text": null
}
```

图片引用必须解析为相对于运行目录 `raw/` 的安全路径。原始 JSON 中存在引用但解压目录不存在文件时仍保留引用，并增加 warning。

## 9. 标准化规则

### 9.1 输入探测

转换器按文件名定位 `*_content_list_v2.json`、`*_content_list.json` 和 `*_middle.json`。选择顺序：

1. v2 文件存在、JSON 有效且顶层形态符合实测 DOCX Office 输出时，使用 v2。
2. 否则，legacy 文件存在、JSON 有效且顶层形态符合实测输出时，使用 legacy。
3. 否则失败，错误信息列出发现的候选文件和不支持的顶层形态。

适配器必须验证容器和关键字段类型。字段缺失时用 `null` 或 warning 表达，不能制造默认业务含义。

### 9.2 顺序与来源定位

按所选内容列表的原始嵌套顺序扁平化，自左至右、从外到内遍历，不排序。每个候选块在 `metadata.source_position` 中记录：

- `page_group_index`：v2 顶层页/组序号；legacy 无该层时为 `null`。
- `item_index`：所在组中的块序号。
- `flat_index`：过滤前的全局自然块序号。

`source_object_index` 仅使用实测 MinerU 原生字段。如果实际 Office 输出未提供，稳定输出 `null`，并在结构分析文档中记录。

### 9.3 文本与结构提取

- 标题和正文从对应结构化 spans/内容字段按原顺序连接。
- 列表项按原顺序以换行连接为 `text`；项目不在第一阶段重写编号或项目符号。
- 公式正文使用 MinerU 的公式文本或 LaTeX 字段；不执行公式识别。
- 表格 `text` 只包含可检索标题、脚注等文字；HTML/Markdown 放在 `table`。
- 图片 `text` 只包含 caption、footnote 或 alt text；路径放在 `image.path`。
- 文本只做两端空白清理和换行规范化，不合并相邻块，不重排 span，不做语义改写。

### 9.4 section_path

转换器维护 `dict[int, str]` 形式的标题栈：

1. 遇到带有效正整数层级 `L` 的标题时，删除所有层级大于等于 `L` 的旧标题，再写入 `stack[L] = title_text`。
2. 当前标题块的 `section_path` 在更新栈后生成，因此包含标题自己。
3. 非标题块继承当前栈。
4. 路径按层级升序输出，只包含实际存在的标题，不为跳级标题虚构祖先。
5. 标题层级跳跃时保留实际路径并记录 warning。
6. MinerU 将块标记为标题却没有有效层级时，保留 `block_type=title`，但不修改标题栈，并记录 warning。
7. 标题文本为空时，该候选块不更新标题栈；若没有其他结构意义则过滤。

示例：三级标题“3.1.2 人员要求”之后的正文获得：

```json
[
  "第三章 技术要求",
  "3.1 总体要求",
  "3.1.2 人员要求"
]
```

### 9.5 有意义块判定

满足任一条件即保留：

- `text.strip()` 非空。
- 表格存在 HTML、Markdown、cells、caption、footnote 或 image path。
- 图片/图表存在路径、caption、footnote 或 alt text。
- 公式存在非空公式内容。
- 未知块存在任何非空、非纯布局的内容字段。

纯空文本、空列表、没有结构内容的空容器不生成块。过滤完成后生成 ID，并为最终序列填写 `prev_id` 和 `next_id`；首块 `prev_id=null`，尾块 `next_id=null`。

### 9.6 未映射字段

标准化已消费的字段不复制到 metadata。其余非空字段存入 `metadata.unmapped_fields`，以便发现 MinerU 升级带来的新信息。完整原始块不复制进 `DocumentBlock`，因为 `raw/` 已保存原始 JSON，`metadata.source_position` 可定位来源。

## 10. CLI 与报告

基础命令：

```bash
python scripts/parse_docx.py path/to/example.docx
```

必要选项：

```text
--mineru-url http://127.0.0.1:7100
--output-dir outputs
--timeout 1800
--top-longest 5
```

CLI 成功时打印并写入 `report.json`：

- MinerU 原始结果目录。
- `document_blocks.json` 路径。
- 各 `block_type` 数量。
- 块总数。
- 总文本长度、平均长度、中位长度和最大长度。
- `title_level` 数量统计。
- 最多五个不同的非空 `section_path` 示例。
- 最长的若干文本块：ID、类型、字符数、截断预览和 section path。
- 未识别 `source_type` 和所有 normalization warning 的计数。

CLI 输入不是存在的 `.docx`、MinerU 不可用、HTTP 请求失败、响应不是 ZIP、ZIP 损坏、缺少可识别内容列表、JSON 无效或写文件失败时返回非零退出码，并向标准错误输出可操作的原因。收到的原始响应必须尽可能先保存。

## 11. 测试策略

### 11.1 单元测试

使用最小 JSON 片段验证：

- 标题层级正确继承。
- 同级标题替换当前层级。
- 返回上级标题清除更深层级。
- 跳级标题不生成虚构祖先并产生 warning。
- 无层级标题不污染标题栈。
- 块顺序与输入自然顺序一致。
- `prev_id`、`next_id` 首尾和中间连接正确。
- 空文本、空列表、空未知块被过滤。
- 有内容的表格和图片即使 `text` 为空也保留。
- 未知但有意义的类型不被删除。
- 原生对象索引不存在时 `source_object_index=null`，转换顺序只写入 metadata。
- v2 不可用时回退 legacy；两者都不支持时明确失败。

### 11.2 契约测试

首次实际解析生成的三类 JSON 会用于结构分析。测试目录保留从实际 MinerU 3.4.4 DOCX 输出中最小化裁剪的 JSON fixture；裁剪只能删除与契约无关的冗长文本或二进制引用，不能改变字段名称、容器层级或值类型。

契约测试锁定：

- v2 顶层容器和块结构。
- DOCX 标题 level 与 anchor 的实际位置。
- legacy 内容块的实际字段名称。
- middle JSON 的 backend/version、page/group、para block、line/span 等实际层级。
- 表格和图片引用的实际字段及路径形式。
- DOCX `page_idx` 与 `source_object_index` 是否实际存在。

### 11.3 集成测试

集成测试通过 pytest marker `integration` 显式运行，调用 `http://127.0.0.1:7100`：

- `/health` 版本为 3.4.4 或记录到 manifest 的可兼容 3.x 版本。
- 生成的测试 DOCX 得到 ZIP 响应。
- 原始 ZIP、middle、legacy content list、v2 content list 和图片均按实际返回保存；若服务对某项不返回，测试失败并促使更新结构分析，而不是静默跳过。
- 标准化结果包含预期标题路径、列表、表格和图片。
- 空段落不会产生无意义块。

普通 `pytest` 排除 `integration` marker，不依赖 MinerU 服务在线。完整验证命令单独运行集成测试。

## 12. 实际输出分析文档

`docs/mineru-docx-output-analysis.md` 必须基于 3.4.4 服务解析生成 fixture 后的真实文件编写，包含：

- 请求参数和 MinerU 版本。
- 每份 JSON 的顶层类型、层级结构和各块 type。
- 每类块的代表性脱敏/合成样例。
- `middle`、legacy、v2 之间能可靠关联和不能可靠关联的部分。
- 与 PDF 常见输出结构的明确差异。
- 每个 DocumentBlock 字段在 DOCX 中是稳定、条件提供、派生还是不可稳定提供。
- 本次实测没有出现的预期结构，如公式块，也必须明确记录。

该文档只描述观察结果，不把一次样例中偶然存在的字段宣称为 MinerU 永久保证。

## 13. 依赖与工程约束

- Python 最低版本为 3.11。
- 使用 `pyproject.toml` 管理项目元数据和依赖。
- 运行时使用一个成熟轻量 HTTP 库完成 multipart 上传。
- 模型使用标准库 `dataclasses` 和 `enum`，不引入 Pydantic。
- 开发依赖包含 pytest、python-docx 和 Pillow，用于测试、DOCX 与图片生成。
- 所有 JSON 使用 UTF-8、`ensure_ascii=false`、两空格缩进写入。
- 路径使用 `pathlib.Path`。
- 不安装或调用 GitHub CLI、Gitee CLI 或桌面客户端。

## 14. 验收条件

满足以下条件才算第一阶段完成：

1. `python scripts/parse_docx.py <docx>` 能调用 7100 服务并生成独立运行目录。
2. 完整原始 ZIP 和解压产物可用于 Debug。
3. 分析文档基于实际 MinerU 3.4.4 DOCX 输出，而非 PDF 假设。
4. `document_blocks.json` 符合本设计 schema。
5. 标题层级继承和 `section_path` 示例正确。
6. 自然块顺序不变，双向相邻 ID 正确。
7. 表格和图片未被误删，空内容未生成无意义块。
8. CLI 输出要求的统计、样例和最长块。
9. 普通测试与显式 MinerU 集成测试分别通过。
10. README 说明项目结构、运行方式、稳定字段与不稳定字段。
