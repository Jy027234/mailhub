/**
 * Headless accessibility gate (WCAG 2.2 AA, semantics half).
 *
 * jsdom has no layout engine, so this suite proves roles, accessible names,
 * ARIA wiring, heading order, keyboard operability and live regions.  Colour
 * contrast, target size, reflow/zoom and motion are explicitly out of scope
 * here and must be checked in a browser — see the coverage table in README.md.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it } from "vitest";

import { MailHubWorkspace, type MailHubUiClient, type MailHubUiThread } from "./index.js";
import { MailHubStandalone } from "./standalone.js";

// vitest runs without globals here, so Testing Library cannot register its own
// auto-cleanup; unmounting explicitly keeps each assertion on one document.
afterEach(cleanup);

const THREADS: MailHubUiThread[] = [
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

function fakeClient(overrides: Partial<MailHubUiClient> = {}): MailHubUiClient {
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

async function violations(container: HTMLElement): Promise<string[]> {
  const results = await axe.run(container, {
    rules: {
      // No layout engine in jsdom: contrast and target size need a browser.
      "color-contrast": { enabled: false },
      "target-size": { enabled: false },
    },
  });
  return results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? "unknown"}): ${violation.nodes
        .map((node) => node.target.join(" "))
        .join(" | ")}`,
  );
}

describe("harness liveness", () => {
  it("flags a known violation, proving the axe gate can fail", async () => {
    const { container } = render(
      <div>
        <button type="button" aria-label="" />
        <img src="unlabelled.png" />
      </div>,
    );
    const found = await violations(container);
    expect(found.some((item) => item.startsWith("button-name"))).toBe(true);
    expect(found.some((item) => item.startsWith("image-alt"))).toBe(true);
  });
});

describe("workspace semantics", () => {
  it("renders the inbox with no detectable violations", async () => {
    const { container } = render(<MailHubWorkspace client={fakeClient()} />);
    await screen.findByText("Delivery schedule");
    expect(await violations(container)).toEqual([]);
  });

  it("renders the candidate and connection views with no detectable violations", async () => {
    const { container } = render(<MailHubWorkspace client={fakeClient()} />);
    await screen.findByText("Delivery schedule");

    fireEvent.click(screen.getAllByRole("tab")[1]);
    await screen.findByText("Confirm delivery date");
    expect(await violations(container)).toEqual([]);

    fireEvent.click(screen.getAllByRole("tab")[2]);
    await screen.findByText("连接状态");
    expect(await violations(container)).toEqual([]);
  });

  it("stays accessible in the English bundle", async () => {
    const { container } = render(<MailHubWorkspace client={fakeClient()} locale="en-US" />);
    await screen.findByText("Delivery schedule");
    expect(screen.getByRole("tablist", { name: "Mail views" })).toBeTruthy();
    expect(await violations(container)).toEqual([]);
  });
});

describe("keyboard and ARIA wiring", () => {
  it("implements a tab widget with roving tabindex and arrow-key navigation", async () => {
    render(<MailHubWorkspace client={fakeClient()} />);
    await screen.findByText("Delivery schedule");

    let tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(3);
    expect(tabs.map((tab) => tab.getAttribute("tabindex"))).toEqual(["0", "-1", "-1"]);
    expect(tabs.every((tab) => tab.getAttribute("aria-controls") === "mailhub-view-panel")).toBe(true);
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(tabs[0].id);

    tabs[0].focus();
    fireEvent.keyDown(tabs[0], { key: "ArrowRight" });
    await waitFor(() => {
      tabs = screen.getAllByRole("tab");
      expect(tabs[1].getAttribute("aria-selected")).toBe("true");
    });
    expect(document.activeElement).toBe(tabs[1]);
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(tabs[1].id);

    fireEvent.keyDown(tabs[1], { key: "End" });
    await waitFor(() => {
      expect(screen.getAllByRole("tab")[2].getAttribute("aria-selected")).toBe("true");
    });

    fireEvent.keyDown(screen.getAllByRole("tab")[2], { key: "Home" });
    await waitFor(() => {
      expect(screen.getAllByRole("tab")[0].getAttribute("aria-selected")).toBe("true");
    });
  });

  it("exposes inbox filters as toggle buttons, not tabs", async () => {
    render(<MailHubWorkspace client={fakeClient()} />);
    await screen.findByText("Delivery schedule");

    const filters = screen.getByRole("group", { name: "邮件筛选" });
    const buttons = within(filters).getAllByRole("button");
    expect(buttons).toHaveLength(6);
    expect(buttons[0].getAttribute("aria-pressed")).toBe("true");
    expect(buttons[1].getAttribute("aria-pressed")).toBe("false");
    expect(within(filters).queryAllByRole("tab")).toHaveLength(0);

    fireEvent.click(buttons[1]);
    await waitFor(() => {
      expect(within(filters).getAllByRole("button")[1].getAttribute("aria-pressed")).toBe("true");
    });
  });

  it("names the search field and announces the empty state", async () => {
    const { container } = render(
      <MailHubWorkspace client={fakeClient({ listThreads: async () => [] })} />,
    );
    const empty = await screen.findByRole("status");
    expect(screen.getByRole("textbox", { name: "搜索邮件" })).toBeTruthy();
    expect(empty.textContent).toBe("暂无线程。");
    expect(container.querySelector("[aria-busy]")).toBeTruthy();
  });

  it("announces a host failure instead of silently showing stale data", async () => {
    render(
      <MailHubWorkspace
        client={fakeClient({
          listConnections: async () => {
            throw new Error("host failure");
          },
        })}
      />,
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("MailHub 请求未完成；宿主未将失败当作成功。");
  });
});

describe("embedding outline", () => {
  it("keeps a single top-level heading in the standalone shell", async () => {
    const { container } = render(<MailHubStandalone client={fakeClient()} />);
    await screen.findByText("Delivery schedule");
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(container.querySelector("h1")?.textContent).toBe("MailHub");
    expect(container.querySelector("h2")?.textContent).toBe("统一收件箱");
    expect(await violations(container)).toEqual([]);
  });

  it("lets the host choose the workspace heading level", async () => {
    const { container } = render(
      <MailHubWorkspace client={fakeClient()} headingLevel={2} locale="en-US" />,
    );
    await screen.findByText("Delivery schedule");
    expect(container.querySelectorAll("h1")).toHaveLength(0);
    expect(container.querySelector("h2")?.textContent).toBe("Unified inbox");
  });
});
