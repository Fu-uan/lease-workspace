# WorkBuddy 更新发布说明

在原轻应用更新当前仓库 main 分支，保留现有数据和部署私有配置，不重写业务代码。

- 安装 requirements.txt，入口为 python backend.py；监听 0.0.0.0，端口读取 PORT。
- 包含根目录运行模块和 index.html、ui-workflow.js。library_client.py 是必需依赖。
- config.json 不在仓库内，沿用原部署配置；首次配置参考 config.example.json，将占位值替换为实际表 ID。
- 设置部署专用 ZL_SESSION_KEY；凭证通过部署环境提供，不提交 Git。
- 不使用 run_local_demo.py 公开发布；它会创建固定密码的本地演示账号。
- 不上传 tests/、docs/history/、本地数据库和私有审计材料作为运行资源。
- 发布后记录实际提交号、启动结果、URL，并检查登录、静态脚本、合同草稿保存与审批。
- AI 调用由后端发起，云端必须能访问配置的模型网关；当前内网连接问题未解决，不能仅凭按钮存在就报告 AI 验收通过。

本说明是部署要求，不代表发布已执行或全部业务已验收。
