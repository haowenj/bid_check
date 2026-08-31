# 新版标书检查 Web 工作流第一版设计

## 目标

在独立项目 `bid_check` 中实现新版“标书检查”Web 工作流第一版。项目仅参考相邻 `contract_risk_review` 的视觉风格与交互方式，不引用或修改其合同审查业务代码。

第一版打通以下路径：

```text
上传招标文件与投标文件
        ↓
选择校验方式
        ↓
创建任务并进入工作流页
        ↓
┌──────────────────────┐
│ 提取合规性检查要求    │
│ 解析投标文件          │  两个步骤并行
└──────────┬───────────┘
           ↓
执行合规性检查（模拟）
           ↓
展示合规性检查要求
```

原始第一版不调用 MinerU 或 LLM；本轮增量仅为招标文件要求提取接入 MinerU-compatible 解析、结构化 LLM（无凭据时本地确定性回退）和内容哈希缓存，仍不执行真实投标文件审查。

## 技术方案

项目使用 FastAPI、Jinja2、SQLite 和原生浏览器 JavaScript。页面由服务端渲染，工作流页通过 JSON 接口轮询任务状态。后台工作流使用线程池执行，两个模拟前置步骤通过独立 future 真正并发。

选择这一方案的原因：

- 与参考项目的 FastAPI/Jinja2 页面风格和运行方式接近；
- 不引入单独的前端构建链和状态管理框架；
- SQLite 能持久保存任务、阶段状态与结构化结果；
- 模拟服务和编排器分离，后续可以替换真实实现而不改变页面和任务协议。

## 项目边界

- `bid_check` 是新建的独立应用。
- `contract_risk_review` 只作只读视觉和技术参考。
- 不修改、复制或调用合同审查业务流程。
- 应用主页面为 `/bid-check`，根路径 `/` 重定向至该页面。
- 不创建除 `main` 之外的 Git 分支，不创建 Git worktree。

## 组件划分

### Web 层

负责路由、上传校验、模板渲染和 JSON 响应，不直接实现工作流逻辑。

- `GET /bid-check`：上传与模式选择页；
- `POST /api/bid-check/tasks`：上传两个文件并创建任务；
- `GET /bid-check/tasks/{task_id}`：任务工作流和结果页；
- `GET /api/bid-check/tasks/{task_id}`：任务状态与结果查询接口。

### 任务仓储

`BidCheckRepository` 使用 SQLite 保存任务。它负责创建任务、查询任务、更新总状态、更新阶段状态、保存结果和失败信息。写操作需要线程安全，并由仓储统一维护 `updated_at`。

任务记录至少包含：

- `task_id`
- `tender_file`
- `bid_file`
- `check_mode`
- `status`
- `requirements_status`
- `bid_parse_status`
- `review_status`
- `failed_stage`
- `error_message`
- `result_json`
- `created_at`
- `updated_at`

`tender_file` 与 `bid_file` 在 API 中返回结构化文件元数据，包括原始文件名、大小和内部存储路径。每个任务使用独立存储目录，内部文件名固定，以避免用户文件名影响业务代码。

### 工作流编排

`BidCheckWorkflow` 只负责任务状态流转和服务调用：

1. 将任务总状态更新为 `running`；
2. 将 `requirements_status` 与 `bid_parse_status` 更新为 `running`；
3. 同时提交 `extract_compliance_requirements()` 和 `parse_bid_document()`；
4. 分别收集两个 future 的返回值并更新对应阶段；
5. 两个阶段都完成后，将 `review_status` 更新为 `running`；
6. 调用 `run_compliance_review(requirements, parsed_bid)`；
7. 保存结构化结果，并将任务和审查阶段更新为 `complete`。

模拟服务允许通过依赖注入替换，以便测试成功、失败和并发场景，也为以后接入真实服务保留稳定接口。

### 服务接口

第一版包含三个独立函数：

- `extract_compliance_requirements(tender_file)`：从招标文件结构化内容提取来源可回溯的合规要求；历史非 DOCX 测试字节仅走兼容回退；
- `parse_bid_document(bid_file)`：根据上传文件名组装模拟解析摘要，统计数字仅存在于模拟服务配置中；
- `run_compliance_review(requirements, parsed_bid)`：返回 `mode=mock` 和“当前版本尚未执行真实合规性检查”的说明，不产生通过、不通过、得分或风险结论。

缓存由要求提取服务内部按文件内容哈希处理，不改变工作流接口。

## 数据协议

`check_mode` 预留三个值：

- `compliance`
- `evaluation`
- `full`

页面展示三个正式名称：

- 标书合规性校验：本轮可执行；
- 评标规则校验：开发中；
- 全面校验：开发中。

创建接口接受三个合法枚举值，但第一版只允许 `compliance` 启动任务。另两个值返回明确的“开发中”错误，不创建后台任务。

任务及阶段状态使用：

- `pending`
- `running`
- `complete`
- `failed`

完成后的 JSON 响应包含：

```json
{
  "task_id": "...",
  "tender_file": {
    "filename": "招标文件.docx",
    "size": 1234
  },
  "bid_file": {
    "filename": "投标文件.docx",
    "size": 5678
  },
  "check_mode": "compliance",
  "status": "complete",
  "requirements_status": "complete",
  "bid_parse_status": "complete",
  "review_status": "complete",
  "failed_stage": null,
  "error_message": null,
  "requirements": [],
  "bid_parse": {},
  "review_result": {
    "mode": "mock",
    "message": "当前版本尚未执行真实合规性检查"
  },
  "created_at": "...",
  "updated_at": "..."
}
```

结构化结果由后端生成并持久化，模板和 JavaScript 只负责渲染，不在页面代码中维护模拟规则。

## 状态流转与并发语义

```text
pending
  ↓
running
  ├─ requirements_status: running → complete / failed
  └─ bid_parse_status:    running → complete / failed
          ↓ 两者 complete
     review_status: running → complete / failed
          ↓
       complete / failed
```

两个前置步骤使用 `ThreadPoolExecutor.submit()` 分别提交。测试使用线程事件或屏障证明两个服务调用在任一调用返回前都已进入执行状态，不通过总耗时推测并发。

若任一并行步骤失败：

- 失败步骤更新为 `failed`；
- 另一步保留其实际最终状态；
- `review_status` 保持 `pending`；
- 任务总状态更新为 `failed`；
- `failed_stage` 标识 `requirements` 或 `bid_parse`；
- `error_message` 保存面向页面的失败信息。

若模拟审查失败，则 `review_status` 和任务总状态均为 `failed`，`failed_stage` 为 `review`。页面必须显示具体失败阶段和错误信息。

## 文件上传

上传页包含两个独立上传卡片：招标文件和投标文件。第一版仅支持 `.docx`，服务端以文件扩展名为最终校验依据，并拒绝缺失文件、空文件或不支持类型。

选中文件后页面显示：

- 文件名称；
- 可读格式的文件大小；
- 上传就绪状态；
- 删除；
- 重新选择。

两个文件都选择完成后才允许选择模式和提交。前端限制用于交互提示，服务端重复执行全部必需校验。

上传或任务创建失败时，页面保留已选择状态并显示可读错误。文件写入和数据库创建应作为一个受控流程处理；发生异常时不留下可被当作有效任务的记录。

## 页面设计

页面沿用参考项目的视觉语言：深蓝渐变顶栏、浅灰蓝背景、居中内容宽度、白色圆角卡片、细边框、轻阴影、深色主按钮、蓝色运行状态、绿色完成状态和红色失败状态。所有页面支持窄屏布局与键盘焦点样式。

### 上传与模式选择页

页面从上到下包含：

1. 标题与第一版说明；
2. 两个并列或窄屏堆叠的 DOCX 上传卡片；
3. 三个模式选择卡片；
4. “开始校验”按钮。

“评标规则校验”和“全面校验”显示“开发中”，不可作为可执行模式提交。正式名称中不出现“评分+废标检查”。

### 工作流页

工作流显示五个用户可理解的阶段：

1. 上传文件；
2. 提取合规性检查要求；
3. 解析投标文件；
4. 执行合规性检查；
5. 检查结果。

步骤 2 和步骤 3 在视觉上并列，并通过汇合关系连接步骤 4。页面显示等待中、运行中、已完成或失败。处于未完成状态时，浏览器定期轮询 JSON 接口；完成或失败后刷新为最终视图。

### 结果页

最终页面标题为“标书合规性校验结果”，展示：

- “本次共提取 5 项合规性检查要求”；
- 每项要求的名称、检查对象、普通项目符号形式的检查要求和来源；
- 投标文件模拟解析摘要；
- 明确提示“当前版本仅展示提取出的合规性检查要求，尚未执行真实投标文件内容校验”。

页面不使用对勾表达检查结果，不伪造通过、不通过、预计得分或废标风险。

## 异常处理

第一版覆盖以下错误：

- 缺少招标文件；
- 缺少投标文件；
- 文件类型不支持；
- 文件为空或上传保存失败；
- 非法或尚未开放的校验方式；
- 数据库任务创建失败；
- 模拟要求提取失败；
- 模拟投标解析失败；
- 模拟审查失败；
- 查询不存在的任务。

API 使用明确的 HTTP 状态码和中文错误信息。后台失败不能吞掉异常，必须落库为失败状态，确保页面停止轮询并指出失败阶段。

## 测试策略

实现采用测试驱动方式，覆盖以下层次：

### 仓储测试

- 创建任务并保存必需字段；
- 更新三个阶段和总状态；
- 保存、读取结构化结果；
- 保存失败阶段与错误信息。

### 工作流测试

- 两个前置步骤确实并发进入；
- 只有两者完成后才启动模拟审查；
- 要求提取失败时状态正确；
- 投标解析失败时状态正确；
- 模拟审查失败时状态正确；
- 完成后持久化 requirements、bid_parse 和 review_result。

### API 测试

- 两个 DOCX 上传成功并返回 `202`；
- 任一文件缺失、为空或扩展名不合法时拒绝；
- `evaluation`、`full` 和未知模式不会启动任务；
- 查询接口返回稳定的结构化任务协议；
- 不存在的任务返回 `404`。

### 页面测试

- `/bid-check` 展示两个上传区、三个正式模式名称及开发中状态；
- 工作流页展示并列的两个前置步骤和五个阶段；
- 失败页显示失败阶段；
- 完成页展示五项要求与模拟说明；
- 页面不出现真实合规结论文案。

### 完整流程验证

使用 FastAPI 测试客户端上传两个最小 DOCX 测试文件，创建任务，等待后台流程完成，验证最终 JSON 和 HTML。另行启动本地服务，通过浏览器完成一次上传到结果页的实际交互检查。

## 非目标与后续扩展

本轮不实现投标文件真实解析、真实合规判断、评分、否决投标判断、任务队列或分布式执行。

后续扩展时保持现有接口：

- 用真实实现替换三个模拟服务函数；
- 在服务函数前增加缓存层；
- 将本地线程池替换为外部任务队列；
- 增加 `evaluation` 与 `full` 工作流；
- 在不改变页面核心协议的前提下增加更细粒度进度。

## 验收标准

初版完成后应能实际完成上传、模式选择、任务创建、并行前置步骤、模拟审查和结果展示；本轮增量在下节明确接入招标文件要求提取所需的 MinerU-compatible 解析与结构化 LLM 调用，但仍不执行真实投标文件校验。

## 本轮增量：招标文件合规要求真实提取

本轮将原“要求提取”模拟服务替换为 `app/compliance_extraction.py` 中的真实来源链路：

```text
招标 DOCX
  ↓ MinerUDocumentParser（配置命令时调用 MinerU；本地开发使用 DOCX 结构回退）
结构化 blocks（顺序、章节、block_id、段落/表格）
  ↓ 确定性候选筛选（填写、占位符、附件、签章日期、文件编制；排除评分语义）
候选窗口
  ↓ 1–8 个有界批次（硬上限 10）
结构化 LLM 或无凭据时的确定性本地抽取器
  ↓ Pydantic Schema 校验
程序按 block_id 恢复来源原文、去重、排序并写入内容哈希缓存
  ↓
ComplianceRequirement[]
```

MinerU 只负责文档结构恢复，LLM 只负责候选内容的要求归纳；程序不接受模型自造的来源，未知 `block_id` 或不符合 Schema 的结果会使 requirements 阶段失败。投标文件解析、真实合规审查、评分和否决投标判断仍保持模拟或未实现。
