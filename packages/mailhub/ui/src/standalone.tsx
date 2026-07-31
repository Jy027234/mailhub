import type { CSSProperties, ReactNode } from "react";
import { MailHubWorkspace, type MailHubWorkspaceProps } from "./index";

/**
 * CSS custom properties accepted by the framework-neutral shell.  The host
 * still owns the document root, router, identity and API client; this shell
 * only supplies a stable semantic frame around MailHubWorkspace.
 */
export type MailHubStandaloneTheme = Record<`--mailhub-${string}`, string>;

export interface MailHubStandaloneProps extends MailHubWorkspaceProps {
  title?: string;
  subtitle?: string;
  theme?: MailHubStandaloneTheme;
  className?: string;
  headerContent?: ReactNode;
}

/**
 * Embeddable standalone shell for hosts that do not want to fork the MailHub
 * workspace.  It intentionally does not import a router, session library,
 * fetch implementation or ReactDOM; a host can mount it in any application
 * root and retain control of navigation and identity.
 */
export function MailHubStandalone({
  title = "MailHub",
  subtitle = "受治理的智能邮件工作台",
  theme,
  className = "",
  headerContent,
  ...workspaceProps
}: MailHubStandaloneProps) {
  return (
    <main
      className={`mailhub-standalone ${className}`.trim()}
      style={toThemeStyle(theme)}
      aria-label="MailHub standalone shell"
    >
      <header className="mailhub-standalone__header">
        <div>
          <span className="mailhub-standalone__eyebrow">MAILHUB</span>
          <h1>{title}</h1>
          <p>{subtitle}</p>
        </div>
        {headerContent ? <div className="mailhub-standalone__header-content">{headerContent}</div> : null}
      </header>
      <MailHubWorkspace
        {...workspaceProps}
        className="mailhub-standalone__workspace"
      />
    </main>
  );
}

function toThemeStyle(theme: MailHubStandaloneTheme | undefined): CSSProperties | undefined {
  if (!theme) return undefined;
  const entries = Object.entries(theme)
    .filter(([name, value]) => name.startsWith("--mailhub-") && name.length <= 80 && value.length <= 200)
    .slice(0, 32);
  return Object.fromEntries(entries) as CSSProperties;
}
