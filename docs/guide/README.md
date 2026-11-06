# 使用指南

[English](../en/guide/README.md) · 中文

给**用**沙箱云的人看的:租户、接入的 agent、写 SDK 调用的人。设计与理由在上一层的编号文档(从 [docs/README](../README.md) 读起),接口合同在 [CONTRACT.md](../../CONTRACT.md),这里是按任务和按端点整理过的版本。

| 读什么 | 给谁 |
| --- | --- |
| [租户手册](manual.md) | 用工作台控制台管项目、成员、沙箱、出口、卷、账单、审计的人 |
| [API 参考](api/README.md) | 直接调 HTTP 的人:按八个上下文分,每个端点一节带例子 |
| [Python SDK](sdk.md) | 写代码接入的人;和 E2B SDK 的对应表 |
| [JS/TS SDK](sdk-js.md) | 用 Node / Deno / Bun / 浏览器接入的人;和 Python SDK 的差别 |
| [上生产](production.md) | 把它部署给外部用户的人:反向代理、域名、环境变量、上线清单 |
| [MCP](mcp.md) | 让 Claude Code / Codex 这类 agent 把沙箱当工具 |
| 教程:[用 Claude Code 接入](tutorials/claude-code-mcp.md) · [从 E2B 迁移](tutorials/migrate-from-e2b.md) · [自己的镜像做模板](tutorials/custom-template.md) · [预算、工单与报告](tutorials/budget-and-ticket.md) | 第一次上手的人 |
| [版本与兼容承诺](compatibility.md) · [变更记录](../../CHANGELOG.md) | 要升级、要依赖接口稳定的人 |
| [运维手册](operations.md):故障手册、指标与告警、备份还原演练、容量规划 | 值班与运维的人 |
| [部署](../../deploy/README.md) · [数字](../research/benchmark-2026-11.md) · [威胁模型](../013-threat-model.md) | 运维与审查的人 |

写法的规矩和编号文档一样(docs/README 末尾):每个端点、字段、按钮都以 CONTRACT 与代码为准,这里不先于它们。
