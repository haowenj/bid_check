# 招标文件模板与编制材料提取重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将招标文件提取链路从大量 `TenderRequirement` 重构为完整模板、少量项目专用编制要求和模板外补充证明材料三类检查对象。

**Architecture:** 保留现有 MinerU-compatible 结构解析、解析缓存、来源校验/修复、LLM 调用记录和 workflow 编排；在其上增加基于标题语义的功能区域识别，并以连续 blocks 聚合模板。确定性逻辑负责主流程和适用性过滤，LLM 仅辅助无法确定的模板边界、名称、前附表相关行和明确提交材料判断；提取结果、API 和页面切换为三类对象。

**Tech Stack:** Python 3.14、标准库 XML/ZIP/JSON/正则、Pydantic、FastAPI/Jinja2、SQLite、pytest、现有 `uv` 环境。

**Spec:** `docs/superpowers/specs/2026-09-01-tender-compliance-objects-design.md`

## Global Constraints

- 本轮不实现投标文件真实解析、模板匹配、RAG、图片识别、签字盖章判断、实际合规判断或最终审查报告。
- 本轮主结果只能包含 `templates`、`project_requirements`、`supplemental_materials` 三类集合，不再以 `TenderRequirement` 为主产物。
- 功能区域按标题语义、heading 类型、章节结构和原文顺序识别，不依赖固定“第六章”等章节编号。
- 一个跨多个连续 blocks 的模板必须保留为一个完整对象，不得按 block 或自然语言规则拆分。
- 模板必须保留名称、所属章节、连续 block ids、正文、表格、填写项、附件说明和后端回填的原始来源文本。
- 项目专用要求只保留直接影响投标文件组成、编制、填写、形成和提交的项目值，不生成复杂执行类型。
- 补充材料只保留明确要求随投标文件提供的具体证明材料，不把资格条件、履约义务或服务能力本身转成检查项。
- 明确项目专用信息优先于通用正文：无需保证金时过滤保证金模板，仅电子文件时过滤纸质正副本/密封包装等通用对象。
- 评标办法、评分标准、评标委员会流程、招标代理流程、中标候选人规则和外部采购系统状态不进入三类主结果。
- 继续复用 MinerU 解析缓存、结果缓存、`ComplianceExtractionRecorder`、执行日志和同一候选窗口内的来源修复；提取结果缓存必须升级版本，不能命中旧 98 条结果。
- 有效 DOCX 在未配置外部 LLM 时使用确定性提取，不回退到固定五条 mock；非 DOCX 历史测试字节的兼容 fallback 可以保留。
- 所有行为改动遵循 TDD：先写一个能证明目标行为的失败测试，确认失败原因正确，再写最小生产实现并验证通过。
- 当前直接在仓库 `main` 工作区修改，不创建 worktree；每个独立任务完成自己的测试后提交一个小 commit。

## 文件结构

- Modify: `app/models.py` — 增加三类轻量 `TypedDict` 和顶层提取结果类型，保留旧类型导入兼容。
- Modify: `app/compliance_extraction.py` — 增加功能区域、完整模板、项目要求、补充材料、适用性和来源归一化逻辑；收窄 LLM schema；复用现有解析/缓存/recorder 基础设施。
- Modify: `app/api.py` — 默认 workflow 调用新提取函数，结果缓存接受对象字典，保留非 DOCX 历史 fixture fallback。
- Modify: `app/mock_services.py` — 如有必要调整 mock review 的输入文案/摘要，让它接受三类结果而不执行真实审查。
- Modify: `app/templates/bid_check_task.html` — 三组结果展示完整模板、项目要求和补充材料来源。
- Modify: `app/static/bid-check.css` — 为三类结果增加最小必要样式，保留已有 workflow 样式。
- Modify: `tests/test_compliance_extraction.py` — 替换旧 Requirement 主链路测试为区域、模板、项目要求、补充材料、来源、缓存和 artifact 测试。
- Modify: `tests/test_tender_requirement_simplification.py` — 删除/改写依赖旧自然语言 Requirement 产物的断言，保留旧导入兼容和“不生成执行字段”的回归测试。
- Modify: `tests/test_end_to_end.py`、`tests/test_api.py`、`tests/test_pages.py`、`tests/test_workflow.py` — 更新 API、页面和 workflow 的三类结果断言。
- Modify: `README.md` — 更新产物、LLM 职责、artifact 文件、缓存和真实验收说明。
- Create if absent: `tests/fixtures/` 下的最小结构化 blocks/真实 DOCX fixture；优先复用现有测试 helper，不复制不必要的大文件。
- Create: `docs/superpowers/plans/2026-09-01-tender-compliance-objects.md` — 本实施计划。

## 结果接口与内部接口

实现阶段应以以下接口为稳定边界，字段命名如需结合现有代码微调，必须保持语义不变。

- `TenderTemplate`：`id`、`name`、`section`、`block_ids`、`body`、`tables`、`fields`、`attachments`、`source`。
- `ProjectRequirement`：`id`、`requirement`、`value`、`source`。
- `SupplementalMaterial`：`id`、`name`、`material`、`source`。
- `TenderExtractionResult`：`templates`、`project_requirements`、`supplemental_materials`。
- `TenderSource`：沿用现有 `section`、`block_ids`、`source_text` 形状。
- 新主函数建议命名为 `extract_tender_compliance_objects(tender_file, *, parser, llm, cache, parser_cache, recorder, ...) -> TenderExtractionResult`；旧 `extract_compliance_requirements_real` 和 `extract_compliance_requirements` 仅在确有导入兼容需要时保留别名，但返回值改为三类结果字典。
- LLM 适配器的主调用应返回有限候选的三类对象候选或结构化分组，不允许返回旧 `name/rule/condition` Requirement 列表作为主协议。
- workflow requirements stage 的输出类型改为 `TenderExtractionResult`；bid parse 和 review 仍保持现有 mock 行为。

### Task 1: 建立三类产物类型和顶层接口契约

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_compliance_extraction.py`
- Test: `tests/test_tender_requirement_simplification.py`

**Interfaces:**
- Produces the typed shapes `TenderTemplate`, `ProjectRequirement`, `SupplementalMaterial`, `TenderExtractionResult`。
- Keeps `TenderRequirement` and `TenderRequirementSource` importable for old callers, but no new extractor test may require a `requirements` list。

- [ ] **Step 1: Write the failing type-shape tests**

  增加测试，构造最小三类对象并断言其必须包含完整模板字段、项目值字段、材料字段和统一 source 字段；断言顶层结果只有三个提取集合，不包含 `requirements`。

- [ ] **Step 2: Run the focused tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "result_shape or template_fields or project_requirement_shape or supplemental_material_shape" -q`

  Expected: FAIL because the new types and result contract are not defined。

- [ ] **Step 3: Add the minimal TypedDict declarations**

  在 `app/models.py` 按现有 `TypedDict` 风格增加 source、template、project requirement、supplemental material 和顶层 result 类型；不增加数据库列、不引入 dataclass 层级或执行字段。

- [ ] **Step 4: Run the focused tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py -k "result_shape or template_fields or project_requirement_shape or supplemental_material_shape" -q`

  Expected: PASS。

- [ ] **Step 5: Commit the contract change**

  Run: `git add app/models.py tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py && git commit -m "refactor: define tender extraction object results"`

### Task 2: 实现标题语义功能区域识别

**Files:**
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`

**Interfaces:**
- Consumes: `Sequence[StructuredBlock]`。
- Produces: `FunctionalRegion` records containing a region kind (`templates`、`project_requirements`、`supplemental_materials`)、标题、section、连续 block ids、blocks and source order。
- Region detector must recognize template aliases and front-table aliases without using chapter numbers。

- [ ] **Step 1: Write failing region-detection tests**

  使用不同章节编号和 heading 文本构造 blocks，覆盖“投标文件格式”“响应文件格式”“商务投标文件格式”“资格审查文件格式”“报价文件格式”“投标人须知前附表”“投标须知前附表”“项目专用表”“响应人须知前附表”。断言模板区和项目区能被识别；包含“评标办法”“评分标准”的 blocks 不会被归入三类功能区域。

- [ ] **Step 2: Run the focused tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "functional_region or region_alias or chapter_number" -q`

  Expected: FAIL because no new region detector exists。

- [ ] **Step 3: Implement normalized semantic title matching**

  增加标题规范化和功能区域分类表；只对 heading block、显式 section 标题或结构上可靠的标题文本分类。区域结束边界由同级/上级的新区域标题或文档末尾确定，保留区域原始 block 顺序和 metadata。

- [ ] **Step 4: Run the focused tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "functional_region or region_alias or chapter_number" -q`

  Expected: PASS。

- [ ] **Step 5: Commit the region detector**

  Run: `git add app/compliance_extraction.py tests/test_compliance_extraction.py && git commit -m "feat: detect tender extraction regions by semantics"`

### Task 3: 聚合完整模板并保留原始结构

**Files:**
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`

**Interfaces:**
- Consumes: template `FunctionalRegion` and parsed `StructuredBlock`。
- Produces: `list[TenderTemplate]` with deterministic ids, one object per complete template, source text reconstructed from backend blocks。
- Helper boundaries should separate template boundary detection, field extraction, attachment extraction and source reconstruction so each can be tested independently。

- [ ] **Step 1: Write failing template aggregation tests**

  构造一个商务模板区：模板标题、多个正文 blocks、一个 table block、填写占位符、附件说明，再接下一个模板标题。断言第一个模板只生成一个对象，包含所有连续 blocks；表格保留 block id/text/metadata；`body` 不被压缩成一条 rule；填写项包含姓名、日期等原文字段；附件包含身份证正反面等明确说明。

- [ ] **Step 2: Add failing cross-block and boundary regression tests**

  覆盖没有固定“第六章”的标题、模板跨 paragraph/table/paragraph、同一模板中间出现无标题说明、连续模板之间有空白/说明 block，以及技术/商务/报价多个区域。断言模板数量等于真实模板数量，而不是 blocks 数量或候选句子数量。

- [ ] **Step 3: Run the template tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "template_aggregation or template_structure or cross_block or template_boundary" -q`

  Expected: FAIL because the existing implementation only creates `TenderRequirement` objects。

- [ ] **Step 4: Implement deterministic template segmentation and structure preservation**

  依据显式子标题、表/附件/格式标记、占位符和结构断点分组；默认将相邻正文和表格归入当前模板，直到下一个可靠模板标题。模板正文和 source text 由 block map 回填，tables 从 table blocks 直接携带文本与 metadata，fields/attachments 只从原文标记抽取，不设计检查执行逻辑。

- [ ] **Step 5: Run the focused template tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "template_aggregation or template_structure or cross_block or template_boundary" -q`

  Expected: PASS。

- [ ] **Step 6: Commit complete-template extraction**

  Run: `git add app/compliance_extraction.py tests/test_compliance_extraction.py && git commit -m "feat: preserve complete tender templates"`

### Task 4: 提取前附表项目专用编制要求

**Files:**
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`

**Interfaces:**
- Consumes: project `FunctionalRegion` blocks/tables。
- Produces: `list[ProjectRequirement]` containing only `requirement`、`value` and source。
- Project selection must include file composition, separate compilation, size limits, readability/scans, electronic or paper form, validity period, bid bond, alternatives and price format when explicitly present。

- [ ] **Step 1: Write failing front-table selection tests**

  构造含有前附表行的 table block，包含“投标文件由哪些部分组成”“各组成部分分别编制”“单个部分 50MB”“总容量 500MB”“文件清晰可读”“投标有效期 90 天”“无需投标保证金”“不允许备选方案”“只上传一份加密电子投标文件”“报价保留两位小数”。断言每个相关行最多产生一个项目要求，`value` 保留项目具体值。

- [ ] **Step 2: Write failing exclusion tests for non-compilation rows**

  在同一前附表或邻近块加入评分、评标委员会流程、代理流程、中标候选人、履约、终验、人员请假/替换、知识产权归属等文本。断言它们不进入项目要求。

- [ ] **Step 3: Run the focused tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "project_requirement or front_table or compilation_scope" -q`

  Expected: FAIL because no project requirement extractor exists。

- [ ] **Step 4: Implement narrow deterministic project-row extraction**

  使用投标文件编制关键词和项目值正则识别相关行；将每行原文作为 requirement 的来源，抽取容量、天数、份数、小数位等 value；不要把一行拆成多个执行要求。对无明确项目值的“清晰可读”等要求保留 value 为 `None` 或现有轻量约定。

- [ ] **Step 5: Run the focused tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "project_requirement or front_table or compilation_scope" -q`

  Expected: PASS。

- [ ] **Step 6: Commit project requirement extraction**

  Run: `git add app/compliance_extraction.py tests/test_compliance_extraction.py && git commit -m "feat: extract project-specific bid compilation requirements"`

### Task 5: 提取模板外明确补充证明材料

**Files:**
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`

**Interfaces:**
- Consumes: blocks in announcement/qualification semantic regions plus template names/attachments for de-duplication。
- Produces: `list[SupplementalMaterial]` with material name, submission wording and backend source。

- [ ] **Step 1: Write failing supplemental-material tests**

  构造公告/资格 blocks：营业执照或事业单位法人证书、分支机构授权、指定时间范围业绩证明、合同关键页、制造商登记证明，并分别写出“须随投标文件提供/提交/附复印件/扫描件”。断言材料进入补充集合且 source ids/text 完整。

- [ ] **Step 2: Write failing negative tests for non-material qualifications and future duties**

  加入“具有良好商业信誉”“具备丰富的软件项目经验”“能够提供 7×24 小时服务”“人员更换需报备”“终验后的质保义务”“知识产权最终归属”。断言这些不会进入补充材料，也不会进入模板或项目要求。

- [ ] **Step 3: Write failing de-duplication tests**

  同时在模板附件和资格区域出现营业执照，断言模板内已有完整材料说明时不重复生成补充材料；资格区域中带额外明确提交条件的材料允许保留一个来源合并对象。

- [ ] **Step 4: Run the focused tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "supplemental_material or qualification_material or future_duty" -q`

  Expected: FAIL because the current path only produces Requirement objects。

- [ ] **Step 5: Implement explicit-submission material extraction**

  仅在“具体材料名 + 明确提交动作/随投标文件语义”同时满足时生成对象；保留完整来源，不提取能力、信誉、服务和合同义务。用规范化材料名与模板 attachments 做去重，保留更完整的原文说明。

- [ ] **Step 6: Run the focused tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "supplemental_material or qualification_material or future_duty" -q`

  Expected: PASS。

- [ ] **Step 7: Commit supplemental material extraction**

  Run: `git add app/compliance_extraction.py tests/test_compliance_extraction.py && git commit -m "feat: extract explicit supplemental bid materials"`

### Task 6: 加入项目专用适用性过滤和来源归一化

**Files:**
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`

**Interfaces:**
- Consumes: raw templates, project requirements, supplemental materials and parsed block map。
- Produces: filtered `TenderExtractionResult` plus deterministic filter report entries with candidate name, block ids, source text and reason。
- Source normalizer must validate every returned block id and build `source.section`/`source.source_text` from backend blocks。

- [ ] **Step 1: Write failing applicability tests for no bid bond**

  构造通用“投标保证金/保证金缴纳证明”模板和前附表“无需递交投标保证金”。断言保证金模板不在 `templates`，前附表要求仍在 `project_requirements`，filter report 记录 `project_no_bid_bond` 类原因和来源。

- [ ] **Step 2: Write failing applicability tests for electronic-only submission**

  构造纸质正本、纸质副本、密封包装、外层包封模板和前附表“只需上传一份加密电子投标文件”。断言这些通用纸质对象不进入主结果；如果前附表明确要求纸质材料，则保留相应对象。

- [ ] **Step 3: Write failing source validation and repair tests**

  传入不存在 block id 必须失败；传入同一候选窗口内错误 block id 时，若 rule/材料文本能在邻近真实 block 支持，则修复为真实 ids；所有最终对象 source text 必须与 block map 原文一致。

- [ ] **Step 4: Run the focused tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "applicability or bid_bond or electronic_only or source_validation or source_repair" -q`

  Expected: FAIL because old filtering only handles Requirement text and does not filter template objects。

- [ ] **Step 5: Implement explicit applicability context and source normalizer**

  从已提取 project requirements 中识别有限标记 `no_bid_bond`、`electronic_only`、`paper_copies_required` 和备选方案适用性；只有明确项目专用信息才过滤。复用现有 source repair 思路，分别支持模板连续 blocks、项目行和补充材料来源。

- [ ] **Step 6: Run the focused tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "applicability or bid_bond or electronic_only or source_validation or source_repair" -q`

  Expected: PASS。

- [ ] **Step 7: Commit applicability and source normalization**

  Run: `git add app/compliance_extraction.py tests/test_compliance_extraction.py && git commit -m "feat: apply project-specific tender applicability"`

### Task 7: 收窄 LLM 协议并保持调用/失败记录

**Files:**
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`
- Test: `tests/test_compliance_artifacts.py` only if recorder assertions need new object labels。

**Interfaces:**
- Consumes: narrowly selected ambiguous region candidates, never the full tender document。
- Produces: template grouping/name candidates, selected project rows or explicit material decisions with copied source block ids; never a Requirement list or generated source text。
- Existing OpenAI-compatible HTTP capture, redaction, retry and recorder behavior remains available。

- [ ] **Step 1: Write failing prompt-contract tests**

  模拟 OpenAI-compatible response and inspect request prompt. Assert it asks for three object classes/模板连续分组 and explicitly forbids `check_type`、`scope`、`evidence_type`、`checks`、`required_field`、`source_text` and natural-language Requirement compilation. Assert it does not ask the model to scan the entire document。

- [ ] **Step 2: Write failing LLM schema tests**

  Test valid narrow output, unknown block ids, extra execution fields, an output that splits one template into many rules, and invalid JSON. Assert schema errors fail the extraction stage or are discarded only according to the narrow ambiguity policy; no source text from the model is trusted。

- [ ] **Step 3: Write failing deterministic fallback tests**

  With no LLM, feed valid DOCX blocks containing several templates and front-table/material candidates. Assert deterministic extraction returns complete templates and three collections without a fixed five-item mock and without calling an LLM。

- [ ] **Step 4: Run the focused tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py -k "prompt_contract or llm_schema or deterministic_fallback or execution_field" -q`

  Expected: FAIL because the current adapter still returns old Requirement-shaped responses and prompt text。

- [ ] **Step 5: Implement the narrow adapter contract**

  Change the prompt/output parser to carry only grouping, names, project values and explicit material decisions tied to candidate block ids. Keep thread-local recorder context, raw response, finish reason, usage, retry counters and elapsed times. Update cache descriptor/version so a prior Requirement result cannot be reused。

- [ ] **Step 6: Run the focused tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py tests/test_compliance_artifacts.py -k "prompt_contract or llm_schema or deterministic_fallback or execution_field or llm" -q`

  Expected: PASS。

- [ ] **Step 7: Commit the narrowed LLM adapter**

  Run: `git add app/compliance_extraction.py tests/test_compliance_extraction.py tests/test_compliance_artifacts.py && git commit -m "refactor: narrow llm to tender object extraction"`

### Task 8: 重写主提取编排、结果缓存和 artifact

**Files:**
- Modify: `app/compliance_extraction.py`
- Modify: `app/compliance_artifacts.py` only if generic payload typing or event labels require it
- Test: `tests/test_compliance_extraction.py`
- Test: `tests/test_compliance_artifacts.py`

**Interfaces:**
- Main extractor returns `TenderExtractionResult` and accepts the same parser/cache/parser_cache/recorder injection points。
- Result cache stores one JSON object with the three collections; parsed document cache continues storing structured blocks。
- Artifact files are `01_parsed_blocks.json`, `02_functional_regions.json`, `03_templates.json`, `04_project_requirements.json`, `05_supplemental_materials.json`, `06_filter_report.json`, `07_result.json`, `summary.json` and existing `llm/call_NNN_*`。

- [ ] **Step 1: Write failing orchestration tests**

  Inject fake parser and narrow fake LLM, run the new main extractor, and assert the return value has three collections. Assert no `requirements` key and no `TenderRequirement` objects are produced。

- [ ] **Step 2: Write failing artifact tests**

  Assert parsed blocks, functional regions, three object files, filter report, result, summary, execution events and LLM call files are persisted under the tender task directory. Summary must record `template_count`、`project_requirement_count`、`supplemental_material_count`、call counts, durations and cache states。

- [ ] **Step 3: Write failing cache tests**

  Run extractor twice with the same tender/parser/LLM configuration. Assert parsed parser and LLM are not called on appropriate cache hits and the cached object result equals the first result. Change extraction version/LLM descriptor and assert result cache misses while parsed MinerU cache remains reusable。

- [ ] **Step 4: Write failing failure-preservation tests**

  Make a later LLM ambiguity call or normalization step fail. Assert earlier parse/region/object artifacts and `summary.json` with failed status remain on disk; assert no partial invalid main result is returned。

- [ ] **Step 5: Run the focused orchestration tests to verify they fail**

  Run: `uv run pytest tests/test_compliance_extraction.py tests/test_compliance_artifacts.py -k "orchestration or artifact or cache or failure_preservation" -q`

  Expected: FAIL because the existing main function persists Requirement artifacts and returns a list。

- [ ] **Step 6: Implement the new orchestration with reused recorder helpers**

  Replace the old candidate-to-Requirement normalization path with region extraction, template/material/project assembly, applicability filtering and result assembly. Persist each stage immediately; keep defensive recorder error handling and final summary behavior. Generalize JSON cache value handling only as much as needed to store the result dict。

- [ ] **Step 7: Run the focused orchestration tests to verify they pass**

  Run: `uv run pytest tests/test_compliance_extraction.py tests/test_compliance_artifacts.py -k "orchestration or artifact or cache or failure_preservation" -q`

  Expected: PASS。

- [ ] **Step 8: Commit the new extraction orchestration**

  Run: `git add app/compliance_extraction.py app/compliance_artifacts.py tests/test_compliance_extraction.py tests/test_compliance_artifacts.py && git commit -m "refactor: orchestrate tender object extraction"`

### Task 9: 接入 workflow/API 并更新页面三类结果

**Files:**
- Modify: `app/api.py`
- Modify: `app/mock_services.py` if review input summary needs adjustment
- Modify: `app/templates/bid_check_task.html`
- Modify: `app/static/bid-check.css`
- Test: `tests/test_api.py`
- Test: `tests/test_end_to_end.py`
- Test: `tests/test_pages.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Workflow requirements stage receives/returns `TenderExtractionResult`。
- Task API retains status/file/bid_parse/review fields and adds only `templates`、`project_requirements`、`supplemental_materials` from extraction output。
- Review remains a mock and must not produce pass/fail/score/risk conclusions。

- [ ] **Step 1: Write failing workflow/API tests**

  Update end-to-end fixture to include a template, front-table rows and explicit qualification material. Assert completed task payload exposes all three collections, has no `requirements`, and preserves mock bid parse/review output。

- [ ] **Step 2: Write failing page tests**

  Assert completed result page renders three section headings/counts and typical template body/table/fields/attachments/source; assert old “合规性检查要求” count wording and `requirements` loop are gone。

- [ ] **Step 3: Run focused integration tests to verify they fail**

  Run: `uv run pytest tests/test_api.py tests/test_end_to_end.py tests/test_pages.py tests/test_workflow.py -q`

  Expected: FAIL because API/UI still expect a Requirement list。

- [ ] **Step 4: Wire the new extractor into `build_default_workflow`**

  Keep `MinerUDocumentParser`, parser cache and result cache configuration; call the new extractor; preserve non-DOCX historical fixture fallback only for invalid byte stubs. Do not alter bid parse/review mock implementation beyond input-shape compatibility。

- [ ] **Step 5: Update the result template and styles**

  Render templates as full cards with body, tables, fields, attachments and sources; render project requirements and supplemental materials as separate compact sections with source details. Keep the page disclaimer that real bid checking is not implemented。

- [ ] **Step 6: Run focused integration tests to verify they pass**

  Run: `uv run pytest tests/test_api.py tests/test_end_to_end.py tests/test_pages.py tests/test_workflow.py -q`

  Expected: PASS。

- [ ] **Step 7: Commit workflow/API/UI integration**

  Run: `git add app/api.py app/mock_services.py app/templates/bid_check_task.html app/static/bid-check.css tests/test_api.py tests/test_end_to_end.py tests/test_pages.py tests/test_workflow.py && git commit -m "feat: expose tender templates and materials in workflow"`

### Task 10: 更新文档、测试全量回归和定位真实文件

**Files:**
- Modify: `README.md`
- Modify: tests touched by prior tasks as needed for consistent terminology
- Test/inspect: `data/`, task directories and existing fixtures; do not add secrets or large derived artifacts to Git

**Interfaces:**
- Documentation must describe three result collections, narrow LLM scope, artifact names, cache reuse and exclusions。
- No source code implementation is added in this task beyond consistency fixes proven by failing documentation/tests。

- [ ] **Step 1: Write failing documentation/contract assertions**

  Add or update tests that assert README and API/page terminology names `templates`、`project_requirements`、`supplemental_materials`, explain no real bid check, and do not describe the old Requirement list as the primary result。

- [ ] **Step 2: Run documentation and current full tests to identify failures**

  Run: `uv run pytest -q`

  Expected: Any failures must be limited to stale old-result assertions or implementation gaps; unrelated baseline failures must be investigated before proceeding。

- [ ] **Step 3: Update README and stale test terminology**

  Describe functional-region detection, complete templates, front-table project requirements, supplemental materials, applicability precedence, new artifacts and the explicit excluded categories. Do not claim real bid parsing/checking exists。

- [ ] **Step 4: Run the full test suite**

  Run: `uv run pytest -v`

  Expected: PASS with zero failures and no unexpected errors/warnings。

- [ ] **Step 5: Verify repository diff and secrets hygiene**

  Run: `git diff --check HEAD^`, `git status --short --branch`, and inspect changed files with `git diff --stat` plus targeted `rg` for API keys, authorization headers, and old `requirements` primary-result references. Do not print `.env` contents or any credential.

- [ ] **Step 6: Commit documentation and regression updates**

  Run: `git add README.md tests && git commit -m "docs: describe tender object extraction outputs"`

### Task 11: 用当前同一份真实招标文件验收并生成结果报告

**Files:**
- No production source changes unless a fresh failing regression test proves a defect from Tasks 1–10.
- Inspect: the current real tender file discovered under existing task/data paths or the user-provided current fixture。
- Generate only task-local runtime artifacts under the existing task directory; do not commit credentials, raw external requests, or large derived files。

**Interfaces:**
- Uses the configured default workflow/extractor and existing MinerU parse cache。
- Produces a user-facing acceptance report containing object counts, names, representative complete objects, exclusions, LLM/caching statistics and artifact locations。

- [ ] **Step 1: Locate the current real tender file without changing data**

  Inspect known task/data directories and existing test references with `rg --files data .` and metadata-only `find`/`stat`. Prefer the same file that produced the existing 98-result run; do not substitute a new tender silently。

- [ ] **Step 2: Record the pre-refactor reference count without exposing source secrets**

  Use existing saved result/artifact metadata or a read-only script to record the old 98-count reference and its task path. Do not print full credentials or unnecessary tender text。

- [ ] **Step 3: Run the refactored extractor on that same tender**

  Run the repository’s configured extraction entry point with the same environment and file. Reuse the existing MinerU parse cache; allow the new result cache to populate under its new version. Capture elapsed time and exit status。

- [ ] **Step 4: Inspect summary and artifacts**

  Read `summary.json`, `07_result.json`, `06_filter_report.json`, `execution.jsonl` and selected template/material artifacts. Verify every final block id exists in `01_parsed_blocks.json`, source text matches backend blocks, and artifacts contain no authentication material。

- [ ] **Step 5: Verify the ten acceptance points**

  Confirm no 98-style natural-language Requirement list, major template names are present, templates remain whole, project requirements are few, supplemental materials include explicit license/performance evidence, future duties and scoring are absent, no-bond/electronic-only precedence is applied, all sources are reliable, and MinerU cache reuse is visible。

- [ ] **Step 6: Report exact outcomes and stop**

  Report: template count and name list; project requirement count; supplemental material count; several complete representative templates; removed categories versus the old 98 results; actual LLM calls, retries, failures and elapsed time; parser/result cache hit status; filtered guarantee/paper candidates and reasons; artifact directory path. Stop after this report and do not begin real bid parsing or checking。

## Plan Self-Review

- Spec coverage: Tasks 2–3 cover semantic regions and complete templates; Task 4 covers front-table project values; Task 5 covers explicit supplemental materials; Task 6 covers project precedence and source traceability; Task 7 narrows LLM scope; Task 8 preserves cache/artifact/failure behavior; Task 9 updates workflow/API/UI; Tasks 10–11 cover documentation, regression and the same-file acceptance report。
- Placeholder scan: No implementation step depends on an unspecified helper, unchosen option, fixed chapter number or future feature. Every test step names a focused command and an expected red/green outcome。
- Type consistency: `TenderTemplate`、`ProjectRequirement`、`SupplementalMaterial` and `TenderExtractionResult` are introduced in Task 1 and consumed consistently by Tasks 3–9; old `TenderRequirement` is compatibility-only。
- Scope: No task adds bid-file parsing, template matching, RAG, image recognition, signature/stamp judgment, real review or final report generation。
- Operational safety: The plan assumes the current `main` worktree as explicitly requested, preserves existing user data, avoids reading secrets, and keeps generated acceptance artifacts task-local。
