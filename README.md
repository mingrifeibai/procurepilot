# ProcurePilot · 企业智能采购与供应商协同平台

ProcurePilot 是一个面向团队设备采购的演示系统，覆盖需求版本、报价比较、约束求解、人工审批、采购单、分批验收和异常处理。平台保存真实的流程状态与审计记录，产品、供应商报价和外部履约使用模拟数据。

## 项目亮点

- **约束求解与增量重规划**。OR-Tools 按预算、库存、交期、数量和兼容关系生成方案。需求变更时优先保留可复用的分配，必要时全量重算。
- **按需多 Agent 分析**。支持单 Agent、固定角色和动态委派。独立角色可并行核对目录、报价与履约条件，按需调用只读 MCP 工具。模型分析和采购执行由不同接口处理，执行需要人工审批。
- **证据可回读**。从 PDF、表格和文本提取内容，检索结果携带原文偏移。默认使用本地词法检索，可选 OpenSearch 混合检索、BGE 重排和 Neo4j 兼容关系。
- **持久化执行**。分析任务使用数据库租约、步骤检查点和用量账本，支持取消与受控恢复。页面通过 SSE 更新进度，断线时重连或回退轮询。采购单有幂等保护。

## 本地运行

Windows 需要 Python 3.10 及以上版本（推荐 3.12）、Node.js 24 和 npm。在克隆后的项目目录执行：

```powershell
.\dev.ps1 -Task setup
.\dev.ps1 -Task run
```

打开 [采购工作台](http://127.0.0.1:8091/) 或 [API 文档](http://127.0.0.1:8091/docs)。默认使用 SQLite 和内置演示数据，可以离线跑完整的采购业务流程。也可以使用 Docker 启动应用和 PostgreSQL：

```sh
docker compose up --build -d
```

演示时先用采购员创建工作区并保存需求，生成方案后切换审批员审批，再切回采购员创建采购单。采购与验收页面支持登记分批到货、处理差异并导出订单。

## 启用模型分析

将命令中的 Base URL 和模型名换成自己的 Chat Completions 兼容服务。PowerShell 会隐藏读取 API Key。

```powershell
.\Start-ProcurePilot-Model.ps1 -BaseUrl 'https://your-provider.example/v1' -ModelName 'your-model'
```

打开 [模型工作台](http://127.0.0.1:8092/)，在「执行与审计」中提交后台模型分析。模型需要支持 JSON 对象响应和用量字段。部署配置见 [环境变量示例](.env.example)，独立 Worker 可通过 `python -m procurepilot.worker` 启动。

## 测试与评测

```powershell
.\dev.ps1 -Task test
.\.venv\Scripts\python.exe -m procurepilot.evaluate
.\.venv\Scripts\python.exe -m procurepilot.evaluate_replanning
.\.venv\Scripts\python.exe -m procurepilot.evaluate_document_evidence
```

评测使用 [固定合成数据](evals)，涵盖预算与库存约束、拆单、兼容关系、需求变更和附件证据。真实模型对照脚本放在 [scripts](scripts)，需要配置模型服务，执行时会消耗 Token。

## 代码入口

| 模块 | 职责 |
| --- | --- |
| [frontend/src](frontend/src) | React、TypeScript 采购工作台 |
| [api.py](procurepilot/api.py) · [service.py](procurepilot/service.py) | FastAPI、权限、业务流程与审计 |
| [planner.py](procurepilot/planner.py) · [replanning.py](procurepilot/replanning.py) | OR-Tools 求解与增量重规划 |
| [runtime.py](procurepilot/runtime.py) · [analysis.py](procurepilot/analysis.py) | LangGraph 离线流程与模型分析 |
| [jobs.py](procurepilot/jobs.py) · [worker.py](procurepilot/worker.py) | 后台任务、租约与故障恢复 |
| [retrieval.py](procurepilot/retrieval.py) · [mcp_server.py](procurepilot/mcp_server.py) | 原文检索与 MCP 领域工具 |
