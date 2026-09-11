# `@fyjtech/mailhub-ui`

这是 MailHub 的可嵌入 React 工作台。它不绑定宿主路由、身份实现、主题、Provider SDK 或
`fetch` URL；宿主通过 `MailHubUiClient` 注入已经完成身份/租户绑定的 API client，通过 CSS 变量或
自己的 class 注入主题。

```tsx
import { MailHubWorkspace } from "@fyjtech/mailhub-ui";
import "@fyjtech/mailhub-ui/styles.css";

<MailHubWorkspace client={mailHubClient} identityLabel="当前工作空间" />
```

不希望复制页面外壳时，可使用同一包提供的 standalone shell。它仍由宿主挂载，
不会自行创建 router、session 或 `fetch` 客户端：

```tsx
import { MailHubStandalone } from "@fyjtech/mailhub-ui/standalone";

<MailHubStandalone
  client={mailHubClient}
  identityLabel="当前工作空间"
  theme={{ "--mailhub-accent": "#0b6bcb" }}
  headerContent={<a href="/">返回工作台</a>}
/>
```

## 文案与本地化（i18n）

组件不含硬编码文案：所有用户可见文本来自可注入的 locale bundle。内置 `zh-CN`（默认）与 `en-US`；
`locale` 同时决定日期/数字格式，`messages` 支持部分覆盖：

```tsx
// 内置语言
<MailHubWorkspace client={client} locale="en-US" />

// 部分覆盖：其余仍沿用所选语言
<MailHubWorkspace client={client} locale="en-US" messages={{ refresh: "Reload" }} />

// 完整自定义语言包（bundle 自带 locale，决定日期格式）
<MailHubWorkspace client={client} messages={myFrBundle} />
```

`MailHubStandalone` 接受同样的 `locale`/`messages`，并本地化自己的外壳文案。

契约要点：

- 语言包类型为 `MailHubMessages`（53 键），值是纯字符串 + `{placeholder}` 占位符，
  可直接做成 JSON 语言包，不引入 i18n 运行时依赖；
- TypeScript 保证**键完整**：漏一个键是编译错误，不会静默回落成中文；
- `npm run build` 额外强制三道门禁：组件内不得残留硬编码文案（CJK 扫描）、
  声明的占位符（如 `{count}`）不得丢失、两个内置语言包键集必须一致；
- 已知限制：`{count}` 是简单插值，**没有复数规则**。需要 "1 message / 3 messages" 这类区别时，
  由宿主在 `messages` 覆盖对应键或提供完整语言包，运行时不会自动选复数形式。

日期/数字通过 `locale` 调 `toLocaleString`，无额外依赖。

## 可访问性（WCAG 2.2 AA）

可访问性门禁分两半，缺一不可：

```bash
npm test              # 语义半程：jsdom + axe-core + Testing Library（10 用例）
npm run test:browser  # 渲染半程：Playwright Chromium（15 用例）
```

### 语义半程：`npm test`（jsdom，无布局引擎）

| 已覆盖 | 相关准则 |
| --- | --- |
| 真 ARIA tab 组件：`tablist`/`tab`/`tabpanel` 接线、`aria-labelledby`、roving tabindex | 4.1.2、1.3.1 |
| 键盘可操作：方向键 / Home / End 切换视图，无键盘陷阱 | 2.1.1、2.1.2 |
| 可访问名称：搜索框、筛选、按钮、卡片标题 | 4.1.2、2.4.6 |
| 状态通知：错误 `role="alert"`、空态 `role="status"`、`aria-busy` | 4.1.3 |
| 标题层级：`headingLevel` 可注入；standalone 内只有一个 h1 | 1.3.1、2.4.6 |
| 筛选语义：`aria-pressed` 切换按钮，不再冒充 tab | 4.1.2 |
| 门禁活性自证：故意渲染违规元素并要求 axe 报出 | —（防止门禁空转） |

### 渲染半程：`npm run test:browser`（真实 Chromium）

jsdom 没有布局引擎，对比度、目标尺寸、回流与动效只能在真实浏览器里证明。该套件加载**实际发布的
`src/styles.css`**，每条准则都配一个能真失败的断言：

| 准则 | 断言 | 证据形式 |
| --- | --- | --- |
| 1.4.3 / 1.4.11 文本对比度 | axe `color-contrast`（三视图 + 英文包 + standalone）零违规，且零 `incomplete` | axe 对真实像素取值 |
| 1.4.10 回流 | 320 CSS px 视口下 `scrollWidth - clientWidth <= 0`，且收件箱由双列塌缩为单列 | 真实视口 + 计算样式 |
| 1.4.4 缩放 | 640 CSS px 视口（1280 的 200%）无横向滚动 | 真实视口 |
| 2.5.8 目标尺寸 | 三视图内全部控件 `min(width, height) >= 24`，并打印实测最小值 | 布局像素 |
| 2.3.3 动效 | 默认媒体下整棵树 transition/animation 时长全为 0；CDP 模拟 `prefers-reduced-motion: reduce` 后，人为写入的 `transition-duration: 3s` 被样式表的 `!important` 压制为 `0.00001s` | 计算样式 |
| CSP 基线 | 未传 `theme` 时渲染树内无任何 `style` 属性，且组件不注入 `<script>` | DOM 检查 |
| 3.1.1 页面语言 | 移除 `<html lang>` 后 axe 必须报出 `html-has-lang` | 门禁活性自证 |

活性自证：对比度与目标尺寸各有一个**故意失败**的探针用例。若 axe 规则被关掉、或标签集变更导致
某条规则不再运行，这两个探针会先失败，而不是让套件"全绿"通过。

**仍未覆盖（必须人工）**：

| 未覆盖 | 原因 |
| --- | --- |
| 屏幕阅读器实机朗读 | 需 NVDA / VoiceOver 人工走查；测试运行器无法断言语音输出 |
| 1.4.11 非文本对比度（焦点框、边框） | axe 只覆盖文本对比度；焦点指示器的几何与配色仍需人工目视 |
| 系统级高对比度（Windows HC / `forced-colors`） | 需真实系统设置，无法在无头浏览器中复现 |

因此：**"两半门禁通过"不等于 WCAG 2.2 AA 认证通过**。它证明的是"语义 + 键盘 + 渲染 + 动效"，
不含屏幕阅读器实机与系统级高对比度。

**宿主页义务**（组件不负责；审计按"宿主已履行"的前提进行）：

| 义务 | 说明 |
| --- | --- |
| `<html lang>`、landmark、跳转链接 | axe 的 `html-has-lang`、`bypass` 属于宿主文档而非组件 |
| 页面底色 | 组件自身不绘制页面背景；对比度按宿主白底测量 |
| 严格 CSP | 未传 `theme` 时组件不产生内联样式，可直接用 `style-src 'self'`；一旦传 `theme`，`--mailhub-*` 会以 `style` 属性输出，此时宿主必须放行内联样式，或改在宿主样式表里覆盖同名变量 |

安全边界：列表/线程默认只渲染 metadata；正文由 client 的 `getMessageContent` 按需返回纯文本；
统一收件箱 API 使用 scope-bound cursor，支持未读/重要/附件/项目/候选和账号筛选；组件提供对应筛选按钮、
搜索回退和加载更多行为；账号标识应由
`account_email`/`provider` 展示，不能只显示内部 connection id。
候选审核、草稿保存、发送都必须由宿主 client 实现 revision/digest/idempotency/approval 合同。组件
不会保存 token、credential_ref 或原始 HTML，也不会自行扩大 OAuth scope。宿主的 `/mail` 页面
使用同一合同和自己的页面主题；它不是该包的 fork。

发布前宿主必须执行自己的 TypeScript、WCAG 2.2 AA、API contract、CSP 和安全扫描门禁；该目录是
可复用源包，不将未验证的 Gmail/Graph fixture 当作 Provider 验收证据。

本包安装依赖后可用 `npm run typecheck` 与 `npm run build` 验证严格 TypeScript 合同；构建产物输出到
`dist/`，发布流水线仍需补充包完整性、WCAG、CSP 和宿主 API contract 门禁。

该包与 MailHub 核心使用 CAPlatform 专有许可（`UNLICENSED`）；不得把包名中的
`UNLICENSED` 解释为可公开再分发。发布前必须附仓库许可、NOTICE、SBOM、来源台账和批准的
宿主主题/安全审计证据。
