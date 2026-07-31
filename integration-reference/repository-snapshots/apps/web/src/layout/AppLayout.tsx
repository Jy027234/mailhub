import { useEffect, useRef, useState, type MouseEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Outlet } from "react-router-dom";
import { switchAccountWorkspace } from "../api/client";
import { Breadcrumbs } from "../components/Breadcrumbs";
import { CommandPalette } from "../components/CommandPalette";
import { Icon } from "../components/Icon";
import { NotificationCenter, usePendingWork } from "../components/NotificationCenter";
import { useSession } from "../components/SessionGate";
import { currentUser } from "../fixtures/mock";
import { fixtureModeEnabled } from "../api/client";
import "../styles/nav-extras.css";

const PRIMARY_NAV = [
  { to: "/", label: "首页", icon: "home" },
  { to: "/conversations", label: "会话", icon: "chat" },
  { to: "/mail", label: "邮件", icon: "mail" },
  { to: "/projects", label: "项目", icon: "project" },
  { to: "/apps", label: "应用", icon: "apps" },
  { to: "/drive", label: "云盘", icon: "drive" },
  { to: "/knowledge", label: "知识库", icon: "knowledge" },
  { to: "/agents", label: "专家", icon: "agent" },
  { to: "/skills", label: "技能", icon: "skill" },
] as const;

const SECONDARY_NAV = [
  { to: "/work", label: "审批", icon: "tasks", requiresAdmin: false },
  { to: "/admin", label: "管理", icon: "admin", requiresAdmin: true },
] as const;

type OpenMenu = "workspace" | "user" | null;

type ThemePreference = "light" | "dark" | "system";

const THEME_STORAGE_KEY = "ca-theme";
const THEME_OPTIONS: Array<{ value: ThemePreference; label: string }> = [
  { value: "light", label: "亮色" },
  { value: "dark", label: "暗色" },
  { value: "system", label: "跟随系统" },
];

function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") {
      return stored;
    }
  } catch {
    // localStorage 不可用（隐私模式等）时按跟随系统处理
  }
  return "system";
}

function applyThemePreference(preference: ThemePreference) {
  if (preference === "system") {
    // 移除显式标记，交给 tokens.css 的 prefers-color-scheme 媒体查询跟随系统
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = preference;
  }
}

// 模块求值早于首次渲染：先恢复用户显式选择的主题，避免刷新时主题闪烁。
// 未做显式选择时不设置 data-theme，由 CSS 媒体查询直接跟随系统（纯 CSS，零延迟）。
applyThemePreference(readThemePreference());

export function AppLayout() {
  const { status, session, openAuth, replaceSession, logout } = useSession();
  const queryClient = useQueryClient();
  const fixture = fixtureModeEnabled();
  const [openMenu, setOpenMenu] = useState<OpenMenu>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [theme, setTheme] = useState<ThemePreference>(readThemePreference);
  const menuRootRef = useRef<HTMLDivElement>(null);
  const { count: pendingWorkCount } = usePendingWork(
    status === "authenticated" && !fixture,
  );
  const isMac =
    typeof navigator !== "undefined" && /mac|iphone|ipad/i.test(navigator.platform);
  const user = asRecord(session?.profile.user);
  const tenant = asRecord(session?.profile.current_tenant);
  const memberships = asRecordList(session?.profile.memberships);
  const displayName = fixture
    ? currentUser.name
    : stringValue(user?.display_name) ?? stringValue(user?.name);
  const tenantName = fixture
    ? currentUser.tenant
    : stringValue(tenant?.name) ?? "CAPLATFORM 个人空间";
  const role = fixture ? currentUser.role : roleLabel(session?.principal.role);
  const canManage = fixture || Boolean(
    session && (
      ["owner", "admin"].includes(session.principal.role) ||
      session.principal.permissions.some((permission) =>
        ["platform.admin", "tenant.manage", "toolkit.admin"].includes(permission)
      )
    ),
  );

  const switchMutation = useMutation({
    mutationFn: switchAccountWorkspace,
    onSuccess: (nextSession) => {
      queryClient.clear();
      replaceSession(nextSession);
      setOpenMenu(null);
    },
  });

  useEffect(() => {
    const closeOnOutside = (event: PointerEvent) => {
      if (menuRootRef.current && !menuRootRef.current.contains(event.target as Node)) {
        setOpenMenu(null);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenMenu(null);
    };
    const togglePalette = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((current) => !current);
      }
    };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("keydown", togglePalette);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("keydown", togglePalette);
    };
  }, []);

  const protectNavigation = (event: MouseEvent<HTMLAnchorElement>) => {
    if (status !== "authenticated") {
      event.preventDefault();
      openAuth("login");
    }
  };

  const selectTheme = (preference: ThemePreference) => {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    } catch {
      // 持久化失败不影响本次切换
    }
    applyThemePreference(preference);
    setTheme(preference);
  };

  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-mark">
            <Icon name="logo" size={18} />
          </span>
          <span>
            <span className="brand-name">CAPlatform</span>
            <span className="brand-sub">民航技术与安全 AI 工作台</span>
          </span>
        </Link>
        <div className="topbar-right">
          <button
            className="topbar-search"
            type="button"
            aria-label="搜索（Ctrl+K）"
            onClick={() => setPaletteOpen(true)}
          >
            <Icon name="search" size={14} />
            <span className="topbar-search-text">搜索</span>
            <kbd className="topbar-kbd">{isMac ? "⌘K" : "Ctrl K"}</kbd>
          </button>
          <NotificationCenter />
          {status === "authenticated" ? (
            <div className="topbar-account" ref={menuRootRef}>
              <div className="topbar-menu-anchor">
                <button
                  className="topbar-tenant"
                  type="button"
                  aria-haspopup="menu"
                  aria-expanded={openMenu === "workspace"}
                  onClick={() => setOpenMenu((current) => current === "workspace" ? null : "workspace")}
                >
                  <span>{tenantName}</span>
                  <span className="topbar-chevron" aria-hidden="true"><Icon name="chevron-down" size={12} /></span>
                </button>
                {openMenu === "workspace" && (
                  <div className="topbar-menu workspace-menu" role="menu">
                    <div className="topbar-menu-title">切换工作空间</div>
                    {(fixture ? [{ id: "fixture", name: tenantName, tenant_type: "team" }] : memberships).map((membership) => {
                      const id = stringValue(membership.id) ?? "";
                      const current = fixture || id === session?.principal.tenant_id;
                      return (
                        <button
                          key={id || stringValue(membership.name) || "workspace"}
                          type="button"
                          role="menuitem"
                          className={`workspace-menu-item${current ? " current" : ""}`}
                          disabled={current || !id || switchMutation.isPending}
                          onClick={() => switchMutation.mutate(id)}
                        >
                          <span className="workspace-menu-symbol">{tenantTypeLabel(stringValue(membership.tenant_type)).slice(0, 1)}</span>
                          <span>
                            <strong>{stringValue(membership.name) ?? "未命名空间"}</strong>
                            <small>{current ? "当前空间" : tenantTypeLabel(stringValue(membership.tenant_type))}</small>
                          </span>
                          {current && <span className="workspace-menu-check"><Icon name="check" size={14} /></span>}
                        </button>
                      );
                    })}
                    {!fixture && memberships.length === 0 && (
                      <div className="topbar-menu-empty">当前账号没有其他工作空间。</div>
                    )}
                    <Link className="topbar-menu-footer" to="/account?tab=workspaces" onClick={() => setOpenMenu(null)}>
                      管理工作空间
                    </Link>
                    {switchMutation.isError && <div className="topbar-menu-error">切换失败，请稍后重试。</div>}
                  </div>
                )}
              </div>

              <div className="topbar-menu-anchor">
                <button
                  className="topbar-user"
                  type="button"
                  aria-haspopup="menu"
                  aria-expanded={openMenu === "user"}
                  onClick={() => setOpenMenu((current) => current === "user" ? null : "user")}
                >
                  <span className="topbar-avatar">{(displayName ?? "用").slice(0, 1)}</span>
                  <span className="topbar-user-copy">
                    <span className="topbar-user-name">{displayName ?? "用户"}</span>
                    <span className="topbar-user-role">{role}</span>
                  </span>
                  <span className="topbar-chevron" aria-hidden="true"><Icon name="chevron-down" size={12} /></span>
                </button>
                {openMenu === "user" && (
                  <div className="topbar-menu user-menu" role="menu">
                    <div className="user-menu-summary">
                      <span className="topbar-avatar">{(displayName ?? "用").slice(0, 1)}</span>
                      <span><strong>{displayName ?? "用户"}</strong><small>{tenantName}</small></span>
                    </div>
                    <div className="theme-menu-group" role="radiogroup" aria-label="主题外观">
                      <div className="topbar-menu-title">主题外观</div>
                      {THEME_OPTIONS.map((option) => (
                        <button
                          key={option.value}
                          type="button"
                          role="menuitemradio"
                          aria-checked={theme === option.value}
                          className={`theme-menu-item${theme === option.value ? " current" : ""}`}
                          onClick={() => selectTheme(option.value)}
                        >
                          <span>{option.label}</span>
                          {theme === option.value && (
                            <span className="workspace-menu-check"><Icon name="check" size={14} /></span>
                          )}
                        </button>
                      ))}
                    </div>
                    <Link role="menuitem" to="/account" onClick={() => setOpenMenu(null)}>个人中心</Link>
                    <Link role="menuitem" to="/account?tab=security" onClick={() => setOpenMenu(null)}>安全设置</Link>
                    {!fixture && (
                      <button role="menuitem" type="button" className="user-menu-logout" onClick={() => void logout()}>
                        退出登录
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="topbar-auth-actions">
              <button className="btn btn-ghost" type="button" onClick={() => openAuth("login")}>
                登录
              </button>
              <button className="btn btn-primary" type="button" onClick={() => openAuth("register")}>
                注册
              </button>
            </div>
          )}
        </div>
      </header>

      <div className="shell-body">
        <aside className="rail">
          <nav className="rail-inner" aria-label="主导航">
            {PRIMARY_NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                onClick={item.to === "/" ? undefined : protectNavigation}
                className={({ isActive }) => `rail-item${isActive ? " active" : ""}`}
              >
                <Icon name={item.icon} size={21} weight="duotone" />
                <span className="rail-label">{item.label}</span>
                <span className="rail-tip" aria-hidden="true">{item.label}</span>
              </NavLink>
            ))}
            <div className="rail-divider" />
            {SECONDARY_NAV.filter((item) => !item.requiresAdmin || canManage).map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                onClick={protectNavigation}
                className={({ isActive }) => `rail-item${isActive ? " active" : ""}`}
              >
                <Icon name={item.icon} size={21} weight="duotone" />
                <span className="rail-label">{item.label}</span>
                <span className="rail-tip" aria-hidden="true">{item.label}</span>
                {item.to === "/work" && pendingWorkCount > 0 && (
                  <span className="rail-count">
                    {pendingWorkCount > 99 ? "99+" : pendingWorkCount}
                  </span>
                )}
              </NavLink>
            ))}
          </nav>
        </aside>

        <main className="content">
          <Breadcrumbs />
          <Outlet />
        </main>
      </div>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function asRecordList(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.map(asRecord).filter((item): item is Record<string, unknown> => item !== null)
    : [];
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function tenantTypeLabel(value: string | null): string {
  if (value === "personal") return "个人空间";
  if (value === "platform") return "平台空间";
  return "组织空间";
}

function roleLabel(value: string | undefined): string {
  if (value === "owner") return "空间所有者";
  if (value === "admin") return "管理员";
  if (value === "viewer") return "访客";
  if (value === "member") return "成员";
  return value || "成员";
}
