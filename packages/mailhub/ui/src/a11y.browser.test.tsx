/**
 * Real-browser accessibility audit (WCAG 2.2 AA, rendering half).
 *
 * `a11y.test.tsx` proves semantics in jsdom, which has no layout engine.  This
 * suite runs the same component tree in Chromium via the Vitest browser runner
 * and proves only what a layout engine can prove:
 *
 *   1.4.3  contrast (minimum)     axe color-contrast against the shipped stylesheet
 *   1.4.10 reflow                 no horizontal scrolling at a 320 CSS px viewport
 *   1.4.4  resize text            no horizontal scrolling at a 640 CSS px viewport
 *   2.5.8  target size (minimum)  every control measured in layout pixels
 *   2.3.3  animation from interactions
 *   3.1.1  language of page       host obligation, proved to be a real finding
 *
 * A rule that cannot fail proves nothing, so the claims below are paired with
 * liveness cases that make the same rules fail on purpose.  Screen-reader output
 * cannot be asserted from a test runner; that pass stays manual (README.md).
 */

import { cdp, page } from "vitest/browser";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { fakeClient } from "./a11y.fixtures.js";
import { MailHubWorkspace, type MailHubUiClient } from "./index.js";
import { MailHubStandalone } from "./standalone.js";
import "./styles.css";

const VIEWPORT = { width: 1280, height: 900 };

/**
 * axe tags covering WCAG 2.2 level A and AA.  target-size (2.5.8) is the only
 * rule tagged wcag22aa in axe-core 4.13, so this tag set is what makes the
 * target-size claim part of the gate rather than a separate opt-in.
 */
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

interface AuditReport {
  violations: string[];
  undeterminedContrast: string[];
  /** How many elements axe actually measured for contrast. Zero would mean the
   *  clean result was vacuous, so the contrast cases assert on this directly. */
  contrastEvaluated: number;
}

function toReport(results: axe.AxeResults): AuditReport {
  const describeNodes = (nodes: { target: unknown[] }[]): string =>
    nodes.map((node) => node.target.join(" ")).join(" | ");
  return {
    violations: results.violations.map(
      (violation) =>
        violation.id + " (" + (violation.impact ?? "unknown") + "): " + describeNodes(violation.nodes),
    ),
    // axe reports an element whose background it cannot resolve as *incomplete*
    // rather than as a violation.  Treating those as clean would let an
    // unpainted surface pass as compliant, so they are asserted on as well.
    undeterminedContrast: results.incomplete
      .filter((result) => result.id === "color-contrast")
      .map((result) => describeNodes(result.nodes)),
    contrastEvaluated: results.passes
      .filter((result) => result.id === "color-contrast")
      .reduce((total, result) => total + result.nodes.length, 0),
  };
}

/** Component-scoped audit: what an embedded MailHub view is responsible for. */
async function audit(container: HTMLElement): Promise<AuditReport> {
  return toReport(await axe.run(container, { runOnly: { type: "tag", values: WCAG_TAGS } }));
}

/**
 * Document-scoped audit.  Rules such as html-has-lang are evaluated against the
 * page, so a container-scoped run would silently never report them - which is
 * exactly the kind of blind spot this suite exists to rule out.
 */
async function auditDocument(): Promise<AuditReport> {
  return toReport(await axe.run(document, { runOnly: { type: "tag", values: WCAG_TAGS } }));
}

interface ControlSize {
  label: string;
  width: number;
  height: number;
}

function measureControls(container: HTMLElement): ControlSize[] {
  const selector = "button, input, select, textarea, a[href], [role='button'], [role='tab']";
  return Array.from(container.querySelectorAll<HTMLElement>(selector))
    .filter((element) => {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    })
    .map((element) => {
      const rect = element.getBoundingClientRect();
      const text = (element.textContent ?? element.getAttribute("aria-label") ?? "").trim().slice(0, 24);
      const classes = element.className ? "." + element.className.split(" ")[0] : "";
      return {
        label: element.tagName.toLowerCase() + classes + ' "' + text + '"',
        width: Math.round(rect.width * 100) / 100,
        height: Math.round(rect.height * 100) / 100,
      };
    });
}

function smallest(controls: ControlSize[]): ControlSize {
  return controls.reduce((worst, control) =>
    Math.min(control.width, control.height) < Math.min(worst.width, worst.height) ? control : worst,
  );
}

function horizontalOverflow(): number {
  return document.documentElement.scrollWidth - document.documentElement.clientWidth;
}

function columnsOf(element: HTMLElement): string[] {
  return getComputedStyle(element).gridTemplateColumns.split(" ");
}

async function renderWorkspace(client: MailHubUiClient = fakeClient(), locale?: string) {
  const view = render(<MailHubWorkspace client={client} locale={locale} />);
  await screen.findByText("Delivery schedule");
  return view;
}

beforeAll(() => {
  // A conforming host document declares its language.  The liveness case below
  // proves this is a real obligation rather than a hidden exclusion.
  document.documentElement.lang = "zh-CN";
  const canvas = document.createElement("style");
  canvas.textContent = "html, body { background: #ffffff; margin: 0; }";
  document.head.append(canvas);
});

afterEach(async () => {
  cleanup();
  await page.viewport(VIEWPORT.width, VIEWPORT.height);
});

describe("harness liveness", () => {
  it("reports real contrast failures in the browser", async () => {
    const { container } = render(
      <p style={{ color: "#f0f0f0", background: "#ffffff", margin: 0 }}>Low contrast probe</p>,
    );
    const report = await audit(container);
    expect(report.violations.some((item) => item.startsWith("color-contrast"))).toBe(true);
  });

  it("reports real target-size failures in the browser", async () => {
    const { container } = render(
      <div>
        <button type="button" style={{ width: 12, height: 12, padding: 0 }}>
          a
        </button>
        <button type="button" style={{ width: 12, height: 12, padding: 0 }}>
          b
        </button>
      </div>,
    );
    const report = await audit(container);
    expect(report.violations.some((item) => item.startsWith("target-size"))).toBe(true);
  });

  it("keeps the host document language obligation enforceable", async () => {
    document.documentElement.removeAttribute("lang");
    try {
      await renderWorkspace();
      const report = await auditDocument();
      expect(report.violations.some((item) => item.startsWith("html-has-lang"))).toBe(true);
    } finally {
      document.documentElement.lang = "zh-CN";
    }
  });
});

describe("contrast (1.4.3, 1.4.11)", () => {
  /** A clean audit only counts if axe really measured contrast. */
  function expectClean(report: AuditReport, label: string): void {
    expect({ label, violations: report.violations, undetermined: report.undeterminedContrast }).toEqual({
      label,
      violations: [],
      undetermined: [],
    });
    expect(report.contrastEvaluated, label + " measured no contrast at all").toBeGreaterThan(0);
    // Read back by scripts/check_ui_browser_a11y.py as evidence that contrast
    // was measured rather than skipped.
    console.log("MAILHUB_CONTRAST_EVALUATED " + label + " " + report.contrastEvaluated);
  }

  it("renders every workspace view without contrast violations", async () => {
    const { container } = await renderWorkspace();
    expectClean(await audit(container), "inbox");

    fireEvent.click(screen.getAllByRole("tab")[1]);
    await screen.findByText("Confirm delivery date");
    expectClean(await audit(container), "candidates");

    fireEvent.click(screen.getAllByRole("tab")[2]);
    await screen.findByText("连接状态");
    expectClean(await audit(container), "connections");
  });

  it("renders the English bundle without contrast violations", async () => {
    const { container } = await renderWorkspace(fakeClient(), "en-US");
    expectClean(await audit(container), "en-US");
  });

  it("renders the standalone shell without contrast violations", async () => {
    const { container } = render(<MailHubStandalone client={fakeClient()} />);
    await screen.findByText("Delivery schedule");
    expectClean(await audit(container), "standalone");
  });
});

describe("reflow and resize text (1.4.10, 1.4.4)", () => {
  it("reflows to a 320 CSS px viewport without horizontal scrolling", async () => {
    await page.viewport(320, 800);
    const { container } = render(<MailHubStandalone client={fakeClient()} />);
    await screen.findByText("Delivery schedule");

    const inbox = container.querySelector<HTMLElement>(".mailhub-workspace__inbox");
    expect(inbox).not.toBeNull();
    // Evidence that the responsive path engaged rather than the content merely
    // being narrow: the two-column inbox collapses to a single grid track.
    expect(columnsOf(inbox as HTMLElement)).toHaveLength(1);
    expect(horizontalOverflow()).toBeLessThanOrEqual(0);
  });

  it("stays free of horizontal scrolling at a 640 CSS px viewport (200% of 1280)", async () => {
    await page.viewport(640, 900);
    const { container } = render(<MailHubStandalone client={fakeClient()} />);
    await screen.findByText("Delivery schedule");
    expect(container.querySelector(".mailhub-standalone")).not.toBeNull();
    expect(horizontalOverflow()).toBeLessThanOrEqual(0);
  });

  it("uses two columns again at the 1280 CSS px baseline", async () => {
    const { container } = await renderWorkspace();
    const inbox = container.querySelector<HTMLElement>(".mailhub-workspace__inbox");
    expect(columnsOf(inbox as HTMLElement)).toHaveLength(2);
  });
});

describe("target size (2.5.8)", () => {
  it("keeps every control at or above 24 by 24 CSS px in all three views", async () => {
    const { container } = await renderWorkspace();
    const inbox = measureControls(container);
    expect(inbox.length).toBeGreaterThan(0);
    expect(Math.min(...inbox.map((c) => Math.min(c.width, c.height)))).toBeGreaterThanOrEqual(24);

    fireEvent.click(screen.getAllByRole("tab")[1]);
    await screen.findByText("Confirm delivery date");
    const candidates = measureControls(container);
    expect(Math.min(...candidates.map((c) => Math.min(c.width, c.height)))).toBeGreaterThanOrEqual(24);

    fireEvent.click(screen.getAllByRole("tab")[2]);
    await screen.findByText("连接状态");
    const connections = measureControls(container);
    expect(Math.min(...connections.map((c) => Math.min(c.width, c.height)))).toBeGreaterThanOrEqual(24);

    // Quoted verbatim by the audit report, so the measured floor is evidence
    // rather than an assurance.
    console.log(
      "MAILHUB_MEASURED_CONTROLS " +
        JSON.stringify({
          inbox: { count: inbox.length, smallest: smallest(inbox) },
          candidates: { count: candidates.length, smallest: smallest(candidates) },
          connections: { count: connections.length, smallest: smallest(connections) },
        }),
    );
  });
});

describe("animation from interactions (2.3.3)", () => {
  const motionDurations = (container: HTMLElement): number[] =>
    Array.from(container.querySelectorAll<HTMLElement>("*")).flatMap((element) => {
      const style = getComputedStyle(element);
      return [style.transitionDuration, style.animationDuration]
        .flatMap((value) => value.split(","))
        .map((value) => Number.parseFloat(value) || 0);
    });

  it("ships no motion in the default media query", async () => {
    const { container } = await renderWorkspace();
    expect(motionDurations(container).filter((seconds) => seconds > 0)).toEqual([]);
  });

  it("ships a reduced-motion guard scoped to both app roots", async () => {
    const guards = Array.from(document.styleSheets).flatMap((sheet) =>
      Array.from(sheet.cssRules).filter(
        (rule): rule is CSSMediaRule =>
          rule instanceof CSSMediaRule && rule.conditionText.includes("prefers-reduced-motion: reduce"),
      ),
    );
    expect(guards.length).toBeGreaterThan(0);
    const guardText = guards.map((rule) => rule.cssText).join("\n");
    expect(guardText).toContain(".mailhub-workspace *");
    expect(guardText).toContain(".mailhub-standalone *");
    // PostCSS normalises .01ms to 0.01ms, so the assertion follows the built CSS.
    expect(guardText).toContain("animation-duration: 0.01ms !important");
    expect(guardText).toContain("transition-duration: 0.01ms !important");
    expect(guardText).toContain("animation-iteration-count: 1 !important");
  });

  it("neutralises a real transition declaration once reduced motion is requested", async () => {
    const { container } = await renderWorkspace();
    const probe = container.querySelector<HTMLElement>(".mailhub-workspace__thread");
    expect(probe).not.toBeNull();
    // The guard uses !important, which outranks a normal inline declaration, so
    // this proves the shipped rule applies rather than merely parsing.
    (probe as HTMLElement).style.transitionDuration = "3s";

    const session = cdp() as unknown as {
      send(method: string, params?: Record<string, unknown>): Promise<unknown>;
    };
    expect(window.matchMedia("(prefers-reduced-motion: reduce)").matches).toBe(false);
    await session.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-motion", value: "reduce" }],
    });
    try {
      expect(window.matchMedia("(prefers-reduced-motion: reduce)").matches).toBe(true);
      const seconds = Number.parseFloat(getComputedStyle(probe as HTMLElement).transitionDuration);
      expect(seconds).toBeGreaterThan(0);
      expect(seconds).toBeLessThanOrEqual(0.001);
    } finally {
      await session.send("Emulation.setEmulatedMedia", { features: [] });
    }
    expect(window.matchMedia("(prefers-reduced-motion: reduce)").matches).toBe(false);
  });
});

describe("content security policy baseline", () => {
  it("renders no inline style attributes when no theme is supplied", async () => {
    const { container } = await renderWorkspace();
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
  });

  it("renders no inline style attributes in the unthemed standalone shell", async () => {
    const { container } = render(<MailHubStandalone client={fakeClient()} />);
    await screen.findByText("Delivery schedule");
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
  });

  it("injects no script elements", async () => {
    const before = document.querySelectorAll("script").length;
    await renderWorkspace();
    expect(document.querySelectorAll("script")).toHaveLength(before);
  });

  it("confines inline styles to the documented theme prop", async () => {
    // The theme prop emits --mailhub-* as an inline style attribute, so a host
    // running style-src 'self' without unsafe-inline must theme through its own
    // stylesheet instead.  Recorded in README.md as a host obligation.
    const { container } = render(
      <MailHubStandalone client={fakeClient()} theme={{ "--mailhub-accent": "#0b5cff" }} />,
    );
    await screen.findByText("Delivery schedule");
    const styled = container.querySelectorAll("[style]");
    expect(styled).toHaveLength(1);
    expect(styled[0].getAttribute("style")).toContain("--mailhub-accent");
  });
});
