# `@caplatform/mailhub-ui`

这是 MailHub 的可嵌入 React 工作台。它不绑定 CAPlatform 路由、身份实现、主题、Provider SDK 或
`fetch` URL；宿主通过 `MailHubUiClient` 注入已经完成身份/租户绑定的 API client，通过 CSS 变量或
自己的 class 注入主题。

```tsx
import { MailHubWorkspace } from "@caplatform/mailhub-ui";
import "@caplatform/mailhub-ui/styles.css";

<MailHubWorkspace client={mailHubClient} identityLabel="当前工作空间" />
```

不希望复制页面外壳时，可使用同一包提供的 standalone shell。它仍由宿主挂载，
不会自行创建 router、session 或 `fetch` 客户端：

```tsx
import { MailHubStandalone } from "@caplatform/mailhub-ui/standalone";

<MailHubStandalone
  client={mailHubClient}
  identityLabel="当前工作空间"
  theme={{ "--mailhub-accent": "#0b6bcb" }}
  headerContent={<a href="/">返回工作台</a>}
/>
```

安全边界：列表/线程默认只渲染 metadata；正文由 client 的 `getMessageContent` 按需返回纯文本；
统一收件箱 API 使用 scope-bound cursor，支持未读/重要/附件/项目/候选和账号筛选；组件提供对应筛选按钮、
搜索回退和加载更多行为；账号标识应由
`account_email`/`provider` 展示，不能只显示内部 connection id。
候选审核、草稿保存、发送都必须由宿主 client 实现 revision/digest/idempotency/approval 合同。组件
不会保存 token、credential_ref 或原始 HTML，也不会自行扩大 OAuth scope。CAPlatform 的 `/mail` 页面
使用同一合同和自己的页面主题；它不是该包的 fork。

发布前宿主必须执行自己的 TypeScript、WCAG 2.2 AA、API contract、CSP 和安全扫描门禁；该目录是
可复用源包，不将未验证的 Gmail/Graph fixture 当作 Provider 验收证据。

本包安装依赖后可用 `npm run typecheck` 与 `npm run build` 验证严格 TypeScript 合同；构建产物输出到
`dist/`，发布流水线仍需补充包完整性、WCAG、CSP 和宿主 API contract 门禁。

该包与 MailHub 核心使用 CAPlatform 专有许可（`UNLICENSED`）；不得把包名中的
`UNLICENSED` 解释为可公开再分发。发布前必须附仓库许可、NOTICE、SBOM、来源台账和批准的
宿主主题/安全审计证据。
