# 本地安装与验证

## 1. 前置条件

- 已安装 Codex 桌面端和 `codex` CLI。
- 已安装并登录官方 ChatCut 插件。
- 当前 Windows 账号可读取产品 NAS。
- Python 3.11 或兼容版本可用。

## 2. 克隆和安装

```powershell
git clone https://github.com/zmq18560817115-eng/shipinskill.git
cd shipinskill
codex plugin marketplace add .
codex plugin add company-flowcut-producer@company-video-workbench
codex plugin list
```

新安装或更新后的技能需在新的 Codex 任务中加载。

## 3. 环境变量

```powershell
$env:COMPANY_VIDEO_PRODUCT_ROOTS = "<部门批准产品资料目录>"
$env:COMPANY_VIDEO_ASSET_ROOTS = "<部门共享素材目录>"
$env:COMPANY_VIDEO_OUTPUT_ROOT = "<部门成片目录>"
$env:COMPANY_VIDEO_DB_PATH = "$env:LOCALAPPDATA\CompanyVideoWorkbench\tasks.sqlite3"
$env:COMPANY_VIDEO_WORK_ROOT = "$env:LOCALAPPDATA\CompanyVideoWorkbench\work"
```

产品和素材变量只指向 NAS 共享目录，并按只读输入使用；素材和输出目录没有安全默认值，需要部门确定真实路径后配置。SQLite 和工作目录必须位于本机磁盘，不能放到 NAS/映射网络盘，也不能落在产品或素材根内。

## 4. 初始化和离线验证

```powershell
cd plugins\company-flowcut-producer
python scripts\task_store.py init
python scripts\validate_workbench.py
python -m unittest discover -s tests -v
python scripts\company_context.py preflight
python scripts\company_context.py get-product --product-id "<真实产品名>" --max-chars 3000
```

成功标准：结构和单元测试通过；产品根可读；SQLite 与工作目录的本机父目录可写且不与 NAS 源目录重叠；产品查询返回精确来源；仓库不存在被禁止的旧依赖或秘密。

创建真实任务前，先用 `get-product --summary-only` 生成不含正文的来源摘要，并把该文件传给 `task_store.py create --source-context-file`。恢复任务时先 `list/read`；如果数据库路径错误，命令会报错且不会静默创建空库。

## 5. ChatCut 只读冒烟

在新 Codex 任务中输入：

```text
使用公司视频插件做只读预检：读取指定产品资料，列出我可访问的 ChatCut 项目和活动时间线，但不要修改、生成、扣费或导出。
```

## 6. 恢复会话

Codex 会话文件丢失时，新建任务并提供 `job_id`。插件先读取 SQLite，再核对 ChatCut 项目和时间线；状态不明的生成或导出不自动重提。
