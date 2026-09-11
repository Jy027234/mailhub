/**
 * Locale contract for the embeddable MailHub workspace.
 *
 * Bundles are plain strings with `{placeholder}` tokens, so a host or a
 * translation vendor can ship a JSON bundle without touching TypeScript.
 * `zh-CN` is the default and `en-US` ships with the package; both are typed as
 * `MailHubMessages`, which makes a missing key a compile error rather than a
 * silent English fallback in production.
 */

export type MailHubInboxFilterKey =
  | "all"
  | "unread"
  | "important"
  | "attachments"
  | "projects"
  | "candidates";

export interface MailHubMessages {
  /** BCP-47 tag used for date/number formatting as well as text. */
  locale: string;
  identityFallback: string;
  workspaceLabel: string;
  eyebrow: string;
  subtitle: string;
  refresh: string;
  requestFailed: string;
  viewsLabel: string;
  tabInbox: string;
  /** `{count}` */
  tabCandidates: string;
  /** `{count}` */
  tabConnections: string;
  titleInbox: string;
  titleCandidates: string;
  titleConnections: string;
  searchLabel: string;
  searchPlaceholder: string;
  searchAction: string;
  filtersLabel: string;
  filterAll: string;
  filterUnread: string;
  filterImportant: string;
  filterAttachments: string;
  filterProjects: string;
  filterCandidates: string;
  noSubject: string;
  accountFallback: string;
  addressSeparator: string;
  /** `{count}` */
  messageCount: string;
  unreadSuffix: string;
  attachmentSuffix: string;
  emptyThreads: string;
  loading: string;
  loadMore: string;
  contentPlaceholder: string;
  selectThread: string;
  unnamedCandidate: string;
  rejectReason: string;
  reject: string;
  approve: string;
  emptyCandidates: string;
  boundedProcessing: string;
  scopeLabel: string;
  notShown: string;
  statusLabel: string;
  syncHealthLabel: string;
  lastSyncLabel: string;
  healthUnknown: string;
  neverSynced: string;
  connectionWarningSuffix: string;
  syncNow: string;
  emptyConnections: string;
  shellLabel: string;
  shellSubtitle: string;
}

/** Keys a host may override without shipping a complete bundle. */
export type MailHubMessageOverrides = Partial<Omit<MailHubMessages, "locale">> & {
  locale?: string;
};

/**
 * Placeholder tokens each key must keep.  A translation that drops `{count}`
 * silently loses information, so the build gate rejects it.
 */
export const REQUIRED_PLACEHOLDERS: Partial<Record<keyof MailHubMessages, readonly string[]>> = {
  tabCandidates: ["count"],
  tabConnections: ["count"],
  messageCount: ["count"],
};

export const zhCN: MailHubMessages = {
  locale: "zh-CN",
  identityFallback: "当前身份",
  workspaceLabel: "MailHub 工作台",
  eyebrow: "MAILHUB",
  subtitle: "metadata-first；正文与副作用按需、按授权执行。",
  refresh: "刷新",
  requestFailed: "MailHub 请求未完成；宿主未将失败当作成功。",
  viewsLabel: "邮件视图",
  tabInbox: "收件箱",
  tabCandidates: "候选 · {count}",
  tabConnections: "连接 · {count}",
  titleInbox: "统一收件箱",
  titleCandidates: "智能处理",
  titleConnections: "连接中心",
  searchLabel: "搜索邮件",
  searchPlaceholder: "搜索主题或发件人",
  searchAction: "搜索",
  filtersLabel: "邮件筛选",
  filterAll: "全部",
  filterUnread: "未读",
  filterImportant: "重要",
  filterAttachments: "附件",
  filterProjects: "项目",
  filterCandidates: "候选",
  noSubject: "（无主题）",
  accountFallback: "邮箱账号",
  addressSeparator: "、",
  messageCount: "{count} 封",
  unreadSuffix: " · 未读",
  attachmentSuffix: " · 附件",
  emptyThreads: "暂无线程。",
  loading: "正在加载…",
  loadMore: "加载更多",
  contentPlaceholder: "正文按需读取。",
  selectThread: "选择线程查看 metadata；正文不会自动展开。",
  unnamedCandidate: "未命名候选",
  rejectReason: "宿主审核拒绝",
  reject: "驳回",
  approve: "批准",
  emptyCandidates: "暂无候选。",
  boundedProcessing: "受限处理",
  scopeLabel: "scope：",
  notShown: "未显示",
  statusLabel: "连接状态",
  syncHealthLabel: "同步健康",
  lastSyncLabel: "最近同步",
  healthUnknown: "待检查",
  neverSynced: "尚未同步",
  connectionWarningSuffix: " · 需要重新授权或查看运行手册。",
  syncNow: "增量同步",
  emptyConnections: "暂无连接。",
  shellLabel: "MailHub standalone shell",
  shellSubtitle: "受治理的智能邮件工作台",
};

export const enUS: MailHubMessages = {
  locale: "en-US",
  identityFallback: "Current identity",
  workspaceLabel: "MailHub workspace",
  eyebrow: "MAILHUB",
  subtitle: "Metadata first; bodies and side effects are on demand and authorised.",
  refresh: "Refresh",
  requestFailed: "The MailHub request did not complete; the host did not treat it as success.",
  viewsLabel: "Mail views",
  tabInbox: "Inbox",
  tabCandidates: "Candidates · {count}",
  tabConnections: "Connections · {count}",
  titleInbox: "Unified inbox",
  titleCandidates: "Review queue",
  titleConnections: "Connection centre",
  searchLabel: "Search mail",
  searchPlaceholder: "Search subject or sender",
  searchAction: "Search",
  filtersLabel: "Mail filters",
  filterAll: "All",
  filterUnread: "Unread",
  filterImportant: "Important",
  filterAttachments: "Attachments",
  filterProjects: "Projects",
  filterCandidates: "Candidates",
  noSubject: "(no subject)",
  accountFallback: "Mail account",
  addressSeparator: ", ",
  messageCount: "{count} messages",
  unreadSuffix: " · unread",
  attachmentSuffix: " · attachment",
  emptyThreads: "No threads yet.",
  loading: "Loading…",
  loadMore: "Load more",
  contentPlaceholder: "Bodies are fetched on demand.",
  selectThread: "Select a thread to read metadata; bodies never expand automatically.",
  unnamedCandidate: "Untitled candidate",
  rejectReason: "Rejected by host review",
  reject: "Reject",
  approve: "Approve",
  emptyCandidates: "No candidates.",
  boundedProcessing: "Bounded processing",
  scopeLabel: "Scope: ",
  notShown: "not shown",
  statusLabel: "Connection status",
  syncHealthLabel: "Sync health",
  lastSyncLabel: "Last sync",
  healthUnknown: "Not checked",
  neverSynced: "Never synced",
  connectionWarningSuffix: " · reauthorise or check the runbook.",
  syncNow: "Sync now",
  emptyConnections: "No connections.",
  shellLabel: "MailHub standalone shell",
  shellSubtitle: "A governed intelligent mail workspace",
};

const BUILT_IN_LOCALES: Readonly<Record<string, MailHubMessages>> = {
  zh: zhCN,
  "zh-cn": zhCN,
  "zh-CN": zhCN,
  en: enUS,
  "en-us": enUS,
  "en-US": enUS,
};

/**
 * Resolve the active bundle: a built-in locale (default `zh-CN`) plus optional
 * host overrides.  A host that ships a complete bundle instead of a locale tag
 * still gets date formatting from its own `locale` value.
 */
export function resolveMessages(
  locale?: string,
  overrides?: MailHubMessageOverrides,
): MailHubMessages {
  const base = (locale ? BUILT_IN_LOCALES[locale] ?? BUILT_IN_LOCALES[locale.toLowerCase()] : undefined) ?? zhCN;
  const merged: MailHubMessages = overrides ? { ...base, ...overrides } : base;
  return locale ? { ...merged, locale } : merged;
}

/** Replace `{name}` tokens; unknown tokens are kept verbatim for debugging. */
export function interpolate(
  template: string,
  params: Readonly<Record<string, string | number>>,
): string {
  return template.replace(/\{(\w+)\}/g, (match: string, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match,
  );
}

/** Report empty values and dropped placeholders.  Used by the build gate. */
export function validateMessages(messages: MailHubMessages): string[] {
  const issues: string[] = [];
  for (const [key, value] of Object.entries(messages)) {
    if (typeof value !== "string" || value.trim().length === 0) {
      issues.push(`${key}: empty value`);
    }
  }
  for (const [key, names] of Object.entries(REQUIRED_PLACEHOLDERS)) {
    const value = String((messages as unknown as Record<string, unknown>)[key] ?? "");
    for (const name of names ?? []) {
      if (!value.includes(`{${name}}`)) {
        issues.push(`${key}: missing {${name}}`);
      }
    }
  }
  return issues;
}

/** Keys that every bundle must define, for the locale parity gate. */
export function messageKeys(messages: MailHubMessages): string[] {
  return Object.keys(messages).sort();
}
