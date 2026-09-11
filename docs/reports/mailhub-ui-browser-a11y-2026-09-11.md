# MailHub UI 浏览器无障碍审计（WCAG 2.2 AA 渲染半程）

- 日期：2026-09-11
- 范围：`@fyjtech/mailhub-ui`（`packages/mailhub/ui`）的 `MailHubWorkspace` / `MailHubStandalone`
- 运行器：Vitest 5.0.0 browser mode（`@vitest/browser-playwright` 5.0.0）+ Playwright 1.63.0
- 浏览器：Chrome for Testing 153.0.8010.12（playwright chromium v1243），headless
- 审计引擎：axe-core 4.13.0，标签集 `wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa`
- 样式来源：发布用的 `src/styles.css`（由 Vite 注入，测量的是真实上色结果，不是测试专用样式）
- 复现：`cd packages/mailhub/ui && npm run test:browser`；或经发布门禁 `python scripts/check_ui_browser_a11y.py`

## 结论

渲染半程**通过**：17 个浏览器用例全绿，覆盖 1.4.3 / 1.4.11（文本对比度）、1.4.10 回流、
1.4.4 缩放、2.5.8 目标尺寸、2.3.3 动效、CSP 基线，并附带 3 个"门禁活性"反证用例。

这**不等于** WCAG 2.2 AA 认证通过：屏幕阅读器实机朗读、非文本对比度（焦点框/边框）目视检查、
系统级高对比度模式仍未验证，见文末。

## 实测数据

### 对比度（1.4.3、1.4.11）

| 渲染视图 | axe 实测元素数 | 违规 | `incomplete`（背景无法判定） |
| --- | --- | --- | --- |
| 统一收件箱（zh-CN） | 19 | 0 | 0 |
| 候选审阅（zh-CN） | 12 | 0 | 0 |
| 连接状态（zh-CN） | 18 | 0 | 0 |
| 统一收件箱（en-US） | 19 | 0 | 0 |
| standalone 外壳 | 22 | 0 | 0 |

`incomplete` 单独统计并断言为空：axe 对无法解析背景色的元素只报 `incomplete` 而不报违规，
把它当成"干净"会让未上色的表面冒充合规。

实测元素数由门禁反读（`MAILHUB_CONTRAST_EVALUATED`）并要求每视图 > 0，
因此"0 违规"不可能来自"0 个适用元素"。

### 目标尺寸（2.5.8）

| 视图 | 控件数 | 最小控件 | 短边 |
| --- | --- | --- | --- |
| 统一收件箱 | 13 | 筛选按钮"全部" | 26.0 CSS px |
| 候选审阅 | 6 | 按钮"刷新" | 33.0 CSS px |
| 连接状态 | 5 | 按钮"刷新" | 33.0 CSS px |

阈值 24×24 CSS px，最小实测短边 26.0。该数字由 `check_ui_browser_a11y.py` 从浏览器进程的输出中
反读并再次比对，门禁不只看退出码。

### 回流与缩放（1.4.10、1.4.4）

| 视口 | 断言 | 结果 |
| --- | --- | --- |
| 320×800 | `scrollWidth - clientWidth <= 0` | 通过（0） |
| 320×800 | 收件箱 grid 由双列塌缩为单列 | 通过（1 条轨道） |
| 640×900（1280 的 200%） | `scrollWidth - clientWidth <= 0` | 通过（0） |
| 1280×900 | 收件箱恢复双列 | 通过（2 条轨道） |

单列/双列断言用于证明"响应式路径确实生效"，避免"内容本来就很窄"这种假通过。

### 动效（2.3.3）

- 默认媒体下，整个组件树的 `transition-duration` / `animation-duration` 计算值全为 0：
  当前 UI 本身不含交互动效，2.3.3 属**空真**成立。
- 已发布的 `styles.css` 含 `@media (prefers-reduced-motion: reduce)` 保护块，作用域覆盖
  `.mailhub-workspace *` 与 `.mailhub-standalone *`，并把
  `transition-duration` / `animation-duration` 压到 `0.01ms !important`。该断言直接遍历
  浏览器 CSSOM 的 `CSSMediaRule`，不是对源文件做正则。
- 行为验证：通过 CDP `Emulation.setEmulatedMedia` 把 `prefers-reduced-motion` 切到 `reduce`
  （先断言 `matchMedia` 从 `false` 变 `true`，证明模拟真的生效），再对一个元素写入
  `transition-duration: 3s` 内联声明，计算值被保护块的 `!important` 压到 `1e-05s`。
  这证明保护块**实际生效**，而不只是被解析。

### CSP 基线

| 断言 | 结果 |
| --- | --- |
| 未传 `theme` 时，收件箱渲染树内 `[style]` 数量为 0 | 通过 |
| 未传 `theme` 时，standalone 渲染树内 `[style]` 数量为 0 | 通过 |
| 组件不注入 `<script>` | 通过 |

因此未使用 `theme` 的宿主可直接用 `style-src 'self'`（无需 `unsafe-inline`）。
传入 `theme` 时 `--mailhub-*` 会以 `style` 属性输出，此时宿主必须放行内联样式，
或改在宿主样式表里覆盖同名变量——这是**已知的、有意的**取舍，已写入 UI README。

### 门禁活性（防止规则空转）

| 反证用例 | 期望 | 结果 |
| --- | --- | --- |
| 渲染 `#f0f0f0` on `#ffffff` 文本 | axe 必须报 `color-contrast` | 通过 |
| 渲染两个紧邻 12×12 按钮 | axe 必须报 `target-size` | 通过 |
| 移除 `<html lang>` 后做**文档级**审计 | axe 必须报 `html-has-lang` | 通过 |

第三条同时暴露了一个真实陷阱：`html-has-lang` 等页面级规则在**容器级** `axe.run(container)`
下不会被评估。审计因此拆成组件级与文档级两个入口，避免"以为测了其实没测"。

## 实现要点

- `ui/src/a11y.browser.test.tsx`：17 个浏览器用例。
- `ui/src/a11y.fixtures.tsx`：jsdom 与浏览器两套门禁共用同一份 DOM 夹具，
  否则两半可能对着不同的树各自通过。
- `ui/vitest.browser.config.ts`：独立配置，`viewport` 固定 1280×900，
  jsdom 配置显式排除 `*.browser.test.tsx`，两个门禁不会互相重复执行。
- `scripts/check_ui_browser_a11y.py`：发布门禁，除退出码外还要求
  ≥15 个用例通过、三个视图都有目标尺寸实测、≥5 个视图有对比度实测且元素数 > 0；
  npm / node_modules / Playwright 浏览器缺失一律**失败**，不静默跳过。

## 仍未覆盖（不得据此声称合规）

| 未覆盖 | 原因 | 建议 |
| --- | --- | --- |
| 屏幕阅读器实机朗读 | 测试运行器无法断言语音输出 | NVDA（Windows）/ VoiceOver（macOS）人工走查 |
| 1.4.11 非文本对比度：焦点指示器、边框 | axe 只覆盖文本对比度 | 人工目视 + 焦点态逐控件走查 |
| 系统级高对比度（Windows HC / `forced-colors`） | 需真实系统设置 | 在开启 HC 的 Windows 上人工走查 |
| 200% 浏览器缩放的真实 UA 缩放行为 | 本次以 640 CSS px 视口作为等效代理 | 真实浏览器 `Ctrl+` 缩放到 200% 复核 |

## 宿主页义务（审计按"宿主已履行"前提进行）

| 义务 | 说明 |
| --- | --- |
| `<html lang>`、landmark、跳转链接 | axe 的 `html-has-lang`、`bypass` 属宿主文档，不属组件 |
| 页面底色 | 组件不绘制页面背景；本次对比度按宿主白底测量 |
| 严格 CSP | 见上：不传 `theme` 可用 `style-src 'self'`；传 `theme` 需放行内联样式 |
