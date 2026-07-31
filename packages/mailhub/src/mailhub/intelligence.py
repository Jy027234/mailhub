"""Deterministic, evidence-first mail understanding primitives.

These rules are safe fallbacks and evaluation fixtures.  They never execute a
host action; they only emit candidates with source locators and confidence.
Model-backed enrichment must run through AiExecutionPort and validate against
the same result schema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import ceil
from uuid import UUID

from mailhub.domain import MailMessageProjection, digest_text

_PROJECT_RE = re.compile(r"\b(?:project|项目)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})\b", re.I)
_TASK_RE = re.compile(r"\b(?:task|任务)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})\b", re.I)
_DATE_RE = re.compile(r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b")
_DECISION_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:decision|decided|决定|结论)\s*[:：-]?\s*(.{1,240})$"
)
_RISK_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:risk|risks|blocker|风险|阻塞)\s*[:：-]?\s*(.{1,240})$"
)
_COMMITMENT_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:commitment|承诺|我们将|我会|we will|we'll)\s*[:：-]?\s*(.{1,240})$"
)
_ACTION_ITEM_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:action item|todo|follow[- ]?up|行动项|待办)\s*[:：-]?\s*(.{1,240})$"
)
_INJECTION_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous\s+instructions|ignore\s+all\s+instructions|system\s+message|developer\s+message|忽略之前规则|系统指令)",
    re.I,
)
_QUOTE_RE = re.compile(r"(?m)^\s*>.*(?:\n|$)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SIGNATURE_RE = re.compile(r"(?m)^\s*(?:--|——)\s*$.*\Z", re.S)


@dataclass(frozen=True, slots=True)
class EvidenceLocator:
    message_id: UUID
    field: str
    start: int
    end: int
    text_sha256: str


@dataclass(frozen=True, slots=True)
class AnalysisPolicy:
    """Versioned, bounded analysis policy owned by MailHub, not by email text."""

    policy_version: str = "mail-ai-policy-v1"
    prompt_version: str = "rules-prompt-v1"
    parser_version: str = "rules-parser-v1"
    calibration_version: str = "evidence-calibration-v1"
    mode: str = "rules"
    max_input_chars: int = 200_000
    max_evidence: int = 100
    max_actions: int = 20
    max_estimated_tokens: int = 50_000
    chars_per_token: float = 4.0
    action_min_confidence: float = 0.65
    knowledge_min_confidence: float = 0.75
    low_confidence_floor: float = 0.35

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.policy_version, "policy_version", 100),
            (self.prompt_version, "prompt_version", 100),
            (self.parser_version, "parser_version", 100),
            (self.calibration_version, "calibration_version", 100),
            (self.mode, "mode", 40),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"analysis_{name}_invalid")
        if (
            not isinstance(self.max_input_chars, int)
            or isinstance(self.max_input_chars, bool)
            or not 1_000 <= self.max_input_chars <= 2_000_000
        ):
            raise ValueError("analysis_max_input_chars_invalid")
        if (
            not isinstance(self.max_evidence, int)
            or isinstance(self.max_evidence, bool)
            or not 1 <= self.max_evidence <= 1_000
        ):
            raise ValueError("analysis_max_evidence_invalid")
        if (
            not isinstance(self.max_actions, int)
            or isinstance(self.max_actions, bool)
            or not 1 <= self.max_actions <= 100
        ):
            raise ValueError("analysis_max_actions_invalid")
        if (
            not isinstance(self.max_estimated_tokens, int)
            or isinstance(self.max_estimated_tokens, bool)
            or not 100 <= self.max_estimated_tokens <= 500_000
        ):
            raise ValueError("analysis_max_estimated_tokens_invalid")
        if (
            not isinstance(self.chars_per_token, (int, float))
            or isinstance(self.chars_per_token, bool)
            or not 1.0 <= self.chars_per_token <= 10.0
        ):
            raise ValueError("analysis_chars_per_token_invalid")
        for confidence_value, confidence_name in (
            (self.action_min_confidence, "action_min_confidence"),
            (self.knowledge_min_confidence, "knowledge_min_confidence"),
            (self.low_confidence_floor, "low_confidence_floor"),
        ):
            if (
                not isinstance(confidence_value, (int, float))
                or isinstance(confidence_value, bool)
                or not 0 <= confidence_value <= 1
            ):
                raise ValueError(f"analysis_{confidence_name}_invalid")
        if self.low_confidence_floor > min(
            self.action_min_confidence, self.knowledge_min_confidence
        ):
            raise ValueError("analysis_confidence_floor_invalid")


DEFAULT_ANALYSIS_POLICY = AnalysisPolicy()


@dataclass(frozen=True, slots=True)
class IntelligenceResult:
    message_id: UUID
    summary: str
    project_refs: tuple[str, ...]
    task_refs: tuple[str, ...]
    date_refs: tuple[str, ...]
    action_candidates: tuple[dict[str, object], ...]
    knowledge_candidate: dict[str, object] | None
    injection_detected: bool
    confidence: float
    evidence: tuple[EvidenceLocator, ...]
    mode: str = "rules"
    model_ref: str | None = None
    parser_version: str = "rules-v1"
    decisions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    commitments: tuple[str, ...] = ()
    content_sha256: str = ""
    policy_version: str = DEFAULT_ANALYSIS_POLICY.policy_version
    prompt_version: str = DEFAULT_ANALYSIS_POLICY.prompt_version
    calibration_version: str = DEFAULT_ANALYSIS_POLICY.calibration_version
    analysis_metadata: Mapping[str, object] = field(default_factory=dict)
    abstain_reason: str | None = None
    action_min_confidence: float = DEFAULT_ANALYSIS_POLICY.action_min_confidence
    knowledge_min_confidence: float = DEFAULT_ANALYSIS_POLICY.knowledge_min_confidence
    max_evidence: int = DEFAULT_ANALYSIS_POLICY.max_evidence
    max_actions: int = DEFAULT_ANALYSIS_POLICY.max_actions


def analyze_message(
    message: MailMessageProjection,
    *,
    policy: AnalysisPolicy | None = None,
) -> IntelligenceResult:
    """Extract bounded facts without treating message content as instructions."""

    resolved_policy = policy or DEFAULT_ANALYSIS_POLICY
    source = message.body_text or ""
    included_source = source[: resolved_policy.max_input_chars]
    estimated_tokens = ceil(len(included_source) / resolved_policy.chars_per_token)
    if estimated_tokens > resolved_policy.max_estimated_tokens:
        included_chars = int(resolved_policy.max_estimated_tokens * resolved_policy.chars_per_token)
        included_source = included_source[:included_chars]
        estimated_tokens = ceil(len(included_source) / resolved_policy.chars_per_token)
    analysis_metadata: dict[str, object] = {
        "policy_version": resolved_policy.policy_version,
        "prompt_version": resolved_policy.prompt_version,
        "parser_version": resolved_policy.parser_version,
        "calibration_version": resolved_policy.calibration_version,
        "mode": resolved_policy.mode,
        "input_chars": len(source),
        "included_chars": len(included_source),
        "omitted_chars": max(0, len(source) - len(included_source)),
        "estimated_tokens": estimated_tokens,
        "max_estimated_tokens": resolved_policy.max_estimated_tokens,
        "max_input_chars": resolved_policy.max_input_chars,
        "max_evidence": resolved_policy.max_evidence,
        "max_actions": resolved_policy.max_actions,
        "truncated": len(included_source) < len(source),
    }
    safe_text = sanitize_text(included_source)
    current_text = _current_message_text(safe_text)
    project_refs = tuple(
        dict.fromkeys(match.group(1) for match in _PROJECT_RE.finditer(current_text))
    )
    task_refs = tuple(dict.fromkeys(match.group(1) for match in _TASK_RE.finditer(current_text)))
    date_refs = tuple(dict.fromkeys(match.group(1) for match in _DATE_RE.finditer(current_text)))
    decisions = _line_values(current_text, _DECISION_RE)
    risks = _line_values(current_text, _RISK_RE)
    commitments = tuple(
        dict.fromkeys(
            (
                *_line_values(current_text, _COMMITMENT_RE),
                *_line_values(current_text, _ACTION_ITEM_RE),
            )
        )
    )
    injection_detected = bool(_INJECTION_RE.search(source))
    evidence: list[EvidenceLocator] = []
    for pattern, evidence_field in (
        (_PROJECT_RE, "project_ref"),
        (_TASK_RE, "task_ref"),
        (_DATE_RE, "date_ref"),
        (_DECISION_RE, "decision"),
        (_RISK_RE, "risk"),
        (_COMMITMENT_RE, "commitment"),
        (_ACTION_ITEM_RE, "action_item"),
    ):
        for match in pattern.finditer(current_text):
            evidence.append(
                EvidenceLocator(
                    message_id=message.message_id,
                    field=evidence_field,
                    start=match.start(),
                    end=match.end(),
                    text_sha256=digest_text(match.group(0)),
                )
            )
            if len(evidence) >= resolved_policy.max_evidence:
                break
        if len(evidence) >= resolved_policy.max_evidence:
            break
    action_candidates: list[dict[str, object]] = []
    if project_refs or task_refs or date_refs or decisions or risks or commitments:
        action_confidence = calibrate_confidence(
            0.72 if not injection_detected else 0.2,
            evidence_count=len(evidence),
            injection_detected=injection_detected,
        )
        action_candidates.append(
            {
                "action_type": "follow_up",
                "project_refs": project_refs,
                "task_refs": task_refs,
                "due_date_refs": date_refs,
                "decisions": decisions,
                "risks": risks,
                "commitments": commitments,
                "source_message_id": str(message.message_id),
                "confidence": action_confidence,
                "requires_review": True,
            }
        )
    knowledge_candidate = None
    if len(current_text) >= 120 and not injection_detected:
        value_breakdown = _knowledge_value_breakdown(
            project_refs=project_refs,
            task_refs=task_refs,
            decisions=decisions,
            risks=risks,
            commitments=commitments,
            evidence_count=len(evidence),
        )
        knowledge_candidate = {
            "title": message.subject,
            "summary": summarize(current_text),
            "value_dimensions": tuple(value_breakdown),
            "value_score": round(sum(value_breakdown.values()) / len(value_breakdown), 3),
            "value_breakdown": value_breakdown,
            "suggested_scope": "project" if project_refs or task_refs else "personal",
            "retention_policy": "review_required",
            "sensitivity": "internal",
            "source_message_id": str(message.message_id),
            "rights_state": "awaiting_review",
            "security_state": "pending_scan",
            "requires_review": True,
        }
    confidence = calibrate_confidence(
        0.1 if injection_detected else min(0.95, 0.5 + 0.1 * len(evidence)),
        evidence_count=len(evidence),
        injection_detected=injection_detected,
    )
    abstain_reason = (
        "prompt_injection_detected"
        if injection_detected
        else "low_confidence"
        if confidence < resolved_policy.low_confidence_floor
        else None
    )
    return IntelligenceResult(
        message_id=message.message_id,
        summary=summarize(current_text),
        project_refs=project_refs,
        task_refs=task_refs,
        date_refs=date_refs,
        action_candidates=tuple(action_candidates),
        knowledge_candidate=knowledge_candidate,
        injection_detected=injection_detected,
        confidence=confidence,
        evidence=tuple(evidence),
        mode=resolved_policy.mode,
        parser_version=resolved_policy.parser_version,
        decisions=decisions,
        risks=risks,
        commitments=commitments,
        content_sha256=message.content_sha256,
        policy_version=resolved_policy.policy_version,
        prompt_version=resolved_policy.prompt_version,
        calibration_version=resolved_policy.calibration_version,
        analysis_metadata=analysis_metadata,
        abstain_reason=abstain_reason,
        action_min_confidence=resolved_policy.action_min_confidence,
        knowledge_min_confidence=resolved_policy.knowledge_min_confidence,
        max_evidence=resolved_policy.max_evidence,
        max_actions=resolved_policy.max_actions,
    )


def calibrate_confidence(
    raw_confidence: float,
    *,
    evidence_count: int,
    injection_detected: bool,
) -> float:
    """Calibrate a bounded score using evidence support; injection always abstains."""

    if not isinstance(raw_confidence, (int, float)) or isinstance(raw_confidence, bool):
        raise ValueError("analysis_raw_confidence_invalid")
    if (
        not isinstance(evidence_count, int)
        or isinstance(evidence_count, bool)
        or evidence_count < 0
    ):
        raise ValueError("analysis_evidence_count_invalid")
    raw = min(1.0, max(0.0, float(raw_confidence)))
    support = min(1.0, evidence_count / 8.0)
    calibrated = 0.7 * raw + 0.3 * (0.35 + 0.65 * support)
    return round(min(1.0, max(0.0, 0.05 if injection_detected else calibrated)), 4)


AI_RESULT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "maxLength": 600},
        "action_candidates": {"type": "array"},
        "knowledge_candidate": {"type": ["object", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "model_ref": {"type": "string", "maxLength": 200},
        "decisions": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 240},
        },
        "risks": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 240}},
        "commitments": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 240},
        },
    },
}


def merge_ai_result(
    baseline: IntelligenceResult, output: Mapping[str, object]
) -> IntelligenceResult:
    """Validate a model result before it can become a MailHub candidate."""

    allowed = {
        "summary",
        "action_candidates",
        "knowledge_candidate",
        "confidence",
        "model_ref",
        "decisions",
        "risks",
        "commitments",
    }
    unknown = set(output) - allowed
    if unknown:
        raise ValueError("ai_output_unknown_fields:" + ",".join(sorted(unknown)))
    summary = output.get("summary", baseline.summary)
    if not isinstance(summary, str) or len(summary) > 600:
        raise ValueError("ai_output_summary_invalid")
    confidence = output.get("confidence", baseline.confidence)
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("ai_output_confidence_invalid")
    actions_provided = "action_candidates" in output
    actions = output.get("action_candidates", baseline.action_candidates)
    if not isinstance(actions, (list, tuple)) or any(
        not isinstance(item, dict) for item in actions
    ):
        raise ValueError("ai_output_actions_invalid")
    if len(actions) > baseline.max_actions:
        raise ValueError("ai_output_actions_limit")
    normalized_actions: tuple[dict[str, object], ...]
    if actions_provided:
        enriched_actions: list[dict[str, object]] = []
        for action in actions:
            normalized_action = dict(action)
            action_confidence = normalized_action.get("confidence")
            if action_confidence is not None:
                if (
                    not isinstance(action_confidence, (int, float))
                    or isinstance(action_confidence, bool)
                    or not 0 <= action_confidence <= 1
                ):
                    raise ValueError("ai_output_action_confidence_invalid")
                normalized_action["confidence"] = calibrate_confidence(
                    float(action_confidence),
                    evidence_count=len(baseline.evidence),
                    injection_detected=baseline.injection_detected,
                )
            enriched_actions.append(normalized_action)
        normalized_actions = tuple(enriched_actions)
    else:
        normalized_actions = tuple(actions)
    knowledge = output.get("knowledge_candidate", baseline.knowledge_candidate)
    if knowledge is not None and not isinstance(knowledge, dict):
        raise ValueError("ai_output_knowledge_invalid")
    model_ref = output.get("model_ref", baseline.model_ref)
    if model_ref is not None and (not isinstance(model_ref, str) or len(model_ref) > 200):
        raise ValueError("ai_output_model_ref_invalid")
    decisions = _bounded_text_values(
        output.get("decisions", baseline.decisions), "ai_output_decisions"
    )
    risks = _bounded_text_values(output.get("risks", baseline.risks), "ai_output_risks")
    commitments = _bounded_text_values(
        output.get("commitments", baseline.commitments), "ai_output_commitments"
    )
    calibrated_confidence = calibrate_confidence(
        float(confidence),
        evidence_count=len(baseline.evidence),
        injection_detected=baseline.injection_detected,
    )
    abstain_reason = baseline.abstain_reason
    if abstain_reason is None and calibrated_confidence < baseline.action_min_confidence:
        abstain_reason = "low_confidence"
    return IntelligenceResult(
        message_id=baseline.message_id,
        summary=summary,
        project_refs=baseline.project_refs,
        task_refs=baseline.task_refs,
        date_refs=baseline.date_refs,
        action_candidates=normalized_actions,
        knowledge_candidate=knowledge,
        injection_detected=baseline.injection_detected,
        confidence=calibrated_confidence,
        evidence=baseline.evidence,
        mode="ai_enriched",
        model_ref=model_ref,
        parser_version=baseline.parser_version + "+ai-schema-v1",
        decisions=decisions,
        risks=risks,
        commitments=commitments,
        content_sha256=baseline.content_sha256,
        policy_version=baseline.policy_version,
        prompt_version=baseline.prompt_version,
        calibration_version=baseline.calibration_version,
        analysis_metadata={**dict(baseline.analysis_metadata), "mode": "ai_enriched"},
        abstain_reason=abstain_reason,
        action_min_confidence=baseline.action_min_confidence,
        knowledge_min_confidence=baseline.knowledge_min_confidence,
        max_evidence=baseline.max_evidence,
        max_actions=baseline.max_actions,
    )


def sanitize_text(value: str) -> str:
    """Remove active HTML content; links are data and are not fetched."""

    without_active = re.sub(
        r"<\s*(script|style|iframe|form)\b[^>]*>.*?<\s*/\s*\1\s*>",
        " ",
        value,
        flags=re.I | re.S,
    )
    return _HTML_TAG_RE.sub(" ", without_active).replace("\x00", " ").strip()


def summarize(value: str, *, maximum: int = 600) -> str:
    compact = " ".join(value.split())
    return compact[:maximum] + ("…" if len(compact) > maximum else "")


def _current_message_text(value: str) -> str:
    without_quotes = _QUOTE_RE.sub(" ", value)
    without_signature = _SIGNATURE_RE.sub(" ", without_quotes)
    return without_signature.strip()


def _line_values(value: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(" ".join(match.group(1).split())[:240] for match in pattern.finditer(value))
    )


def _bounded_text_values(value: object, error_code: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        raise ValueError(f"{error_code}_invalid")
    result = tuple(item.strip() for item in value if isinstance(item, str))
    if len(result) != len(value) or any(not item or len(item) > 240 for item in result):
        raise ValueError(f"{error_code}_invalid")
    return tuple(dict.fromkeys(result))


def _knowledge_value_breakdown(
    *,
    project_refs: tuple[str, ...],
    task_refs: tuple[str, ...],
    decisions: tuple[str, ...],
    risks: tuple[str, ...],
    commitments: tuple[str, ...],
    evidence_count: int,
) -> dict[str, float]:
    return {
        "relevance": 0.9 if project_refs or task_refs else 0.45,
        "action_value": 0.9 if decisions or commitments else (0.7 if risks else 0.35),
        "persistence": 0.85 if decisions or risks else 0.5,
        "completeness": min(0.95, 0.35 + 0.08 * evidence_count),
    }
