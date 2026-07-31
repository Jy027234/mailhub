import { lazy, StrictMode, Suspense, type ReactElement } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, Navigate, RouterProvider, useParams } from "react-router-dom";
import { AppLayout } from "./layout/AppLayout";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { SessionGate } from "./components/SessionGate";
import { ToastProvider } from "./components/ui/Toast";
import "./styles/global.css";
import "./styles/ui.css";
import "./styles/layout.css";
import "./styles/home.css";
import "./styles/conversation.css";
import "./styles/pages.css";
import "./styles/project-hub.css";
import "./styles/login-search.css";
import "./styles/mailhub.css";

const AccountPage = lazy(() =>
  import("./pages/AccountPage").then((module) => ({ default: module.AccountPage })),
);
const AdminPage = lazy(() =>
  import("./pages/AdminPage").then((module) => ({ default: module.AdminPage })),
);
const AgentEditorPage = lazy(() =>
  import("./pages/AgentEditorPage").then((module) => ({ default: module.AgentEditorPage })),
);
const AgentsPage = lazy(() =>
  import("./pages/AgentsPage").then((module) => ({ default: module.AgentsPage })),
);
const AppDetailPage = lazy(() =>
  import("./pages/AppDetailPage").then((module) => ({ default: module.AppDetailPage })),
);
const AppsPage = lazy(() =>
  import("./pages/AppsPage").then((module) => ({ default: module.AppsPage })),
);
const ConversationPage = lazy(() =>
  import("./pages/ConversationPage").then((module) => ({ default: module.ConversationPage })),
);
const ConversationsPage = lazy(() =>
  import("./pages/ConversationsPage").then((module) => ({ default: module.ConversationsPage })),
);
const ConnectorsPage = lazy(() =>
  import("./pages/ConnectorsPage").then((module) => ({ default: module.ConnectorsPage })),
);
const MailHubPage = lazy(() =>
  import("./pages/MailHubPage").then((module) => ({ default: module.MailHubPage })),
);
const DrivePage = lazy(() =>
  import("./pages/DrivePage").then((module) => ({ default: module.DrivePage })),
);
const HomePage = lazy(() =>
  import("./pages/HomePage").then((module) => ({ default: module.HomePage })),
);
const KnowledgePage = lazy(() =>
  import("./pages/KnowledgePage").then((module) => ({ default: module.KnowledgePage })),
);
const LoginPage = lazy(() =>
  import("./pages/LoginPage").then((module) => ({ default: module.LoginPage })),
);
const NotFoundPage = lazy(() =>
  import("./pages/NotFoundPage").then((module) => ({ default: module.NotFoundPage })),
);
const projectsPageModule = () => import("./pages/ProjectsPage");
const ProjectsPage = lazy(() =>
  projectsPageModule().then((module) => ({ default: module.ProjectsPage })),
);
const ProjectTaskWorkspacePage = lazy(() =>
  projectsPageModule().then((module) => ({ default: module.ProjectTaskWorkspacePage })),
);
const SearchPage = lazy(() =>
  import("./pages/SearchPage").then((module) => ({ default: module.SearchPage })),
);
const SkillsPage = lazy(() =>
  import("./pages/SkillsPage").then((module) => ({ default: module.SkillsPage })),
);
const WorkPage = lazy(() =>
  import("./pages/WorkPage").then((module) => ({ default: module.WorkPage })),
);

const queryClient = new QueryClient();

/** 路由级错误边界：任一页面抛错时只影响该页面内容区 */
function page(element: ReactElement) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<div role="status">正在加载页面…</div>}>{element}</Suspense>
    </ErrorBoundary>
  );
}

/** 按 conversationId 强制重挂载，避免切换会话时事件/游标状态串话 */
function ConversationPageKeyed() {
  const { conversationId } = useParams();
  return <ConversationPage key={conversationId} />;
}

const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: page(<HomePage />) },
      { path: "/login", element: page(<LoginPage />) },
      { path: "/register", element: page(<LoginPage />) },
      { path: "/search", element: page(<SearchPage />) },
      { path: "/conversations", element: page(<ConversationsPage />) },
      { path: "/conversations/:conversationId", element: page(<ConversationPageKeyed />) },
      { path: "/mail", element: page(<MailHubPage />) },
      { path: "/projects", element: page(<ProjectsPage />) },
      { path: "/projects/:projectId", element: page(<ProjectsPage />) },
      { path: "/projects/:projectId/tasks/:taskId", element: page(<ProjectTaskWorkspacePage />) },
      { path: "/apps", element: page(<AppsPage />) },
      { path: "/connectors", element: page(<ConnectorsPage />) },
      { path: "/apps/aiprojectops", element: <Navigate to="/projects" replace /> },
      { path: "/apps/:appId", element: page(<AppDetailPage />) },
      { path: "/drive", element: page(<DrivePage />) },
      { path: "/knowledge", element: page(<KnowledgePage />) },
      { path: "/agents", element: page(<AgentsPage />) },
      { path: "/agents/new", element: page(<AgentEditorPage />) },
      { path: "/agents/:agentId/edit", element: page(<AgentEditorPage />) },
      { path: "/skills", element: page(<SkillsPage />) },
      { path: "/work", element: page(<WorkPage />) },
      { path: "/admin", element: page(<AdminPage />) },
      { path: "/account", element: page(<AccountPage />) },
      { path: "*", element: page(<NotFoundPage />) },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <SessionGate>
          <ToastProvider>
            <RouterProvider router={router} />
          </ToastProvider>
        </SessionGate>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
