# bid-check

第一阶段的“标书检查智能体”基础项目：调用本机 MinerU 3.4.4 DOCX 服务，保留完整原始结果，再将 Office backend 的自然块转换为统一 `DocumentBlock`。

当前范围只包含 DOCX 解析、原始产物保存、结构分析、标准化和统计报告；不包含 Agent、RAG、向量数据库、Web 页面或长文本切分。

## 项目结构

```text
bid_check/
├── scripts/
│   ├── generate_test_docx.py     # 生成覆盖完整结构的测试 DOCX
│   └── parse_docx.py             # 用户命令行入口
├── src/bid_check/
│   ├── mineru_client.py          # 7100 HTTP 调用、ZIP 保存、安全解压、manifest
│   ├── models.py                 # DocumentBlock 稳定 dataclass schema
│   ├── normalizer.py             # v2 优先、legacy 回退、标题栈和块转换
│   └── reporting.py              # 统计、最长块和 JSON 输出
├── tests/
│   ├── fixtures/generated/       # 合成 DOCX
│   ├── fixtures/mineru_3_4_4_docx/ # 实测 JSON 契约 fixture
│   ├── integration/              # 需要 7100 在线的测试
│   └── test_*.py                 # 普通单元/契约/CLI 测试
└── docs/
    ├── mineru-docx-output-analysis.md
    └── superpowers/specs/        # 已确认设计与实施计划
```

## 安装

项目不安装或启动 MinerU；默认复用 `http://127.0.0.1:7100` 上已经运行的服务。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

生成测试 DOCX：

```bash
.venv/bin/python scripts/generate_test_docx.py \
  --output tests/fixtures/generated/mineru_docx_features.docx
```

## 运行解析

```bash
.venv/bin/python scripts/parse_docx.py path/to/bid.docx
```

可选参数：

```text
--mineru-url http://127.0.0.1:7100
--output-dir outputs
--timeout 1800
--top-longest 5
```

每次运行生成独立目录，不覆盖历史 Debug 结果：

```text
outputs/<docx-stem>/<run-id>/
├── manifest.json
├── raw/
│   ├── response.zip
│   ├── <stem>_middle.json
│   ├── <stem>_content_list.json
│   ├── <stem>_content_list_v2.json
│   ├── <stem>.md
│   └── images/
├── document_blocks.json
└── report.json
```

CLI 会打印原始结果目录、标准化文件、各 `block_type` 数量、文本长度、标题层级、`section_path` 示例和最长文本块。原始 ZIP 会在解压前保存；ZIP 成员会检查绝对路径、`..` 穿越和 symlink。

## DocumentBlock schema

每个 MinerU 自然块最多生成一个标准块；过滤纯空内容后才生成 ID 和邻接链接。

```json
{
  "id": "doc_abcdef123456_b000000",
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
  "prev_id": null,
  "next_id": "doc_abcdef123456_b000002",
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

支持的 `block_type`：`title`、`paragraph`、`list`、`table`、`image`、`formula`、`code`、`chart`、`header`、`footer`、`page_number`、`footnote`、`aside`、`unknown`。实际 3.4.4 fixture 出现的是 title、paragraph、list、table、image；OMML 没有被识别成 formula，所以普通文本不会被硬映射为公式。

`section_path` 由标题栈派生：标题自身先进入路径；同级标题替换旧标题；回到上级标题时清除更深层；跳级不虚构缺失祖先。示例中“项目经理应具有……”得到三级路径。

表格使用 `table.html`、`table.caption` 等可用引用，二维 `cells` 在本次 DOCX 输出中为 `null`。图片使用相对于运行目录 `raw/` 的安全路径和说明文字。

## MinerU 3.4.4 DOCX 实际结构

完整字段、样例和 DOCX/PDF 差异见：[docs/mineru-docx-output-analysis.md](docs/mineru-docx-output-analysis.md)。关键结论：

- `content_list_v2.json` 是外层组列表，块为 `type + content`，本次没有逐块 `page_idx`、`anchor` 或对象索引。
- legacy `content_list.json` 是扁平列表；标题文本带 Markdown `**`，列表/表格/图片字段不同。
- `middle.json` 使用 `_backend="office"`，保留 `pdf_info → para_blocks → lines → spans`、`page_idx` 和显式 `index`，但不与 v2 按数组位置强行合并。
- DOCX 表格直接提供 HTML；图片引用的最终扩展名由 MinerU 输出决定，本次 PNG 输入得到 JPG 文件。
- 本次生成的 OMML 公式没有产生公式块，公式字段不能宣称为稳定可用。

## 测试

普通测试不依赖 MinerU 在线：

```bash
.venv/bin/python -m pytest -m "not integration" -q
```

集成测试会向 `http://127.0.0.1:7100` 上传生成的 DOCX：

```bash
.venv/bin/python -m pytest tests/integration -m integration -vv -s
```

本地重新生成完整 fixture：

```bash
.venv/bin/python scripts/generate_test_docx.py \
  --output tests/fixtures/generated/mineru_docx_features.docx
```

DOCX 能稳定提供哪些字段取决于实际 Office backend 输出。项目只把明确存在的字段映射到公共 schema；不可确定的字段为 `null`，未映射字段进入 `metadata.unmapped_fields`，完整原始 JSON 始终保留在运行目录中。
