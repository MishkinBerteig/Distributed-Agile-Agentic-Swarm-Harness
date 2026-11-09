"""Model routing (learning mechanism 3, docs/DESIGN-0.1.md section 6.6). Pure."""
from __future__ import annotations

from typing import Iterable, Optional

from app.models import KNOWN_HARNESSES, AgentRole, Complexity, SwarmConfig

CLASSIFIER_ROLE = "classifier"
MIN_STATS_SAMPLES = 5
MIN_SUCCESS_RATE = 0.5
_DEFAULT_COMPLEXITY = Complexity.MODERATE.value


def ladder(config: SwarmConfig) -> list[str]:
    """model_ladder restricted to allowed_models (ladder order); falls back to allowed_models."""
    allowed = set(config.allowed_models)
    result = [m for m in config.model_ladder if m in allowed]
    return result or list(config.allowed_models)


def _norm_complexity(complexity: Optional[str]) -> str:
    complexity = getattr(complexity, "value", complexity)
    valid = {c.value for c in Complexity}
    return complexity if complexity in valid else _DEFAULT_COMPLEXITY


def _resolve_allowed(model: str, config: SwarmConfig) -> str:
    """The model itself if allowed, else the nearest allowed ladder model.

    Nearest is by distance along the full configured ladder (ties go to the
    more capable model). A model that is not on the ladder at all falls back
    to the most capable allowed ladder model.
    """
    if model in config.allowed_models:
        return model
    lad = ladder(config)
    if not lad:
        return model  # nothing is allowed; validate_config reports this
    if model in config.model_ladder:
        pos = config.model_ladder.index(model)
        positions = {m: config.model_ladder.index(m) for m in lad if m in config.model_ladder}
        if positions:
            return min(positions, key=lambda m: (abs(positions[m] - pos), -positions[m]))
    return lad[-1]


def _ladder_index(model: str, lad: list[str], config: SwarmConfig) -> int:
    """Index of `model` in the working ladder (nearest position if absent)."""
    if model in lad:
        return lad.index(model)
    if model in config.model_ladder:
        pos = config.model_ladder.index(model)
        best, best_key = 0, None
        for i, m in enumerate(lad):
            if m in config.model_ladder:
                key = (abs(config.model_ladder.index(m) - pos), -i)
                if best_key is None or key < best_key:
                    best, best_key = i, key
        return best
    return 0


def choose_model(role: str, config: SwarmConfig, complexity: Optional[str] = None,
                 attempts: int = 0, stats: Optional[dict] = None) -> str:
    """Pick the model for a job.

    Judges, the classifier and the coordinator use their configured model.
    Delivery starts at its configured rung (+1 for complex work, +1 when
    learned stats show that rung failing for this complexity), then climbs one
    rung per prior failed attempt, capped at the top of the ladder.
    """
    role = getattr(role, "value", role)
    rm = config.role_models
    if role == AgentRole.SWARM_COORDINATOR.value:
        return _resolve_allowed(config.coordinator.model, config)
    if role == AgentRole.MISSION_JUDGEMENT.value:
        return _resolve_allowed(rm.mission_judgement, config)
    if role == AgentRole.QUALITY_JUDGEMENT.value:
        return _resolve_allowed(rm.quality_judgement, config)
    if role == CLASSIFIER_ROLE:
        return _resolve_allowed(rm.classifier, config)
    if role != AgentRole.MISSION_DELIVERY.value:
        raise ValueError(f"Unknown role '{role}' for model routing.")

    lad = ladder(config)
    if not lad:
        return rm.mission_delivery
    top = len(lad) - 1
    cx = _norm_complexity(complexity)
    base = _resolve_allowed(rm.mission_delivery, config)
    idx = _ladder_index(base, lad, config)
    if cx == Complexity.COMPLEX.value:
        idx += 1
    idx = min(idx, top)
    if stats:
        succ, total = stats.get((lad[idx], cx), (0, 0))
        if total >= MIN_STATS_SAMPLES and succ / total < MIN_SUCCESS_RATE:
            idx += 1
    idx += max(0, attempts)
    return lad[min(idx, top)]


def routing_stats(outcomes: Iterable[tuple[str, Optional[str], bool]]
                  ) -> dict[tuple[str, str], tuple[int, int]]:
    """(model, complexity) -> (successes, total); None complexity counts as 'moderate'."""
    acc: dict[tuple[str, str], list[int]] = {}
    for model, complexity, success in outcomes:
        cell = acc.setdefault((model, _norm_complexity(complexity)), [0, 0])
        cell[0] += 1 if success else 0
        cell[1] += 1
    return {k: (v[0], v[1]) for k, v in acc.items()}


def validate_config(config: SwarmConfig) -> list[str]:
    """Human-readable problems with a SwarmConfig (empty list = valid)."""
    errors: list[str] = []
    allowed = set(config.allowed_models)
    if not config.allowed_models:
        errors.append("allowed_models is empty; allow at least one model.")
    if not config.allowed_harnesses:
        errors.append("allowed_harnesses is empty; allow at least one harness.")
    if not config.allowed_providers:
        errors.append("allowed_providers is empty; allow at least one provider.")
    if not config.model_ladder:
        errors.append("model_ladder is empty; list models from cheapest to most capable.")
    elif config.allowed_models and not (set(config.model_ladder) & allowed):
        errors.append("model_ladder shares no model with allowed_models, so escalation "
                      "has no rungs; include at least one allowed model in the ladder.")

    rm = config.role_models
    for label, model in (("role_models.mission_judgement", rm.mission_judgement),
                         ("role_models.quality_judgement", rm.quality_judgement),
                         ("role_models.mission_delivery", rm.mission_delivery),
                         ("role_models.classifier", rm.classifier),
                         ("coordinator.model", config.coordinator.model)):
        if config.allowed_models and model not in allowed:
            errors.append(f"{label} '{model}' is not in allowed_models "
                          f"({', '.join(config.allowed_models)}).")
    if config.allowed_harnesses and config.coordinator.harness not in config.allowed_harnesses:
        errors.append(f"coordinator.harness '{config.coordinator.harness}' is not in "
                      f"allowed_harnesses ({', '.join(config.allowed_harnesses)}).")
    for h in config.allowed_harnesses:
        if h not in KNOWN_HARNESSES:
            errors.append(f"allowed_harnesses contains unknown harness '{h}' "
                          f"(known: {', '.join(KNOWN_HARNESSES)}).")
    for i, spec in enumerate(config.delivery_harnesses, 1):
        if spec.harness not in KNOWN_HARNESSES:
            errors.append(f"delivery_harnesses[{i}] harness '{spec.harness}' is unknown "
                          f"(known: {', '.join(KNOWN_HARNESSES)}).")
        elif config.allowed_harnesses and spec.harness not in config.allowed_harnesses:
            errors.append(f"delivery_harnesses[{i}] harness '{spec.harness}' is not in "
                          f"allowed_harnesses ({', '.join(config.allowed_harnesses)}).")
    if config.remote_fallback_minutes is not None and config.remote_fallback_minutes < 1:
        errors.append("remote_fallback_minutes must be at least 1 (or null to wait for a runner).")
    if config.allowed_providers and config.coordinator.provider not in config.allowed_providers:
        errors.append(f"coordinator.provider '{config.coordinator.provider}' is not in "
                      f"allowed_providers ({', '.join(config.allowed_providers)}).")

    for model, price in config.model_prices.items():
        if price < 0:
            errors.append(f"model_prices['{model}'] is negative ({price}); prices must be >= 0.")
    t = config.dod_thresholds
    for label, v in (("deterministic", t.deterministic), ("rubric", t.rubric),
                     ("mission_confidence", t.mission_confidence)):
        if not 0.0 <= v <= 1.0:
            errors.append(f"dod_thresholds.{label} is {v}; it must be between 0 and 1.")
    return errors
