# 投标文件 MinerU 数据清洗与结构整理设计

## 目标

在当前 `bid_check` 项目中，把投标文件的 MinerU 原始解析结果整理成稳定、完整、可追溯的结构化数据，作为后续模块定位、模板对照、表格/图片检查和检索的共同输入。

本轮只处理：

```text
投标文件 → MinerU 原始 content list → 保守清洗 → 跨页正文整理 → 章节归属 → 结构化产物
```

不加入模板匹配、fields/attachments 校验、RAG 建索引、LLM 合规判断、签字盖章识别或最终检查规则。

## 当前代码与真实样本判断

- `app/compliance_extraction.py` 已有 `StructuredBlock`、MinerU `/tasks` 解析、嵌套 content list 展平、章节字段、解析诊断和缓存相关实现，但这些逻辑主要服务招标文件要求提取。
- `app/mock_services.py::parse_bid_document` 当前只返回固定统计，是投标文件真实解析的替身，不能作为本轮数据基础。
- 真实样本 `data/tasks/f5a91fe3-a4f1-4a49-b2bc-788c618d590e/bid.docx` 包含大量模板正文、11 个表格和约 70 个图片资源，且表格/图片与正文混排。
- 合同项目的 `clean_mineru_data.py`、`merge_cross_page_paragraphs.py`、`mineru_raw_parse.py` 验证了保守删除、跨页合并安全条件、原始来源保留和资源路径安全检查；但合同项目的业务分类和检索逻辑不进入本轮。

## 设计原则

1. 信息保留优先于文本整洁。明确为正文、表格、图片或未知类型的对象不会因无法分类而丢弃。
2. 原始结果与派生结果分开保存。原始 `content_list` 不回写、不重排；清洗和结构化结果通过来源索引追溯回原始对象。
3. 内容顺序是主关系。所有 block 按 MinerU 展平后的文档顺序保留 `order`，跨页合并只生成带来源列表的派生 block。
4. 章节归属是派生信息，不替代原始文本。block 保留现有 `section` 字段，同时在 metadata 中保存 `section_id` 和完整 `section_path`。
5. 表格和图片是独立对象。表格保留原始 HTML/body、解析出的行结构和图片引用；图片保留 caption/alt、资源路径和邻接正文关系，不转成普通段落。
6. 不依赖固定章节编号。标题层级优先使用 MinerU 的 `text_level`/`heading_level`/`level`，缺失时只使用有限的编号样式推断，不凭业务关键词强行分类。

## 组件与接口

新增 `app/bid_document.py`，职责限定为投标文件 MinerU 获取、清洗、结构整理和产物写入：

```python
class BidDocumentParser(Protocol):
    def parse(self, path: Path, *, output_dir: Path) -> dict[str, Any]: ...

class MinerUBidDocumentParser:
    def parse(self, path: Path, *, output_dir: Path) -> dict[str, Any]: ...

def parse_bid_document(
    bid_file: FileMetadata,
    *,
    parser: BidDocumentParser,
) -> dict[str, Any]: ...
```

`MinerUBidDocumentParser` 使用当前项目已有的 `MINERU_URL`、backend、server URL、超时和轮询配置，调用同一 `/tasks` 协议，但独立保留 raw ZIP 中的 content list 字节和引用资源。测试通过注入 parser，不调用外部服务。

## 清洗与整理流程

### 1. 原始结果读取

- 校验输入文件和 MinerU 结果 ZIP。
- 选择唯一的 content list；若同时存在 v2，优先结构化的 v2 成员。
- 保存原始 JSON 字节到 `raw_content_list.json`。
- 对嵌套 title/paragraph/list/table/image 做有序展平；展平对象携带原始 item index、父 item index 或子项定位，不把原始对象从追溯链中隐藏。
- 扫描 `image`/`table` 的安全相对 `img_path`，提取到任务产物目录；非法或缺失资源只记录状态，不删除对应内容 block。

### 2. 保守清洗

只删除以下确定性噪声：

- `page_number` 类型；
- `header` 类型；
- 空白正文；
- 仅由一个标点符号组成的正文块。

`footer`、未知类型、空 caption 的图片、有表格结构但无可显示文本的表格、扫描附件和模板占位内容均保留。清洗日志记录原始索引、类型、原因和文本预览。

### 3. 跨页正文整理

参考合同项目的边界判断，但针对投标文件保持更谨慎：只合并相邻页、页面底部到下一页顶部、均为普通正文、无标题层级、下一页不呈现新标题样式、前一段没有完整终止符或冒号的文本块。

合并结果保留：

- 合并后正文；
- 首尾页码；
- `source_item_indices`；
- `source_page_indices`；
- `source_bboxes`；
- `merged_cross_page`；
- 详细 merge log。

不跨越表格、图片、标题或附件边界合并。未满足条件的块原样保留。

### 4. 章节归属

对合并后的 block 按顺序维护标题栈：

- 标题层级使用 MinerU 提供的层级；
- 缺失层级时，仅对明确标题类型或有限编号样式推断层级；
- 同级或上级标题弹出旧栈；
- 正文、表格和图片继承当前章节路径；
- 每个章节生成稳定的 `section_id`、标题、层级、路径、首尾 order 和 block ids。

章节归属失败时使用空路径和 `section_id=null`，不丢弃 block。

### 5. 结构化产物

`structured_document.json` 顶层包含：

- `schema_version`、文件名、源文件 hash、解析器诊断；
- `stats`；
- `sections[]`；
- `blocks[]`；
- `tables[]`；
- `images[]`；
- `relationships[]`。

`blocks[]` 使用当前 `StructuredBlock` 的 `block_id/type/text/section/order/metadata/heading_level` 字段。metadata 中补充原始 MinerU 类型、原始 item 定位、页码/bbox、来源索引、章节路径、表格/图片专有字段及前后 block id。

`tables[]` 和 `images[]` 是独立索引，引用对应 block id 和完整来源信息，避免后续消费者为了找表格或图片而把正文重新解析一遍。

## 任务工作流接入

- `build_default_workflow` 为 bid_parse 构造真实 `MinerUBidDocumentParser`，并继续保留 parser 依赖注入。
- `bid_parse` 结果返回文档名、状态、统计和产物路径；review 阶段仍使用现有 mock，不读取或判断清洗内容。
- 现有测试使用 fixture parser，避免真实 MinerU 网络调用；旧的 `mock_services.parse_bid_document` 兼容测试 helper 不作为生产默认实现。

## 产物布局

每个任务的 `bid_document_cleaning/` 下生成：

```text
raw_content_list.json
cleaned_content_list.json
merged_content_list.json
structured_document.json
cleaning_summary.json
cleaning_log.json
merge_log.json
images/...
```

所有 JSON 使用 UTF-8、中文不转义、稳定缩进；写入时使用临时文件加原子替换，避免中途失败留下看似完整的产物。

## 测试与验收

自动化测试覆盖：

- 嵌套 MinerU content list 展平和来源定位；
- 页码、页眉、空白和单标点噪声删除；
- 表格、图片、未知类型不丢失；
- 跨页合并正例及标题/表格/图片/完整句边界反例；
- 章节层级和混合 block 归属；
- 图片资源安全提取与缺图降级；
- JSON 产物和统计；
- 默认 workflow 的 parser 注入和结果协议。

验收时先运行全量 `uv run pytest -q`，再使用真实 `bid.docx` 调用配置好的 MinerU，检查 raw、cleaned、merged、structured、summary 产物，并人工抽查正文、表格、图片、章节和来源索引的一致性。若外部 MinerU 不可用，必须明确报告失败原因，不伪造真实解析统计。
