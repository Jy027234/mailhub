import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  analyzeMailMessage,
  applyMailCandidate,
  cancelMailSyncJob,
  controlMailAutonomyRun,
  createMailDraft,
  deleteMailConnection,
  enqueueMailAutonomy,
  enqueueMailSync,
  exportMailData,
  fixtureModeEnabled,
  getMailConnectionImpactPreview,
  getMailMessageContent,
  getMailThread,
  listMailAgentPolicies,
  listMailAutonomyRuns,
  listMailCandidates,
  listMailConnections,
  beginMailOAuth,
  completeMailOAuth,
  listMailOAuthProviders,
  listMailDelegations,
  listMailSyncJobs,
  listMailThreadPage,
  reviewMailCandidate,
  revokeMailConnection,
  searchMailMessages,
  sendMailDraft,
  updateMailConnectionScopes,
  type MailHubCandidate,
  type MailHubAgentPolicy,
  type MailHubAutonomyRun,
  type MailHubConnection,
  type MailHubConnectionImpactPreview,
  type MailHubDelegation,
  type MailHubDraft,
  type MailHubMessage,
  type MailHubOAuthProvider,
  type MailHubSyncJob,
  type MailHubThread,
  type MailHubThreadDetail,
} from "../api/client";
import { Icon } from "../components/Icon";

type MailTab = "inbox" | "intelligence" | "connections";
type InboxFilter = "all" | "unread" | "important" | "attachments" | "projects" | "candidates";

const fixtureConnections: MailHubConnection[] = [
  {
    connection_id: "fixture-conn-gmail",
    provider: "gmail",
    email_address: "operations@example.com",
    status: "active",
    revision: 3,
    granted_scopes: ["mail.read", "mail.drafts"],
    content_mode: "bounded_processing",
    sync_state: "healthy",
    last_sync_at: "2026-07-28T14:32:00Z",
  },
  {
    connection_id: "fixture-conn-graph",
    provider: "microsoft_graph",
    email_address: "safety@example.com",
    status: "active",
    revision: 2,
    granted_scopes: ["mail.read"],
    content_mode: "metadata_only",
    sync_state: "healthy",
    last_sync_at: "2026-07-28T14:28:00Z",
  },
];

const fixtureThreads: MailHubThread[] = [
  {
    thread_id: "fixture-thread-rfq",
    connection_id: "fixture-conn-gmail",
    normalized_subject: "航材询价：A320 起落架组件",
    participant_addresses: ["buyer@example.com", "supplier@example.net"],
    latest_at: "2026-07-28T13:12:00Z",
    message_count: 3,
    revision: 4,
    labels: ["rfq", "project"],
    unread: true,
    has_attachment: true,
    project_hint: "航材采购",
  },
  {
    thread_id: "fixture-thread-audit",
    connection_id: "fixture-conn-graph",
    normalized_subject: "Q3 安全审计行动项确认",
    participant_addresses: ["safety@example.com", "audit@example.org"],
    latest_at: "2026-07-27T08:45:00Z",
    message_count: 2,
    revision: 2,
    labels: ["project", "audit"],
    unread: true,
    project_hint: "安全审计",
  },
  {
    thread_id: "fixture-thread-knowledge",
    connection_id: "fixture-conn-gmail",
    normalized_subject: "服务通告 SB-2026-17（待归档）",
    participant_addresses: ["engineering@example.org"],
    latest_at: "2026-07-25T03:20:00Z",
    message_count: 1,
    revision: 1,
    labels: ["knowledge", "attachment"],
    unread: false,
    has_attachment: true,
  },
];

const fixtureMessages: Record<string, MailHubMessage[]> = {
  "fixture-thread-rfq": [
    {
      message_id: "fixture-message-rfq-1",
      connection_id: "fixture-conn-gmail",
      thread_id: "fixture-thread-rfq",
      sender_address: "buyer@example.com",
      recipient_addresses: ["supplier@example.net"],
      cc_addresses: ["engineering@example.com"],
      reply_to_addresses: ["rfq-replies@example.com"],
      subject: "航材询价：A320 起落架组件",
      received_at: "2026-07-28T10:02:00Z",
      has_content: true,
      attachment_count: 1,
      source_locator: { provider: "gmail", thread_ref: "fixture-rfq" },
    },
    {
      message_id: "fixture-message-rfq-2",
      connection_id: "fixture-conn-gmail",
      thread_id: "fixture-thread-rfq",
      sender_address: "supplier@example.net",
      recipient_addresses: ["buyer@example.com"],
      subject: "Re: 航材询价：A320 起落架组件",
      received_at: "2026-07-28T13:12:00Z",
      has_content: true,
      attachment_count: 2,
      source_locator: { provider: "gmail", message_ref: "fixture-rfq-2" },
    },
  ],
  "fixture-thread-audit": [
    {
      message_id: "fixture-message-audit-1",
      connection_id: "fixture-conn-graph",
      thread_id: "fixture-thread-audit",
      sender_address: "audit@example.org",
      recipient_addresses: ["safety@example.com"],
      subject: "Q3 安全审计行动项确认",
      received_at: "2026-07-27T08:45:00Z",
      has_content: true,
      source_locator: { provider: "microsoft_graph", conversation_ref: "fixture-audit" },
    },
  ],
  "fixture-thread-knowledge": [
    {
      message_id: "fixture-message-knowledge-1",
      connection_id: "fixture-conn-gmail",
      thread_id: "fixture-thread-knowledge",
      sender_address: "engineering@example.org",
      recipient_addresses: ["operations@example.com"],
      subject: "服务通告 SB-2026-17（待归档）",
      received_at: "2026-07-25T03:20:00Z",
      has_content: true,
      attachment_count: 1,
      source_locator: { provider: "gmail", label: "service-bulletin" },
    },
  ],
};

const fixtureContents: Record<string, string> = {
  "fixture-message-rfq-1": "请确认 A320 起落架组件的可供数量、交付周期和报价有效期。\n\n附件：需求清单.pdf",
  "fixture-message-rfq-2": "我们可以在 2026-08-15 前交付。报价有效期 14 天，最终价格以批准的报价单为准。\n\n附件：报价单.pdf、合规声明.pdf",
  "fixture-message-audit-1": "请在 2026-08-02 前确认三项安全审计行动项的责任人和证据链接。",
  "fixture-message-knowledge-1": "服务通告 SB-2026-17 已发布，请按适用性评估流程完成内部评审。",
};

const fixtureCandidates: MailHubCandidate[] = [
  {
    candidate_id: "fixture-candidate-task",
    candidate_type: "task",
    state: "proposed",
    revision: 1,
    title: "创建：确认供应商报价有效期",
    summary: "来自 RFQ 线程的明确跟进项，截止 2026-08-02。",
    confidence: 0.93,
    source_refs: [{ message_id: "fixture-message-rfq-2", locator: "body:1-2" }],
    target: { project_hint: "航材采购", due_at: "2026-08-02" },
    risk_flags: [],
    created_at: "2026-07-28T13:15:00Z",
  },
  {
    candidate_id: "fixture-candidate-knowledge",
    candidate_type: "knowledge",
    state: "proposed",
    revision: 2,
    title: "知识候选：服务通告 SB-2026-17",
    summary: "建议进入个人知识空间，等待病毒/DLP/权利检查。",
    confidence: 0.86,
    source_refs: [{ message_id: "fixture-message-knowledge-1", attachment_ref: "obj://mail/fixture-sb" }],
    target: { scope: "personal", retention_days: 365 },
    risk_flags: ["attachment_scan_pending"],
    created_at: "2026-07-25T03:21:00Z",
  },
];

const fixturePolicies: MailHubAgentPolicy[] = [
  {
    policy_id: "fixture-policy-owner",
    tenant_id: "fixture-tenant",
    owner_subject_id: "fixture-user",
    allowed_connection_ids: ["fixture-conn-gmail"],
    allowed_folder_refs: ["INBOX"],
    allowed_actions: ["label", "draft_reply"],
    allowed_domains: ["example.com", "example.net"],
    allowed_data_classes: ["internal"],
    thread_only: true,
    allowed_automation_level: "l1_recommend",
    max_per_hour: 20,
    max_per_day: 100,
    valid_from: "2026-07-28T00:00:00Z",
    valid_until: "2026-08-28T00:00:00Z",
    revision: 2,
    enabled: true,
  },
];

const fixtureDelegations: MailHubDelegation[] = [
  {
    grant_id: "fixture-grant-agent",
    tenant_id: "fixture-tenant",
    policy_id: "fixture-policy-owner",
    agent_subject_id: "mail-agent-triage",
    granted_by_subject_id: "fixture-user",
    capability_ids: ["label", "draft_reply"],
    granted_at: "2026-07-28T08:00:00Z",
    expires_at: "2026-08-04T08:00:00Z",
    revoked_at: null,
    revision: 1,
  },
];

export function MailHubPage() {
  const fixture = fixtureModeEnabled();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const routeParams = useParams<{ provider?: string }>();
  const [searchParams] = useSearchParams();
  const callbackProvider = routeParams.provider === "gmail" || routeParams.provider === "microsoft_graph"
    ? routeParams.provider
    : null;
  const [oauthStatus, setOauthStatus] = useState<string | null>(null);
  const handledCallback = useRef<string | null>(null);
  const [tab, setTab] = useState<MailTab>("inbox");
  const [filter, setFilter] = useState<InboxFilter>("all");
  const [search, setSearch] = useState("");
  const [submittedSearch, setSubmittedSearch] = useState("");
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(
    fixture ? fixtureThreads[0]?.thread_id ?? null : null,
  );
  const [selectedMessageId, setSelectedMessageId] = useState<string | null>(null);
  const [selectedCandidates, setSelectedCandidates] = useState<string[]>([]);
  const [showComposer, setShowComposer] = useState(false);
  const [reviewReason, setReviewReason] = useState("");
  const [composer, setComposer] = useState({
    to: "",
    cc: "",
    subject: "",
    body: "",
  });

  const oauthProvidersQuery = useQuery({
    queryKey: ["mailhub", "oauth-providers"],
    queryFn: listMailOAuthProviders,
    enabled: !fixture,
    initialData: fixture ? [] : undefined,
    retry: false,
  });
  const oauthStartMutation = useMutation({
    mutationFn: (input: { provider: "gmail" | "microsoft_graph"; connectionId?: string; expectedRevision?: number }) =>
      beginMailOAuth(input.provider, {
        connectionId: input.connectionId,
        expectedRevision: input.expectedRevision,
      }),
    onSuccess: (result) => {
      window.location.assign(result.authorization_url);
    },
  });
  const oauthCallbackMutation = useMutation({
    mutationFn: ({ provider, state, code }: { provider: "gmail" | "microsoft_graph"; state: string; code: string }) =>
      completeMailOAuth(provider, state, code),
    onSuccess: async () => {
      setOauthStatus("邮箱已完成授权，正在刷新连接与同步状态。");
      await queryClient.invalidateQueries({ queryKey: ["mailhub"] });
      navigate("/mail?oauth=connected", { replace: true });
    },
    onError: () => setOauthStatus("授权回调未完成；未假定 Provider 已接受，请检查状态或重新授权。"),
  });
  useEffect(() => {
    if (fixture || !callbackProvider) return;
    const code = searchParams.get("code");
    const state = searchParams.get("state");
    const providerError = searchParams.get("error");
    const callbackKey = `${callbackProvider}:${state ?? ""}:${code ?? ""}:${providerError ?? ""}`;
    if (handledCallback.current === callbackKey) return;
    handledCallback.current = callbackKey;
    if (providerError) {
      setOauthStatus(`Provider 未完成授权（${providerError}）；未创建或修改邮箱连接。`);
      navigate("/mail?oauth=cancelled", { replace: true });
      return;
    }
    if (code && state) {
      oauthCallbackMutation.mutate({ provider: callbackProvider, state, code });
    } else {
      setOauthStatus("授权回调缺少一次性 state 或 code；未继续处理。" );
      navigate("/mail?oauth=invalid", { replace: true });
    }
  }, [callbackProvider, fixture, navigate, oauthCallbackMutation, searchParams]);
  useEffect(() => {
    const result = searchParams.get("oauth");
    if (result === "connected") setOauthStatus("邮箱已连接；仍保持只读同步，未启用自动发信。" );
    if (result === "cancelled") setOauthStatus("已取消邮箱授权，现有连接未改变。" );
    if (result === "invalid") setOauthStatus("授权回调无效，未改变邮箱连接。" );
  }, [searchParams]);

  const connectionsQuery = useQuery({
    queryKey: ["mailhub", "connections"],
    queryFn: listMailConnections,
    enabled: !fixture,
    initialData: fixture ? fixtureConnections : undefined,
    retry: false,
  });
  const syncJobsQuery = useQuery({
    queryKey: ["mailhub", "sync-jobs"],
    queryFn: () => listMailSyncJobs(undefined, 100),
    enabled: !fixture,
    initialData: fixture ? [] : undefined,
    retry: false,
    refetchInterval: (query) => {
      const jobs = query.state.data ?? [];
      return jobs.some((job) => ["queued", "running", "cancelling"].includes(job.status)) ? 5000 : false;
    },
  });
  const policiesQuery = useQuery({
    queryKey: ["mailhub", "agent-policies"],
    queryFn: () => listMailAgentPolicies(100),
    enabled: !fixture,
    initialData: fixture ? fixturePolicies : undefined,
    retry: false,
  });
  const delegationsQuery = useQuery({
    queryKey: ["mailhub", "delegations"],
    queryFn: () => listMailDelegations(100),
    enabled: !fixture,
    initialData: fixture ? fixtureDelegations : undefined,
    retry: false,
  });
  const threadsQuery = useInfiniteQuery({
    queryKey: ["mailhub", "threads", filter],
    queryFn: ({ pageParam }) => listMailThreadPage({
      limit: 100,
      cursor: pageParam,
      unread: filter === "unread",
      important: filter === "important",
      attachment: filter === "attachments",
      project: filter === "projects",
      candidate: filter === "candidates",
    }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.has_more ? lastPage.next_cursor : undefined,
    enabled: !fixture && !submittedSearch,
    initialData: fixture && !submittedSearch
      ? { pages: [{ data: fixtureThreads, next_cursor: null, has_more: false }], pageParams: [null] }
      : undefined,
    retry: false,
  });
  const searchQuery = useQuery({
    queryKey: ["mailhub", "search", submittedSearch],
    queryFn: () => searchMailMessages(submittedSearch, 100),
    enabled: !fixture && Boolean(submittedSearch),
    initialData: fixture && submittedSearch ? [] : undefined,
    retry: false,
  });
  const candidatesQuery = useQuery({
    queryKey: ["mailhub", "candidates"],
    queryFn: () => listMailCandidates(undefined, 100),
    enabled: !fixture,
    initialData: fixture ? fixtureCandidates : undefined,
    retry: false,
  });
  const autonomyQuery = useQuery({
    queryKey: ["mailhub", "autonomy-runs"],
    queryFn: () => listMailAutonomyRuns(undefined, 100),
    enabled: !fixture,
    initialData: fixture ? [] : undefined,
    retry: false,
  });
  const threadQuery = useQuery({
    queryKey: ["mailhub", "thread", selectedThreadId],
    queryFn: () => getMailThread(selectedThreadId as string),
    enabled: Boolean(selectedThreadId) && !fixture,
    initialData: fixture && selectedThreadId ? fixtureDetail(selectedThreadId) : undefined,
    retry: false,
  });
  const selectedMessage = (threadQuery.data?.messages ?? []).find(
    (message) => message.message_id === selectedMessageId,
  );
  const contentQuery = useQuery({
    queryKey: ["mailhub", "content", selectedMessageId],
    queryFn: () => getMailMessageContent(selectedMessageId as string),
    enabled: Boolean(selectedMessageId) && !fixture,
    initialData: fixture && selectedMessageId ? fixtureContents[selectedMessageId] ?? "" : undefined,
    retry: false,
  });

  const syncMutation = useMutation({
    mutationFn: (connectionId: string) => enqueueMailSync(connectionId, "incremental", 100),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mailhub"] });
    },
  });
  const backfillMutation = useMutation({
    mutationFn: ({
      connectionId,
      folderRef,
      labelRefs,
      receivedAfter,
      receivedBefore,
    }: {
      connectionId: string;
      folderRef: string;
      labelRefs: string[];
      receivedAfter?: string;
      receivedBefore?: string;
    }) => enqueueMailSync(connectionId, "backfill", 100, {
      folderRef,
      labelRefs,
      receivedAfter: receivedAfter || null,
      receivedBefore: receivedBefore || null,
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mailhub"] });
    },
  });
  const cancelSyncMutation = useMutation({
    mutationFn: (jobId: string) => cancelMailSyncJob(jobId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mailhub"] });
    },
  });
  const autonomyEnqueueMutation = useMutation({
    mutationFn: (connectionId: string) => enqueueMailAutonomy(
      connectionId,
      `mailhub-ui:${crypto.randomUUID()}`,
      100,
      100,
    ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "autonomy-runs"] });
    },
  });
  const autonomyControlMutation = useMutation({
    mutationFn: ({ runId, command, reason }: { runId: string; command: "run" | "pause" | "resume" | "cancel"; reason?: string }) =>
      controlMailAutonomyRun(runId, command, reason),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "autonomy-runs"] });
    },
  });
  const deleteMutation = useMutation({
    mutationFn: (connection: MailHubConnection) => deleteMailConnection(connection.connection_id, connection.revision),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "connections"] });
    },
  });
  const revokeMutation = useMutation({
    mutationFn: (connection: MailHubConnection) => revokeMailConnection(connection.connection_id, connection.revision),
    onSuccess: async () => {
      setOauthStatus("Provider 访问已撤销；连接投影保留，便于核对撤权后的零访问状态。需要继续使用时请重新授权。" );
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "connections"] });
    },
  });
  const impactPreviewMutation = useMutation({
    mutationFn: ({ connectionId, folderRefs }: { connectionId: string; folderRefs?: string[] }) =>
      getMailConnectionImpactPreview(connectionId, folderRefs),
    onError: () => setOauthStatus("影响预览未完成；未执行 Provider 查询或撤权/删除操作。"),
  });
  const scopeMutation = useMutation({
    mutationFn: ({ connection, grantedScopes }: { connection: MailHubConnection; grantedScopes: string[] }) =>
      updateMailConnectionScopes(connection.connection_id, connection.revision, grantedScopes),
    onSuccess: async () => {
      setOauthStatus("本地可用 scope 已收窄；如需扩权，必须重新完成绑定连接的 OAuth 授权。" );
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "connections"] });
    },
    onError: () => setOauthStatus("scope 收窄未完成；未扩大授权，也未执行 Provider 操作。"),
  });
  const exportMutation = useMutation({
    mutationFn: ({ includeContent }: { includeContent: boolean }) => exportMailData(includeContent, 200),
    onSuccess: (envelope, variables) => {
      const payload = envelope.data ?? envelope;
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = variables.includeContent ? "mailhub-data-export-with-content.json" : "mailhub-data-export.json";
      anchor.click();
      URL.revokeObjectURL(url);
      setOauthStatus(variables.includeContent ? "已生成含正文的用户范围导出；请按安全策略保存并及时清理。" : "已生成元数据导出。" );
    },
  });
  const reviewMutation = useMutation({
    mutationFn: async ({ candidate, approved }: { candidate: MailHubCandidate; approved: boolean }) =>
      reviewMailCandidate(candidate.candidate_id, approved, candidate.revision, reviewReason.trim() || undefined),
    onSuccess: async () => {
      setReviewReason("");
      setSelectedCandidates([]);
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "candidates"] });
    },
  });
  const applyMutation = useMutation({
    mutationFn: (candidate: MailHubCandidate) => {
      if (fixture) return Promise.resolve({ status: "fixture_only" });
      return applyMailCandidate(candidate.candidate_id, candidate.revision);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "candidates"] });
    },
  });
  const batchReviewMutation = useMutation({
    mutationFn: async (approved: boolean) => {
      const candidates = (candidatesQuery.data ?? []).filter((candidate) => selectedCandidates.includes(candidate.candidate_id));
      if (fixture) return candidates;
      return Promise.all(candidates.map((candidate) => reviewMailCandidate(
        candidate.candidate_id,
        approved,
        candidate.revision,
        reviewReason.trim() || undefined,
      )));
    },
    onSuccess: async () => {
      setReviewReason("");
      setSelectedCandidates([]);
      await queryClient.invalidateQueries({ queryKey: ["mailhub", "candidates"] });
    },
  });
  const analyzeMutation = useMutation({
    mutationFn: (messageId: string) => analyzeMailMessage(messageId),
  });
  const draftMutation = useMutation({
    mutationFn: () => {
      const connection = connections[0];
      if (!connection) throw new Error("mailhub_connection_required");
      return createMailDraft({
        connectionId: connection.connection_id,
        threadId: selectedThreadId,
        recipientAddresses: splitAddresses(composer.to),
        ccAddresses: splitAddresses(composer.cc),
        subject: composer.subject.trim(),
        bodyText: composer.body,
      });
    },
  });
  const sendMutation = useMutation({
    mutationFn: (draft: MailHubDraft) => sendMailDraft({
      draftId: draft.draft_id,
      expectedRevision: draft.revision,
      expectedContentSha256: draft.content_sha256,
      expectedRecipientDigest: recipientDigest(draft),
      confirmationRef: `mailhub-ui-confirm:${crypto.randomUUID()}`,
    }),
    onSuccess: () => {
      setShowComposer(false);
      setComposer({ to: "", cc: "", subject: "", body: "" });
    },
  });

  const connections = connectionsQuery.data ?? [];
  const threads = useMemo(() => {
    if (submittedSearch) return threadRowsFromMessages(searchQuery.data ?? []);
    return threadsQuery.data?.pages.flatMap((page) => page.data) ?? [];
  }, [searchQuery.data, submittedSearch, threadsQuery.data]);
  const candidates = candidatesQuery.data ?? [];
  const detail = threadQuery.data;
  const activeError = oauthProvidersQuery.error || connectionsQuery.error || syncJobsQuery.error || policiesQuery.error || delegationsQuery.error || threadsQuery.error || searchQuery.error || candidatesQuery.error || autonomyQuery.error || threadQuery.error;

  const openThread = (thread: MailHubThread) => {
    setSelectedThreadId(thread.thread_id);
    setSelectedMessageId(null);
  };
  const startComposer = () => {
    const firstMessage = detail?.messages[0];
    setComposer({
      to: firstMessage?.sender_address ?? "",
      cc: "",
      subject: detail?.thread.normalized_subject?.startsWith("Re:")
        ? detail.thread.normalized_subject
        : `Re: ${detail?.thread.normalized_subject ?? ""}`,
      body: "",
    });
    setShowComposer(true);
  };

  return (
    <div className="page mailhub-page">
      <div className="page-head mailhub-head">
        <div>
          <div className="eyebrow"><Icon name="mail" size={14} /> MAILHUB · 受治理邮箱</div>
          <h1 className="page-title">智能邮件</h1>
          <p className="page-sub">多账号统一收件、同步健康、项目/知识候选与受控草稿；正文按授权按需读取。</p>
        </div>
        <div className="mailhub-head-actions">
          <span className="badge badge-info">{fixture ? "演示数据" : "宿主会话隔离"}</span>
          <button className="btn btn-primary" type="button" onClick={() => setTab("connections")}>
            <Icon name="plus" size={15} /> 连接邮箱
          </button>
        </div>
      </div>

      {activeError && !fixture && (
        <div className="safety-banner mailhub-alert" role="alert">
          邮箱服务暂不可用或权限不足；未使用示例数据替代真实记录。请检查 MailHub 地址、会话权限或重新授权。
        </div>
      )}
      {oauthStatus && <div className="safety-banner mailhub-alert" role="status">{oauthStatus}</div>}

      <div className="mailhub-tabs" role="tablist" aria-label="邮件工作台">
        {(["inbox", "intelligence", "connections"] as const).map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={tab === value}
            className={tab === value ? "active" : ""}
            onClick={() => setTab(value)}
          >
            {value === "inbox" ? "统一收件箱" : value === "intelligence" ? `智能处理${candidates.length ? ` · ${candidates.length}` : ""}` : `连接中心 · ${connections.length}`}
          </button>
        ))}
      </div>

      {tab === "inbox" && (
        <InboxView
          filter={filter}
          onFilterChange={setFilter}
          search={search}
          onSearchChange={setSearch}
          onSearch={() => setSubmittedSearch(search.trim())}
          threads={threads}
          selectedThreadId={selectedThreadId}
          onSelectThread={openThread}
          detail={detail}
          selectedMessageId={selectedMessageId}
          onSelectMessage={setSelectedMessageId}
          selectedMessage={selectedMessage}
          content={contentQuery.data ?? ""}
          contentLoading={contentQuery.isLoading}
          onAnalyze={(messageId) => analyzeMutation.mutate(messageId)}
          analyzing={analyzeMutation.isPending}
          onCompose={startComposer}
          onOpenIntelligence={() => setTab("intelligence")}
          onRefresh={() => void queryClient.invalidateQueries({ queryKey: ["mailhub"] })}
          hasMore={!fixture && !submittedSearch && Boolean(threadsQuery.hasNextPage)}
          loadingMore={threadsQuery.isFetchingNextPage}
          onLoadMore={() => void threadsQuery.fetchNextPage()}
        />
      )}

      {tab === "intelligence" && (
        <IntelligenceView
          candidates={candidates}
          selected={selectedCandidates}
          onToggle={(candidateId) => setSelectedCandidates((current) => current.includes(candidateId) ? current.filter((id) => id !== candidateId) : [...current, candidateId])}
          onReview={(candidate, approved) => reviewMutation.mutate({ candidate, approved })}
          onBatchReview={(approved) => batchReviewMutation.mutate(approved)}
          onApply={(candidate) => applyMutation.mutate(candidate)}
          onReasonChange={setReviewReason}
          reviewReason={reviewReason}
          busy={reviewMutation.isPending || batchReviewMutation.isPending || applyMutation.isPending}
          error={reviewMutation.error || batchReviewMutation.error || applyMutation.error}
        />
      )}

      {tab === "connections" && (
        <ConnectionsView
          connections={connections}
          oauthProviders={oauthProvidersQuery.data ?? []}
          syncJobs={syncJobsQuery.data ?? []}
          policies={policiesQuery.data ?? []}
          delegations={delegationsQuery.data ?? []}
          autonomyRuns={autonomyQuery.data ?? []}
          onSync={(connectionId) => fixture ? undefined : syncMutation.mutate(connectionId)}
          onConnect={(provider) => {
            if (!fixture) oauthStartMutation.mutate({ provider });
          }}
          onReauthorize={(connection) => {
            if (!fixture && (connection.provider === "gmail" || connection.provider === "microsoft_graph")) {
              oauthStartMutation.mutate({
                provider: connection.provider,
                connectionId: connection.connection_id,
                expectedRevision: connection.revision,
              });
            }
          }}
          onBackfill={(input) => { if (!fixture) backfillMutation.mutate(input); }}
          onCancelSync={(jobId) => { if (!fixture) cancelSyncMutation.mutate(jobId); }}
          onEnqueueAutonomy={(connectionId) => { if (!fixture) autonomyEnqueueMutation.mutate(connectionId); }}
          onControlAutonomy={(runId, command, reason) => { if (!fixture) autonomyControlMutation.mutate({ runId, command, reason }); }}
           onExport={(includeContent) => { if (!fixture) exportMutation.mutate({ includeContent }); }}
           onRevoke={(connection) => {
             if (window.confirm(`确认撤销 ${connection.email_address} 的 Provider 访问？撤销后会停止新同步，但保留 MailHub 连接投影。`)) {
               if (!fixture) revokeMutation.mutate(connection);
             }
           }}
           onImpactPreview={(connection) => {
             if (!fixture) impactPreviewMutation.mutate({ connectionId: connection.connection_id });
           }}
           onUpdateScopes={(connection, grantedScopes) => {
             if (!fixture) scopeMutation.mutate({ connection, grantedScopes });
           }}
           onDelete={(connection) => {
            if (window.confirm(`撤销 ${connection.email_address}？这会停止同步并进入数据删除流程。`)) {
              if (!fixture) deleteMutation.mutate(connection);
            }
          }}
            impactPreview={impactPreviewMutation.data}
            impactPreviewBusy={impactPreviewMutation.isPending}
            scopeBusy={scopeMutation.isPending}
            busy={syncMutation.isPending || backfillMutation.isPending || cancelSyncMutation.isPending || revokeMutation.isPending || deleteMutation.isPending || impactPreviewMutation.isPending || scopeMutation.isPending || exportMutation.isPending || autonomyEnqueueMutation.isPending || autonomyControlMutation.isPending || oauthStartMutation.isPending || oauthCallbackMutation.isPending}
          exportBusy={exportMutation.isPending}
          fixture={fixture}
        />
      )}

      {showComposer && (
        <Composer
          value={composer}
          onChange={setComposer}
          onClose={() => setShowComposer(false)}
          onCreate={() => draftMutation.mutate()}
          onSend={(draft) => {
            if (window.confirm(`确认发送给 ${draft.recipient_addresses.join(", ")}？发送将写入 Provider，且不可由 MailHub 撤回。`)) {
              if (fixture) setShowComposer(false);
              else sendMutation.mutate(draft);
            }
          }}
          draft={draftMutation.data}
          creating={draftMutation.isPending}
          sending={sendMutation.isPending}
          error={draftMutation.error || sendMutation.error}
          fixture={fixture}
        />
      )}
    </div>
  );
}

function InboxView(props: {
  filter: InboxFilter;
  onFilterChange: (value: InboxFilter) => void;
  search: string;
  onSearchChange: (value: string) => void;
  onSearch: () => void;
  threads: MailHubThread[];
  selectedThreadId: string | null;
  onSelectThread: (thread: MailHubThread) => void;
  detail?: MailHubThreadDetail;
  selectedMessageId: string | null;
  onSelectMessage: (messageId: string) => void;
  selectedMessage?: MailHubMessage;
  content: string;
  contentLoading: boolean;
  onAnalyze: (messageId: string) => void;
  analyzing: boolean;
  onCompose: () => void;
  onOpenIntelligence: () => void;
  onRefresh: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const { detail } = props;
  const visibleThreads = props.threads.filter((thread) => {
    if (props.filter === "all") return true;
    if (props.filter === "unread") return thread.unread === true;
    if (props.filter === "important") return thread.important === true || thread.labels?.includes("important");
    if (props.filter === "attachments") return thread.has_attachment === true || thread.labels?.includes("attachment");
    if (props.filter === "projects") return Boolean(thread.project_hint) || thread.labels?.includes("project") || /项目|RFQ|审计/i.test(thread.normalized_subject);
    return (thread.candidate_count ?? 0) > 0;
  });
  return (
    <div className="mailhub-layout">
      <aside className="card mailhub-sidebar">
        <div className="mailhub-search-row">
          <label className="mailhub-search">
            <Icon name="search" size={16} />
            <input value={props.search} onChange={(event) => props.onSearchChange(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") props.onSearch(); }} placeholder="搜索主题、发件人或标签" aria-label="搜索邮件" />
          </label>
          <button className="btn btn-ghost btn-sm" type="button" onClick={props.onRefresh} aria-label="刷新邮件"><Icon name="arrow-right" size={15} /></button>
        </div>
        <div className="mailhub-filter-row" role="tablist" aria-label="邮件筛选">
          {(["all", "unread", "important", "attachments", "projects", "candidates"] as const).map((value) => (
            <button key={value} type="button" role="tab" aria-selected={props.filter === value} className={props.filter === value ? "active" : ""} onClick={() => props.onFilterChange(value)}>
              {value === "all" ? "全部" : value === "unread" ? "未读" : value === "important" ? "重要" : value === "attachments" ? "附件" : value === "projects" ? "项目" : "候选"}
            </button>
          ))}
        </div>
        <div className="mailhub-thread-list">
          {visibleThreads.map((thread) => (
            <button key={thread.thread_id} type="button" className={`mailhub-thread-row${props.selectedThreadId === thread.thread_id ? " selected" : ""}`} onClick={() => props.onSelectThread(thread)}>
              <span className="mailhub-thread-dot" aria-hidden="true" />
              <span className="mailhub-thread-copy">
                <strong>{thread.normalized_subject || "（无主题）"}</strong>
                <span>{thread.participant_addresses.slice(0, 2).join("、") || "未知参与者"}</span>
                <small>{thread.message_count} 封 · {formatDate(thread.latest_at)}</small>
              </span>
              <span className="mailhub-provider-mark">{thread.account_email ?? providerLabel(thread.connection_id)}</span>
            </button>
          ))}
          {!visibleThreads.length && <div className="empty-state">没有符合条件的邮件线程。</div>}
          {props.hasMore ? <button className="btn btn-ghost mailhub-load-more" type="button" disabled={props.loadingMore} onClick={props.onLoadMore}>{props.loadingMore ? "正在加载…" : "加载更多"}</button> : null}
        </div>
      </aside>

      <section className="card mailhub-detail" aria-live="polite">
        {!detail ? (
          <div className="mailhub-detail-empty"><Icon name="mail" size={34} /><h2>选择一个线程</h2><p>正文不会在列表中自动展开。选择线程后，按需读取经过治理的纯文本内容。</p></div>
        ) : (
          <>
            <div className="mailhub-detail-head">
              <div><span className="eyebrow">THREAD · {detail.thread.message_count} 封邮件</span><h2>{detail.thread.normalized_subject}</h2><p>{detail.thread.participant_addresses.join("、")}</p></div>
              <div className="mailhub-detail-actions"><button className="btn btn-ghost btn-sm" type="button" onClick={props.onOpenIntelligence}>查看候选</button><button className="btn btn-primary btn-sm" type="button" onClick={props.onCompose}><Icon name="send" size={14} /> 写回复</button></div>
            </div>
            <div className="mailhub-message-stack">
              {detail.messages.map((message) => (
                <article key={message.message_id} className={`mailhub-message${props.selectedMessageId === message.message_id ? " active" : ""}`}>
                  <button type="button" className="mailhub-message-head" onClick={() => props.onSelectMessage(message.message_id)} aria-expanded={props.selectedMessageId === message.message_id}>
                    <span className="mailhub-avatar">{message.sender_address.slice(0, 1).toUpperCase()}</span>
                    <span><strong>{message.sender_address}</strong><small>收件人：{message.recipient_addresses.join(", ")}{message.cc_addresses?.length ? ` · 抄送：${message.cc_addresses.join(", ")}` : ""}{message.bcc_addresses?.length ? ` · 密送：${message.bcc_addresses.join(", ")}` : ""}{message.reply_to_addresses?.length ? ` · 回复地址：${message.reply_to_addresses.join(", ")}` : ""} · {formatDate(message.received_at)}</small></span>
                    <span className="mailhub-message-meta">{message.attachment_count ? `附件 ${message.attachment_count}` : ""}</span>
                  </button>
                  {props.selectedMessageId === message.message_id && (
                    <div className="mailhub-message-body">
                      {props.contentLoading ? <div role="status">正在读取受控正文…</div> : <pre>{props.content || "正文不可用或已过期。"}</pre>}
                      <div className="mailhub-message-foot"><span className="meta">来源定位：{sourceLocator(message)}</span><button className="btn btn-ghost btn-sm" type="button" disabled={props.analyzing} onClick={() => props.onAnalyze(message.message_id)}>{props.analyzing ? "分析中…" : "生成候选"}</button></div>
                    </div>
                  )}
                </article>
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function IntelligenceView(props: {
  candidates: MailHubCandidate[];
  selected: string[];
  onToggle: (candidateId: string) => void;
  onReview: (candidate: MailHubCandidate, approved: boolean) => void;
  onBatchReview: (approved: boolean) => void;
  onApply: (candidate: MailHubCandidate) => void;
  reviewReason: string;
  onReasonChange: (value: string) => void;
  busy: boolean;
  error: unknown;
}) {
  return (
    <section className="mailhub-intelligence">
      <div className="mailhub-panel-head"><div><span className="eyebrow">HUMAN REVIEW · L2</span><h2>智能处理中心</h2><p>候选只包含摘要、来源引用和目标差异；批准后才交给项目/知识宿主。</p></div><div className="mailhub-bulk-actions"><input className="input" value={props.reviewReason} onChange={(event) => props.onReasonChange(event.target.value)} placeholder="批量驳回原因（可选）" aria-label="批量审核原因" /><button className="btn btn-ghost btn-sm" type="button" disabled={!props.selected.length || props.busy} onClick={() => props.onBatchReview(false)}>批量驳回</button><button className="btn btn-primary btn-sm" type="button" disabled={!props.selected.length || props.busy} onClick={() => props.onBatchReview(true)}>批量批准</button></div></div>
      {Boolean(props.error) && <div className="safety-banner" role="alert">审核未完成，系统保留原候选状态。</div>}
      <div className="mailhub-approval-ref"><span className="muted">点击执行后，CAPlatform 会在服务端签发一次性、作用域绑定的审批凭据；浏览器和邮件正文都不能生成或覆盖该凭据。</span></div>
      <div className="mailhub-candidate-grid">
        {props.candidates.map((candidate) => (
          <article className="card mailhub-candidate" key={candidate.candidate_id}>
            <div className="mailhub-candidate-head"><label><input type="checkbox" checked={props.selected.includes(candidate.candidate_id)} onChange={() => props.onToggle(candidate.candidate_id)} aria-label={`选择 ${candidate.title ?? candidate.candidate_id}`} /><span className={`badge ${candidate.candidate_type === "knowledge" ? "badge-info" : "badge-accent"}`}>{candidate.candidate_type === "knowledge" ? "知识" : candidate.candidate_type === "task" ? "任务" : "项目"}</span></label><span className="mono-ref">v{candidate.revision} · {candidate.confidence == null ? "置信度待校准" : `${Math.round(candidate.confidence * 100)}%`}</span></div>
            <h3>{candidate.title ?? "未命名候选"}</h3><p>{candidate.summary ?? "没有可展示的摘要。"}</p>
            <div className="mailhub-candidate-target"><strong>目标差异</strong><pre>{JSON.stringify(candidate.target ?? {}, null, 2)}</pre></div>
            <div className="mailhub-candidate-source"><strong>来源引用</strong>{(candidate.source_refs ?? []).map((source, index) => <code key={index}>{JSON.stringify(source)}</code>)}</div>
            {candidate.risk_flags?.length ? <div className="mailhub-risk-list">{candidate.risk_flags.map((flag) => <span key={flag} className="badge badge-warning">{flag}</span>)}</div> : null}
            <div className="mailhub-candidate-foot"><button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || candidate.state !== "proposed"} onClick={() => props.onReview(candidate, false)}>驳回</button><button className="btn btn-primary btn-sm" type="button" disabled={props.busy || candidate.state !== "proposed"} onClick={() => props.onReview(candidate, true)}>批准交给宿主</button>{candidate.state === "approved" && <button className="btn btn-danger btn-sm" type="button" disabled={props.busy} onClick={() => props.onApply(candidate)}>确认并执行写入</button>}{candidate.state !== "proposed" && <span className="badge badge-info">{candidate.state === "approved" ? "已批准，等待执行" : candidate.state}</span>}</div>
          </article>
        ))}
        {!props.candidates.length && <div className="card empty-state">当前没有待审核候选。同步新邮件或在消息详情中运行分析。</div>}
      </div>
    </section>
  );
}

function ConnectionsView(props: {
  connections: MailHubConnection[];
  oauthProviders: MailHubOAuthProvider[];
  syncJobs: MailHubSyncJob[];
  policies: MailHubAgentPolicy[];
  delegations: MailHubDelegation[];
  autonomyRuns: MailHubAutonomyRun[];
  onSync: (connectionId: string) => void;
  onConnect: (provider: "gmail" | "microsoft_graph") => void;
  onReauthorize: (connection: MailHubConnection) => void;
  onBackfill: (input: {
    connectionId: string;
    folderRef: string;
    labelRefs: string[];
    receivedAfter?: string;
    receivedBefore?: string;
  }) => void;
  onCancelSync: (jobId: string) => void;
  onEnqueueAutonomy: (connectionId: string) => void;
  onControlAutonomy: (runId: string, command: "run" | "pause" | "resume" | "cancel", reason?: string) => void;
  onExport: (includeContent: boolean) => void;
  onRevoke: (connection: MailHubConnection) => void;
  onImpactPreview: (connection: MailHubConnection) => void;
  onUpdateScopes: (connection: MailHubConnection, grantedScopes: string[]) => void;
  onDelete: (connection: MailHubConnection) => void;
  impactPreview?: MailHubConnectionImpactPreview;
  impactPreviewBusy: boolean;
  scopeBusy: boolean;
  busy: boolean;
  exportBusy: boolean;
  fixture: boolean;
}) {
  const [backfillConnectionId, setBackfillConnectionId] = useState("");
  const [backfillFolder, setBackfillFolder] = useState("INBOX");
  const [backfillLabels, setBackfillLabels] = useState("");
  const [backfillAfter, setBackfillAfter] = useState("");
  const [backfillBefore, setBackfillBefore] = useState("");
  const [scopeDrafts, setScopeDrafts] = useState<Record<string, string>>({});
  const selectedConnection = props.connections.find(
    (connection) => connection.connection_id === backfillConnectionId,
  ) ?? props.connections[0];
  const selectBackfillConnection = (connectionId: string) => {
    setBackfillConnectionId(connectionId);
    const connection = props.connections.find((item) => item.connection_id === connectionId);
    if (connection?.provider === "microsoft_graph") setBackfillLabels("");
  };
  const normalizeDateTime = (value: string): string | undefined => {
    if (!value.trim()) return undefined;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return undefined;
    return parsed.toISOString();
  };
  const submitBackfill = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedConnection) return;
    props.onBackfill({
      connectionId: selectedConnection.connection_id,
      folderRef: backfillFolder.trim() || "INBOX",
      labelRefs: backfillLabels.split(/[;,\n]/).map((item) => item.trim()).filter(Boolean),
      receivedAfter: normalizeDateTime(backfillAfter),
      receivedBefore: normalizeDateTime(backfillBefore),
    });
  };
  const submitScopes = (event: FormEvent<HTMLFormElement>, connection: MailHubConnection) => {
    event.preventDefault();
    const raw = scopeDrafts[connection.connection_id] ?? connection.granted_scopes.join(", ");
    const grantedScopes = raw.split(/[;,\n]/).map((scope) => scope.trim()).filter(Boolean);
    if (!grantedScopes.length) return;
    props.onUpdateScopes(connection, grantedScopes);
  };
  return (
    <section className="mailhub-connections">
      <div className="mailhub-panel-head"><div><span className="eyebrow">CONNECTION CENTER · OAUTH / IMAP</span><h2>连接与同步健康</h2><p>授权范围、内容模式和删除影响在此确认。未完成真实 Provider 验证的能力保持不可用。</p></div><div className="mailhub-bulk-actions"><span className="badge badge-success">{props.connections.length} 个账号</span><button className="btn btn-ghost btn-sm" type="button" disabled={props.fixture || props.exportBusy} onClick={() => props.onExport(false)}>{props.exportBusy ? "导出中…" : "导出元数据"}</button><button className="btn btn-ghost btn-sm" type="button" disabled={props.fixture || props.exportBusy} onClick={() => { if (window.confirm("导出将包含当前主体可读取的邮件正文；请确认已具备安全保存和清理条件。")) props.onExport(true); }}>导出含正文</button></div></div>
      <section className="card mailhub-oauth-card" aria-labelledby="mailhub-oauth-title">
        <div className="mailhub-panel-head"><div><span className="eyebrow">HOST-CONFIGURED OAUTH · READ ONLY</span><h3 id="mailhub-oauth-title">连接新的邮箱</h3><p>授权地址、client id、redirect 和 scope 由宿主服务端配置；浏览器不会提交或接收 Provider 凭据。</p></div><span className="badge badge-warning">首期只读</span></div>
        <div className="mailhub-oauth-provider-grid">
          {props.oauthProviders.map((provider) => <div className="mailhub-oauth-provider" key={provider.provider}><div><strong>{provider.display_name}</strong><p>{provider.read_only ? "仅 Mail.Read / Gmail readonly" : "按宿主策略"} · {provider.scopes.length ? `${provider.scopes.length} 个 scope` : "未配置 scope"}</p></div><button className="btn btn-primary btn-sm" type="button" disabled={props.busy || props.fixture || !provider.enabled || !provider.ready} onClick={() => props.onConnect(provider.provider)}>{!provider.enabled ? "未启用" : !provider.ready ? "配置未就绪" : props.busy ? "准备中…" : "开始授权"}</button></div>)}
          {!props.oauthProviders.length && <div className="empty-state">OAuth 提供方配置未就绪；请由部署者完成 Provider preflight 后再启用。</div>}
        </div>
      </section>
      <div className="mailhub-connection-grid">
        {props.connections.map((connection) => (
          <article className="card mailhub-connection" key={connection.connection_id}>
            <div className="mailhub-connection-head"><span className="mailhub-provider-logo">{providerShort(connection.provider)}</span><div><h3>{connection.email_address}</h3><p>{providerLabel(connection.provider)} · revision {connection.revision}</p></div><span className={`badge ${props.fixture ? "badge-warning" : connection.status === "active" ? "badge-success" : "badge-warning"}`}>{props.fixture ? "演示连接 · 未验证" : connection.status === "active" ? "已连接" : connection.status}</span></div>
             <dl className="mailhub-connection-facts"><div><dt>内容模式</dt><dd>{connection.content_mode === "metadata_only" ? "仅元数据" : "受限处理"}</dd></div><div><dt>OAuth scope</dt><dd>{connection.granted_scopes.join(" · ") || "未显示"}</dd></div><div><dt>同步健康</dt><dd>{connection.sync_state === "healthy" ? "正常" : connection.sync_state ?? "待检查"}</dd></div><div><dt>最近同步</dt><dd>{connection.last_sync_at ? formatDate(connection.last_sync_at) : "尚未同步"}</dd></div></dl>
             {connection.error_code && <div className="safety-banner">{connection.error_code} · 需要重新授权或查看运行手册。</div>}
             <form className="mailhub-scope-form" onSubmit={(event) => submitScopes(event, connection)}><label htmlFor={`mailhub-scope-${connection.connection_id}`}>本地可用 scope（仅可收窄）</label><div><input id={`mailhub-scope-${connection.connection_id}`} className="input" value={scopeDrafts[connection.connection_id] ?? connection.granted_scopes.join(", ")} onChange={(event) => setScopeDrafts((current) => ({ ...current, [connection.connection_id]: event.target.value }))} aria-describedby={`mailhub-scope-help-${connection.connection_id}`} /><button className="btn btn-ghost btn-sm" type="submit" disabled={props.busy || props.fixture || props.scopeBusy}>收窄本地权限</button></div><span id={`mailhub-scope-help-${connection.connection_id}`} className="muted">用逗号分隔；扩权会被服务端拒绝并要求重新授权。</span></form>
             <div className="mailhub-connection-foot"><button className="btn btn-primary btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onSync(connection.connection_id)}>立即增量同步</button><button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => selectBackfillConnection(connection.connection_id)}>设置有界回填</button>{(connection.status !== "active" || connection.error_code) && (connection.provider === "gmail" || connection.provider === "microsoft_graph") ? <button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onReauthorize(connection)}>重新授权</button> : null}<button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onImpactPreview(connection)}>{props.impactPreviewBusy && props.impactPreview?.connection.connection_id === connection.connection_id ? "预览中…" : "预览影响"}</button><button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture || connection.status !== "active"} onClick={() => props.onRevoke(connection)}>仅撤销访问</button><button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onDelete(connection)}>撤销并删除</button></div>
             {props.impactPreview?.connection.connection_id === connection.connection_id ? <ConnectionImpactPreview preview={props.impactPreview} /> : null}
           </article>
        ))}
        {!props.connections.length && <div className="card empty-state">尚未连接邮箱。部署 MailHub 后从 OAuth 向导开始。</div>}
      </div>
      {selectedConnection ? (
        <section className="card mailhub-backfill-card" aria-labelledby="mailhub-backfill-title">
          <div className="mailhub-panel-head"><div><span className="eyebrow">BOUNDED BACKFILL · READ ONLY</span><h3 id="mailhub-backfill-title">按范围补偿同步</h3><p>只允许文件夹、标签和日期边界；此作业不会读取或推进增量 cursor。日期按本地输入并在提交时标准化为 UTC。</p></div><span className="badge badge-info">{selectedConnection.email_address}</span></div>
          <form className="mailhub-backfill-form" onSubmit={submitBackfill}>
            <label>账号<select className="input" value={selectedConnection.connection_id} onChange={(event) => selectBackfillConnection(event.target.value)}>{props.connections.map((connection) => <option value={connection.connection_id} key={connection.connection_id}>{connection.email_address} · {providerLabel(connection.provider)}</option>)}</select></label>
            <label>文件夹 / folder ref<input className="input" value={backfillFolder} onChange={(event) => setBackfillFolder(event.target.value)} placeholder="INBOX 或 Provider folder id" maxLength={200} /></label>
            <label>标签 / category（逗号分隔）<input className="input" value={backfillLabels} onChange={(event) => setBackfillLabels(event.target.value)} placeholder="Gmail label；Graph 使用 category:Name" maxLength={4096} /></label>
            <label>接收时间起点<input className="input" type="datetime-local" value={backfillAfter} onChange={(event) => setBackfillAfter(event.target.value)} /></label>
            <label>接收时间终点<input className="input" type="datetime-local" value={backfillBefore} onChange={(event) => setBackfillBefore(event.target.value)} /></label>
            <div className="mailhub-backfill-actions"><button className="btn btn-primary btn-sm" type="submit" disabled={props.busy || props.fixture}>排队 backfill</button><span className="muted">不支持的 Provider 过滤条件会 fail closed。</span></div>
          </form>
        </section>
      ) : null}
      <section className="card mailhub-sync-jobs" aria-labelledby="mailhub-sync-jobs-title">
        <div className="mailhub-panel-head"><div><span className="eyebrow">DURABLE JOBS · CURSOR SAFE</span><h3 id="mailhub-sync-jobs-title">同步作业与进度</h3><p>状态来自服务端 durable job；queued/running/cancelling 都不是完成，取消请求会保留 Worker lease 并在安全检查点结束。</p></div><span className="badge badge-info">{props.syncJobs.length}</span></div>
        {props.syncJobs.length ? <div className="mailhub-sync-job-list">{props.syncJobs.slice(0, 12).map((job) => <div className="mailhub-sync-job-row" key={job.job_id}><div><strong>{job.mode === "backfill" ? "有界回填" : job.mode === "reconcile" ? "对账" : "增量"} · {job.status}</strong><p>{job.folder_ref ?? "INBOX"}{job.label_refs?.length ? ` · ${job.label_refs.length} 个标签` : ""}{job.received_after ? ` · ${formatDate(job.received_after)}` : ""}{job.received_before ? ` → ${formatDate(job.received_before)}` : ""}</p>{job.error_code ? <span className="badge badge-warning">{job.error_code}</span> : null}<span className="muted">抓取 {job.fetched_count ?? 0} · 保存 {job.saved_count ?? 0} · 重复 {job.duplicate_count ?? 0} · 删除 {job.deleted_count ?? 0}</span></div>{["queued", "running"].includes(job.status) ? <button className="btn btn-danger btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onCancelSync(job.job_id)}>取消</button> : job.status === "cancelling" ? <span className="badge badge-warning">取消中</span> : null}</div>)}</div> : <div className="mailhub-autonomy-empty">暂无同步作业。增量同步和有界回填都会先持久化再交给 Worker。</div>}
      </section>
      <section className="card mailhub-autonomy-card">
        <div className="mailhub-panel-head">
          <div><span className="eyebrow">AGENT RUN · L0/L1</span><h3>自主处理运行</h3><p>只执行同步、去重、摘要和候选生成；项目/知识写入、草稿和发信仍需人工审核。</p></div>
          <button className="btn btn-primary btn-sm" type="button" disabled={props.fixture || props.busy || !props.connections.length} onClick={() => props.onEnqueueAutonomy(props.connections[0].connection_id)}>启动推荐运行</button>
        </div>
        {props.autonomyRuns.length ? <div className="mailhub-autonomy-list">{props.autonomyRuns.slice(0, 6).map((run) => <div className="mailhub-autonomy-row" key={run.run_id}><div><strong>{run.status}</strong><p>{run.connection_id} · {run.message_ids?.length ?? 0} 封 · {run.candidate_ids?.length ?? 0} 个候选</p>{run.error_code && <span className="badge badge-warning">{run.error_code}</span>}</div><div className="mailhub-autonomy-actions">{run.status === "queued" && <><button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onControlAutonomy(run.run_id, "run")}>立即运行</button><button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onControlAutonomy(run.run_id, "pause", "owner_paused_from_mailhub_ui")}>暂停</button></>}{run.status === "paused" && <button className="btn btn-ghost btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onControlAutonomy(run.run_id, "resume")}>恢复</button>}{(run.status === "queued" || run.status === "paused" || run.status === "running") && <button className="btn btn-danger btn-sm" type="button" disabled={props.busy || props.fixture} onClick={() => props.onControlAutonomy(run.run_id, "cancel")}>取消</button>}</div></div>)}</div> : <div className="mailhub-autonomy-empty">{props.fixture ? "演示环境不启动 Agent 运行；真实部署后可在此排队并监控。" : "暂无运行。启动后会先进入 queued，再由独立 Worker 执行。"}</div>}
      </section>
      <div className="mailhub-authorization-grid">
        <section className="card mailhub-authorization-card">
          <div className="mailhub-panel-head"><div><span className="eyebrow">AGENT POLICY</span><h3>Agent 策略</h3></div><span className="badge badge-info">{props.policies.length}</span></div>
          {props.policies.map((policy) => <div className="mailhub-authorization-row" key={policy.policy_id}><div><strong>{policy.enabled ? "已启用" : "已停用"} · {policy.allowed_automation_level}</strong><p>{policy.allowed_actions.join(" / ")} · 每小时 {policy.max_per_hour} · 每日 {policy.max_per_day}</p></div><span className="badge badge-warning">revision {policy.revision}</span></div>)}
          {!props.policies.length && <p className="muted">当前主体没有可管理策略。</p>}
        </section>
        <section className="card mailhub-authorization-card">
          <div className="mailhub-panel-head"><div><span className="eyebrow">DELEGATION</span><h3>Agent 授权</h3></div><span className="badge badge-info">{props.delegations.length}</span></div>
          {props.delegations.map((grant) => <div className="mailhub-authorization-row" key={grant.grant_id}><div><strong>{grant.agent_subject_id}</strong><p>{grant.capability_ids.join(" / ")} · 到期 {new Date(grant.expires_at).toLocaleDateString("zh-CN")}</p></div><span className={`badge ${grant.revoked_at ? "badge-warning" : "badge-success"}`}>{grant.revoked_at ? "已撤销" : "有效"}</span></div>)}
          {!props.delegations.length && <p className="muted">当前主体没有签发的授权。</p>}
        </section>
      </div>
      <div className="mailhub-connection-note"><Icon name="shield" size={17} /><span>浏览器只持有 CAPlatform 会话；Provider credential_ref、refresh token 和原始正文不会进入前端、普通日志或 Agent memory。</span></div>
    </section>
  );
}

function ConnectionImpactPreview({ preview }: { preview: MailHubConnectionImpactPreview }) {
  const count = (key: string): number => {
    const value = preview.counts[key];
    return typeof value === "number" ? value : 0;
  };
  return (
    <section className="mailhub-impact-preview" aria-label="连接影响预览" role="status">
      <div className="mailhub-impact-preview-head">
        <div><strong>本地影响预览</strong><span>projection-only · 不查询 Provider</span></div>
        <span className="badge badge-warning">远端变化未知</span>
      </div>
      <div className="mailhub-impact-preview-grid">
        <span>消息 {count("messages")}</span>
        <span>线程 {count("threads")}</span>
        <span>候选 {count("candidates")}</span>
        <span>草稿 {count("drafts")}</span>
        <span>同步作业 {count("sync_jobs")}</span>
        <span>对象引用 {count("governed_object_refs_estimate")}</span>
      </div>
      <p>文件夹水位：{preview.scope.current_folder_refs.join("、") || "未记录"}；远端消息变化估算：未知。</p>
      {preview.project_hints.length ? <p>项目提示：{preview.project_hints.join("、")}</p> : null}
      <ul>{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
    </section>
  );
}

function Composer(props: {
  value: { to: string; cc: string; subject: string; body: string };
  onChange: (value: { to: string; cc: string; subject: string; body: string }) => void;
  onClose: () => void;
  onCreate: () => void;
  onSend: (draft: MailHubDraft) => void;
  draft?: MailHubDraft;
  creating: boolean;
  sending: boolean;
  error: unknown;
  fixture: boolean;
}) {
  const set = (key: keyof typeof props.value, value: string) => props.onChange({ ...props.value, [key]: value });
  const ready = Boolean(props.value.to.trim() && props.value.subject.trim() && props.value.body.trim());
  return (
    <div className="mailhub-composer-backdrop" role="presentation">
      <section className="card mailhub-composer" role="dialog" aria-modal="true" aria-labelledby="mailhub-composer-title">
        <div className="mailhub-panel-head"><div><span className="eyebrow">DRAFT · EXPLICIT CONFIRMATION</span><h2 id="mailhub-composer-title">受控回复草稿</h2><p>保存草稿不会发送；发送前会重新绑定 revision、内容 digest 和收件人 digest。</p></div><button className="btn btn-ghost btn-sm" type="button" onClick={props.onClose}>关闭</button></div>
        <label>收件人<input className="input" value={props.value.to} onChange={(event) => set("to", event.target.value)} placeholder="name@example.com，多个地址用逗号分隔" /></label>
        <label>抄送（可选）<input className="input" value={props.value.cc} onChange={(event) => set("cc", event.target.value)} /></label>
        <label>主题<input className="input" value={props.value.subject} onChange={(event) => set("subject", event.target.value)} /></label>
        <label>正文<textarea className="input mailhub-composer-body" value={props.value.body} onChange={(event) => set("body", event.target.value)} /></label>
        {Boolean(props.error) && <div className="safety-banner" role="alert">草稿或发送未完成；不假定 Provider 已接受。</div>}
        {props.draft && <div className="mailhub-draft-receipt"><strong>草稿已保存 · revision {props.draft.revision}</strong><span>内容 {props.draft.content_sha256.slice(0, 12)}… · 状态 {props.draft.status}</span><button className="btn btn-danger btn-sm" type="button" disabled={props.sending} onClick={() => props.onSend(props.draft!)}>{props.sending ? "提交中…" : props.fixture ? "演示发送" : "确认发送"}</button></div>}
        <div className="mailhub-composer-foot"><button className="btn btn-ghost" type="button" onClick={props.onClose}>取消</button><button className="btn btn-primary" type="button" disabled={!ready || props.creating || Boolean(props.draft)} onClick={props.onCreate}>{props.creating ? "保存中…" : "保存为草稿"}</button></div>
      </section>
    </div>
  );
}

function fixtureDetail(threadId: string): MailHubThreadDetail {
  const thread = fixtureThreads.find((item) => item.thread_id === threadId) ?? fixtureThreads[0];
  return { thread, messages: fixtureMessages[thread.thread_id] ?? [], content_policy: "plain_text_endpoint_only" };
}

function threadRowsFromMessages(messages: MailHubMessage[]): MailHubThread[] {
  const grouped = new Map<string, MailHubMessage[]>();
  for (const message of messages) grouped.set(message.thread_id, [...(grouped.get(message.thread_id) ?? []), message]);
  return [...grouped.entries()].map(([threadId, items]) => ({
    thread_id: threadId,
    connection_id: items[0].connection_id,
    normalized_subject: items[0].subject,
    participant_addresses: [...new Set(items.flatMap((item) => [
      item.sender_address,
      ...item.recipient_addresses,
      ...(item.cc_addresses ?? []),
      ...(item.bcc_addresses ?? []),
      ...(item.reply_to_addresses ?? []),
    ]))],
    latest_at: items.reduce((latest, item) => item.received_at > latest ? item.received_at : latest, items[0].received_at),
    message_count: items.length,
    revision: 1,
  }));
}

function splitAddresses(value: string): string[] {
  return value.split(/[;,\n]/).map((item) => item.trim()).filter(Boolean);
}

function recipientDigest(draft: MailHubDraft): string {
  // The server returns the canonical SHA-256 digest with the draft.  Never
  // reconstruct a subtly different recipient order in the browser.
  return draft.recipient_digest ?? "";
}

function sourceLocator(message: MailHubMessage): string {
  if (!message.source_locator) return "governed message projection";
  return Object.entries(message.source_locator).map(([key, value]) => `${key}=${String(value)}`).join(" · ");
}

function providerShort(provider: string): string {
  if (provider.toLowerCase().includes("microsoft") || provider.toLowerCase().includes("graph")) return "M";
  if (provider.toLowerCase().includes("imap")) return "I";
  return "G";
}

function providerLabel(providerOrConnection: string): string {
  const value = providerOrConnection.toLowerCase();
  if (value.includes("microsoft") || value.includes("graph") || value.includes("fixture-conn-graph")) return "Microsoft Graph";
  if (value.includes("imap")) return "IMAP/SMTP";
  return value.includes("gmail") || value.includes("fixture-conn-gmail") ? "Gmail" : "邮箱账号";
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}
