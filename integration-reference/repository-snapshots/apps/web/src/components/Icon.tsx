import {
  AirplaneTilt,
  ArrowRight,
  BookOpen,
  CaretDown,
  ChatCircleText,
  Check,
  CheckCircle,
  Cloud,
  FolderSimple,
  GearSix,
  House,
  EnvelopeSimple,
  Info,
  ListChecks,
  MagnifyingGlass,
  Paperclip,
  PaperPlaneRight,
  Plus,
  Robot,
  ShieldCheck,
  Sparkle,
  SquaresFour,
  WarningCircle,
  X,
  type IconWeight,
  type Icon as PhosphorIcon,
} from "@phosphor-icons/react";

export type IconName =
  | "home"
  | "chat"
  | "apps"
  | "project"
  | "drive"
  | "knowledge"
  | "agent"
  | "skill"
  | "tasks"
  | "admin"
  | "mail"
  | "send"
  | "attach"
  | "logo"
  | "shield"
  | "search"
  | "chevron-down"
  | "check"
  | "check-circle"
  | "warning-circle"
  | "info"
  | "close"
  | "plus"
  | "arrow-right";

const icons: Record<IconName, PhosphorIcon> = {
  home: House,
  chat: ChatCircleText,
  apps: SquaresFour,
  project: FolderSimple,
  drive: Cloud,
  knowledge: BookOpen,
  agent: Robot,
  skill: Sparkle,
  tasks: ListChecks,
  admin: GearSix,
  mail: EnvelopeSimple,
  send: PaperPlaneRight,
  attach: Paperclip,
  logo: AirplaneTilt,
  shield: ShieldCheck,
  search: MagnifyingGlass,
  "chevron-down": CaretDown,
  check: Check,
  "check-circle": CheckCircle,
  "warning-circle": WarningCircle,
  info: Info,
  close: X,
  plus: Plus,
  "arrow-right": ArrowRight,
};

export interface IconProps {
  name: IconName;
  size?: number;
  /** regular（正文默认）/ duotone（导航）/ fill 等，同 Phosphor */
  weight?: IconWeight;
}

export function Icon({ name, size = 20, weight }: IconProps) {
  const Component = icons[name];
  const resolvedWeight = weight ?? (name === "logo" ? "fill" : "regular");
  return <Component size={size} weight={resolvedWeight} aria-hidden="true" />;
}
