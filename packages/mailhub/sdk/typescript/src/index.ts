export type MailHubError = {
  status: number;
  code: string;
  message: string;
  details?: unknown;
};

export type MailHubClientOptions = {
  baseUrl: string;
  tenantId: string;
  subjectId: string;
  fetchImpl?: typeof fetch;
};

export type DraftCreateInput = {
  connectionId: string;
  threadId?: string | null;
  recipientAddresses: string[];
  ccAddresses?: string[];
  bccAddresses?: string[];
  attachmentRefs?: string[];
  subject: string;
  bodyText: string;
};

export type DraftSendInput = {
  expectedRevision: number;
  expectedContentSha256: string;
  expectedRecipientDigest: string;
  confirmationRef: string;
  policyId?: string | null;
  grantId?: string | null;
  agentSubjectId?: string | null;
};

export type CandidateReviewInput = {
  approved: boolean;
  expectedRevision: number;
  reviewReason?: string | null;
};

export type CandidateApplyInput = {
  approvalRef: string;
};

export type CandidateRevokeInput = {
  expectedRevision: number;
  reason: string;
};

export type CandidateListOptions = {
  status?: string;
  candidateType?: "project" | "knowledge" | "task";
  limit?: number;
};

export type RuleExecuteInput = {
  messageIds: string[];
  dryRun?: boolean;
  policyId?: string | null;
  grantId?: string | null;
};

export type AutonomyRunInput = {
  connectionId: string;
  replayKey: string;
  limit?: number;
  messageLimit?: number;
};

export type SyncJobInput = {
  mode?: "incremental" | "backfill" | "reconcile";
  limit?: number;
  folderRef?: string;
  labelRefs?: string[];
  receivedAfter?: string | null;
  receivedBefore?: string | null;
};

function normalizeSyncDate(value: string | null | undefined, field: string): string | null {
  if (value == null) return null;
  if (!value.trim() || value.length > 80 || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) {
    throw new Error(`mailhub_sync_${field}_invalid`);
  }
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) throw new Error(`mailhub_sync_${field}_invalid`);
  return new Date(parsed).toISOString();
}

export type AutonomyCommand = "run" | "pause" | "resume" | "cancel";

export type SubscriptionEnsureInput = {
  callbackEndpoint: string;
  desiredExpiry: string;
  folderRef?: string;
  clientStateRef?: string | null;
};

export type SubscriptionRenewInput = {
  folderRef?: string;
  renewalWindowHours?: number;
  desiredExpiry?: string | null;
};

export type WebhookReceiptReplayInput = {
  expectedBodySha256: string;
  reason: string;
};

export type OAuthAuthorizeInput = {
  authorizationEndpoint: string;
  clientId: string;
  redirectUri: string;
  scopes: string[];
  extraParameters?: Record<string, string>;
  connectionId?: string;
  expectedRevision?: number;
};

export type OAuthCallbackInput = {
  state: string;
  code: string;
  redirectUri: string;
};

export type MailHubThreadListOptions = {
  limit?: number;
  cursor?: string | null;
  connectionId?: string;
  unread?: boolean;
  important?: boolean;
  attachment?: boolean;
  project?: boolean;
  candidate?: boolean;
};

export type MailHubThreadPage = {
  data: Array<Record<string, unknown>>;
  next_cursor: string | null;
  has_more: boolean;
  filters?: Record<string, unknown>;
};

export type MailHubConnectionImpactPreview = {
  schema_version: string;
  connection: Record<string, unknown>;
  mode: string;
  provider_query_performed: boolean;
  scope: Record<string, unknown>;
  counts: Record<string, unknown>;
  project_hints: string[];
  deletion_effects: Record<string, unknown>;
  warnings: string[];
};

export type MailHubSearchMode = "metadata" | "provider";

export type MailHubSearchResponse = {
  data: Array<Record<string, unknown>>;
  mode: MailHubSearchMode;
  complete: boolean;
  coverage: string[];
  incomplete_reason?: string;
};

/** Provider-neutral browser/server client. Provider tokens never enter this API. */
export class MailHubClient {
  private readonly baseUrl: string;
  private readonly headers: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(options: MailHubClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.headers = {
      "X-MailHub-Tenant": options.tenantId,
      "X-MailHub-Subject": options.subjectId,
    };
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!path.startsWith("/") || path.includes("//")) throw new Error("mailhub_path_invalid");
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      ...init,
      redirect: "error",
      headers: { ...this.headers, ...(init.headers ?? {}) },
    });
    const payload = (await response.json()) as T & { error?: { code?: string; message?: string; details?: unknown } };
    if (!response.ok) {
      throw { status: response.status, code: payload.error?.code ?? "http_error", message: payload.error?.message ?? "MailHub request failed", details: payload.error?.details } satisfies MailHubError;
    }
    return payload;
  }

  listConnections(): Promise<unknown> { return this.request("/v1/mail/connections"); }
  providerHealth(): Promise<unknown> { return this.request("/v1/mail/admin/provider-health"); }
  listAudit(limit = 100): Promise<unknown> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) throw new Error("mailhub_audit_limit_invalid");
    return this.request(`/v1/mail/audit?limit=${limit}`);
  }
  updateConnectionScopes(connectionId: string, expectedRevision: number, grantedScopes: string[]): Promise<unknown> {
    if (!Number.isInteger(expectedRevision) || expectedRevision < 1) throw new Error("mailhub_connection_revision_invalid");
    if (grantedScopes.length < 1 || grantedScopes.length > 40 || grantedScopes.some((scope) => !scope.trim() || scope.trim().length > 200 || /[\u0000-\u001f\u007f]/.test(scope))) throw new Error("mailhub_connection_scopes_invalid");
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}:scopes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision, granted_scopes: grantedScopes }),
    });
  }
  refreshConnection(connectionId: string, expectedRevision: number, reason = "manual_refresh"): Promise<unknown> {
    if (!Number.isInteger(expectedRevision) || expectedRevision < 1) throw new Error("mailhub_connection_revision_invalid");
    if (!reason.trim() || reason.length > 500) throw new Error("mailhub_connection_refresh_reason_invalid");
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}:refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision, reason }),
    });
  }
  beginOAuth(provider: "gmail" | "microsoft_graph", input: OAuthAuthorizeInput): Promise<unknown> {
    if (!input.authorizationEndpoint.trim() || input.authorizationEndpoint.length > 2000) throw new Error("mailhub_oauth_endpoint_invalid");
    if (!input.clientId.trim() || input.clientId.length > 512) throw new Error("mailhub_oauth_client_id_invalid");
    if (!input.redirectUri.trim() || input.redirectUri.length > 2000) throw new Error("mailhub_oauth_redirect_uri_invalid");
    if (input.scopes.length < 1 || input.scopes.length > 20 || input.scopes.some((scope) => !scope.trim())) throw new Error("mailhub_oauth_scopes_invalid");
    if (input.expectedRevision !== undefined && (!Number.isInteger(input.expectedRevision) || input.expectedRevision < 1 || !input.connectionId)) throw new Error("mailhub_oauth_expected_revision_invalid");
    return this.request(`/v1/mail/oauth/${provider}:authorize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        authorization_endpoint: input.authorizationEndpoint,
        client_id: input.clientId,
        redirect_uri: input.redirectUri,
        scopes: input.scopes,
        extra_parameters: input.extraParameters ?? {},
        connection_id: input.connectionId ?? null,
        expected_revision: input.expectedRevision ?? null,
      }),
    });
  }
  completeOAuth(provider: "gmail" | "microsoft_graph", input: OAuthCallbackInput): Promise<unknown> {
    if (input.state.length < 20 || input.state.length > 4000 || !input.code.trim()) throw new Error("mailhub_oauth_callback_invalid");
    if (!input.redirectUri.trim() || input.redirectUri.length > 2000) throw new Error("mailhub_oauth_redirect_uri_invalid");
    return this.request(`/v1/mail/oauth/${provider}:callback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: input.state, code: input.code, redirect_uri: input.redirectUri }),
    });
  }
  listSyncStates(connectionId: string): Promise<unknown> {
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}/sync-state`);
  }
  connectionImpactPreview(connectionId: string, folderRefs: string[] = []): Promise<MailHubConnectionImpactPreview> {
    if (!connectionId.trim()) throw new Error("mailhub_connection_id_invalid");
    if (folderRefs.length > 50 || folderRefs.some((value) => !value.trim() || value.length > 200 || /[\u0000-\u001f\u007f]/.test(value))) {
      throw new Error("mailhub_impact_preview_folder_refs_invalid");
    }
    const params = new URLSearchParams();
    for (const value of folderRefs) params.append("folder_ref", value.trim());
    const query = params.toString();
    return this.request<MailHubConnectionImpactPreview>(
      `/v1/mail/connections/${encodeURIComponent(connectionId)}/impact-preview${query ? `?${query}` : ""}`,
    );
  }
  ensureSubscription(connectionId: string, input: SubscriptionEnsureInput, idempotencyKey: string): Promise<unknown> {
    if (!input.callbackEndpoint.trim() || input.callbackEndpoint.length > 2000) throw new Error("mailhub_subscription_callback_invalid");
    if (!idempotencyKey.trim() || idempotencyKey.length > 300) throw new Error("mailhub_subscription_idempotency_key_invalid");
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}/subscription:ensure`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        callback_endpoint: input.callbackEndpoint,
        desired_expiry: input.desiredExpiry,
        folder_ref: input.folderRef ?? "INBOX",
        client_state_ref: input.clientStateRef ?? null,
      }),
    });
  }
  renewSubscription(connectionId: string, input: SubscriptionRenewInput = {}): Promise<unknown> {
    const windowHours = input.renewalWindowHours ?? 24;
    if (!Number.isInteger(windowHours) || windowHours < 1 || windowHours > 168) throw new Error("mailhub_subscription_renewal_window_invalid");
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}/subscription:renew`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder_ref: input.folderRef ?? "INBOX",
        renewal_window_hours: windowHours,
        desired_expiry: input.desiredExpiry ?? null,
      }),
    });
  }
  cancelSubscription(connectionId: string, idempotencyKey: string, folderRef = "INBOX"): Promise<unknown> {
    if (!idempotencyKey.trim() || idempotencyKey.length > 300) throw new Error("mailhub_subscription_idempotency_key_invalid");
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}/subscription:cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ folder_ref: folderRef }),
    });
  }
  replayWebhookReceipt(provider: "gmail" | "microsoft_graph", eventId: string, input: WebhookReceiptReplayInput, idempotencyKey: string): Promise<unknown> {
    if (!eventId.trim() || eventId.length > 512) throw new Error("mailhub_receipt_event_id_invalid");
    if (!/^[0-9a-f]{64}$/.test(input.expectedBodySha256)) throw new Error("mailhub_receipt_digest_invalid");
    if (!input.reason.trim() || input.reason.length > 500) throw new Error("mailhub_receipt_reason_invalid");
    if (!idempotencyKey.trim() || idempotencyKey.length > 300) throw new Error("mailhub_receipt_idempotency_key_invalid");
    return this.request(`/v1/mail/webhooks/receipts/${encodeURIComponent(provider)}/${encodeURIComponent(eventId)}:replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ expected_body_sha256: input.expectedBodySha256, reason: input.reason }),
    });
  }
  enqueueSync(connectionId: string, idempotencyKey: string, input: SyncJobInput = {}): Promise<unknown> {
    const mode = input.mode ?? "incremental";
    const limit = input.limit ?? 50;
    const folderRef = input.folderRef ?? "INBOX";
    const labelRefs = input.labelRefs ?? [];
    const receivedAfter = normalizeSyncDate(input.receivedAfter, "received_after");
    const receivedBefore = normalizeSyncDate(input.receivedBefore, "received_before");
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) throw new Error("mailhub_sync_limit_invalid");
    if (!folderRef.trim() || folderRef.length > 200 || /[\u0000-\u001f\u007f]/.test(folderRef)) throw new Error("mailhub_sync_folder_invalid");
    if (labelRefs.length > 20 || labelRefs.some((label) => !label.trim() || label.length > 200 || /[\u0000-\u001f\u007f]/.test(label))) throw new Error("mailhub_sync_labels_invalid");
    if ((labelRefs.length > 0 || folderRef !== "INBOX" || receivedAfter || receivedBefore) && mode !== "backfill") throw new Error("mailhub_sync_filter_requires_backfill");
    if (receivedAfter && receivedBefore && Date.parse(receivedAfter) >= Date.parse(receivedBefore)) throw new Error("mailhub_sync_date_range_invalid");
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}/sync-jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        mode,
        limit,
        folder_ref: folderRef,
        label_refs: labelRefs,
        received_after: receivedAfter,
        received_before: receivedBefore,
      }),
    });
  }
  enqueueAutonomy(input: AutonomyRunInput, idempotencyKey: string): Promise<unknown> {
    if (!input.replayKey.trim()) throw new Error("mailhub_autonomy_replay_key_invalid");
    return this.request("/v1/mail/autonomy/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        connection_id: input.connectionId,
        replay_key: input.replayKey,
        limit: input.limit ?? 50,
        message_limit: input.messageLimit ?? 50,
        run_inline: false,
      }),
    });
  }
  listAutonomyRuns(connectionId?: string, limit = 50): Promise<unknown> {
    const bounded = Math.min(Math.max(limit, 1), 200);
    const query = new URLSearchParams({ limit: String(bounded) });
    if (connectionId) query.set("connection_id", connectionId);
    return this.request(`/v1/mail/autonomy/runs?${query.toString()}`);
  }
  getAutonomyRun(runId: string): Promise<unknown> {
    return this.request(`/v1/mail/autonomy/runs/${encodeURIComponent(runId)}`);
  }
  controlAutonomyRun(runId: string, command: AutonomyCommand, reason?: string): Promise<unknown> {
    if (command === "pause" && !reason?.trim()) throw new Error("mailhub_autonomy_pause_reason_required");
    return this.request(`/v1/mail/autonomy/runs/${encodeURIComponent(runId)}:${command}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: command === "pause" ? JSON.stringify({ reason }) : undefined,
    });
  }
  listThreadPage(options: MailHubThreadListOptions = {}): Promise<MailHubThreadPage> {
    const params = new URLSearchParams({
      limit: String(Math.min(Math.max(options.limit ?? 50, 1), 200)),
    });
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.connectionId) params.set("connection_id", options.connectionId);
    for (const [key, enabled] of Object.entries({
      unread: options.unread,
      important: options.important,
      attachment: options.attachment,
      project: options.project,
      candidate: options.candidate,
    })) {
      if (enabled) params.set(key, "true");
    }
    return this.request<MailHubThreadPage>(`/v1/mail/threads?${params.toString()}`);
  }
  listThreads(limit = 50): Promise<MailHubThreadPage> {
    return this.listThreadPage({ limit });
  }
  searchMessages(query: string, limit = 50, mode: MailHubSearchMode = "metadata"): Promise<MailHubSearchResponse> {
    const normalized = query.trim();
    if (!normalized || normalized.length > 200) throw new Error("mailhub_search_query_invalid");
    const bounded = Math.min(Math.max(limit, 1), 200);
    const params = new URLSearchParams({ q: normalized, limit: String(bounded), mode });
    return this.request<MailHubSearchResponse>(`/v1/mail/search?${params.toString()}`);
  }
  createDraft(input: DraftCreateInput, idempotencyKey: string): Promise<unknown> {
    return this.request("/v1/mail/drafts", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        connection_id: input.connectionId,
        thread_id: input.threadId ?? null,
        recipient_addresses: input.recipientAddresses,
        cc_addresses: input.ccAddresses ?? [],
        bcc_addresses: input.bccAddresses ?? [],
        attachment_refs: input.attachmentRefs ?? [],
        subject: input.subject,
        body_text: input.bodyText,
      }),
    });
  }
  sendDraft(draftId: string, input: DraftSendInput, idempotencyKey: string): Promise<unknown> {
    return this.request(`/v1/mail/drafts/${encodeURIComponent(draftId)}:send`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        expected_revision: input.expectedRevision,
        expected_content_sha256: input.expectedContentSha256,
        expected_recipient_digest: input.expectedRecipientDigest,
        confirmation_ref: input.confirmationRef,
        policy_id: input.policyId ?? null,
        grant_id: input.grantId ?? null,
        agent_subject_id: input.agentSubjectId ?? null,
      }),
    });
  }
  getThread(threadId: string): Promise<unknown> { return this.request(`/v1/mail/threads/${encodeURIComponent(threadId)}`); }
  listCandidates(options: CandidateListOptions = {}): Promise<unknown> {
    const params = new URLSearchParams();
    params.set("limit", String(options.limit ?? 50));
    if (options.status) params.set("status", options.status);
    if (options.candidateType) params.set("candidate_type", options.candidateType);
    return this.request(`/v1/mail/candidates?${params.toString()}`);
  }
  listTypedCandidates(
    candidateType: "project" | "knowledge" | "task",
    options: Omit<CandidateListOptions, "candidateType"> = {},
  ): Promise<unknown> {
    const params = new URLSearchParams();
    params.set("limit", String(options.limit ?? 50));
    if (options.status) params.set("status", options.status);
    return this.request(
      `/v1/mail/candidates/${encodeURIComponent(candidateType)}?${params.toString()}`,
    );
  }
  getCandidate(candidateId: string): Promise<unknown> {
    return this.request(`/v1/mail/candidates/${encodeURIComponent(candidateId)}`);
  }
  revokeCandidate(candidateId: string, input: CandidateRevokeInput): Promise<unknown> {
    if (!input.reason.trim() || input.reason.length > 500) {
      throw new Error("mailhub_candidate_revoke_reason_invalid");
    }
    return this.request(`/v1/mail/candidates/${encodeURIComponent(candidateId)}:revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: input.expectedRevision, reason: input.reason }),
    });
  }
  reviewCandidate(candidateId: string, input: CandidateReviewInput): Promise<unknown> {
    return this.request(`/v1/mail/candidates/${encodeURIComponent(candidateId)}:review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved: input.approved,
        expected_revision: input.expectedRevision,
        review_reason: input.reviewReason ?? null,
      }),
    });
  }
  applyCandidate(candidateId: string, input: CandidateApplyInput, idempotencyKey: string): Promise<unknown> {
    if (!input.approvalRef.trim()) throw new Error("mailhub_candidate_approval_ref_invalid");
    return this.request(`/v1/mail/candidates/${encodeURIComponent(candidateId)}:apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ approval_ref: input.approvalRef }),
    });
  }
  deleteConnection(connectionId: string, expectedRevision: number): Promise<unknown> {
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}:delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  }
  revokeConnection(connectionId: string, expectedRevision: number): Promise<unknown> {
    if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
      throw new Error("mailhub_connection_revision_invalid");
    }
    return this.request(`/v1/mail/connections/${encodeURIComponent(connectionId)}:revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  }
  exportData(includeContent = false, limit = 200): Promise<unknown> {
    if (typeof includeContent !== "boolean") throw new Error("mailhub_export_include_content_invalid");
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) throw new Error("mailhub_export_limit_invalid");
    return this.request(`/v1/mail/data-export?include_content=${includeContent ? "true" : "false"}&limit=${limit}`);
  }
  executeRule(ruleId: string, input: RuleExecuteInput): Promise<unknown> {
    return this.request(`/v1/mail/rules/${encodeURIComponent(ruleId)}:execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message_ids: input.messageIds,
        dry_run: input.dryRun ?? true,
        policy_id: input.policyId ?? null,
        grant_id: input.grantId ?? null,
      }),
    });
  }
  listRuleExecutions(ruleId: string, limit = 100): Promise<unknown> {
    return this.request(`/v1/mail/rules/${encodeURIComponent(ruleId)}/executions?limit=${limit}`);
  }
}
