# MinerU 3.4.4 DOCX Office 输出分析

## 实测条件

- 服务：`http://127.0.0.1:7100`
- `/health`：版本 `3.4.4`，协议版本 `2`
- 输入：`tests/fixtures/generated/mineru_docx_features.docx`
- 输入 SHA-256：`8353fa3fa3e6b2757a5d5f9930467d351994ca6de6aace9346e38dd1737b0fba`
- 请求：ZIP、Markdown、middle JSON、content list 和图片均开启；model JSON 与原文件关闭。
- 输出目录中的 Office 根目录：`raw/mineru_docx_features/office/`

本文件记录一次 3.4.4 实测结果。它用于锁定当前项目的适配契约，不把一次样例中存在的字段宣称为 MinerU 的永久保证。

另用根目录真实标书 `test_bid.docx` 完成回归：v2 共 408 个原始对象，展平为 405 个有效 `DocumentBlock`，其中 `index=1`、`unknown=0`；3 个纯空对象被过滤，所以有效对象索引中保留 1、2、21 三个间隙。全部有效块的 `source_object_index` 非空且唯一。相对修改前结果，ID、原始位置、`section_path`、`prev_id`、`next_id` 全部逐项一致，11 个表格和 69 个图片块未丢失。

## 输出文件

本次服务实际返回：

```text
mineru_docx_features.md
mineru_docx_features_middle.json
mineru_docx_features_content_list.json
mineru_docx_features_content_list_v2.json
images/dc37d7cc9f1f551e3fbefcdab47207aa55d6fd3bfcf501243f23000b50e823d1.jpg
```

没有返回 model JSON，也没有返回原始 DOCX，因为本次请求关闭了这两个选项。三份 JSON 均能加载。

## middle.json

顶层是对象：

```json
{
  "pdf_info": [...],
  "_backend": "office",
  "_version_name": "3.4.4"
}
```

`pdf_info` 本次只有一页/组。page 对象字段为：

```text
para_blocks: list
discarded_blocks: list
page_idx: 0
```

`para_blocks` 保持自然块顺序。本次出现的类型为 `title`、`text`、`list`、`table`、`image`。title 有 `level`、`is_numbered_style`、`lines`、`index`；text 有 `lines`、`index`；list/table/image 都有 `index`。

文本块结构为：

```json
{
  "type": "text",
  "lines": [{"spans": [{"type": "text", "content": "项目经理应具有三年以上同类项目经验。"}]}],
  "index": 3
}
```

标题结构与普通文本相同，但增加 `level`：

```json
{
  "type": "title",
  "lines": [{"spans": [{"type": "text", "content": "3.1.2 人员要求", "style": ["bold"]}]}],
  "index": 2,
  "is_numbered_style": true,
  "level": 3
}
```

本次 list 的关键结构是 `attribute`、`ilevel`、`blocks`、可选 `start`、`index`；每个 `blocks` 项又是 text block。table 使用 `blocks[0].type=table_body`，HTML 位于其 span 的 `html` 字段。image 使用 `blocks[0].type=image_body` 和 `image_path`，caption 是同一 image block 的第二个子 block，类型为 `image_caption`。

middle 的优势是保留 line/span 粒度和明确的 `index`、`page_idx`，但它是更深的内部结构。本阶段只完整保留它用于 Debug 和契约分析，不按它与 v2 的数组位置做隐式合并。

## content_list.json（legacy）

顶层是扁平列表，每一项是对象。所有本次项目块都有 `page_idx: 0`。出现的字段/类型如下：

| `type` | 实际字段 |
|---|---|
| `text` | `text`、标题时额外有 `text_level`、`page_idx` |
| `list` | `list_items`、`page_idx` |
| `table` | `table_caption`、`table_body`、`page_idx` |
| `image` | `img_path`、`image_caption`、`page_idx` |

legacy 标题文本带 Markdown 包裹，例如 `**第三章 技术要求**`；v2 标题文本不带该包裹。legacy list 的 `list_items` 已含 `- ` 或 `1. ` 前缀。legacy table 的 HTML 位于 `table_body`，图片路径位于 `img_path`，图片说明位于 `image_caption`。

## content_list_v2.json

顶层是“组列表”，本次只有一个组；组是长度为 15 的块列表。块统一为：

```json
{
  "type": "paragraph",
  "content": {
    "paragraph_content": [
      {"type": "text", "content": "项目经理应具有三年以上同类项目经验。"}
    ]
  }
}
```

生成的契约 DOCX 出现的 v2 `type` 为 `title`、`paragraph`、`list`、`table`、`image`。真实标书 `test_bid.docx` 还稳定观察到一个 `type=index` 块，其 `content` 使用与 list 相同的 `list_type + list_items` 结构。

- title：`content.title_content` 是 span 列表，层级在 `content.level`。
- paragraph：`content.paragraph_content` 是 span 列表。
- list：`content.list_type`、`attribute`、`list_items`；每个 list item 有 `item_type`、`ilevel`、`prefix`、`item_content`。
- index：同样从 `list_items[].prefix + item_content` 提取，保留缩进和原始顺序；标准块标记 `metadata.default_rag_eligible=false`。
- table：`content.table_caption`、`html`、`table_type`、`table_nest_level`。
- image：`content.image_source.path` 与 `content.image_caption`。

本次 v2 块没有逐块 `page_idx`、`anchor` 或原生对象索引。适配器按外层组顺序展平全部对象，将过滤前的 `flat_index` 同时写入 `source_object_index` 和 `metadata.source_position.flat_index`；被过滤的空块会留下索引间隙。组序号仍只用于定位，不猜作 `page_idx`。

## DOCX 与 PDF 输出的实际差异

- DOCX 使用 `_backend: "office"`；不能套用 pipeline/vlm PDF 的 `pdf_info` 内容细节或 bbox 假设。
- DOCX v2 是组列表，每块主要是 `type + content`；本次没有块级 bbox、page_idx、anchor。
- DOCX middle 保留的是 Office 段落/行/span 结构，页面信息只有外层 page 对象的 `page_idx`。
- legacy DOCX 标题文本包含 Markdown `**`，v2 标题文本保留纯文字和 style span。
- DOCX 表格直接返回 HTML；本次没有二维 `cells` 数据。
- DOCX 图片返回相对 Office 输出目录的图片路径；解压后实际文件扩展名是 `.jpg`，与输入 PNG 扩展名不同。
- 生成的 OMML 公式没有产生公式块；公式只留下相邻的普通文本“公式示例：”。不能据此宣称 3.4.4 Office backend 已稳定提供 formula。

## DocumentBlock 字段可用性矩阵

| 字段 | 本次 DOCX 结论 | 标准化策略 |
|---|---|---|
| `id` | MinerU 不提供 | 项目按输入 hash + 最终顺序派生 |
| `block_type` | v2 `type` 稳定可见，真实标书出现 `index` | 已知类型直接映射；未知保留 `unknown` |
| `text` | title/paragraph/list 有内容；table/image 文字分开 | 从实际 content/span/list item/caption 提取 |
| `title_level` | v2 `content.level`、legacy `text_level`、middle `level` | 保留明确层级 |
| `section_path` | MinerU 不提供 | 项目按标题栈派生 |
| `page_idx` | middle/legacy 提供；v2 本次不提供 | v2 为 null；legacy/middle 只在其自身适配器中保留 |
| `anchor` | 本次未观察到 | null，并记录未观察到 |
| `source_object_index` | v2/legacy 不提供原生对象索引；middle 的 `index` 不是选定 v2 块的可证明关联键 | 项目写入所选 content list 过滤前的 `flat_index`，不与 middle 数组对齐 |
| `source_type` | v2/legacy/middle 均提供 `type` | 保留原始值 |
| `table` | HTML、caption 可用；二维 cells 未提供 | `html`/caption 填充，`cells=null` |
| `image` | path、caption 可用 | 解析为 raw 下安全相对路径 |
| `prev_id`/`next_id` | MinerU 不提供 | 项目在过滤后派生 |
| `metadata` | 可保存来源 flavor、组/项/flat 位置和未映射字段 | 完整原始文件仍在 raw/ |
| `formula` | 本次未观察到 | 不把普通文本硬映射为 formula |

异常文本检测是非破坏性的：仅对短小且完全由标点/符号组成的文本，以及标题中的替换字符、控制字符、私用区或代理区字符记录 `normalization_warnings`。例如 `★资格审查资料` 含正常文字，不会仅因装饰符号被告警。检测不会删除块、改变文本或猜测修正原文。

真实标书回归中有两个段落被标记，原文分别是 `……` 和 `©`；两者仍完整保留在 `document_blocks.json`。本次没有标题异常字符告警。

## 当前适配边界

第一版选择 v2 作为标准化主输入；legacy 仅在 v2 缺失或结构不支持时回退。middle、legacy、v2 只通过各自明确字段被消费，不按数组位置做三方合并。`index` 只带一个供后续消费方使用的默认 RAG 排除标记；本阶段没有 Node Builder、Embedding 或 RAG。后续若 MinerU 版本增加块级 page、anchor、formula 或原生对象键，应先更新本分析文档与契约 fixture，再扩展转换器和测试。
