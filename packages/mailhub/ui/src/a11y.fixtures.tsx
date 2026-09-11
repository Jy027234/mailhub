/**
 * Shared fixtures for the accessibility suites.
 *
 * The jsdom suite (`a11y.test.tsx`) proves semantics; the browser suite
 * (`a11y.browser.test.tsx`) proves rendering.  Both must exercise the *same*
 * DOM, otherwise the two halves could pass against different trees, so the
 * fake host client lives here and nowhere else.
 */

import type { MailHubUiClient, MailHubUiThread } from "./index.js";

export const THREADS: MailHubUiThread[] = [
  {
    thread_id: "thread-1",
    connection_id: "connection-1",
    account_email: "ops@example.test",
    normalized_subject: "Delivery schedule",
    participant_addresses: ["buyer@example.test"],
    latest_at: "2026-08-19T08:00:00Z",
    message_count: 2,
    revision: 1,
    unread: true,
    has_attachment: true,
  },
];

export function fakeClient(overrides: Partial<MailHubUiClient> = {}): MailHubUiClient {
  const base: MailHubUiClient = {
    listConnections: async () => [
      {
        connection_id: "connection-1",
        provider: "imap_smtp",
        email_address: "ops@example.test",
        status: "active",
        revision: 3,
        granted_scopes: ["mail.read"],
        content_mode: "bounded_processing",
        sync_state: "healthy",
        last_sync_at: "2026-08-19T07:00:00Z",
      },
    ],
    listThreads: async () => THREADS,
    listThreadPage: async () => ({ data: THREADS, next_cursor: null, has_more: false }),
    getThread: async () => ({
      thread: THREADS[0],
      messages: [
        {
          message_id: "message-1",
          thread_id: "thread-1",
          sender_address: "buyer@example.test",
          recipient_addresses: ["ops@example.test"],
          subject: "Delivery schedule",
          received_at: "2026-08-19T08:00:00Z",
        },
      ],
      content_policy: "metadata_only",
    }),
    getMessageContent: async () => "Quoted delivery window.",
    listCandidates: async () => [
      {
        candidate_id: "candidate-1",
        candidate_type: "task",
        state: "proposed",
        revision: 2,
        title: "Confirm delivery date",
        summary: "Supplier proposed a new window.",
        confidence: 0.7,
      },
    ],
    reviewCandidate: async () => ({}),
    enqueueSync: async () => ({}),
    createDraft: async () => {
      throw new Error("createDraft is not used by the workspace tests");
    },
    sendDraft: async () => ({}),
  };
  return { ...base, ...overrides };
}
