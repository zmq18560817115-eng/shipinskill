# GitHub 与部门复用

代码仓库：<https://github.com/zmq18560817115-eng/shipinskill>

## 仓库内容边界

仓库当前是公开仓库，因此只能保存插件代码、规则、模板和脱敏测试夹具。产品文档、共享素材、客户数据、SQLite、任务快照、日志、Cookie 和密钥都不能上传。如需在文档中包含内部目录结构或真实业务样本，应先把仓库改为私有并完成公司权限审批。

## Workspace 导入

仓库根保留 `.agents/plugins/marketplace.json`，其中本地来源指向 `./plugins/company-flowcut-producer`。管理员可在 Workspace 的插件管理中使用仓库 URL 导入市场，路径留空，选择 `main` 或固定发布标签。

GitHub 导入不会自动授予 ChatCut 权限；每位成员仍需安装/启用官方 ChatCut 并完成自己的登录。详见 [官方插件管理文档](https://learn.chatgpt.com/zh-Hans/docs/enterprise/plugin-management)。

## 发布流程

1. 修改技能、配置或脚本时同步更新测试和版本。
2. 运行插件/技能校验器、离线测试、NAS 只读预检和 ChatCut 只读冒烟。
3. 审核仓库中无内部数据和运行产物。
4. 通过分支和 Pull Request 合并，创建版本标签。
5. Workspace 管理员同步市场，试用成员在新任务中验证。

建议为 `main` 启用分支保护和至少一名审核人。目前初版 SQLite 采用单任务单写者；需要共享队列时新增专属轻量存储服务，而不是把 SQLite 文件放到 NAS。
