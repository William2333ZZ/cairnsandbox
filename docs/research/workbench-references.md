# 工作台参考:DSH、dsh-superagent、ZCode、Codex

日期:2026-11-01。为 `decisions.md` D12(工作台自己做)做的调研;结论进 `008-workbench.md`。

## 1. 看了什么

| 产品 | 形态 | 技术 | 读的材料 |
|---|---|---|---|
| DeepSeek Harness(DSH)Web | 本机 `dsh web`,三栏:左侧 Workspace / Session 树,中栏会话,右栏停靠面板(文件、终端、文档预览、计划、子 agent、Trajectory);一切皆插件(Cordis) | TypeScript、React 18、Vite;客户端按包拆成 40 多个 `ui-*` 插件,宿主注入 `window.__DSH_BOOT__` 的入口图 | `packages/client/ui-*/README.zh.md`、`docs/subsystems/client-modules.zh.md`、`docs/user/guide` |
| dsh-superagent 面板 | DSH 侧栏里的一页:Overview(待处理)、Source、Trigger、Eval、Execution、Settings | 同 DSH 插件 | `docs/005-panel.md`(见 `dsh-superagent-notes.md`) |
| ZCode(智谱) | 桌面(Electron)+ Web + TUI 三端同一套 UI;工作区侧栏、分组任务、会话、Git 面板、终端、计划 | TypeScript、React、Zustand、Tailwind;`packages/{ui,web,server,services,shared,rpc}`;`DESIGN.md` 是给 agent 读的设计系统;`architecture-policy.yaml` 强制模块依赖 | `DESIGN.md`、`architecture-policy.yaml`、`docs/task/*.md`、`packages/ui/src` 目录 |
| OpenAI Codex(云端) | 任务列表 + 每任务一页(diff、日志、PR 按钮),并行任务,环境配置页 | 闭源 | 公开界面 |

## 2. 值得照搬的

**信息架构(dsh-superagent)**:打开先回答"有什么需要我",其次"系统健不健康",最后"一件事的来龙去脉";每个概念只在一处;分类互斥穷尽;健康状态不占地方。我们的 008 已经采用。

**布局(DSH)**:三栏,右栏是贴边的停靠面板,先让步保护中栏;侧栏 264 到 420 px,低于 1024 px 自动收起,低于 768 px 变抽屉;全局面板占 `main` 槽位,会话是其中一个。右栏用标签分裂树(dockkit),操作可逆可重放。对我们:中栏放"一个对象的详情"(工单、沙箱、批次),右栏放"看着它干"的面板(终端、文件、事件流、轨迹、diff)。

**设计系统给 agent 读(ZCode)**:`DESIGN.md` 第一句就是"这份文件是给编码 agent 读的";统一的 `text-ui-*` 字号刻度是强制约束,违反算缺陷;语义色只用于语义状态;产品性格"calm, dense, operational"。这对我们尤其对:工作台会由 agent 在沙箱里持续改,设计规则必须是它能读、能检查的文本。

**架构策略可执行(ZCode)**:`architecture-policy.yaml` 声明模块、层(domain / app / adapters)、允许的依赖,CI 检查;`dependency-graph.mmd` 自动生成。我们的 011 代码索引测试是同一思路的雏形。

**会话列表的水位线(DSH ui-workspace)**:列表只显示前 N 条加"正在运行、等人、有未读"的那些,其余折叠;置顶、最近更新 / 手动排序。工单列表照这个做。

**轨迹视图(DSH ui-trajectory)**:按轮次分组的事件表 + 时间概览;只渲染可见行;流式时跟随尾部直到用户上滚;进行中的记录只显示开始标记,不虚构耗时。我们的 Run 与 Assignment 事件流共用这一个视图。

**子 agent 目录(DSH ui-subagent)**:页头的谱系面包屑 + 后代数量;展开行才加载嵌套。我们的 fork 探测与派工树照这个呈现。

**需求记录的写法(ZCode docs/task)**:每个功能一篇:需求与确认记录(编号)、交互与状态(文本流程图)、执行边界、验收。我们 008 之后每个工作台功能按这个格式写在 `docs/workbench/`。

## 3. 不照搬的

- 插件化到 40 多个包、宿主注入入口图(DSH):我们一个前端包就够,先不做插件系统。
- Electron 桌面端与 SSH/WSL 远程(ZCode):桌面端在 D2 里,远程不是我们的模型——执行永远在 microVM。
- 本机工作区目录模型(两者):我们的"工作区"是沙箱里的可写层,不是本机目录;文件面板读的是 `GET /sandboxes/<id>/files`。
- 对话为中心(两者的中栏是聊天):我们的中心对象是工单与版本;对话只是工单详情里的一个面板。

## 4. 对技术选型的结论

React 18 + TypeScript + Vite + Zustand,Tailwind 只用它的工具类生成,主题走 CSS 变量;构建在 Mac 上做(服务器出不去),`dist/` 作为静态文件进仓库由控制面在 `/ui/` 提供;不引入 SSR、不引入组件库,基础组件自己写并写进 `DESIGN.md`。原因:三家都是 React + TS,agent 改起来材料最多;Vite 产物是静态文件,部署形态不变;Zustand 够小。
