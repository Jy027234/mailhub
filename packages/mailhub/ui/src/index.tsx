import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import {
  type MailHubInboxFilterKey,
  type MailHubMessageOverrides,
  type MailHubMessages,
  interpolate,
  resolveMessages,
} from "./i18n.js";

// A host that ships its own locale bundle needs the contract, not a fork of it.
export {
  enUS,
  interpolate as interpolateMessage,
  messageKeys,
  REQUIRED_PLACEHOLDERS,
  resolveMessages,
  validateMessages,
  zhCN,
} from "./i18n.js";
export type {
  MailHubInboxFilterKey,
  MailHubMessageOverrides,
  MailHubMessages,
} from "./i18n.js";

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

export type MailHubInboxFilter = MailHubInboxFilterKey;

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
  /**
   * Level of the workspace's own heading.  A host that already renders a page
   * `<h1>` (or mounts `MailHubStandalone`) passes 2 so the document keeps a
   * single top-level heading and an unbroken heading order.
   */
  headingLevel?: 1 | 2 | 3;
  /** Built-in locale tag: `zh-CN` (default) or `en-US`. */
  locale?: string;
  /** Partial or complete bundle overriding the resolved locale. */
  messages?: MailHubMessageOverrides;
  onError?: (error: unknown) => void;
}

type Tab = NonNullable<MailHubWorkspaceProps["initialTab"]>;
type HeadingTag = "h1" | "h2" | "h3" | "h4" | "h5" | "h6";

const TAB_ORDER: readonly Tab[] = ["inbox", "candidates", "connections"];
const PANEL_ID = "mailhub-view-panel";

function headingTag(level: number): HeadingTag {
  const clamped = Math.min(6, Math.max(1, Math.round(level)));
  return `h${clamped}` as HeadingTag;
}

/**
 * Host-neutral MailHub workspace.  It deliberately uses only client-injected
 * contracts so it can be embedded in any router, session and design system.  It
 * renders text from an injectable locale bundle, exposes a real ARIA tab widget
 * with roving tabindex and arrow-key navigation, and lets the host pick the
 * heading level so embedding never breaks the document outline.
 */
export function MailHubWorkspace({
  client,
  identityLabel,
  className = "",
  initialTab = "inbox",
  initialInboxFilter = "all",
  headingLevel = 1,
  locale,
  messages,
  onError,
}: MailHubWorkspaceProps) {
  const text = useMemo(() => resolveMessages(locale, messages), [locale, messages]);
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
  const tabRefs = useRef<Partial<Record<Tab, HTMLButtonElement | null>>>({});

  const Heading = headingTag(headingLevel);
  const cardHeading = headingTag(headingLevel + 1);
  const tabId = (value: Tab) => `mailhub-tab-${value}`;

  const reportError = (reason: unknown) => {
    setError(reason);
    onError?.(reason);
  };
  const selectTab = (value: Tab, moveFocus = false) => {
    setTab(value);
    if (moveFocus) tabRefs.current[value]?.focus();
  };
  const handleTabKeys = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: Tab | null = null;
    if (event.key === "ArrowRight") next = TAB_ORDER[(index + 1) % TAB_ORDER.length];
    else if (event.key === "ArrowLeft") next = TAB_ORDER[(index - 1 + TAB_ORDER.length) % TAB_ORDER.length];
    else if (event.key === "Home") next = TAB_ORDER[0];
    else if (event.key === "End") next = TAB_ORDER[TAB_ORDER.length - 1];
    if (next !== null) {
      event.preventDefault();
      selectTab(next, true);
    }
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

  const title = useMemo(
    () => (tab === "inbox" ? text.titleInbox : tab === "candidates" ? text.titleCandidates : text.titleConnections),
    [tab, text],
  );
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
    <section className={`mailhub-workspace ${className}`.trim()} aria-label={text.workspaceLabel} aria-busy={loading}>
      <header className="mailhub-workspace__header">
        <div><span className="mailhub-workspace__eyebrow">{text.eyebrow} · {identityLabel ?? text.identityFallback}</span><Heading>{title}</Heading><p>{text.subtitle}</p></div>
        <button type="button" onClick={() => void refresh()} disabled={loading}>{text.refresh}</button>
      </header>
      {error ? <div className="mailhub-workspace__alert" role="alert">{text.requestFailed}</div> : null}
      <div className="mailhub-workspace__tabs" role="tablist" aria-label={text.viewsLabel}>
        {TAB_ORDER.map((value, index) => (
          <button
            key={value}
            ref={(node) => { tabRefs.current[value] = node; }}
            id={tabId(value)}
            type="button"
            role="tab"
            aria-selected={tab === value}
            aria-controls={PANEL_ID}
            tabIndex={tab === value ? 0 : -1}
            onClick={() => selectTab(value)}
            onKeyDown={(event) => handleTabKeys(event, index)}
          >
            {value === "inbox" ? text.tabInbox : value === "candidates" ? interpolate(text.tabCandidates, { count: candidates.length }) : interpolate(text.tabConnections, { count: connections.length })}
          </button>
        ))}
      </div>
      <div id={PANEL_ID} role="tabpanel" aria-labelledby={tabId(tab)} tabIndex={-1}>
        {tab === "inbox" ? <Inbox text={text} cardHeading={cardHeading} client={client} threads={filterThreads(threads, inboxFilter)} detail={detail} content={content} query={query} setQuery={setQuery} filter={inboxFilter} onFilterChange={(value) => { setInboxFilter(value); void refresh(value); }} onSearch={() => void refresh()} onLoadMore={() => void loadMore()} hasMore={threadPage?.has_more === true} loadingMore={loading} onThread={openThread} onContent={openContent} /> : null}
        {tab === "candidates" ? <Candidates text={text} cardHeading={cardHeading} client={client} candidates={candidates} onRefresh={() => void refresh()} /> : null}
        {tab === "connections" ? <Connections text={text} cardHeading={cardHeading} client={client} connections={connections} onRefresh={() => void refresh()} /> : null}
      </div>
    </section>
  );
}

function Inbox(props: { text: MailHubMessages; cardHeading: HeadingTag; client: MailHubUiClient; threads: MailHubUiThread[]; detail: MailHubUiThreadDetail | null; content: string; query: string; setQuery: (value: string) => void; filter: MailHubInboxFilter; onFilterChange: (value: MailHubInboxFilter) => void; onSearch: () => void; onLoadMore: () => void; hasMore: boolean; loadingMore: boolean; onThread: (thread: MailHubUiThread) => void; onContent: (messageId: string) => void }) {
  const { text, cardHeading: Heading } = props;
  return <div className="mailhub-workspace__inbox"><aside><form onSubmit={(event) => { event.preventDefault(); props.onSearch(); }}><input aria-label={text.searchLabel} value={props.query} onChange={(event) => props.setQuery(event.target.value)} placeholder={text.searchPlaceholder} /><button type="submit">{text.searchAction}</button></form><div className="mailhub-workspace__filters" role="group" aria-label={text.filtersLabel}>{(["all", "unread", "important", "attachments", "projects", "candidates"] as const).map((value) => <button key={value} type="button" aria-pressed={props.filter === value} onClick={() => props.onFilterChange(value)}>{filterLabel(value, text)}</button>)}</div>{props.threads.map((thread) => <button className="mailhub-workspace__thread" key={thread.thread_id} type="button" onClick={() => props.onThread(thread)}><strong>{thread.normalized_subject || text.noSubject}</strong><span>{thread.account_email ?? thread.provider ?? text.accountFallback} · {thread.participant_addresses.join(text.addressSeparator)}</span><small>{interpolate(text.messageCount, { count: thread.message_count })} · {formatDate(thread.latest_at, text.locale)}{thread.unread ? text.unreadSuffix : ""}{thread.has_attachment ? text.attachmentSuffix : ""}</small></button>)}{!props.threads.length ? <p role="status">{text.emptyThreads}</p> : null}{props.hasMore ? <button type="button" onClick={props.onLoadMore} disabled={props.loadingMore}>{props.loadingMore ? text.loading : text.loadMore}</button> : null}</aside><article className="mailhub-workspace__detail">{props.detail ? <><Heading>{props.detail.thread.normalized_subject}</Heading>{props.detail.messages.map((message) => <div className="mailhub-workspace__message" key={message.message_id}><button type="button" onClick={() => props.onContent(message.message_id)}><strong>{message.sender_address}</strong><span>{formatDate(message.received_at, text.locale)}</span></button><pre>{props.content || text.contentPlaceholder}</pre></div>)}</> : <p>{text.selectThread}</p>}</article></div>;
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

function filterLabel(filter: MailHubInboxFilter, text: MailHubMessages): string {
  return {
    all: text.filterAll,
    unread: text.filterUnread,
    important: text.filterImportant,
    attachments: text.filterAttachments,
    projects: text.filterProjects,
    candidates: text.filterCandidates,
  }[filter];
}

function Candidates(props: { text: MailHubMessages; cardHeading: HeadingTag; client: MailHubUiClient; candidates: MailHubUiCandidate[]; onRefresh: () => void }) {
  const { text, cardHeading: Heading } = props;
  return <div className="mailhub-workspace__cards">{props.candidates.map((candidate) => <article className="mailhub-workspace__card" key={candidate.candidate_id}><span>{candidate.candidate_type} · v{candidate.revision}</span><Heading>{candidate.title ?? text.unnamedCandidate}</Heading><p>{candidate.summary ?? ""}</p><div className="mailhub-workspace__card-actions"><button type="button" onClick={async () => { await props.client.reviewCandidate({ candidateId: candidate.candidate_id, approved: false, expectedRevision: candidate.revision, reviewReason: text.rejectReason }); props.onRefresh(); }}>{text.reject}</button><button type="button" onClick={async () => { await props.client.reviewCandidate({ candidateId: candidate.candidate_id, approved: true, expectedRevision: candidate.revision }); props.onRefresh(); }}>{text.approve}</button></div></article>)}{!props.candidates.length ? <p>{text.emptyCandidates}</p> : null}</div>;
}

function Connections(props: { text: MailHubMessages; cardHeading: HeadingTag; client: MailHubUiClient; connections: MailHubUiConnection[]; onRefresh: () => void }) {
  const { text, cardHeading: Heading } = props;
  return <div className="mailhub-workspace__cards">{props.connections.map((connection) => <article className="mailhub-workspace__card" key={connection.connection_id}><Heading>{connection.email_address}</Heading><p>{connection.provider} · {connection.content_mode ?? text.boundedProcessing}</p><p>{text.scopeLabel}{(connection.granted_scopes ?? []).join(" · ") || text.notShown}</p><dl className="mailhub-workspace__facts"><div><dt>{text.statusLabel}</dt><dd>{connection.status}</dd></div><div><dt>{text.syncHealthLabel}</dt><dd>{connection.sync_state ?? text.healthUnknown}</dd></div><div><dt>{text.lastSyncLabel}</dt><dd>{connection.last_sync_at ? formatDate(connection.last_sync_at, text.locale) : text.neverSynced}</dd></div></dl>{connection.error_code ? <p className="mailhub-workspace__warning" role="alert">{connection.error_code}{text.connectionWarningSuffix}</p> : null}<div className="mailhub-workspace__card-actions"><button type="button" onClick={async () => { await props.client.enqueueSync(connection.connection_id); props.onRefresh(); }}>{text.syncNow}</button><span>{connection.status}</span></div></article>)}{!props.connections.length ? <p role="status">{text.emptyConnections}</p> : null}</div>;
}

function formatDate(value: string, locale: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale);
}
