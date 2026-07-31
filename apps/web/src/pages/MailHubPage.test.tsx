import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MailHubPage } from "./MailHubPage";

const api = vi.hoisted(() => ({
  fixtureModeEnabled: vi.fn(() => true),
}));

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

describe("MailHubPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fixtureModeEnabled.mockReturnValue(true);
  });

  it("renders metadata-first inbox and requires an explicit draft confirmation path", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <MemoryRouter initialEntries={["/mail"]}>
        <QueryClientProvider client={queryClient}>
          <MailHubPage />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "智能邮件" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /航材询价/ })).toBeInTheDocument();
    expect(screen.getByText(/抄送：engineering@example.com/)).toBeInTheDocument();
    expect(screen.getByText(/回复地址：rfq-replies@example.com/)).toBeInTheDocument();
    expect(screen.queryByText("选择一个线程")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /连接中心/ }));
    expect(screen.getByRole("heading", { name: "连接与同步健康" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出元数据" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "导出含正文" })).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "仅撤销访问" }).every((button) => button.hasAttribute("disabled"))).toBe(true);
    expect(screen.getAllByRole("button", { name: "预览影响" }).every((button) => button.hasAttribute("disabled"))).toBe(true);
    expect(screen.getAllByRole("button", { name: "收窄本地权限" }).every((button) => button.hasAttribute("disabled"))).toBe(true);
    expect(screen.getByRole("heading", { name: "按范围补偿同步" })).toBeInTheDocument();
    expect(screen.getByLabelText("接收时间起点")).toHaveAttribute("type", "datetime-local");
    expect(screen.getByText("浏览器只持有 CAPlatform 会话；Provider credential_ref、refresh token 和原始正文不会进入前端、普通日志或 Agent memory。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /智能处理/ }));
    expect(screen.getByRole("heading", { name: "智能处理中心" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "批准交给宿主" })).toHaveLength(2);
  });
});
