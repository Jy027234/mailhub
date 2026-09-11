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

`npm test` 是**无头语义门禁**（jsdom + axe-core + Testing Library），10 个用例覆盖：

| 已覆盖 | 相关准则 |
| --- | --- |
| 真 ARIA tab 组件：`tablist`/`tab`/`tabpanel` 接线、`aria-labelledby`、roving tabindex | 4.1.2、1.3.1 |
| 键盘可操作：方向键 / Home / End 切换视图，无键盘陷阱 | 2.1.1、2.1.2 |
| 可访问名称：搜索框、筛选、按钮、卡片标题 | 4.1.2、2.4.6 |
| 状态通知：错误 `role="alert"`、空态 `role="status"`、`aria-busy` | 4.1.3 |
| 标题层级：`headingLevel` 可注入；standalone 内只有一个 h1 | 1.3.1、2.4.6 |
| 筛选语义：`aria-pressed` 切换按钮，不再冒充 tab | 4.1.2 |
| 门禁活性自证：故意渲染违规元素并要求 axe 报出 | —（防止门禁空转） |

**未覆盖，必须做浏览器审计**：

| 未覆盖 | 原因 |
| --- | --- |
| 颜色对比度（1.4.3、1.4.11） | jsdom 无布局与层叠，测不出真实渲染色 |
| 目标尺寸（2.5.8）、回流/缩放 200–400%（1.4.10） | 需要真实视口 |
| 动效（2.3.3） | 样式已含 `prefers-reduced-motion`，仍需浏览器复验 |
| 屏幕阅读器实机朗读 | 需 NVDA/VoiceOver 人工走查 |

因此：**"无头门禁通过"不等于 WCAG 2.2 AA 认证通过**，它只证明"语义 + 键盘"这一半。
对比度与缩放必须在真实浏览器里过一遍，别把 CI 绿灯当成无障碍合规。

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
