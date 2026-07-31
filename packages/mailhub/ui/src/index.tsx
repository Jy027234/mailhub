import { useEffect, useMemo, useState } from "react";

export interface MailHubUiConnection {
  connection_id: string;
  provider: string;
  email_address: string;
  status: string;
  revision: number;
  granted_scopes?: string[];
  content_mode?: string;
  sync_state?: string;
  last_sync_at?: string | null;
  error_code?: string | null;
}

export interface MailHubUiThread {
  thread_id: string;
  connection_id: string;
  provider?: string;
  account_email?: string;
  normalized_subject: string;
  participant_addresses: string[];
  latest_at: string;
  message_count: number;
  revision: number;
  labels?: string[];
  unread?: boolean;
  important?: boolean;
  has_attachment?: boolean;
  project_hint?: string | null;
  candidate_count?: number;
}

export interface MailHubUiThreadPage {
  data: MailHubUiThread[];
  next_cursor: string | null;
  has_more: boolean;
}

export type MailHubInboxFilter =
  | "all"
  | "unread"
  | "important"
  | "attachments"
  | "projects"
  | "candidates";

export interface MailHubUiMessage {
  message_id: string;
  thread_id: string;
  sender_address: string;
  recipient_addresses: string[];
  cc_addresses?: string[];
  bcc_addresses?: string[];
  reply_to_addresses?: string[];
  subject: string;
  received_at: string;
  attachment_count?: number;
  source_locator?: Record<string, unknown> | null;
}

export interface MailHubUiCandidate {
  candidate_id: string;
  candidate_type: string;
  state: string;
  revision: number;
  title?: string;
  summary?: string;
  confidence?: number | null;
  source_refs?: Array<Record<string, unknown>>;
  risk_flags?: string[];
}

export interface MailHubUiThreadDetail {
  thread: MailHubUiThread;
  messages: MailHubUiMessage[];
  content_policy: string;
}

export interface MailHubUiDraft {
  draft_id: string;
  revision: number;
  status: string;
  subject: string;
  recipient_addresses: string[];
  cc_addresses: string[];
  bcc_addresses: string[];
  content_sha256: string;
  recipient_digest?: string;
}

export interface MailHubUiClient {
  listConnections(): Promise<MailHubUiConnection[]>;
  listThreads(options?: { query?: string; limit?: number }): Promise<MailHubUiThread[]>;
  listThreadPage?(options?: { cursor?: string | null; limit?: number; unread?: boolean; important?: boolean; attachment?: boolean; project?: boolean; candidate?: boolean }): Promise<MailHubUiThreadPage>;
  getThread(threadId: string): Promise<MailHubUiThreadDetail>;
  getMessageContent(messageId: string): Promise<string>;
  listCandidates(): Promise<MailHubUiCandidate[]>;
  reviewCandidate(input: { candidateId: string; approved: boolean; expectedRevision: number; reviewReason?: string }): Promise<unknown>;
  enqueueSync(connectionId: string): Promise<unknown>;
  createDraft(input: { connectionId: string; threadId?: string | null; to: string[]; cc: string[]; subject: string; body: string }): Promise<MailHubUiDraft>;
  sendDraft(input: { draftId: string; expectedRevision: number; contentSha256: string; recipientDigest: string; confirmationRef: string }): Promise<unknown>;
}

export interface MailHubWorkspaceProps {
  client: MailHubUiClient;
  identityLabel?: string;
  className?: string;
  initialTab?: "inbox" | "candidates" | "connections";
  initialInboxFilter?: MailHubInboxFilter;
  onError?: (error: unknown) => void;
}

type Tab = NonNullable<MailHubWorkspaceProps["initialTab"]>;

/**
 * Host-neutral MailHub workspace.  It deliberately uses only client-injected
 * contracts so it can be embedded in any router, session and design system.
 */
export function MailHubWorkspace({
  client,
  identityLabel = "当前身份",
  className = "",
  initialTab = "inbox",
  initialInboxFilter = "all",
  onError,
}: MailHubWorkspaceProps) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [connections, setConnections] = useState<MailHubUiConnection[]>([]);
  const [threads, setThreads] = useState<MailHubUiThread[]>([]);
  const [threadPage, setThreadPage] = useState<MailHubUiThreadPage | null>(null);
  const [inboxFilter, setInboxFilter] = useState<MailHubInboxFilter>(initialInboxFilter);
  const [candidates, setCandidates] = useState<MailHubUiCandidate[]>([]);
  const [detail, setDetail] = useState<MailHubUiThreadDetail | null>(null);
  const [content, setContent] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const reportError = (reason: unknown) => {
    setError(reason);
    onError?.(reason);
  };
  const refresh = async (requestedFilter: MailHubInboxFilter = inboxFilter) => {
    setLoading(true);
    try {
      const listPage = client.listThreadPage;
      const [nextConnections, nextThreads, nextCandidates] = await Promise.all([
        client.listConnections(),
        listPage && !query.trim()
          ? listPage({ limit: 100, ...filterOptions(requestedFilter) })
          : client.listThreads({ query: query.trim() || undefined, limit: 100 }),
        client.listCandidates(),
      ]);
      setConnections(nextConnections);
      if (!Array.isArray(nextThreads) && "data" in nextThreads && Array.isArray(nextThreads.data)) {
        setThreads(nextThreads.data);
        setThreadPage(nextThreads);
      } else {
        setThreads(Array.isArray(nextThreads) ? nextThreads : []);
        setThreadPage(null);
      }
      setCandidates(nextCandidates);
    } catch (reason) {
      reportError(reason);
    } finally {
      setLoading(false);
    }
  };
  const loadMore = async () => {
    if (!client.listThreadPage || !threadPage?.has_more || !threadPage.next_cursor) return;
    setLoading(true);
    try {
      const next = await client.listThreadPage({
        cursor: threadPage.next_cursor,
        limit: 100,
        ...filterOptions(inboxFilter),
      });
      setThreads((current: MailHubUiThread[]) => [...current, ...next.data]);
      setThreadPage(next);
    } catch (reason) {
      reportError(reason);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void refresh(); }, [client]); // eslint-disable-line react-hooks/exhaustive-deps

  const title = useMemo(() => tab === "inbox" ? "统一收件箱" : tab === "candidates" ? "智能处理" : "连接中心", [tab]);
  const openThread = async (thread: MailHubUiThread) => {
    try {
      setLoading(true);
      setDetail(await client.getThread(thread.thread_id));
      setContent("");
    } catch (reason) {
      reportError(reason);
    } finally {
      setLoading(false);
    }
  };
  const openContent = async (messageId: string) => {
    try {
      setLoading(true);
      setContent(await client.getMessageContent(messageId));
    } catch (reason) {
      reportError(reason);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className={`mailhub-workspace ${className}`.trim()} aria-label="MailHub 工作台" aria-busy={loading}>
      <header className="mailhub-workspace__header">
        <div><span className="mailhub-workspace__eyebrow">MAILHUB · {identityLabel}</span><h1>{title}</h1><p>metadata-first；正文与副作用按需、按授权执行。</p></div>
        <button type="button" onClick={() => void refresh()} disabled={loading}>刷新</button>
      </header>
      {error ? <div className="mailhub-workspace__alert" role="alert">MailHub 请求未完成；宿主未将失败当作成功。</div> : null}
      <nav className="mailhub-workspace__tabs" role="tablist" aria-label="邮件视图">
        {(["inbox", "candidates", "connections"] as const).map((value) => <button key={value} type="button" role="tab" aria-selected={tab === value} onClick={() => setTab(value)}>{value === "inbox" ? "收件箱" : value === "candidates" ? `候选 · ${candidates.length}` : `连接 · ${connections.length}`}</button>)}
      </nav>
      {tab === "inbox" ? <Inbox client={client} threads={filterThreads(threads, inboxFilter)} detail={detail} content={content} query={query} setQuery={setQuery} filter={inboxFilter} onFilterChange={(value) => { setInboxFilter(value); void refresh(value); }} onSearch={() => void refresh()} onLoadMore={() => void loadMore()} hasMore={threadPage?.has_more === true} loadingMore={loading} onThread={openThread} onContent={openContent} /> : null}
      {tab === "candidates" ? <Candidates client={client} candidates={candidates} onRefresh={() => void refresh()} /> : null}
      {tab === "connections" ? <Connections client={client} connections={connections} onRefresh={() => void refresh()} /> : null}
    </section>
  );
}

function Inbox(props: { client: MailHubUiClient; threads: MailHubUiThread[]; detail: MailHubUiThreadDetail | null; content: string; query: string; setQuery: (value: string) => void; filter: MailHubInboxFilter; onFilterChange: (value: MailHubInboxFilter) => void; onSearch: () => void; onLoadMore: () => void; hasMore: boolean; loadingMore: boolean; onThread: (thread: MailHubUiThread) => void; onContent: (messageId: string) => void }) {
  return <div className="mailhub-workspace__inbox"><aside><form onSubmit={(event) => { event.preventDefault(); props.onSearch(); }}><input aria-label="搜索邮件" value={props.query} onChange={(event) => props.setQuery(event.target.value)} placeholder="搜索主题或发件人" /><button type="submit">搜索</button></form><div className="mailhub-workspace__filters" role="tablist" aria-label="邮件筛选">{(["all", "unread", "important", "attachments", "projects", "candidates"] as const).map((value) => <button key={value} type="button" role="tab" aria-selected={props.filter === value} onClick={() => props.onFilterChange(value)}>{filterLabel(value)}</button>)}</div>{props.threads.map((thread) => <button className="mailhub-workspace__thread" key={thread.thread_id} type="button" onClick={() => props.onThread(thread)}><strong>{thread.normalized_subject || "（无主题）"}</strong><span>{thread.account_email ?? thread.provider ?? "邮箱账号"} · {thread.participant_addresses.join("、")}</span><small>{thread.message_count} 封 · {formatDate(thread.latest_at)}{thread.unread ? " · 未读" : ""}{thread.has_attachment ? " · 附件" : ""}</small></button>)}{!props.threads.length ? <p role="status">暂无线程。</p> : null}{props.hasMore ? <button type="button" onClick={props.onLoadMore} disabled={props.loadingMore}>{props.loadingMore ? "正在加载…" : "加载更多"}</button> : null}</aside><article className="mailhub-workspace__detail">{props.detail ? <><h2>{props.detail.thread.normalized_subject}</h2>{props.detail.messages.map((message) => <div className="mailhub-workspace__message" key={message.message_id}><button type="button" onClick={() => props.onContent(message.message_id)}><strong>{message.sender_address}</strong><span>{formatDate(message.received_at)}</span></button><pre>{props.content || "正文按需读取。"}</pre></div>)}</> : <p>选择线程查看 metadata；正文不会自动展开。</p>}</article></div>;
}

function filterOptions(filter: MailHubInboxFilter): { unread?: boolean; important?: boolean; attachment?: boolean; project?: boolean; candidate?: boolean } {
  return {
    unread: filter === "unread",
    important: filter === "important",
    attachment: filter === "attachments",
    project: filter === "projects",
    candidate: filter === "candidates",
  };
}

function filterThreads(threads: MailHubUiThread[], filter: MailHubInboxFilter): MailHubUiThread[] {
  if (filter === "all") return threads;
  return threads.filter((thread) => {
    if (filter === "unread") return thread.unread === true;
    if (filter === "important") return thread.important === true || thread.labels?.some((label) => label.toLowerCase() === "important");
    if (filter === "attachments") return thread.has_attachment === true || thread.labels?.some((label) => label.toLowerCase() === "attachment");
    if (filter === "projects") return Boolean(thread.project_hint) || thread.labels?.some((label) => ["project", "rfq", "audit"].includes(label.toLowerCase()));
    return (thread.candidate_count ?? 0) > 0;
  });
}

function filterLabel(filter: MailHubInboxFilter): string {
  return { all: "全部", unread: "未读", important: "重要", attachments: "附件", projects: "项目", candidates: "候选" }[filter];
}

function Candidates(props: { client: MailHubUiClient; candidates: MailHubUiCandidate[]; onRefresh: () => void }) {
  return <div className="mailhub-workspace__cards">{props.candidates.map((candidate) => <article className="mailhub-workspace__card" key={candidate.candidate_id}><span>{candidate.candidate_type} · v{candidate.revision}</span><h2>{candidate.title ?? "未命名候选"}</h2><p>{candidate.summary ?? ""}</p><div className="mailhub-workspace__card-actions"><button type="button" onClick={async () => { await props.client.reviewCandidate({ candidateId: candidate.candidate_id, approved: false, expectedRevision: candidate.revision, reviewReason: "宿主审核拒绝" }); props.onRefresh(); }}>驳回</button><button type="button" onClick={async () => { await props.client.reviewCandidate({ candidateId: candidate.candidate_id, approved: true, expectedRevision: candidate.revision }); props.onRefresh(); }}>批准</button></div></article>)}{!props.candidates.length ? <p>暂无候选。</p> : null}</div>;
}

function Connections(props: { client: MailHubUiClient; connections: MailHubUiConnection[]; onRefresh: () => void }) {
  return <div className="mailhub-workspace__cards">{props.connections.map((connection) => <article className="mailhub-workspace__card" key={connection.connection_id}><h2>{connection.email_address}</h2><p>{connection.provider} · {connection.content_mode ?? "受限处理"}</p><p>scope：{(connection.granted_scopes ?? []).join(" · ") || "未显示"}</p><dl className="mailhub-workspace__facts"><div><dt>连接状态</dt><dd>{connection.status}</dd></div><div><dt>同步健康</dt><dd>{connection.sync_state ?? "待检查"}</dd></div><div><dt>最近同步</dt><dd>{connection.last_sync_at ? formatDate(connection.last_sync_at) : "尚未同步"}</dd></div></dl>{connection.error_code ? <p className="mailhub-workspace__warning" role="alert">{connection.error_code} · 需要重新授权或查看运行手册。</p> : null}<div className="mailhub-workspace__card-actions"><button type="button" onClick={async () => { await props.client.enqueueSync(connection.connection_id); props.onRefresh(); }}>增量同步</button><span>{connection.status}</span></div></article>)}{!props.connections.length ? <p role="status">暂无连接。</p> : null}</div>;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
