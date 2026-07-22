# Obsidian 坚果云 WebDAV 集成

该集成用于 GitHub Actions 在线运行：分析前从坚果云读取最新持仓，分析后把两类 Markdown 报告写回坚果云，由桌面客户端同步到本地 Obsidian。

## GitHub 配置

在仓库 `Settings → Secrets and variables → Actions` 中配置。

Secrets：

- `NUTSTORE_WEBDAV_USERNAME`：坚果云账号。
- `NUTSTORE_WEBDAV_PASSWORD`：坚果云第三方应用密码，不是登录密码。

Variables：

- `OBSIDIAN_WEBDAV_ENABLED=true`
- `NUTSTORE_WEBDAV_BASE_URL=https://dav.jianguoyun.com/dav/`
- `NUTSTORE_VAULT_PATH=Obsidian_note_2026`
- `NUTSTORE_PORTFOLIO_PATH=02_DailyNotes/投资笔记`
- `NUTSTORE_STOCK_MAP_PATH=30_Research_Input/B_Business_FinTech/投资风向日报/投资_股票名单.md`
- `NUTSTORE_STOCK_REPORTS_PATH=02_DailyNotes/投资笔记`
- `NUTSTORE_MARKET_REPORTS_PATH=30_Research_Input/B_Business_FinTech/投资风向日报`

WebDAV 集成关闭时，工作流继续使用原有 `STOCK_LIST` 配置，且不上传到坚果云。

## 运行行为

- 定时任务仍在工作日北京时间 18:00 运行。
- `full` 模式要求股票报告与大盘复盘都存在。
- `stocks-only` 和 `market-only` 只上传对应报告。
- WebDAV 认证、持仓读取、名称解析或报告上传失败时，任务失败；GitHub Artifact 仍保留 30 天。
- 同一天重跑会覆盖同名报告。

第一次联调时，手动运行工作流并勾选 `webdav_smoke_test`。测试会在仓库根目录对应的坚果云文件夹中创建临时文件，读取校验后立即删除。
