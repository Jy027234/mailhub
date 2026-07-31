# MailHub 独立归档

本目录保存从 CAPlatform 主项目中移出的 MailHub 产品模块，供后续独立开发、审计和重新集成使用。

## 来源

- 原仓库：`Jy027234/CAPLATFORM`
- 来源提交：`8f2bbc2514594d1b2685e194883211c5a7963d15`
- 归档日期：`2026-07-31`
- 归档状态：源码快照；不包含运行时数据库、邮箱凭据、OAuth token 或用户邮件数据

## 目录

- `packages/mailhub/`：MailHub 独立 Python 包、API、SDK、UI、迁移、测试和部署制品。
- `apps/`：历史 CAPlatform MailHub BFF/Web 专用适配器与测试。
- `deployment/`：历史 AgentCTL MailHub handler 与测试。
- `docs/`：MailHub ADR、用户/开发/运维文档、兼容性说明和历史待办。
- `scripts/`：历史 MailHub 验证入口。
- `integration-reference/repository-snapshots/`：来源提交中的宿主共享文件快照，仅供以后重建集成差异时参考，不应直接覆盖当前 CAPlatform 文件。
- `source-archives/`：从来源提交直接导出的原始 ZIP，作为文件级恢复副本保留。

## 边界

`SKILLS/email-manager` 不属于 MailHub 产品模块，未从 CAPlatform 移入本归档。它是 CAPlatform 需要保留的独立 IMAP/SMTP SKILL。

后续重新接入 CAPlatform 时，应通过标准 MCP/AgentCTL 产品能力、Secret Broker、发送前审批、幂等键和执行证据完成，不应直接复制历史宿主共享文件。

## 独立验证入口

在 `packages/mailhub` 中安装开发依赖并运行其验证脚本：

```powershell
python -m pip install -e ".[dev]"
pwsh -NoProfile -File scripts/verify.ps1
```
