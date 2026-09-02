# Company Video Workbench

公司内部产品视频工作的独立 Codex 插件。它只读访问公司 NAS 产品资料和共享素材，使用本机 SQLite 保存任务、审批、成本与恢复记录，使用本机工作目录保存临时运行数据，并通过官方 ChatCut 插件执行可编辑剪辑、生成、质检和导出。

它不要求部署额外任务平台，也不会把真实产品资料、媒体或任务数据库打包进插件和 GitHub。

## 架构

```text
部门成员
   │
   ▼
Codex + Company FlowCut Producer
   ├── NAS 产品资料（只读事实源）
   ├── NAS 共享素材（只读检索，授权另行确认）
   ├── 本机 SQLite（任务、事件、审批、成本、外部引用）
   ├── 本机工作目录（缓存、临时文件和日志）
   ├── ChatCut（项目、时间线、字幕、生成与导出）
   └── 独立成片目录（审核后写入）
```

需求提出和剪辑执行可以由同一批人完成，无需拆成两个门户；数据、任务、编辑和交付仍保持职责分层，以便恢复和审计。

## 当前能力

- 从可配置 NAS 根目录精确读取产品 Markdown、JSON 或 YAML，并记录修改时间和 SHA-256。
- 检索可配置共享素材目录，但不根据文件名推断授权。
- 预检并拒绝位于 NAS、映射网络盘或 NAS 源目录内的 SQLite 与运行工作目录。
- 用 SQLite 创建、查询和迁移任务，保存审批、成本、事件与 ChatCut 引用。
- 编排已有素材剪辑、混合制作、A/B/C 版本、自动质检和中断恢复。
- 固化付费生成、破坏性修改和最终导出的独立确认闸门。
- 提供两天真实测试流程脱敏形成的 8 条回归用例。

## 快速开始

```powershell
git clone https://github.com/zmq18560817115-eng/shipinskill.git
cd shipinskill
codex plugin marketplace add .
codex plugin add company-flowcut-producer@company-video-workbench
python plugins\company-flowcut-producer\scripts\task_store.py init
python plugins\company-flowcut-producer\scripts\company_context.py preflight
```

官方 ChatCut 插件需要由每位成员单独安装并登录。新安装或更新的技能请在新的 Codex 任务中使用。

## 仓库结构

```text
shipinskill/
├── .agents/plugins/marketplace.json
├── plugins/company-flowcut-producer/
│   ├── .codex-plugin/plugin.json
│   ├── skills/company-video-producer/
│   ├── department-config/
│   ├── scripts/
│   ├── templates/
│   ├── tests/
│   └── docs/
└── AGENTS.md
```

## 文档

- [系统架构](plugins/company-flowcut-producer/docs/system-architecture.md)
- [数据源与存储契约](plugins/company-flowcut-producer/docs/data-source-contract.md)
- [本地安装与验证](plugins/company-flowcut-producer/docs/local-install.md)
- [测试计划](plugins/company-flowcut-producer/docs/test-plan.md)
- [验证报告](plugins/company-flowcut-producer/docs/validation-report.md)
- [GitHub 与部门复用](plugins/company-flowcut-producer/docs/github-handoff.md)
