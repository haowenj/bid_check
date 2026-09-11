# 标书检查

新版“标书检查”Web 工作流。招标文件提取链路以投标文件模板为主要检查对象，并保留少量项目专用编制要求和模板外证明材料；当前已接入全部明确匹配模板的模板文本对照检查，以及普通证明材料的多模态附件检查，单模板一次调用并发数为 3。

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
- “标书合规性校验”“评标规则校验”和“全面校验”均可执行；全面校验按“合规检查 → 评标检查”串行编排，复用合规阶段已经生成的投标文件解析、检查产物和事实，不重复执行合规检查；评标阶段继续复用现有客观评分、主观评分和否决规则执行逻辑；
- “评标规则校验”从招标文件的 MinerU 结构块中提取评分规则和具有明确后果的否决性规则，并继续执行现有客观评分和否决规则流程；主观评分仍可通过独立接口触发；“全面校验”会在客观评分后自动执行主观评分，再执行否决规则检查；
- 招标文件要求提取只调用项目现有 MinerU `/tasks` 服务链路；MinerU 配置从环境变量读取，调用失败时任务明确失败；
- 配置 `LLM_API_KEY` 后，招标对象提取仅在模板边界、模板命名或前附表行存在歧义时使用 OpenAI-compatible Chat Completions；模板文本检查对全部明确匹配且有可靠模块内容的模板各执行一次完整模块对照调用，并发数为 3；未配置时使用安全的 uncertain fallback；
- MinerU/结构解析结果和各阶段提取结果分别按招标文件内容哈希缓存；
- 结果页展示完整模板、项目专用编制要求、模板外补充证明材料，以及当前单份投标文件自身的文件级要求；文件级检查仅使用用户原始上传文件的文件名、后缀和 `Path.stat().st_size`，不使用 MinerU 中间文件。
- 文件级要求提取从结构化招标文件中保留投标人须知/前附表、投标文件编制制作递交、电子投标文件、上传加密解密等完整候选区域，专用 LLM 只返回明确约束当前单份文件的大小、格式、文件名等规则；文件数量、分别提交、备份、多格式组合、U 盘/光盘、纸质副本、密封包装和平台上传能力说明均不进入正式规则。
- 投标文件清洗阶段已接入真实 MinerU `/tasks` 结果：保留原始 content list、清洗日志、跨页合并日志、章节结构、独立表格和独立图片索引；本阶段不执行模板匹配、附件/字段校验、RAG、LLM 判断、签字盖章识别或最终检查规则。
- 当前模板文本检查以一个完整招标模板和一个已匹配投标文件模块为一次 LLM 调用；模型先在同一次调用中确认两者的语义、用途和核心内容是否对应，只有确认后才判断文本层面的填写、占位残留、正文遗漏、实质性修改和条件适用性。语义不匹配或不确定只记录为候选状态，不形成业务 fail；本阶段不判断签字、盖章、图片、附件真实性或外部状态。
- 当前附件检查从完整模板正文中识别明确的普通证明材料要求，仅处理已经明确匹配且不属于 21/21.x 业绩合同内部审查范围的模板；每个模块将正文、表格和结构化关联图片提交给多模态模型，先在同一次调用中确认候选确实是投标时额外提供的独立证明材料，再保存视觉事实并形成附件要求结论。模板正文、表格填写、承诺函、普通签字盖章和未来履约义务不会仅因关键词命中而进入附件业务检查；不判断证件或银行账户真实性，也不处理业绩合同。
- 21.x 业绩合同检查当前启用 `21.1`～`21.6`：DOCX 首次经过 MinerU 解析时同步对图片做 OCR，并将结果按 `image_id`、图片块和 `section_path` 写入 `structured_document`；检查阶段按每个 21.x 子章节独立收集全部图片及完整 OCR，按原始顺序一次提交给现有文本 LLM，由文本模型提取合同事实、证明材料和签署页候选，不再用关键词筛选结果删减合同正文。只有文本模型定位出的少量签署候选图片，以及 OCR unavailable 图片，交给现有视觉模型，不重复调用图片 OCR。金额、时间、甲乙方和服务内容等事实必须带有对应 `image_id` 及 OCR 原文证据；同时核对业绩情况表当前行与合同项目、合同相对方、金额和服务期限的一致性，不能以顺序对应代替事实一致。签字、盖章和签署日期仍由视觉模型确认。检查证明文件顺序、合同服务内容、实施时间、合同金额、合同签页、合同签署日期及框架合同条件性结算材料；不同 21.x 之间不共享证据。结果写入 `10_performance_reviews.json`，并与现有模板文本/普通附件检查结果汇合展示。

### 评标规则提取（第一阶段）

- evaluation 模式仍沿用现有招标文件 + 投标文件上传接口，但本阶段只使用招标文件；投标文件不解析、不评分、不计算总分/排名，也不执行否决判断。
- 从完整的评标办法、评审办法、评分表、初步/详细评审、资格/符合性审查和明确否决条款中提取 `score_categories[]`、`score_items[]`、`veto_rules[]` 和 `uncertain_rules[]`；普通评标流程、委员会组成、开标说明、目录索引和没有明确后果的提醒会被过滤。
- 每个正式规则保存父子层级、分值、原文、条件、计分方式、分档、时间/数量/金额/比例/上下限、证明材料、客观/主观性质，以及对应的 MinerU `block_ids`；来源无法确认的内容进入 `uncertain_rules[]`，不补充招标文件之外的规则。
- 评标结果写入任务目录的 `compliance_extraction/`：`10_evaluation_rule_candidates.json` 保存完整候选章节和送模字符数，`11_evaluation_rules.json` 是独立正式产出物，`12_evaluation_filter_report.json` 记录被过滤的流程/目录/章节边界内容，`llm/call_NNN_input.json` 与 `llm/call_NNN_output.json` 保存脱敏调用、原始响应、Schema 校验、usage 和耗时。

## 真实要求提取配置

MinerU 使用项目既有服务配置：

- `MINERU_URL`：MinerU 服务地址，必填；
- `MINERU_BACKEND`：MinerU 后端，默认 `hybrid-engine`；
- `MINERU_SERVER_URL`：使用 `hybrid-http-client` 时的 MinerU server 地址；
- `MINERU_API_KEY`：MinerU 服务需要鉴权时配置；
- `MINERU_TIMEOUT_SECONDS`、`MINERU_POLL_INTERVAL_SECONDS`：任务超时与轮询间隔；
- `LLM_API_KEY`：启用 OpenAI-compatible LLM；同时可设置 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_MAX_TOKENS`（默认 8192）、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_CONCURRENCY`（默认 5，限制单个应用进程内同时进行的 LLM 请求数）。
- 项目根目录可放置本地 `.env`，配置项命名与合同审查项目一致；当前真实运行配置使用 DashScope、`LLM_MODEL=qwen3.8-27b` 和 `LLM_ENABLE_THINKING=false`。`.env` 不纳入 Git。
- `COMPLIANCE_MAX_BATCHES`：候选批次数，范围 1–10，默认 8。

MinerU 未配置、调用失败或结果无法解析时，任务会失败并保留错误原因；不会切换到其他解析器。未配置 LLM 时，系统使用确定性、来源受限的本地对象提取器。解析摘要和执行日志会记录实际解析器（`mineru`）、传输协议、是否真实调用 MinerU、耗时和结构统计。

## 投标文件清洗产物

投标文件解析成功后，在投标文件所在任务目录生成 `bid_document_cleaning/`：

- `raw_content_list.json`：从 MinerU ZIP 原样复制的 content list 字节；
- `cleaned_content_list.json`、`cleaning_log.json`：仅移除确定性的页码、页眉、空白块和单标点噪声，并逐项记录来源；
- `merged_content_list.json`、`merge_log.json`：只合并跨连续页、位于页边缘且正文未完结的相邻段落；保留参与合并的原始索引、页码、bbox 和 source path；
- `structured_document.json`：包含 `blocks[]`、`sections[]`、`tables[]`、`images[]` 和 `unsupported_items[]`。表格不拍平为普通正文，图片保留 `img_path`、资源状态、章节路径和来源关系，并在 `images[]` 中保存一次性 MinerU OCR 的 `ocr_status`、`ocr_source`、`ocr_text`、`ocr_blocks[]` 及其图片块关联；
- `cleaning_summary.json`：记录源文件 SHA-256、各阶段数量、页/章节/表格/图片统计和 artifact 路径。

也可单独运行清洗器，便于人工检查数据质量：

```bash
uv run python -m app.bid_document \
  data/tasks/<task_id>/bid.docx \
  --output-dir data/tasks/<task_id>/bid_document_cleaning
```

## 处理日志

工作流和对象提取链路使用 Python `logging` 输出阶段日志。默认启动命令会在终端显示 `start`、`end`、`retry` 和 `error` 事件，包括文件名、实际解析器、是否调用 MinerU、对象数量、模型名和耗时；不会记录 API Key。重点事件前缀包括 `workflow.*`、`document.parse.*`、`functional.region.*`、`evaluation.*`、`llm.call.*` 和 `compliance.extract.*`。

每个任务的招标文件目录下会生成 `compliance_extraction/`：`execution.jsonl` 保存结构化执行事件，`01_parsed_blocks.json`、`02_functional_regions.json`、`03_templates.json`、`04_project_requirements.json`、`05_supplemental_materials.json`、`06_filter_report.json` 和 `07_result.json` 保存阶段产物，`08_file_requirement_candidates.json` 保存文件级 LLM 实际候选上下文及字符统计，`09_file_requirements.json` 保存过滤后的正式单文件要求，`09_file_requirement_filter_report.json` 仅保存被过滤数量和原因，`08_template_text_reviews.json` 保存本轮全部明确匹配模板的文本检查结果，`09_attachment_reviews.json` 保存普通附件检查的视觉事实与要求结论，`10_file_requirement_reviews.json` 保存原始投标文件元数据、每条文件自身要求、实际值、判定和招标原文依据，`10_performance_reviews.json` 保存业绩合同检查结果。`llm/call_NNN_input.json` 和 `llm/call_NNN_output.json` 保存每次调用的脱敏请求、原始响应、解析结果、耗时、finish reason、usage 和错误信息，`semantic_match`、`business_status` 和 `execution_status` 区分候选语义状态与正式业务状态，`stats` 额外保存代码候选数、语义分类、无候选和无证据统计，`summary.json` 保存实际解析器、解析统计、模板/项目要求/补充材料/文件级要求数量及 LLM 调用统计，`workflow_summary.json` 保存并行解析、复核和整个任务耗时。阶段文件在成功后立即写入，后续失败不会清理已有文件。`data/mineru_cache/` 独立保存按解析器版本、MinerU 传输方式、服务地址、后端和文件内容哈希得到的结构解析结果，对象结果缓存也会区分解析器、LLM 模型和批次参数，避免旧结果遮蔽新的提取配置。

最终结果是四类对象：`templates[]` 保存完整模板名称、所属章节、连续 `block_ids`、模板正文、表格、填写项、附件说明和来源；`project_requirements[]` 保存直接影响投标文件组成、编制、容量、形式、有效期、保证金、备选方案和报价格式的项目专用要求及其具体值；`supplemental_materials[]` 保存模板之外明确要求随投标文件提交的证明材料；`file_requirements[]` 保存当前单份投标文件自身的文件级约束。模板正文和来源原文直接来自 MinerU/DOCX 结构块，LLM 只辅助识别歧义边界、命名或筛选，不生成 `check_type`、`scope`、`evidence_type`、`checks` 等执行字段，也不把模板编译成自然语言规则。评标办法、评分、履约、终验、人员管理、知识产权归属和违约责任不会进入这四类核心结果；明确的项目专用条款优先过滤通用保证金或纸质递交模板。
最终结果另有 `file_requirements[]`：每项包含单份投标文件约束对象、大小/后缀/文件名结构化参数、自动检查能力、来源 block 和原文。文件名只有包含具体字面量时才自动检查；只有“项目名称”“投标人名称”等未提供具体值的泛化命名规则会保留为“无法自动检查”。

## 测试

```bash
uv run pytest -v
```
