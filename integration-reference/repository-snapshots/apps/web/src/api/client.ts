import type { ApplicationInteractionEventPageV1 } from "../types/applicationEvents";
import type {
  AgentEntry,
  AgentDraft,
  AgentDraftWrite,
  AgentTrial,
  AppEntry,
  DriveFile,
  KnowledgeSource,
  SkillEntry,
} from "../types/domain";

const BFF_BASE_URL =
  (import.meta.env.VITE_BFF_BASE_URL as string | undefined)?.replace(
    /\/$/,
    "",
  ) ?? "";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail: unknown = null,
  ) {
    super(code);
    this.name = "ApiError";
  }
}

export interface ConversationCreateResponse {
  conversation_id: string;
  title: string;
  agent_id: string | null;
}

export interface ConversationDirectoryEntry {
  conversationId: string;
  title: string;
  agentId: string | null;
  status: "active" | "archived";
  createdAt: string;
  updatedAt: string;
}

export interface ConversationDirectoryPage {
  items: ConversationDirectoryEntry[];
  hasMore: boolean;
  nextCursor: string | null;
}

export interface SessionResponse {
  principal: {
    user_id: string;
    tenant_id: string;
    role: string;
    permissions: string[];
    apps: string[];
  };
  profile: Record<string, unknown>;
  expires_in: number;
}

export interface AccountSession {
  session_id: string;
  device_label: string;
  current: boolean;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
}

/** Provider-neutral MailHub projections.  Bodies are fetched separately as
 * plain text; list and thread projections intentionally contain metadata only. */
export interface MailHubConnection {
  connection_id: string;
  provider: string;
  email_address: string;
  status: string;
  revision: number;
  granted_scopes: string[];
  content_mode?: string;
  sync_state?: string;
  last_sync_at?: string | null;
  error_code?: string | null;
}

export interface MailHubThread {
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

export interface MailHubThreadPage {
  data: MailHubThread[];
  next_cursor: string | null;
  has_more: boolean;
  filters?: Record<string, unknown>;
}

export interface MailHubMessage {
  message_id: string;
  connection_id: string;
  thread_id: string;
  provider_message_ref?: string;
  internet_message_id?: string | null;
  sender_address: string;
  recipient_addresses: string[];
  subject: string;
  received_at: string;
  has_content?: boolean;
  body_object_ref?: string | null;
  attachment_count?: number;
  source_locator?: Record<string, unknown> | null;
}

export interface MailHubThreadDetail {
  thread: MailHubThread;
  messages: MailHubMessage[];
  content_policy: "plain_text_endpoint_only" | string;
}

export interface MailHubCandidate {
  candidate_id: string;
  candidate_type: "project" | "task" | "knowledge" | string;
  state: string;
  revision: number;
  title?: string;
  summary?: string;
  confidence?: number | null;
  source_refs?: Array<Record<string, unknown>>;
  target?: Record<string, unknown> | null;
  risk_flags?: string[];
  created_at?: string;
}

export interface MailHubSyncJob {
  job_id: string;
  connection_id: string;
  status: string;
  mode: string;
  fetched_count?: number;
  saved_count?: number;
  duplicate_count?: number;
  cursor_before?: string | null;
  cursor_after?: string | null;
  error_code?: string | null;
}

export interface MailHubAutonomyRun {
  run_id: string;
  tenant_id: string;
  subject_id: string;
  connection_id: string;
  replay_key: string;
  mode: string;
  requested_limit: number;
  message_limit: number;
  status: string;
  message_ids: string[];
  analyzed_message_ids: string[];
  candidate_ids: string[];
  sync_result?: Record<string, unknown>;
  sync_job_id?: string | null;
  error_code?: string | null;
  pause_reason?: string | null;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string;
}

export interface MailHubDraft {
  draft_id: string;
  connection_id: string;
  thread_id: string | null;
  recipient_addresses: string[];
  cc_addresses: string[];
  bcc_addresses: string[];
  attachment_refs: string[];
  subject: string;
  body_text?: string | null;
  content_sha256: string;
  revision: number;
  status: string;
  provider_draft_ref?: string | null;
  expires_at: string;
  recipient_digest?: string;
}

export interface MailHubAgentPolicy {
  policy_id: string;
  tenant_id: string;
  owner_subject_id: string;
  allowed_connection_ids: string[];
  allowed_folder_refs: string[];
  allowed_actions: string[];
  allowed_domains: string[];
  allowed_data_classes: string[];
  thread_only: boolean;
  allowed_automation_level: string;
  max_per_hour: number;
  max_per_day: number;
  valid_from: string;
  valid_until: string;
  revision: number;
  enabled: boolean;
}

export interface MailHubDelegation {
  grant_id: string;
  tenant_id: string;
  policy_id: string;
  agent_subject_id: string;
  granted_by_subject_id: string;
  capability_ids: string[];
  granted_at: string;
  expires_at: string;
  revoked_at: string | null;
  revision: number;
}

export type ProductAnalyticsEventName =
  | "conversation_started"
  | "answer_completed"
  | "answer_failed"
  | "expert_copied"
  | "expert_used"
  | "knowledge_source_created"
  | "return_7d";

export interface ProductAnalyticsEvent {
  event_name: ProductAnalyticsEventName;
  event_id?: string;
  surface: "home" | "conversation" | "expert" | "knowledge" | "account";
  outcome?: "success" | "failure" | "cancelled";
  latency_bucket?: "lt_2s" | "2_10s" | "10_30s" | "gte_30s" | "unknown";
  expert_scope?: "public" | "personal" | "unknown";
  has_citations?: boolean;
}

export interface ProductAnalyticsSummary {
  window_days: number;
  items: Array<{ event_name: string; total: number }>;
}

export interface TotpStatus {
  enabled: boolean;
  pending_setup: boolean;
  enabled_at: string | null;
}

export interface TotpSetup {
  secret: string;
  otpauth_uri: string;
  issuer: string;
  account_name: string;
}

export interface MemoryPolicy {
  enabled: boolean;
  retention_days: number;
  agent_id: string | null;
  inherited: boolean;
  updated_at: string | null;
}

export interface MemoryNode {
  id: string;
  type: string;
  title: string;
  text?: string;
  summary?: string | null;
  status: string;
  namespace: {
    agent_id?: string | null;
    memory_space?: string | null;
  };
  created_at: string;
  updated_at: string;
}

export interface MemoryCenter {
  policy: MemoryPolicy;
  namespace: Record<string, unknown>;
  nodes: MemoryNode[];
  source_refs: Array<Record<string, unknown>>;
  audit_events: Array<Record<string, unknown>>;
}

export interface AgentGrowth {
  agent_id: string;
  signals_total: number;
  feedback_distribution: Record<string, number>;
  pending_proposals: number;
  current_proposals: Array<Record<string, unknown>>;
  release_state: {
    status?: string;
    proposal_id?: string | null;
    rollback_available?: boolean;
  };
}

export interface ApprovalDecisionRequest {
  conversation_id: string;
  expected_request_version: number;
  decision: "allow" | "deny";
}

export interface ReviewCardProjection {
  schema_version: "aios.approval_card.v1";
  requestId: string;
  productId: string;
  capabilityId: string;
  requesterActorId: string;
  status: "pending" | "allow" | "deny" | "revoked" | "superseded" | "expired";
  riskLevel: "R0" | "R1" | "R2" | "R3" | "R4";
  reviewMode: "sample" | "full" | "four_eyes";
  separationRequired: boolean;
  requestVersion: number;
  requirementBriefVersion: string;
  expiresAt: string;
  reviewSurfaceRef: string;
  deepLink: string;
  notificationRef: string;
  statusCursor: string;
  canDecide: boolean;
  decisionBlockReasons: string[];
  evidenceRefs: string[];
  agentShare?: AgentShareReviewContext;
}

export interface AgentShareReviewContext {
  agentId: string;
  agentName: string;
  agentVersion: string;
  reason: string;
  organizationAgentId: string;
  skillIds: string[];
  knowledgeSourceIds: string[];
  publicationRetryAllowed: boolean;
}

export interface AgentShareRequest {
  requestId: string;
  agentId: string;
  agentName: string;
  agentVersion: string;
  status:
    | "not_requested"
    | "pending"
    | "allow"
    | "deny"
    | "revoked"
    | "superseded"
    | "expired";
  publicationStatus: "not_applicable" | "pending" | "published";
  organizationAgentId: string;
  skillIds: string[];
  knowledgeSourceIds: string[];
  reason: string;
  expiresAt: string | null;
  canDecide: boolean;
  sourceSystem: "platform-core";
}

export interface ReviewInboxProjection {
  schema_version: "aios.approval_inbox.v1";
  items: ReviewCardProjection[];
  count: number;
  hasMore: boolean;
  nextCursor: string | null;
  sourceSystem: "platform-core";
}

export interface WorkItemProjection {
  schema_version: "aios.application_work_item.v1";
  tenantId: string;
  subjectUserId: string;
  productId: string;
  conversationId: string;
  workItemId: string;
  requestId: string;
  traceId: string;
  status: string;
  terminalStatus: string | null;
  latestEventType: string;
  updatedAt: string;
  sourceCursor: string;
  sourceSequence: number;
  producer: string;
  factOwner: "agentctl";
  sourceSystem: "platform-core";
  runRef: Record<string, unknown> | null;
  approvalRef: Record<string, unknown> | null;
  artifactRefs: Array<Record<string, unknown>>;
  evidenceRefs: Array<Record<string, unknown>>;
  errorSummary: Record<string, unknown> | null;
  authorityRef: Record<string, unknown>;
}

export interface WorkItemPageProjection {
  schema_version: "aios.application_work_item_page.v1";
  tenantId: string;
  subjectUserId: string;
  productId: string;
  items: WorkItemProjection[];
  count: number;
  summary: {
    total: number;
    active: number;
    attention: number;
    completed: number;
  };
  hasMore: boolean;
  nextCursor: string | null;
  sourceSystem: "platform-core";
  factOwner: "agentctl";
}

export interface KnowledgeSearchHit {
  sourceId: string | null;
  title: string;
  text: string;
  score: number;
  citation: Record<string, unknown>;
}

export interface KnowledgeSearchResponse {
  answer: string | null;
  hits: KnowledgeSearchHit[];
  evidence_sufficient: boolean;
}

export interface ToolPackageProjection {
  packageId: string;
  version: string;
  digest: string;
  displayName: string;
  capabilities: string[];
  releaseStatus: string;
  governance: Record<string, unknown>;
}

export interface DataStructuringReadiness {
  status: "blocked" | "metadata_ready" | "binding_ready" | "execution_ready";
  factOwner: "platform-core";
  toolhostStatus: "ready" | "drift" | "unavailable" | "not_configured";
  packages: Array<{
    packageId: string;
    version: string;
    expectedDigest: string;
    coreProjectionDigest: string | null;
    manifestState: "compatible" | "drift" | "unavailable";
    declaredCapabilities: string[];
    executableCapabilities: string[];
    bindingState: "ready" | "blocked" | "unavailable";
    grantedCapabilities: string[];
    pendingCapabilities: string[];
    verifierAppId: string | null;
    bindingBlockers: string[];
    executionEnabled: boolean;
  }>;
  blockers: string[];
  executionEnabled: boolean;
}

export interface DataStructuringInvocation {
  schemaVersion: "caplatform.data_structuring_invocation.v1";
  status: "completed";
  packageId: string;
  capabilityId: string;
  pinnedVersion: string;
  pinnedDigest: string;
  grantId: string;
  output: Record<string, unknown>;
  latencyMs: number | null;
  executionMode: "explicit_toolhost_only";
  factOwner: "agentctl";
}

export interface DriveFilePreview {
  objectRef: string;
  name: string;
  mime: string;
  previewKind: string;
  text: string | null;
  truncated: boolean;
  limit: number;
  traceId: string;
}

export interface DriveSignedDownload {
  objectRef: string;
  signedUrl: string;
  method: string;
  expiresAt: string;
  traceId: string;
}

export interface ProjectProjection {
  id: string;
  goal: string;
  status: string;
  ownerId: string | null;
  createdAt: string;
}

export interface ProjectPortfolioTask {
  id: string;
  project_id: string;
  project_name: string;
  title: string;
  state: string;
  kind: string;
  risk_level: string;
  progress: number | null;
  due_at: string | null;
}

export interface ProjectPortfolioWorkbench {
  actor_ref: string;
  my_projects: Array<{
    id: string;
    goal: string;
    status: string;
    owner_id: string | null;
    due_at: string | null;
  }>;
  assigned_to_me: ProjectPortfolioTask[];
  pending_confirmation: ProjectPortfolioTask[];
  blocked: ProjectPortfolioTask[];
  failed: ProjectPortfolioTask[];
  due_soon: ProjectPortfolioTask[];
  overdue: ProjectPortfolioTask[];
  counts: Record<string, number>;
  data_as_of: string;
}

export interface ProjectPortfolioMaturity {
  projects: Array<{
    project_id: string;
    project_name: string;
    health: number;
    risk: number;
    delay: number;
    resource_load: number;
    delivery_quality: number;
    overdue_tasks: number;
    open_risks: number;
  }>;
  portfolio_health: number;
  data_as_of: string;
}

export interface ProjectPortfolioResources {
  date_range: { start: string; end: string };
  resources: Array<{
    resource_id: string;
    name: string;
    role: string;
    skills: string[];
    capacity_hours: number;
    allocated_hours: number;
    utilization: number;
    overloaded: boolean;
  }>;
  overload_count: number;
  skill_index: Record<string, string[]>;
}

export interface ProjectPortfolioRecommendations {
  templates: Array<{
    id: string;
    name: string;
    description: string;
    quality_score: number;
  }>;
  retrospectives: Array<{
    id: string;
    project_id: string;
    title: string;
    summary: string;
  }>;
  assets: Array<{
    id: string;
    project_id: string;
    title: string;
    asset_type: string;
    published_at: string;
  }>;
}

export interface ProjectPortfolioSearch {
  query: string;
  items: Array<{
    type: string;
    id: string;
    project_id: string | null;
    title: string;
    snippet: string;
    updated_at: string | null;
  }>;
  counts: Record<string, number>;
  truncated: boolean;
}

export interface ProjectRiskRecord {
  id: string;
  project_id: string;
  title: string;
  category: string;
  severity: "low" | "medium" | "high" | "critical";
  probability: number;
  score: number;
  status: string;
  owner_ref: string | null;
  mitigation: string | null;
  trigger_condition: string | null;
  escalation_history: Array<Record<string, unknown>>;
}

export interface ProjectScheduleInsights {
  baselines: Array<{
    id: string;
    name: string;
    description: string | null;
    created_at: string;
  }>;
  latest_baseline_id: string | null;
  task_variances: Array<{
    task_id: string;
    title: string | null;
    planned_end: string | null;
    actual_end: string | null;
  }>;
  milestone_trend: Array<{ id: string; due_at: string | null; status: string }>;
  changes: Array<{
    id: string;
    summary: string;
    change_type: string;
    occurred_at: string;
  }>;
}

export interface RuntimeVersions {
  schema_version: "caplatform.runtime_versions.v1";
  service: {
    name: string;
    version: string;
    build_ref: string;
    provides: string[];
  };
  dependencies: {
    aiprojectops: {
      service: string;
      version: string;
      build_ref: string;
      provides: string[];
    } | null;
  };
  issues: string[];
}

export interface ProjectPlanningSession {
  sessionId: string;
  status: string;
  projectName: string;
  projectGoal: string;
  sourceMode: "sentence" | "file";
  sourceDocumentId: string | null;
  parseRef: string | null;
}

export interface ProjectPlanningJob {
  jobId: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  sessionId: string;
  resultDraftId: string | null;
  progress: number;
  errorCode: string | null;
  errorMessage: string | null;
  traceId: string;
}

export interface ProjectBlueprint {
  id: string;
  session_id: string;
  status: string;
  generation_mode: string;
  version: number;
  revision: number;
  blueprint: {
    schema_version: "aiprojectops.project_blueprint.v1";
    charter: {
      name: string;
      goal: string;
      scope: string | null;
      success_criteria: string[];
      assumptions: string[];
      constraints: string[];
      exclusions: string[];
      start_at?: string | null;
      end_at?: string | null;
    };
    stages: Array<{
      client_key: string;
      name: string;
      description?: string | null;
      order: number;
    }>;
    tasks: Array<{
      client_key: string;
      stage_client_key: string | null;
      parent_client_key: string | null;
      title: string;
      description: string | null;
      kind: "HUMAN" | "AGENT" | "HYBRID" | "SYSTEM";
      priority: "none" | "low" | "medium" | "high" | "urgent";
      risk_level: "low" | "medium" | "high";
      trust_route: "low" | "mid" | "high";
      start_at: string | null;
      end_at: string | null;
      duration_minutes: number | null;
      acceptance_criteria: string[];
      deliverable_requirements: Array<{
        client_key: string;
        title: string;
        kind: string;
        required: boolean;
        acceptance_criteria?: string[];
      }>;
      dependencies: Array<{
        predecessor_client_key: string;
        dependency_type: string;
        hard: boolean;
        lag_minutes: number;
      }>;
      assignee_suggestions: Array<{
        target_type: string;
        target_id: string;
        display_name?: string | null;
        reason?: string | null;
      }>;
      source_refs: Array<{
        source_type: string;
        source_id: string;
        title?: string | null;
        section?: string | null;
        snippet?: string | null;
      }>;
      confidence: number | null;
    }>;
    milestones: Array<Record<string, unknown>>;
    risks: Array<{
      client_key: string;
      title: string;
      description?: string | null;
      impact: "low" | "medium" | "high" | "critical";
      probability: number;
      mitigation?: string | null;
      source_refs?: Array<Record<string, unknown>>;
    }>;
    source_refs: Array<Record<string, unknown>>;
    generation: Record<string, unknown>;
    metadata: Record<string, unknown>;
  };
  items: Array<Record<string, unknown>>;
}

export interface ProjectBlueprintSync {
  id: string;
  draft_id: string;
  status: string;
  target_project_id: string | null;
  created_task_count: number;
  result: Record<string, unknown>;
}

export interface ProjectTaskProjection {
  id: string;
  title: string;
  state: string;
  riskLevel: string;
  progress: number | null;
  createdAt: string | null;
  startAt: string | null;
  endAt: string | null;
  isBlocked: boolean;
}

export interface ProjectTreeProjection {
  id: string;
  goal: string;
  status: string;
  scope: string | null;
  stages: Array<{
    id: string | null;
    name: string;
    order: number;
    tasks: ProjectTaskProjection[];
  }>;
  taskCounts: Record<string, number>;
}

export interface ProjectActivityProjection {
  id: string;
  taskId: string | null;
  category: string;
  actorType: string;
  actorId: string | null;
  title: string;
  detail: string | null;
  occurredAt: string;
}

export interface ProjectActivityPageProjection {
  items: ProjectActivityProjection[];
  nextBefore: string | null;
}

export interface ProjectOperationsDashboardProjection {
  projectId: string;
  dataAsOf: string;
  weekSummary: {
    completed?: number;
    active?: number;
    overdue?: number;
    total?: number;
  };
  risks: Array<Record<string, unknown>>;
  blocked: Array<Record<string, unknown>>;
  myActions: Array<Record<string, unknown>>;
  nextMilestone: Record<string, unknown> | null;
  acceptanceBacklog: number;
  missingData: string[];
}

export interface ProjectAgentPolicyProjection {
  id: string | null;
  projectId: string;
  expertSource: "PUBLIC" | "PROJECT";
  expertRef: string | null;
  expertName: string | null;
  isEnabled: boolean;
  defaultRulesEnabled: boolean;
  memoryScopes: string[];
  memoryContentTypes: string[];
  authoritativeStateInMemory: boolean;
  operationPolicy: Record<string, unknown>;
  version: number;
  defaultRuleCount: number;
}

export interface ProjectNotificationPreferenceProjection {
  id: string | null;
  projectId: string;
  userRef: string;
  channels: Array<"in_app" | "email" | "external">;
  externalDestinationRefs: string[];
  eventTypes: string[];
  quietHours: Record<string, unknown>;
  isEnabled: boolean;
  secretsStored: boolean;
}

export interface ProjectOperationalReportProjection {
  id: string;
  projectId: string;
  reportType: string;
  title: string;
  summary: string;
  dataAsOf: string;
  coverage: Record<string, unknown>;
  sourceTaskIds: string[];
  missingData: string[];
  sections: Record<string, unknown>;
  generatedByRef: string | null;
  createdAt: string;
}

export interface IntegrationEventCatalogItem {
  event_type: string;
  schema_version: string;
  description: string;
  sample: Record<string, unknown>;
}

export interface IntegrationDestination {
  id: string;
  name: string;
  endpoint_url: string;
  environment: "test" | "production";
  status: string;
  signature_version: string;
  credential_ref: string;
  secret_status: string;
  published_at: string | null;
  enabled_at: string | null;
  health: {
    success_rate: number | null;
    p95_latency_ms: number | null;
    backlog: number;
    failure_reasons: Record<string, number>;
    last_success_at: string | null;
  };
}

export interface IntegrationEndpoint {
  id: string;
  name: string;
  environment: "test" | "production";
  provider: string;
  verifier: string;
  handler_ref: string | null;
  status: string;
  secret_status: string;
  allowed_ip_cidrs: string[];
  created_at: string;
}

export interface IntegrationDelivery {
  id: string;
  event_id: string;
  event_type: string;
  destination_id: string;
  status: string;
  attempt: number;
  request_hash: string | null;
  request_summary: Record<string, unknown>;
  response_status: number | null;
  response_summary: string | null;
  duration_ms: number | null;
  next_retry_at: string | null;
  dead_lettered_at: string | null;
  trace_id: string | null;
  replay_of: string | null;
  created_at: string;
}

export interface IntegrationReceipt {
  id: string;
  endpoint_id: string;
  endpoint_name: string;
  provider: string;
  provider_event_id: string | null;
  body_hash: string;
  raw_summary: { size: number; content_type: string };
  status: string;
  verification: Record<string, unknown>;
  normalized: Record<string, unknown> | null;
  route: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  quarantine_reason: string | null;
  replay_of: string | null;
  received_at: string;
}

export interface ProjectAssetProjection {
  id: string;
  title: string;
  assetType: "document" | "deliverable" | "source";
  subtype: string;
  updatedBy: string | null;
  updatedAt: string | null;
  status: string | null;
  taskTitle: string | null;
}

export interface ProjectWorkspaceContextProjection {
  summary: {
    taskCount: number;
    sourceCount: number;
    documentCount: number;
    deliverableCount: number;
    skillCount: number;
    toolCount: number;
    connectorCount: number;
  };
  assets: ProjectAssetProjection[];
}

export interface TaskWorkspaceProjection {
  schemaVersion: "aiprojectops.task_workspace.v1";
  tenantId: string;
  task: {
    id: string;
    projectId: string;
    stageId: string | null;
    parentTaskId: string | null;
    title: string;
    description: string | null;
    state: string;
    kind: string;
    taskType: string;
    priority: string | null;
    riskLevel: string;
    trustRoute: string;
    progress: number | null;
    startAt: string | null;
    endAt: string | null;
    sla: string | null;
    acceptanceCriteria: string[];
    createdAt: string;
    updatedAt: string;
  };
  assignments: Array<{
    id: string | null;
    targetType: string;
    targetId: string;
    responsibility: string | null;
    status: string | null;
    weight: number | null;
    metadata: Record<string, unknown>;
  }>;
  assignmentSuggestions: Array<Record<string, unknown>>;
  dependencies: Array<{
    id: string | null;
    revision: number;
    predecessorTaskId: string;
    successorTaskId: string;
    dependencyType: string;
    hard: boolean;
    lagMinutes: number;
    predecessorTitle: string | null;
    predecessorState: string | null;
    successorTitle: string | null;
    successorState: string | null;
    direction: "predecessor" | "successor";
    blocking: boolean;
    description: string | null;
  }>;
  contents: Array<{
    id: string;
    role: string;
    resourceType: string;
    resourceId: string;
    title: string;
    mimeType: string | null;
    version: string | null;
    visibility: string | null;
    downloadRef: string | null;
    sourceRefs: Array<Record<string, unknown>>;
    createdAt: string | null;
    updatedAt: string | null;
  }>;
  deliverableRequirements: Array<{
    id: string;
    title: string;
    kind: string;
    description: string | null;
    required: boolean;
    status: string;
    dueAt: string | null;
    reviewerIds: string[];
    acceptanceCriteria: string[];
    evidenceRequired: boolean;
    latestDeliverableId: string | null;
  }>;
  deliverables: Array<{
    id: string;
    requirementId: string | null;
    kind: string;
    status: string;
    currentVersionId: string | null;
    sourceAdapter: string;
    validationStatus: string | null;
    acceptanceStatus: string | null;
    reviewerRef: string | null;
    returnReason: string | null;
    assetId: string | null;
    versions: Array<Record<string, unknown>>;
    executionRunIds: string[];
    createdAt: string;
    updatedAt: string;
  }>;
  executions: Array<{
    id: string;
    status: string;
    triggerType: string;
    agentId: string | null;
    agentctlRunId: string | null;
    conversationId: string | null;
    traceId: string | null;
    startedBy: string | null;
    startedAt: string;
    finishedAt: string | null;
    errorMessage: string | null;
    contextDigest: string | null;
    output: Record<string, unknown> | null;
    metadata: Record<string, unknown>;
    events: Array<Record<string, unknown>>;
    canRetry: boolean;
    canCancel: boolean;
  }>;
  timeLogs: Array<{
    id: string;
    resourceId: string | null;
    actorRef: string | null;
    hours: number;
    description: string | null;
    loggedAt: string;
    createdAt: string;
  }>;
  replanDrafts: Array<{
    id: string;
    executionRunId: string;
    revision: number;
    status: string;
    reason: string | null;
    proposalDigest: string;
    proposal: Record<string, unknown>;
    sourceRefs: Array<Record<string, unknown>>;
    createdBy: string;
    decidedBy: string | null;
    decisionReason: string | null;
    decidedAt: string | null;
    createdAt: string;
  }>;
  sourceRefs: Array<Record<string, unknown>>;
  discussion: {
    comment_count?: number;
    attachment_count?: number;
    latest_comment_at?: string | null;
  };
  recentActivities: Array<{
    id: string;
    action: string;
    actorType: string;
    actorId: string | null;
    occurredAt: string;
    summary: string | null;
    metadata: Record<string, unknown>;
  }>;
}

export interface ProjectScheduleProjection {
  schema_version: string;
  project_id: string;
  calendar_id: string | null;
  task_count: number;
  projected_end_at: string | null;
  critical_path_task_ids: string[];
  applied?: boolean;
  tasks: Array<{
    task_id: string;
    title: string;
    start_at: string;
    end_at: string;
    baseline_start_at: string | null;
    baseline_end_at: string | null;
    variance_minutes: number;
    critical: boolean;
  }>;
}

export interface AcceptanceQueueProjection {
  deliverable_id: string;
  project_id: string;
  task_id: string;
  task_title: string;
  requirement_id: string | null;
  requirement_title: string | null;
  reviewer_ids: string[];
  submitted_at: string;
  due_at: string | null;
  overdue: boolean;
  validation_status: string | null;
}

export interface ProjectTaskProposal {
  schema_version: "caplatform.project_task_proposal.v1";
  proposalId: string;
  proposalVersion: number;
  capabilityId: "ca.project.create_task";
  status:
    | "pending"
    | "executing"
    | "denied"
    | "succeeded"
    | "outcome_unknown"
    | "rejected"
    | "expired";
  target: {
    projectId: string;
    title: string;
    description: string | null;
    stageId: string | null;
  };
  impact: string;
  preflight: {
    allowed: boolean;
    reasons: string[];
    checks: Record<string, boolean>;
    requiredConfirmation: "explicit";
    sideEffectClass: "external";
  };
  decision: "allow" | "deny" | null;
  taskRef: {
    taskId: string;
    projectId: string;
    state: string;
    sourceSystem: "aiprojectops";
    creationRef: string;
  } | null;
  evidence: {
    kind: "aiprojectops_mcp";
    traceId: string;
    providerStatus: "created" | "replayed";
    idempotencyReplayed: boolean;
  } | null;
  errorCode: string | null;
  workItemProjectionStatus: "not_required" | "pending" | "published" | "failed";
  createdAt: string;
  updatedAt: string;
  expiresAt: string;
}

export function fixtureModeEnabled(): boolean {
  return import.meta.env.VITE_AI_MODE === "fixture";
}

export async function trackProductEvent(
  event: ProductAnalyticsEvent,
): Promise<void> {
  if (fixtureModeEnabled()) return;
  try {
    await request("/api/analytics/events", {
      method: "POST",
      body: JSON.stringify({
        ...event,
        event_id: event.event_id ?? crypto.randomUUID(),
      }),
    });
  } catch {
    // Product telemetry is deliberately best-effort and never blocks the user's work.
  }
}

export async function getProductAnalyticsSummary(
  days = 30,
): Promise<ProductAnalyticsSummary> {
  return request(`/api/analytics/summary?days=${days}`);
}

export function trackSevenDayReturn(): void {
  if (fixtureModeEnabled()) return;
  const firstSeenKey = "caplatform.analytics.first_seen";
  const lastTrackedKey = "caplatform.analytics.return_7d";
  const now = Date.now();
  const firstSeen = Number(window.localStorage.getItem(firstSeenKey) || now);
  if (!window.localStorage.getItem(firstSeenKey)) {
    window.localStorage.setItem(firstSeenKey, String(now));
    return;
  }
  const lastTracked = Number(window.localStorage.getItem(lastTrackedKey) || 0);
  const sevenDays = 7 * 24 * 60 * 60 * 1000;
  if (now - firstSeen >= sevenDays && now - lastTracked >= sevenDays) {
    window.localStorage.setItem(lastTrackedKey, String(now));
    void trackProductEvent({
      event_name: "return_7d",
      surface: "home",
      outcome: "success",
    });
  }
}

export function conversationEventStreamUrl(conversationId: string): string {
  return `${BFF_BASE_URL}/api/conversations/${encodeURIComponent(conversationId)}/events/stream`;
}

export async function exchangeSession(
  platformToken: string,
): Promise<SessionResponse> {
  return request("/api/session/exchange", {
    method: "POST",
    headers: { Authorization: `Bearer ${platformToken}` },
  });
}

export async function getSession(): Promise<SessionResponse> {
  return request("/api/me");
}

export async function login(
  identityType: "email" | "phone",
  identifier: string,
  password: string,
  totpCode?: string,
): Promise<SessionResponse> {
  return request("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      identity_type: identityType,
      identifier,
      password,
      totp_code: totpCode || null,
    }),
  });
}

export async function register(
  identityType: "email" | "phone",
  identifier: string,
  password: string,
  displayName: string,
): Promise<SessionResponse> {
  return request("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({
      identity_type: identityType,
      identifier,
      password,
      display_name: displayName,
    }),
  });
}

export async function logout(): Promise<void> {
  return request("/api/session", { method: "DELETE" });
}

export async function updateAccountProfile(
  displayName: string,
): Promise<SessionResponse> {
  return request("/api/account/profile", {
    method: "PATCH",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function changeAccountPassword(
  currentPassword: string,
  newPassword: string,
): Promise<SessionResponse> {
  return request("/api/account/change-password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}

export async function getAccountTotpStatus(): Promise<TotpStatus> {
  return request("/api/account/totp");
}

export async function setupAccountTotp(): Promise<TotpSetup> {
  return request("/api/account/totp/setup", { method: "POST" });
}

export async function enableAccountTotp(code: string): Promise<TotpStatus> {
  return request("/api/account/totp/enable", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

export async function disableAccountTotp(
  currentPassword: string,
  code?: string,
): Promise<TotpStatus> {
  return request("/api/account/totp/disable", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      code: code || null,
    }),
  });
}

export async function switchAccountWorkspace(
  tenantId: string,
): Promise<SessionResponse> {
  return request("/api/account/workspaces/switch", {
    method: "POST",
    body: JSON.stringify({ tenant_id: tenantId }),
  });
}

export async function listAccountSessions(): Promise<{
  items: AccountSession[];
}> {
  return request("/api/account/sessions");
}

export async function revokeAccountSession(
  sessionId: string,
): Promise<{ revoked: number }> {
  return request(`/api/account/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

export async function revokeOtherAccountSessions(): Promise<{
  revoked: number;
}> {
  return request("/api/account/sessions/others", { method: "DELETE" });
}

export async function exportAccountData(): Promise<Record<string, unknown>> {
  return request("/api/account/export");
}

export async function deactivateAccount(
  currentPassword: string,
  totpCode?: string,
): Promise<void> {
  return request("/api/account", {
    method: "DELETE",
    body: JSON.stringify({
      current_password: currentPassword,
      totp_code: totpCode || null,
      confirmation: "DELETE",
    }),
  });
}

export async function getAccountMemory(
  agentId?: string,
): Promise<MemoryCenter> {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  return request(`/api/account/memory${query}`);
}

export async function getAgentGrowth(agentId: string): Promise<AgentGrowth> {
  return request(`/api/agents/${encodeURIComponent(agentId)}/growth`);
}

export async function updateAccountMemoryPolicy(
  enabled: boolean,
  retentionDays: number,
  agentId?: string,
): Promise<MemoryPolicy> {
  return request("/api/account/memory/policy", {
    method: "PATCH",
    body: JSON.stringify({
      enabled,
      retention_days: retentionDays,
      agent_id: agentId || null,
    }),
  });
}

export async function correctAccountMemory(
  nodeId: string,
  agentId: string,
  summary: string,
  reason: string,
): Promise<Record<string, unknown>> {
  return request(`/api/account/memory/${encodeURIComponent(nodeId)}/correct`, {
    method: "POST",
    body: JSON.stringify({ agent_id: agentId, summary, reason }),
  });
}

export async function deleteAccountMemory(
  nodeId: string,
  agentId: string,
  mode: "soft_delete" | "hard_delete",
  reason: string,
): Promise<Record<string, unknown>> {
  return request(`/api/account/memory/${encodeURIComponent(nodeId)}`, {
    method: "DELETE",
    body: JSON.stringify({ agent_id: agentId, mode, reason }),
  });
}

export async function createConversation(
  title?: string,
  agentId?: string,
): Promise<ConversationCreateResponse> {
  const created = await request<ConversationCreateResponse>(
    "/api/conversations",
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ title: title || null, agent_id: agentId || null }),
    },
  );
  void trackProductEvent({
    event_name: "conversation_started",
    surface: agentId ? "expert" : "conversation",
    outcome: "success",
    expert_scope: agentId ? "unknown" : undefined,
  });
  if (agentId) {
    void trackProductEvent({
      event_name: "expert_used",
      surface: "expert",
      outcome: "success",
      expert_scope: "unknown",
    });
  }
  return created;
}

export async function listConversations(
  status: "active" | "archived" = "active",
  query = "",
): Promise<ConversationDirectoryPage> {
  const params = new URLSearchParams({ status, limit: "100" });
  if (query.trim()) params.set("q", query.trim());
  return request(`/api/conversations?${params.toString()}`);
}

export async function listMailConnections(): Promise<MailHubConnection[]> {
  const envelope = await request<{ data?: MailHubConnection[] }>("/api/mail/connections");
  return Array.isArray(envelope.data) ? envelope.data : [];
}

export async function listMailAgentPolicies(limit = 50): Promise<MailHubAgentPolicy[]> {
  const envelope = await request<{ data?: MailHubAgentPolicy[] }>(
    `/api/mail/agent-policies?limit=${Math.min(Math.max(limit, 1), 200)}`,
  );
  return Array.isArray(envelope.data) ? envelope.data : [];
}

export async function listMailDelegations(limit = 50): Promise<MailHubDelegation[]> {
  const envelope = await request<{ data?: MailHubDelegation[] }>(
    `/api/mail/delegations?limit=${Math.min(Math.max(limit, 1), 200)}`,
  );
  return Array.isArray(envelope.data) ? envelope.data : [];
}

export async function deleteMailConnection(
  connectionId: string,
  expectedRevision: number,
): Promise<Record<string, unknown>> {
  return request(`/api/mail/connections/${encodeURIComponent(connectionId)}:delete`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ expected_revision: expectedRevision }),
  });
}

export async function enqueueMailSync(
  connectionId: string,
  mode: "incremental" | "backfill" | "reconcile" = "incremental",
  limit = 50,
): Promise<MailHubSyncJob> {
  const envelope = await request<{ data?: MailHubSyncJob }>(
    `/api/mail/connections/${encodeURIComponent(connectionId)}/sync-jobs`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ mode, limit }),
    },
  );
  if (!envelope.data) throw new ApiError(502, "mailhub_sync_response_invalid");
  return envelope.data;
}

export async function enqueueMailAutonomy(
  connectionId: string,
  replayKey: string,
  limit = 50,
  messageLimit = 50,
): Promise<MailHubAutonomyRun> {
  if (!replayKey.trim()) throw new ApiError(422, "mailhub_autonomy_replay_key_invalid");
  const envelope = await request<{ data?: MailHubAutonomyRun }>("/api/mail/autonomy/runs", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      connection_id: connectionId,
      replay_key: replayKey,
      limit,
      message_limit: messageLimit,
      run_inline: false,
    }),
  });
  if (!envelope.data) throw new ApiError(502, "mailhub_autonomy_response_invalid");
  return envelope.data;
}

export async function listMailAutonomyRuns(
  connectionId?: string,
  limit = 50,
): Promise<MailHubAutonomyRun[]> {
  const params = new URLSearchParams({ limit: String(Math.min(Math.max(limit, 1), 200)) });
  if (connectionId) params.set("connection_id", connectionId);
  const envelope = await request<{ data?: MailHubAutonomyRun[] }>(`/api/mail/autonomy/runs?${params}`);
  return Array.isArray(envelope.data) ? envelope.data : [];
}

export async function getMailAutonomyRun(runId: string): Promise<MailHubAutonomyRun> {
  const envelope = await request<{ data?: MailHubAutonomyRun }>(
    `/api/mail/autonomy/runs/${encodeURIComponent(runId)}`,
  );
  if (!envelope.data) throw new ApiError(502, "mailhub_autonomy_response_invalid");
  return envelope.data;
}

export async function controlMailAutonomyRun(
  runId: string,
  command: "run" | "pause" | "resume" | "cancel",
  reason?: string,
): Promise<MailHubAutonomyRun> {
  if (command === "pause" && !reason?.trim()) {
    throw new ApiError(422, "mailhub_autonomy_pause_reason_required");
  }
  const envelope = await request<{ data?: MailHubAutonomyRun }>(
    `/api/mail/autonomy/runs/${encodeURIComponent(runId)}:${command}`,
    {
      method: "POST",
      body: command === "pause" ? JSON.stringify({ reason }) : undefined,
    },
  );
  if (!envelope.data) throw new ApiError(502, "mailhub_autonomy_response_invalid");
  return envelope.data;
}

export async function listMailThreads(limit = 50): Promise<MailHubThread[]> {
  const page = await listMailThreadPage({ limit });
  return page.data;
}

export async function listMailThreadPage(options: {
  limit?: number;
  cursor?: string | null;
  connectionId?: string;
  unread?: boolean;
  important?: boolean;
  attachment?: boolean;
  project?: boolean;
  candidate?: boolean;
} = {}): Promise<MailHubThreadPage> {
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
  const envelope = await request<Partial<MailHubThreadPage>>(`/api/mail/threads?${params}`);
  return {
    data: Array.isArray(envelope.data) ? envelope.data : [],
    next_cursor: typeof envelope.next_cursor === "string" ? envelope.next_cursor : null,
    has_more: envelope.has_more === true,
    filters: envelope.filters,
  };
}

export async function getMailThread(
  threadId: string,
  limit = 200,
): Promise<MailHubThreadDetail> {
  const envelope = await request<{ data?: MailHubThreadDetail }>(
    `/api/mail/threads/${encodeURIComponent(threadId)}?limit=${Math.min(Math.max(limit, 1), 500)}`,
  );
  if (!envelope.data) throw new ApiError(502, "mailhub_thread_response_invalid");
  return envelope.data;
}

export async function listMailMessages(limit = 50): Promise<MailHubMessage[]> {
  const envelope = await request<{ data?: MailHubMessage[] }>(`/api/mail/messages?limit=${Math.min(Math.max(limit, 1), 200)}`);
  return Array.isArray(envelope.data) ? envelope.data : [];
}

export async function searchMailMessages(
  query: string,
  limit = 50,
): Promise<MailHubMessage[]> {
  const normalized = query.trim();
  if (!normalized) return [];
  const envelope = await request<{ data?: MailHubMessage[] }>(
    `/api/mail/search?q=${encodeURIComponent(normalized)}&limit=${Math.min(Math.max(limit, 1), 200)}`,
  );
  return Array.isArray(envelope.data) ? envelope.data : [];
}

export async function getMailMessageContent(messageId: string): Promise<string> {
  return requestText(`/api/mail/messages/${encodeURIComponent(messageId)}/content`);
}

export async function analyzeMailMessage(messageId: string): Promise<Record<string, unknown>> {
  const envelope = await request<{ data?: Record<string, unknown> }>(
    `/api/mail/messages/${encodeURIComponent(messageId)}:analyze`,
    { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() } },
  );
  return envelope.data ?? {};
}

export async function listMailCandidates(
  status?: string,
  limit = 50,
): Promise<MailHubCandidate[]> {
  const params = new URLSearchParams({ limit: String(Math.min(Math.max(limit, 1), 200)) });
  if (status) params.set("status", status);
  const envelope = await request<{ data?: MailHubCandidate[] }>(`/api/mail/candidates?${params}`);
  return Array.isArray(envelope.data) ? envelope.data : [];
}

export async function reviewMailCandidate(
  candidateId: string,
  approved: boolean,
  expectedRevision: number,
  reviewReason?: string,
): Promise<Record<string, unknown>> {
  return request(`/api/mail/candidates/${encodeURIComponent(candidateId)}:review`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      approved,
      expected_revision: expectedRevision,
      review_reason: reviewReason || null,
    }),
  });
}

export async function applyMailCandidate(
  candidateId: string,
  expectedRevision: number,
): Promise<Record<string, unknown>> {
  if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
    throw new ApiError(422, "mailhub_candidate_revision_invalid");
  }
  return request(`/api/mail/candidates/${encodeURIComponent(candidateId)}:apply`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ expected_revision: expectedRevision }),
  });
}

export async function createMailDraft(body: {
  connectionId: string;
  threadId?: string | null;
  recipientAddresses: string[];
  ccAddresses?: string[];
  bccAddresses?: string[];
  attachmentRefs?: string[];
  subject: string;
  bodyText: string;
}): Promise<MailHubDraft> {
  const envelope = await request<{ data?: MailHubDraft }>("/api/mail/drafts", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      connection_id: body.connectionId,
      thread_id: body.threadId ?? null,
      recipient_addresses: body.recipientAddresses,
      cc_addresses: body.ccAddresses ?? [],
      bcc_addresses: body.bccAddresses ?? [],
      attachment_refs: body.attachmentRefs ?? [],
      subject: body.subject,
      body_text: body.bodyText,
    }),
  });
  if (!envelope.data) throw new ApiError(502, "mailhub_draft_response_invalid");
  return envelope.data;
}

export async function getMailDraft(draftId: string): Promise<MailHubDraft> {
  const envelope = await request<{ data?: MailHubDraft }>(`/api/mail/drafts/${encodeURIComponent(draftId)}`);
  if (!envelope.data) throw new ApiError(502, "mailhub_draft_response_invalid");
  return envelope.data;
}

export async function sendMailDraft(body: {
  draftId: string;
  expectedRevision: number;
  expectedContentSha256: string;
  expectedRecipientDigest: string;
  confirmationRef: string;
}): Promise<Record<string, unknown>> {
  return request(`/api/mail/drafts/${encodeURIComponent(body.draftId)}:send`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      expected_revision: body.expectedRevision,
      expected_content_sha256: body.expectedContentSha256,
      expected_recipient_digest: body.expectedRecipientDigest,
      confirmation_ref: body.confirmationRef,
    }),
  });
}

export async function updateConversation(
  conversationId: string,
  changes: { title?: string; status?: "active" | "archived" },
): Promise<ConversationDirectoryEntry> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
}

export async function deleteConversation(
  conversationId: string,
): Promise<void> {
  return request(`/api/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
  });
}

export async function sendConversationMessage(
  conversationId: string,
  text: string,
  attachments: Pick<DriveFile, "objectRef" | "name">[] = [],
  agentId?: string,
  signal?: AbortSignal,
): Promise<ApplicationInteractionEventPageV1> {
  return request(
    `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
    {
      method: "POST",
      signal,
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        text,
        attachments: attachments.map((attachment) => ({
          object_ref: attachment.objectRef,
          name: attachment.name,
        })),
        agent_id: agentId || null,
      }),
    },
  );
}

export async function replayConversation(
  conversationId: string,
  afterCursor?: string | null,
): Promise<ApplicationInteractionEventPageV1> {
  const query = afterCursor
    ? `?after_cursor=${encodeURIComponent(afterCursor)}`
    : "";
  return request(
    `/api/conversations/${encodeURIComponent(conversationId)}/events${query}`,
  );
}

export async function decideApproval(
  requestId: string,
  body: ApprovalDecisionRequest,
): Promise<ApplicationInteractionEventPageV1> {
  return request(`/api/approvals/${encodeURIComponent(requestId)}/decisions`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

export async function listReviews(
  afterCursor: string | null = null,
  limit = 20,
): Promise<ReviewInboxProjection> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (afterCursor) params.set("after_cursor", afterCursor);
  return request(`/api/reviews?${params.toString()}`);
}

export async function listAgentSharePublicationFollowups(
  afterCursor: string | null = null,
  limit = 20,
): Promise<ReviewInboxProjection> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (afterCursor) params.set("after_cursor", afterCursor);
  return request(`/api/agent-share-publication-followups?${params.toString()}`);
}

export async function listWorkItems(
  afterCursor: string | null = null,
  limit = 20,
): Promise<WorkItemPageProjection> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (afterCursor) params.set("after_cursor", afterCursor);
  return request(`/api/work-items?${params.toString()}`);
}

export async function listDriveFiles(): Promise<DriveFile[]> {
  return request("/api/drive/files");
}

export async function uploadDriveFile(file: File): Promise<DriveFile> {
  return uploadDriveFileWithProgress(file);
}

export async function uploadDriveFileWithProgress(
  file: File,
  options: { onProgress?: (percent: number) => void } = {},
): Promise<DriveFile> {
  const created = await request<{
    sessionId: string;
    status: string;
    uploadMethod: string;
    expiresAt: string;
    traceId: string;
  }>("/api/drive/uploads", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      scope: "personal",
      name: file.name,
      content_type: file.type || "application/octet-stream",
      size_bytes: file.size,
      sensitivity: "internal",
    }),
  });
  const contentPath = `/api/drive/uploads/${encodeURIComponent(created.sessionId)}/content`;
  const contentIdempotencyKey = crypto.randomUUID();
  let lastUploadError: unknown;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await uploadBinaryContent(
        contentPath,
        file,
        contentIdempotencyKey,
        options.onProgress,
      );
      lastUploadError = undefined;
      break;
    } catch (error) {
      lastUploadError = error;
    }
  }
  if (lastUploadError) throw lastUploadError;
  return request(
    `/api/drive/uploads/${encodeURIComponent(created.sessionId)}/commit`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ checksum: null }),
    },
  );
}

function uploadBinaryContent(
  path: string,
  file: File,
  idempotencyKey: string,
  onProgress?: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `${BFF_BASE_URL}${path}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader(
      "Content-Type",
      file.type || "application/octet-stream",
    );
    xhr.setRequestHeader("Idempotency-Key", idempotencyKey);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        onProgress?.(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onerror = () => reject(new ApiError(0, "upload_network_error"));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        resolve();
      } else {
        reject(new ApiError(xhr.status, `http_${xhr.status}`));
      }
    };
    xhr.send(file);
  });
}

export async function previewDriveFile(
  objectRef: string,
): Promise<DriveFilePreview> {
  return request(
    `/api/drive/files/${encodeURIComponent(coreObjectId(objectRef))}/preview`,
  );
}

export async function createDriveDownload(
  objectRef: string,
): Promise<DriveSignedDownload> {
  return request(
    `/api/drive/files/${encodeURIComponent(coreObjectId(objectRef))}/download-url`,
    { method: "POST" },
  );
}

export async function listKnowledgeSources(): Promise<KnowledgeSource[]> {
  return request("/api/knowledge/sources");
}

export async function createKnowledgeSource(body: {
  objectRef: string;
  title?: string;
  scope: "personal" | "org";
  version?: string;
  effectiveAt?: string;
  expiresAt?: string;
}): Promise<KnowledgeSource> {
  const created = await request<KnowledgeSource>("/api/knowledge/sources", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      object_ref: body.objectRef,
      title: body.title || null,
      scope: body.scope,
      version: body.version || null,
      effective_at: body.effectiveAt || null,
      expires_at: body.expiresAt || null,
    }),
  });
  void trackProductEvent({
    event_name: "knowledge_source_created",
    surface: "knowledge",
    outcome: "success",
  });
  return created;
}

export async function updateKnowledgeSource(
  sourceId: string,
  changes: { status?: "active" | "disabled"; title?: string },
): Promise<KnowledgeSource> {
  return request(`/api/knowledge/sources/${encodeURIComponent(sourceId)}`, {
    method: "PATCH",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(changes),
  });
}

export async function reindexKnowledgeSource(
  sourceId: string,
): Promise<KnowledgeSource> {
  return request(
    `/api/knowledge/sources/${encodeURIComponent(sourceId)}/reindex`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    },
  );
}

export async function deleteKnowledgeSource(sourceId: string): Promise<void> {
  return request(`/api/knowledge/sources/${encodeURIComponent(sourceId)}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}

export async function searchKnowledge(
  query: string,
  sourceIds: string[] = [],
): Promise<KnowledgeSearchResponse> {
  return request("/api/knowledge/query", {
    method: "POST",
    body: JSON.stringify({ query, source_ids: sourceIds, top_k: 5 }),
  });
}

export async function listApps(): Promise<AppEntry[]> {
  return request("/api/apps");
}

export async function setAppPinned(
  appId: string,
  pinned: boolean,
): Promise<void> {
  return request(`/api/preferences/apps/${encodeURIComponent(appId)}/pin`, {
    method: pinned ? "PUT" : "DELETE",
    headers: {
      "Idempotency-Key": crypto.randomUUID(),
    },
  });
}

export async function listToolPackages(): Promise<ToolPackageProjection[]> {
  return request("/api/tool-packages");
}

export async function getDataStructuringReadiness(): Promise<DataStructuringReadiness> {
  return request("/api/data-structuring/readiness");
}

export async function invokeDataStructuringTool(body: {
  packageId: string;
  capabilityId: string;
  input: Record<string, unknown>;
  userConfirmation?: boolean;
}): Promise<DataStructuringInvocation> {
  return request("/api/data-structuring/invoke", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listProjects(): Promise<ProjectProjection[]> {
  return request("/api/projects");
}

export async function getProjectPortfolioWorkbench(): Promise<ProjectPortfolioWorkbench> {
  return request("/api/project-portfolio/workbench");
}

export async function getProjectPortfolioMaturity(): Promise<ProjectPortfolioMaturity> {
  return request("/api/project-portfolio/maturity");
}

export async function getProjectPortfolioResources(): Promise<ProjectPortfolioResources> {
  return request("/api/project-portfolio/resources");
}

export async function getProjectPortfolioRecommendations(): Promise<ProjectPortfolioRecommendations> {
  return request("/api/project-portfolio/recommendations");
}

export async function searchProjectPortfolio(
  query: string,
): Promise<ProjectPortfolioSearch> {
  return request(
    `/api/project-portfolio/search?q=${encodeURIComponent(query)}`,
  );
}

export async function listProjectRisks(
  projectId: string,
): Promise<ProjectRiskRecord[]> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/risks`);
}

export async function createProjectRisk(
  projectId: string,
  body: {
    title: string;
    category: string;
    severity: "low" | "medium" | "high" | "critical";
    probability: number;
    owner_ref?: string | null;
    mitigation?: string | null;
    trigger_condition?: string | null;
  },
): Promise<ProjectRiskRecord> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/risks`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getProjectScheduleInsights(
  projectId: string,
): Promise<ProjectScheduleInsights> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/schedule-insights`,
  );
}

export async function getRuntimeVersions(): Promise<RuntimeVersions> {
  return request("/api/system/runtime");
}

export async function createProjectPlanningSession(
  body: {
    mode: "sentence" | "file";
    project_name: string;
    project_goal: string;
    project_scope?: string | null;
    industry?: string | null;
    start_at?: string | null;
    end_at?: string | null;
    object_ref?: string | null;
    file_name?: string | null;
    media_type?: string | null;
    instruction?: string | null;
  },
  idempotencyKey: string,
): Promise<ProjectPlanningSession> {
  return request("/api/project-planning/sessions", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(body),
  });
}

export async function generateProjectBlueprint(
  sessionId: string,
  body: {
    instruction?: string | null;
    planning_profile?: "balanced" | "concise" | "detailed";
    base_draft_id?: string | null;
  },
  idempotencyKey: string,
): Promise<ProjectPlanningJob> {
  return request(
    `/api/project-planning/sessions/${encodeURIComponent(sessionId)}/generate`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    },
  );
}

export async function getProjectPlanningJob(
  jobId: string,
): Promise<ProjectPlanningJob> {
  return request(`/api/project-planning/jobs/${encodeURIComponent(jobId)}`);
}

export async function cancelProjectPlanningJob(
  jobId: string,
  idempotencyKey: string,
): Promise<ProjectPlanningJob> {
  return request(
    `/api/project-planning/jobs/${encodeURIComponent(jobId)}/cancel`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
}

export async function getProjectBlueprint(
  draftId: string,
): Promise<ProjectBlueprint> {
  return request(
    `/api/project-planning/blueprints/${encodeURIComponent(draftId)}`,
  );
}

export async function updateProjectBlueprint(
  draftId: string,
  expectedRevision: number,
  blueprint: ProjectBlueprint["blueprint"],
): Promise<ProjectBlueprint> {
  return request(
    `/api/project-planning/blueprints/${encodeURIComponent(draftId)}`,
    {
      method: "PUT",
      body: JSON.stringify({ expected_revision: expectedRevision, blueprint }),
    },
  );
}

export async function approveProjectBlueprint(
  draftId: string,
  expectedRevision: number,
): Promise<ProjectBlueprint> {
  return request(
    `/api/project-planning/blueprints/${encodeURIComponent(draftId)}/approve`,
    {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    },
  );
}

export async function rejectProjectBlueprint(
  draftId: string,
  expectedRevision: number,
): Promise<ProjectBlueprint> {
  return request(
    `/api/project-planning/blueprints/${encodeURIComponent(draftId)}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    },
  );
}

export async function syncProjectBlueprint(
  draftId: string,
  expectedRevision: number,
): Promise<ProjectBlueprintSync> {
  return request(
    `/api/project-planning/blueprints/${encodeURIComponent(draftId)}/sync`,
    {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    },
  );
}

export async function getProjectTree(
  projectId: string,
): Promise<ProjectTreeProjection> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/tree`);
}

export async function getProjectActivities(
  projectId: string,
): Promise<ProjectActivityPageProjection> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/activities`);
}

export async function getProjectOperationsDashboard(
  projectId: string,
): Promise<ProjectOperationsDashboardProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/operations/dashboard`,
  );
}

export async function getProjectAgentPolicy(
  projectId: string,
): Promise<ProjectAgentPolicyProjection> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/agent-policy`);
}

export async function updateProjectAgentPolicy(
  projectId: string,
  policy: {
    expert_source: "PUBLIC" | "PROJECT";
    expert_ref: string | null;
    expert_name: string | null;
    is_enabled: boolean;
    default_rules_enabled: boolean;
    memory_scopes: Array<"project" | "user" | "expert">;
    operation_policy: Record<string, unknown>;
  },
): Promise<ProjectAgentPolicyProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/agent-policy`,
    {
      method: "PUT",
      body: JSON.stringify(policy),
    },
  );
}

export async function generateProjectOperationalReport(
  projectId: string,
): Promise<ProjectOperationalReportProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/operations/reports`,
    {
      method: "POST",
    },
  );
}

export async function scanProjectOperations(projectId: string): Promise<{
  project_id: string;
  checked_at: string;
  created_count: number;
  events: Array<Record<string, unknown>>;
}> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/operations/scan`,
    {
      method: "POST",
    },
  );
}

export async function getProjectNotificationPreference(
  projectId: string,
): Promise<ProjectNotificationPreferenceProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/notification-preferences/me`,
  );
}

export async function updateProjectNotificationPreference(
  projectId: string,
  preference: {
    channels: Array<"in_app" | "email" | "external">;
    external_destination_refs: string[];
    event_types: string[];
    quiet_hours: Record<string, unknown>;
    is_enabled: boolean;
  },
): Promise<ProjectNotificationPreferenceProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/notification-preferences/me`,
    {
      method: "PUT",
      body: JSON.stringify(preference),
    },
  );
}

export async function listIntegrationEventCatalog(): Promise<
  IntegrationEventCatalogItem[]
> {
  return request("/api/integrations/catalog");
}

export async function getIntegrationDeveloperDocs(): Promise<
  Record<string, unknown>
> {
  return request("/api/integrations/docs");
}

export async function listIntegrationDestinations(): Promise<
  IntegrationDestination[]
> {
  return request("/api/integrations/destinations");
}

export async function createIntegrationDestination(body: {
  name: string;
  endpoint_url: string;
  environment: "test" | "production";
}): Promise<{ destination: IntegrationDestination; secret_once: string }> {
  return request("/api/integrations/destinations", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function publishIntegrationDestination(
  destinationId: string,
  enable = true,
): Promise<IntegrationDestination> {
  return request(
    `/api/integrations/destinations/${encodeURIComponent(destinationId)}:publish?enable=${enable}`,
    { method: "POST" },
  );
}

export async function rotateIntegrationDestinationSecret(
  destinationId: string,
): Promise<{ destination: IntegrationDestination; secret_once: string }> {
  return request(
    `/api/integrations/destinations/${encodeURIComponent(destinationId)}:rotate-secret`,
    { method: "POST" },
  );
}

export async function testIntegrationDestination(
  destinationId: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/integrations/destinations/${encodeURIComponent(destinationId)}:test`,
    {
      method: "POST",
    },
  );
}

export async function listIntegrationSubscriptions(): Promise<
  Array<{
    id: string;
    destination_id: string;
    topics: string[];
    project_ids: string[];
    filters: Record<string, unknown>;
    data_policy: string;
    enabled: boolean;
  }>
> {
  return request("/api/integrations/subscriptions");
}

export async function createIntegrationSubscription(body: {
  destination_id: string;
  topics: string[];
  project_ids: string[];
  filters: Record<string, unknown>;
  data_policy: "reference_only" | "summary";
}): Promise<Record<string, unknown>> {
  return request("/api/integrations/subscriptions", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listIntegrationDeliveries(): Promise<
  IntegrationDelivery[]
> {
  return request("/api/integrations/deliveries");
}

export async function processIntegrationDeliveries(): Promise<
  Array<Record<string, unknown>>
> {
  return request("/api/integrations/deliveries:process", { method: "POST" });
}

export async function replayIntegrationDelivery(
  deliveryId: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/integrations/deliveries/${encodeURIComponent(deliveryId)}:replay`,
    {
      method: "POST",
    },
  );
}

export async function listIntegrationEndpoints(): Promise<
  IntegrationEndpoint[]
> {
  return request("/api/integrations/endpoints");
}

export async function createIntegrationEndpoint(body: {
  name: string;
  environment: "test" | "production";
  provider: string;
  verifier: "generic_hmac" | "github_hmac" | "tapd_hmac" | "cnb_hmac";
  handler_ref: string | null;
  allowed_ip_cidrs: string[];
}): Promise<{
  endpoint: IntegrationEndpoint;
  endpoint_key_once: string;
  signing_secret_once: string;
}> {
  return request("/api/integrations/endpoints", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listIntegrationReceipts(): Promise<IntegrationReceipt[]> {
  return request("/api/integrations/receipts");
}

export async function processIntegrationReceipts(): Promise<
  Array<Record<string, unknown>>
> {
  return request("/api/integrations/receipts:process", { method: "POST" });
}

export async function actOnIntegrationReceipt(
  receiptId: string,
  action: "replay" | "discard" | "reroute",
  handlerRef?: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/integrations/receipts/${encodeURIComponent(receiptId)}:act`,
    {
      method: "POST",
      body: JSON.stringify({ action, handler_ref: handlerRef }),
    },
  );
}

export interface IntegrationAuditRecord {
  id: string;
  actor_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  metadata: Record<string, unknown>;
  occurred_at: string;
}

export async function listIntegrationAudit(): Promise<
  IntegrationAuditRecord[]
> {
  return request("/api/integrations/audit");
}

export async function getProjectWorkspaceContext(
  projectId: string,
): Promise<ProjectWorkspaceContextProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/workspace-context`,
  );
}

export async function getTaskWorkspace(
  taskId: string,
): Promise<TaskWorkspaceProjection> {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/workspace`);
}

export async function linkTaskContent(
  taskId: string,
  file: DriveFile,
  role: "INPUT" | "REFERENCE" | "WORKING" | "OUTPUT" | "EVIDENCE" = "INPUT",
): Promise<TaskWorkspaceProjection["contents"][number]> {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/contents`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      object_ref: file.objectRef,
      title: file.name,
      mime_type: file.mime,
      role,
      version: file.version,
      visibility: "PROJECT",
    }),
  });
}

export async function updateTaskContent(
  taskId: string,
  contentId: string,
  patch: {
    role?: string;
    title?: string;
    version?: string;
    visibility?: string;
  },
): Promise<TaskWorkspaceProjection["contents"][number]> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/contents/${encodeURIComponent(contentId)}`,
    {
      method: "PATCH",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(patch),
    },
  );
}

export async function unlinkTaskContent(
  taskId: string,
  contentId: string,
): Promise<void> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/contents/${encodeURIComponent(contentId)}`,
    { method: "DELETE", headers: { "Idempotency-Key": crypto.randomUUID() } },
  );
}

export async function getTaskContentVersions(
  taskId: string,
  contentId: string,
): Promise<Array<Record<string, unknown>>> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/contents/${encodeURIComponent(contentId)}/versions`,
  );
}

export async function batchUpdateTasks(body: {
  updates: Array<{
    task_id: string;
    state?: string;
    start_at?: string | null;
    end_at?: string | null;
    assignments?: Array<{
      target_type: "USER" | "ROLE" | "TEAM" | "AGENT" | "SYSTEM";
      target_id: string;
    }>;
    tags?: string[];
  }>;
  reason?: string;
}): Promise<Array<Record<string, unknown>>> {
  return request("/api/tasks/batch", {
    method: "PATCH",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

export async function replaceTaskAssignments(
  taskId: string,
  assignments: Array<{
    target_type: "USER" | "ROLE" | "TEAM" | "AGENT" | "SYSTEM";
    target_id: string;
    weight?: number;
  }>,
  reason: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/assignments/replace`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ assignments, reason }),
    },
  );
}

export async function runProjectTaskAction(
  taskId: string,
  action:
    | "claim"
    | "complete"
    | "start_ai"
    | "start"
    | "pause"
    | "resume"
    | "submit"
    | "return",
  reason?: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/actions/${encodeURIComponent(action)}`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ reason: reason || null }),
    },
  );
}

export async function createTaskTimeLog(
  taskId: string,
  body: {
    hours: number;
    description?: string | null;
    logged_at?: string | null;
    resource_id?: string | null;
  },
): Promise<Record<string, unknown>> {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/time-logs`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

export async function submitTaskDeliverable(
  taskId: string,
  body: {
    submission_id: string;
    requirement_id?: string | null;
    kind: string;
    source_type: "UPLOAD" | "DRIVE" | "EMAIL" | "URL" | "AGENT";
    source_ref: Record<string, unknown>;
    title?: string | null;
    mime_type?: string | null;
    version?: string | null;
    hash?: string | null;
  },
): Promise<Record<string, unknown>> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/deliverable-submissions`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    },
  );
}

export async function decideDeliverableAcceptance(
  deliverableId: string,
  decision: "ACCEPTED" | "RETURNED",
  reason?: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/deliverables/${encodeURIComponent(deliverableId)}/acceptance`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ decision, reason: reason || null, evidence: {} }),
    },
  );
}

export async function verifyDeliverable(
  deliverableId: string,
  payload: Record<string, unknown> = {},
): Promise<Record<string, unknown>> {
  return request(
    `/api/deliverables/${encodeURIComponent(deliverableId)}/verification`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ payload }),
    },
  );
}

export async function listProjectAcceptanceQueue(
  projectId: string,
): Promise<AcceptanceQueueProjection[]> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/acceptance-queue`,
  );
}

export async function batchDecideDeliverableAcceptance(
  items: Array<{
    deliverable_id: string;
    decision: "ACCEPTED" | "RETURNED";
    reason?: string | null;
  }>,
): Promise<Array<Record<string, unknown>>> {
  return request("/api/deliverables/batch-acceptance", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ items }),
  });
}

export async function cancelProjectTaskExecution(
  taskId: string,
  executionId: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/executions/${encodeURIComponent(executionId)}/cancel`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    },
  );
}

export async function decideTaskReplanDraft(
  taskId: string,
  draftId: string,
  decision: "approve" | "reject",
  reason?: string,
): Promise<Record<string, unknown>> {
  return request(
    `/api/tasks/${encodeURIComponent(taskId)}/replan-drafts/${encodeURIComponent(draftId)}/${decision}`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ reason: reason || null }),
    },
  );
}

export async function createTaskDependency(
  projectId: string,
  body: {
    predecessor_task_id: string;
    successor_task_id: string;
    dependency_type: "FS" | "SS" | "FF" | "SF";
    hard: boolean;
    lag_minutes: number;
    description?: string | null;
  },
): Promise<Record<string, unknown>> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/dependencies`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    },
  );
}

export async function deleteTaskDependency(
  projectId: string,
  dependencyId: string,
  expectedRevision: number,
): Promise<void> {
  const query = new URLSearchParams({
    expected_revision: String(expectedRevision),
  });
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/dependencies/${encodeURIComponent(dependencyId)}?${query.toString()}`,
    { method: "DELETE", headers: { "Idempotency-Key": crypto.randomUUID() } },
  );
}

export async function previewProjectSchedule(
  projectId: string,
  body: { anchor_at?: string | null; calendar_id?: string | null } = {},
): Promise<ProjectScheduleProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/schedule/preview`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export async function applyProjectSchedule(
  projectId: string,
  body: { anchor_at?: string | null; calendar_id?: string | null } = {},
): Promise<ProjectScheduleProjection> {
  return request(
    `/api/projects/${encodeURIComponent(projectId)}/schedule/apply`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    },
  );
}

export async function createProjectTaskProposal(
  body: {
    project_id: string;
    title: string;
    description: string | null;
    stage_id: string | null;
  },
  idempotencyKey: string = crypto.randomUUID(),
): Promise<ProjectTaskProposal> {
  return request("/api/project-task-proposals", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(body),
  });
}

export async function decideProjectTaskProposal(
  proposalId: string,
  proposalVersion: number,
  decision: "allow" | "deny",
  idempotencyKey: string = crypto.randomUUID(),
): Promise<ProjectTaskProposal> {
  return request(
    `/api/project-task-proposals/${encodeURIComponent(proposalId)}/decisions`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        decision,
        expected_proposal_version: proposalVersion,
      }),
    },
  );
}

export async function listSkills(): Promise<SkillEntry[]> {
  return request("/api/skills");
}

export async function listAgents(): Promise<AgentEntry[]> {
  return request("/api/agents");
}

export async function copyAgent(
  agentId: string,
  displayName?: string,
): Promise<AgentEntry> {
  const copied = await request<AgentEntry>(
    `/api/agents/${encodeURIComponent(agentId)}/copies`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ display_name: displayName?.trim() || null }),
    },
  );
  void trackProductEvent({
    event_name: "expert_copied",
    surface: "expert",
    outcome: "success",
    expert_scope: "public",
  });
  return copied;
}

export async function getAgentDraft(agentId: string): Promise<AgentDraft> {
  return request(`/api/agents/${encodeURIComponent(agentId)}/draft`);
}

export async function createAgentDraft(
  body: AgentDraftWrite,
): Promise<AgentDraft> {
  return request("/api/agents", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

export async function updateAgentDraft(
  agentId: string,
  body: AgentDraftWrite,
): Promise<AgentDraft> {
  return request(`/api/agents/${encodeURIComponent(agentId)}`, {
    method: "PUT",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

export async function trialAgentDraft(
  agentId: string,
  message: string,
): Promise<AgentTrial> {
  return request(`/api/agents/${encodeURIComponent(agentId)}/test-runs`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ message }),
  });
}

export async function publishAgentDraft(agentId: string): Promise<AgentEntry> {
  return request(`/api/agents/${encodeURIComponent(agentId)}/publish`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({}),
  });
}

export async function getAgentShareRequest(
  agentId: string,
): Promise<AgentShareRequest> {
  return request(`/api/agents/${encodeURIComponent(agentId)}/share-request`);
}

export async function createAgentShareRequest(
  agentId: string,
  reason: string,
): Promise<AgentShareRequest> {
  return request(`/api/agents/${encodeURIComponent(agentId)}/share-requests`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ reason }),
  });
}

export async function decideAgentShareRequest(
  requestId: string,
  expectedRequestVersion: number,
  decision: "allow" | "deny",
): Promise<AgentShareRequest> {
  return request(
    `/api/agent-share-requests/${encodeURIComponent(requestId)}/decisions`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        expected_request_version: expectedRequestVersion,
        decision,
      }),
    },
  );
}

export async function changeSkillInstallation(
  packageId: string,
  version: string,
  action: "install" | "enable" | "disable" | "update" | "uninstall",
  confirmation?: {
    acknowledgedDigest: string;
    confirmedPermissionRefs: string[];
  },
): Promise<import("../types/domain").SkillInstallation> {
  return request(
    `/api/skills/${encodeURIComponent(packageId)}/versions/${encodeURIComponent(version)}/installation`,
    {
      method: "PUT",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        action,
        acknowledged_digest: confirmation?.acknowledgedDigest,
        confirmed_permission_refs: confirmation?.confirmedPermissionRefs ?? [],
      }),
    },
  );
}

export async function submitFrontdeskFeedback(
  actionId: string,
  verdict: "accepted" | "modified" | "rejected",
  context: {
    runId?: string;
    agentId?: string;
    agentVersion?: string;
    comment?: string;
    before?: string;
    after?: string;
  } = {},
): Promise<{ recorded: boolean; learning_queued?: boolean }> {
  return request("/api/frontdesk/feedback", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      action_id: actionId,
      run_id: context.runId || null,
      agent_id: context.agentId || "frontdesk",
      agent_version: context.agentVersion || null,
      verdict,
      comment: context.comment || "",
      before: context.before || "",
      after: context.after || "",
    }),
  });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BFF_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
    },
  });
  if (!response.ok) {
    let code = `http_${response.status}`;
    let detail: unknown = null;
    try {
      const body = (await response.json()) as { detail?: unknown };
      detail = body.detail ?? null;
      if (typeof body.detail === "string") code = body.detail;
      else if (
        body.detail &&
        typeof body.detail === "object" &&
        "code" in body.detail &&
        typeof body.detail.code === "string"
      )
        code = body.detail.code;
    } catch {
      // Error bodies are deliberately treated as optional and untrusted.
    }
    throw new ApiError(response.status, code, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function requestText(path: string, init: RequestInit = {}): Promise<string> {
  const response = await fetch(`${BFF_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "text/plain",
      ...init.headers,
    },
  });
  if (!response.ok) {
    let code = `http_${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") code = body.detail;
    } catch {
      // Text error responses are deliberately treated as untrusted.
    }
    throw new ApiError(response.status, code);
  }
  return response.text();
}

function coreObjectId(objectRef: string): string {
  const prefix = "obj://core/";
  if (!objectRef.startsWith(prefix) || objectRef.length === prefix.length) {
    throw new ApiError(400, "invalid_core_object_ref");
  }
  return objectRef.slice(prefix.length);
}
