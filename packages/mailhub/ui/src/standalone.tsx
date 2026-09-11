import type { CSSProperties, ReactNode } from "react";
import { MailHubWorkspace, type MailHubWorkspaceProps } from "./index.js";
import { resolveMessages } from "./i18n.js";

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
 * root and retain control of navigation and identity.  Shell copy follows the
 * same injectable locale bundle as the workspace.
 */
export function MailHubStandalone({
  title = "MailHub",
  subtitle,
  headingLevel = 2,
  theme,
  className = "",
  headerContent,
  locale,
  messages,
  ...workspaceProps
}: MailHubStandaloneProps) {
  const text = resolveMessages(locale, messages);
  return (
    <main
      className={["mailhub-standalone", className].filter((value) => value.length > 0).join(" ")}
      style={toThemeStyle(theme)}
      aria-label={text.shellLabel}
    >
      <header className="mailhub-standalone__header">
        <div>
          <span className="mailhub-standalone__eyebrow">{text.eyebrow}</span>
          <h1>{title}</h1>
          <p>{subtitle ?? text.shellSubtitle}</p>
        </div>
        {headerContent ? <div className="mailhub-standalone__header-content">{headerContent}</div> : null}
      </header>
      <MailHubWorkspace
        {...workspaceProps}
        headingLevel={headingLevel}
        locale={locale}
        messages={messages}
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
