# 租赁线上化 Demo

面向财务人员的合同录入、识别复核、租赁台账及审批演示应用。Python HTTP 服务提供 API 和静态页面，可选择 WorkBuddy 团队空间或本地 SQLite 存储。

**本项目仍是 Demo，并非完整财务系统。**已有基础台账及单级审批逻辑；OA 对接、历史 Excel 批量迁移、多级付款审批、完整凭证场景和公司正式部署尚未完成验收。AI 接口已编写，但 WorkBuddy 云端访问公司内网网关的网络问题尚未解决。自动化测试通过不代表以上全部业务已实现。

## 目录与入口

```text
backend.py             HTTP 服务入口、业务 API、台账计算
workflow.py            草稿、审批事件与付款批次流程
importer.py            文档解析、OCR 与字段提取
library_client.py      WorkBuddy 团队空间存储客户端（运行必需）
local_store.py         本地 SQLite 存储适配器
index.html             页面结构、样式与基础交互
ui-workflow.js         合同录入与审批交互扩展
run_local_demo.py      本地隔离演示启动器
config.example.json    WorkBuddy 配置模板，不含真实表 ID 或密钥
requirements.txt       文档识别依赖
requirements-dev.txt   测试依赖
pytest.ini             默认只收集离线测试
tests/                 自动化回归测试
tests/manual/          需人工指定测试服务的诊断脚本
docs/                  发布说明与交付边界
docs/history/          历史记录，不作为当前验收结论
```

运行代码保留在根目录以兼容当前 WorkBuddy 启动命令；测试与记录不参与服务启动。

## 本地快速体验（Python 3.11）

在项目根目录执行：

```sh
python -m pip install -r requirements.txt
python run_local_demo.py
```

打开 http://127.0.0.1:18700 。本地启动器使用 `.demo-data/` 保存 SQLite 和会话密钥，不需要 `config.json`，不会读取 WorkBuddy 数据。

| 演示账号 | 角色 |
|---|---|
| demo-keeper | 台账维护人 |
| demo-reviewer | 台账审批人 |
| demo-payment | 付款审批人 |

以上本地演示账号密码均为 `Demo-lease-2026!`。仅用于本机演示；不要把该启动器用于公开部署。正常工作入口为 `backend.py`。

## WorkBuddy 更新发布

1. 在原轻应用中更新源码，保留原有 `config.json`、数据库和账号。
2. 首次部署需参考 `config.example.json` 配置既有团队空间各表 ID，不能直接使用占位值。
3. 安装 `requirements.txt`，使用 `python backend.py` 启动，端口读取 `PORT`，默认监听 `0.0.0.0`。
4. 设置独立的 `ZL_SESSION_KEY`；WorkBuddy 云环境沿用平台鉴权代理，非沙箱访问需要 `ZL_TOKEN`。
5. 确认 `index.html`、`ui-workflow.js` 和全部 Python 运行模块均已包含。

详见 [发布说明](docs/DEPLOY-PROMPT.md)。不要上传本地 `.demo-data/`、测试合同或真实密钥。

## 配置

| 配置项 | 作用 |
|---|---|
| `PORT` / `ZL_HOST` | 服务端口与监听地址 |
| `ZL_SESSION_KEY` | 应用签名密钥，部署时配置 |
| `ZL_TOKEN` | 非沙箱模式的 WorkBuddy 访问凭证 |
| `ZL_STORAGE=local` | 使用本地适配器 |
| `ZL_DATA_DIR` | 本地数据库所在目录 |
| `ZL_UPLOAD_MAX_MB` | 后端单文件上限，默认 50MB |
| `ZL_PDF_MAX_PAGES` | 后端 PDF 页数上限，默认 50 页 |

上传边界可配置，页面目前显示默认值；50MB/50页并不代表长合同 OCR 性能已完成验收。

AI 配置入口在新建合同的“配置 AI 接口”。Key 当前保存在浏览器 `sessionStorage`，请求经应用后端代理；不保存到 Git 或业务表。模型地址必须能从**后端所在网络**访问，公司电脑能访问并不代表 WorkBuddy 云端能访问。现有 AI 图片输入与 OCR 文本输入仍需端到端验收，不能宣称支持任意文档一键识别。

## 运行测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

默认测试由 `tests/conftest.py` 强制使用临时本地存储，不需要真实配置与凭证。`tests/manual/performance_probe.py` 不自动执行；它是历史性能诊断脚本，须先检查目标地址和测试账号，不能作为本次线上时延已达标的证据。

## 接手与迁移

修改页面从 `index.html` / `ui-workflow.js` 开始；业务动作从 `backend.py` / `workflow.py` 开始；存储访问见两个适配器。公司部署需对接身份认证、数据库事务、文件存储、备份和内网模型网关。本地 SQLite 只验证存储可替换方向，尚未完成正式服务器迁移。

历史审核材料及本地配置不会全部提交仓库。[当前交付说明](docs/当前交付状态-2026-09-24.md)为阶段记录；仓库源码与实际验收结果优先，旧报告中的通过数量和功能描述可能已过时。
