# fc 工作台设计系统

这份文件是给改这个前端的 agent 读的。生成或修改 `web/src` 下的界面时,先遵守这里,再发明新规则。它是 `docs/008-workbench.md` 的执行层;`scripts/design-check.mjs` 在 CI 里检查其中可机检的部分,违反算缺陷,不是风格偏好。

## 产品性格

安静、密集、可操作。界面是长时间开着看的工作面,不是营销页:没有大留白、没有渐变装饰、没有整面品牌色。中心对象是工单与 Cairn 版本,对话、终端、文件、事件流都是看一个对象的面板。

## 最高优先级约束:字号

界面文字只用六档 `text-ui-xl`、`text-ui-lg`、`text-ui-base`、`text-ui-caption`、`text-ui-sm`、`text-ui-xs`。不用 Tailwind 自带的 `text-sm` / `text-base` / `text-lg` 等,不用 `text-[13px]` 这类任意值,不写内联 `font-size`。唯一例外是代码、diff、终端的正文,用 `text-code`;它们周围的标签、表头、元数据仍用 `text-ui-*`。界面缩放只改 `--ui-font-size`,不改 `html` 的字号。

## 颜色

只用 `src/theme.css` 里的语义 token(经 Tailwind 的类名,如 `bg-panel`、`text-fg-subtle`、`border-border`):

| 用途 | token |
| --- | --- |
| 页面、面板、侧栏、卡片、弹层、输入框 | `bg`、`bg-alt`、`panel`、`sidebar`、`card`、`card-selected`、`popover`、`input` |
| 边框 | `border`、`border-hover` |
| 文字三级 + 反色 | `fg`、`fg-subtle`、`fg-subtlest`、`fg-inverse` |
| 强调 | `brand`(链接、选中指示、主按钮;从不做大面积背景)、`accent`(弱强调面) |
| 状态 | `success`、`warning`、`destructive`、`waiting`,各带 `-surface` 作底 |
| diff | `diff-add`、`diff-del` |

状态色只用于真实状态。"等人"统一用 `waiting`,不借 `success` 或 `brand`。不写颜色字面量(`#xxx`、`rgb(`)到组件里;新颜色先进 `theme.css`。亮暗两套都要能看。

## 固定的词与色

| 事物 | 词 | 色 |
| --- | --- | --- |
| 工单阶段 | 输入未送达、等待回答、等待审阅、受阻、失败、排队、工作中、等待 Trigger、已结束 | 等人四种 `waiting`;失败 `destructive`;受阻 `warning`;工作中 `success`;其余中性 |
| Report | progress、waiting、ready、blocked、failed、dropped | ready `success`;blocked `warning`;failed `destructive`;其余中性 |
| Decision | accept、stop、ignore、unavailable | accept `success`;unavailable `warning`;其余中性 |
| 沙箱状态 | creating、running、paused、unreachable、failed、deleted | running `success`;paused `warning`;unreachable / failed `destructive` |
| 版本状态 | candidate、stable、retired | stable `success`;其余中性 |

## 布局与行为

- 三栏:左栏导航 264 到 420 px,低于 1024 px 自动收起成 56 px,低于 768 px 变抽屉;中栏至少 400 px;右栏停靠面板首次 45%,记住宽度,上限 70%,空间不够时先让步。
- 列表用水位线:默认显示前 5 条加正在运行、等人、有未读的;"展开其余"每次加 5。
- 健康状态不占地方;问题必须可操作;一次性网络错误不算问题。
- 数字只在改变人要做的事时出现;计数等于它链接的列表长度。
- 破坏性操作要确认;每个设置块一条保存栏,只在有改动时出现。
- 流式输出跟随尾部直到用户上滚;进行中的记录只显示开始标记,不虚构耗时。
- 内部标识(id、哈希)折叠在详情里,用短名与标题。
- 键盘可达:所有操作有可聚焦的控件;列表支持上下键。

## 代码组织

`src/api` 只有 HTTP 与 SSE,不含界面;`src/store` 是 Zustand 状态;`src/ui` 是无业务的基础组件(按钮、徽章、表格、面板、抽屉);`src/features/<页>` 是页面与它的组件;`src/layout` 是三栏外壳。页面不直接 fetch,经 store。类型在 `src/api/types.ts`,与 `CONTRACT.md` 对齐。

## 2026-11-01 起的外观:照 ZCode 的 Zai Light / Zai Dark

- 表面:`bg`(页面)、`sidebar`、`panel`(卡片与输入框外壳)、`surface`(弱强调块、工具输出)、`hover`、`selected`、`bubble`(用户消息气泡);边框是前景色的 10% 透明(`border`),悬停 15%。
- 品牌色是黑(亮)/ 白(暗),只用于主按钮与应用标记;链接用 `link`;状态色只用于状态,`*-surface` 是它们的透明底。
- 字号 14px 基准,`text-ui-*` 刻度;圆角:控件 `rounded-lg`,卡片 `rounded-xl`,输入框外壳 `rounded-2xl`,阴影只有 `shadow-composer`。
- 布局:侧栏 272px(收起 52px):应用标记、"新任务"、任务列表(`sidebar` slot)、底部"管理"分组;中栏内容宽度不超过 `max-w-3xl` 居中;输入框是底部停靠的卡片,右下角圆形发送键。
- 对话里 agent 的话是正文,不是气泡;用户的话是右对齐气泡;一件事的开始是一条带横线的标记;命令是 `$ …` 等宽行,输出在 `surface` 块里折叠。
- 对话首页的空状态文案固定为『这是你的 agent。告诉它一件事。』,改文案要同步改 `web/src/plugins/session/index.tsx`。
