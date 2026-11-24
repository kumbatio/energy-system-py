"""Zero-dependency Python implementation of the Kumbatio Energy Model (SPEC.md).

Verified directly against `@kumbatio/energy-system`'s `conformance.json` vectors
so Python backends, AI agents, and research tools share the exact same model,
reconciliation rule, and local interchange file as TypeScript apps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Literal

EnergyLevel = Literal[0, 25, 50, 75, 100]
EnergySource = Literal["manual", "scheduled", "inferred"]
EnergyPresence = Literal["visible", "muted", "hidden"]
NotificationPriority = Literal["normal", "high", "critical"]
OriginatorTier = Literal["exempt", "known", "unknown"]

MAX_SAFE_INTEGER = 9_007_199_254_740_991
UNPRODUCED_ORIGIN = "0-initial"
UNPRODUCED_TIMESTAMP = 0

ENERGY_LEVEL_VALUES: tuple[EnergyLevel, ...] = (100, 75, 50, 25, 0)
ENERGY_SOURCE_VALUES: frozenset[str] = frozenset(("manual", "scheduled", "inferred"))
ENERGY_PRESENCE_VALUES: frozenset[str] = frozenset(("visible", "muted", "hidden"))
SOURCE_PRIORITY: dict[str, int] = {"manual": 3, "scheduled": 2, "inferred": 1}

LEVELS: tuple[dict[str, Any], ...] = (
    {
        "value": 100,
        "key": "peak",
        "label": "Peak",
        "description": "Full capacity for planning, tough decisions, and deep creative work.",
        "cognitiveProfile": {
            "decisionCapacity": "high",
            "focusDuration": "extended",
            "taskComplexity": "complex",
            "interruptionTolerance": "high",
        },
    },
    {
        "value": 75,
        "key": "active",
        "label": "Active",
        "description": "Good capacity for focused work and problem-solving.",
        "cognitiveProfile": {
            "decisionCapacity": "moderate",
            "focusDuration": "moderate",
            "taskComplexity": "moderate",
            "interruptionTolerance": "moderate",
        },
    },
    {
        "value": 50,
        "key": "steady",
        "label": "Steady",
        "description": "Moderate capacity for steady, familiar, routine work.",
        "cognitiveProfile": {
            "decisionCapacity": "low",
            "focusDuration": "short",
            "taskComplexity": "routine",
            "interruptionTolerance": "low",
        },
    },
    {
        "value": 25,
        "key": "low",
        "label": "Low",
        "description": "Limited capacity for light review and simple steps.",
        "cognitiveProfile": {
            "decisionCapacity": "minimal",
            "focusDuration": "minimal",
            "taskComplexity": "simple",
            "interruptionTolerance": "minimal",
        },
    },
    {
        "value": 0,
        "key": "rest",
        "label": "Rest",
        "description": "Recovery mode for quiet reading and resting — no work expected.",
        "cognitiveProfile": {
            "decisionCapacity": "none",
            "focusDuration": "none",
            "taskComplexity": "consumption",
            "interruptionTolerance": "none",
        },
    },
)

_LEVEL_BY_VALUE: dict[int, dict[str, Any]] = {entry["value"]: entry for entry in LEVELS}


def is_energy_level(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in _LEVEL_BY_VALUE


def is_energy_source(value: Any) -> bool:
    return isinstance(value, str) and value in ENERGY_SOURCE_VALUES


def get_energy_levels() -> tuple[dict[str, Any], ...]:
    return LEVELS


def get_energy_level(level: int) -> dict[str, Any]:
    if not is_energy_level(level):
        raise ValueError(f"Invalid energy level: {level!r}")
    return _LEVEL_BY_VALUE[level]


def cycle_energy_level(current: int) -> EnergyLevel:
    if not is_energy_level(current):
        return 100
    idx = ENERGY_LEVEL_VALUES.index(current)  # type: ignore[arg-type]
    return ENERGY_LEVEL_VALUES[(idx + 1) % len(ENERGY_LEVEL_VALUES)]


@dataclass(frozen=True, slots=True)
class EnergyState:
    level: EnergyLevel
    timestamp: int
    source: EnergySource
    revision: int
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "timestamp": self.timestamp,
            "source": self.source,
            "revision": self.revision,
            "origin": self.origin,
        }


def is_unproduced_state(state: EnergyState) -> bool:
    return state.timestamp == UNPRODUCED_TIMESTAMP and state.origin == UNPRODUCED_ORIGIN


def parse_external_energy_state(value: Any) -> EnergyState:
    """Strictly validate an incoming dict against spec/energy-state.schema.json."""
    if not isinstance(value, dict):
        raise ValueError("Energy state must be an object")

    expected_keys = {"level", "timestamp", "source", "revision", "origin"}
    actual_keys = set(value.keys())
    unknown = actual_keys - expected_keys
    if unknown:
        raise ValueError(f"Energy state has unknown properties: {', '.join(sorted(unknown))}")
    missing = expected_keys - actual_keys
    if missing:
        raise ValueError(f"Energy state is missing properties: {', '.join(sorted(missing))}")

    level = value["level"]
    if not is_energy_level(level):
        raise ValueError(f"Invalid energy level: {level!r}")

    source = value["source"]
    if not is_energy_source(source):
        raise ValueError(f"Invalid energy source: {source!r}")

    timestamp = value["timestamp"]
    if (
        not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or timestamp < 0
        or timestamp > MAX_SAFE_INTEGER
    ):
        raise ValueError(f"Invalid energy timestamp: {timestamp!r}")

    revision = value["revision"]
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or revision > MAX_SAFE_INTEGER
    ):
        raise ValueError(f"Invalid energy revision: {revision!r}")

    origin = value["origin"]
    if not isinstance(origin, str) or len(origin.strip()) == 0:
        raise ValueError(f"Invalid energy origin: {origin!r}")

    return EnergyState(
        level=level,
        timestamp=timestamp,
        source=source,
        revision=revision,
        origin=origin,
    )


def is_preferred_energy_state(candidate: EnergyState, current: EnergyState) -> bool:
    """SPEC.md §4 deterministic state reconciliation."""
    if candidate.timestamp != current.timestamp:
        return candidate.timestamp > current.timestamp
    if candidate.revision != current.revision:
        return candidate.revision > current.revision
    if candidate.source != current.source:
        return SOURCE_PRIORITY[candidate.source] > SOURCE_PRIORITY[current.source]
    if candidate.origin != current.origin:
        return candidate.origin > current.origin
    if candidate.level != current.level:
        return candidate.level > current.level
    return False


# ── Built-in Strategy Tables (SPEC.md §5) ──

STRATEGIES: dict[str, dict[int, dict[str, Any]]] = {
    "ui-visibility": {
        100: {
            "sidebar": True,
            "tabBar": True,
            "statusBar": True,
            "toolbar": True,
            "chromeOpacity": 1,
            "chromeOpacityHover": 1,
            "contentMaxWidth": "none",
            "contentFontScale": 1,
            "readOnlyCursor": False,
        },
        75: {
            "sidebar": True,
            "tabBar": True,
            "statusBar": True,
            "toolbar": True,
            "chromeOpacity": 0.7,
            "chromeOpacityHover": 1,
            "contentMaxWidth": "none",
            "contentFontScale": 1,
            "readOnlyCursor": False,
        },
        50: {
            "sidebar": True,
            "tabBar": True,
            "statusBar": True,
            "toolbar": True,
            "chromeOpacity": 0.4,
            "chromeOpacityHover": 1,
            "contentMaxWidth": "90ch",
            "contentFontScale": 1,
            "readOnlyCursor": False,
        },
        25: {
            "sidebar": False,
            "tabBar": False,
            "statusBar": False,
            "toolbar": True,
            "chromeOpacity": 0.1,
            "chromeOpacityHover": 1,
            "contentMaxWidth": "80ch",
            "contentFontScale": 1.05,
            "readOnlyCursor": False,
        },
        0: {
            "sidebar": False,
            "tabBar": False,
            "statusBar": False,
            "toolbar": False,
            "chromeOpacity": 0.05,
            "chromeOpacityHover": 0.8,
            "contentMaxWidth": "75ch",
            "contentFontScale": 1.1,
            "readOnlyCursor": True,
        },
    },
    "notifications": {
        100: {
            "allowVisual": True,
            "allowSound": True,
            "allowVibration": True,
            "batchInterval": 0,
            "priorityThreshold": "all",
        },
        75: {
            "allowVisual": True,
            "allowSound": True,
            "allowVibration": False,
            "batchInterval": 0,
            "priorityThreshold": "all",
        },
        50: {
            "allowVisual": True,
            "allowSound": False,
            "allowVibration": False,
            "batchInterval": 300_000,
            "priorityThreshold": "high",
        },
        25: {
            "allowVisual": True,
            "allowSound": False,
            "allowVibration": False,
            "batchInterval": 900_000,
            "priorityThreshold": "critical",
        },
        0: {
            "allowVisual": False,
            "allowSound": False,
            "allowVibration": False,
            "batchInterval": 0,
            "priorityThreshold": "none",
        },
    },
    "task-complexity": {
        100: {"maxComplexity": "complex", "suggestBreaks": False, "breakIntervalMinutes": 0},
        75: {"maxComplexity": "moderate", "suggestBreaks": False, "breakIntervalMinutes": 0},
        50: {"maxComplexity": "routine", "suggestBreaks": True, "breakIntervalMinutes": 45},
        25: {"maxComplexity": "simple", "suggestBreaks": True, "breakIntervalMinutes": 25},
        0: {"maxComplexity": "consumption", "suggestBreaks": False, "breakIntervalMinutes": 0},
    },
    "interaction-forgiveness": {
        100: {"undoWindowMs": 5000, "confirmDestructive": False, "autosaveIntervalMs": 60_000},
        75: {"undoWindowMs": 8000, "confirmDestructive": False, "autosaveIntervalMs": 45_000},
        50: {"undoWindowMs": 10_000, "confirmDestructive": True, "autosaveIntervalMs": 30_000},
        25: {"undoWindowMs": 15_000, "confirmDestructive": True, "autosaveIntervalMs": 20_000},
        0: {"undoWindowMs": 20_000, "confirmDestructive": True, "autosaveIntervalMs": 15_000},
    },
    "deferral": {
        100: {
            "orderedPresetIds": [
                "in-1-hour",
                "this-evening",
                "tomorrow-morning",
                "next-workday",
                "next-monday",
            ],
            "defaultPresetId": "in-1-hour",
        },
        75: {
            "orderedPresetIds": [
                "in-1-hour",
                "this-evening",
                "tomorrow-morning",
                "next-workday",
                "next-monday",
            ],
            "defaultPresetId": "in-1-hour",
        },
        50: {
            "orderedPresetIds": [
                "this-evening",
                "tomorrow-morning",
                "in-1-hour",
                "next-workday",
                "next-monday",
            ],
            "defaultPresetId": "this-evening",
        },
        25: {
            "orderedPresetIds": [
                "tomorrow-morning",
                "next-workday",
                "this-evening",
                "next-monday",
                "in-1-hour",
            ],
            "defaultPresetId": "tomorrow-morning",
        },
        0: {
            "orderedPresetIds": [
                "tomorrow-morning",
                "next-monday",
                "next-workday",
                "this-evening",
                "in-1-hour",
            ],
            "defaultPresetId": "tomorrow-morning",
        },
    },
    "autonomy": {
        100: {"confidenceThreshold": 0.6, "allowGeneratedContent": True, "maxUnattendedSteps": 8},
        75: {"confidenceThreshold": 0.7, "allowGeneratedContent": True, "maxUnattendedSteps": 5},
        50: {"confidenceThreshold": 0.8, "allowGeneratedContent": True, "maxUnattendedSteps": 3},
        25: {"confidenceThreshold": 0.9, "allowGeneratedContent": False, "maxUnattendedSteps": 1},
        0: {"confidenceThreshold": 1, "allowGeneratedContent": False, "maxUnattendedSteps": 1},
    },
    "demand-admission": {
        100: {"originatorThreshold": "all", "acknowledge": False, "acknowledgmentDetail": "full"},
        75: {"originatorThreshold": "known", "acknowledge": True, "acknowledgmentDetail": "full"},
        50: {"originatorThreshold": "exempt", "acknowledge": True, "acknowledgmentDetail": "full"},
        25: {"originatorThreshold": "exempt", "acknowledge": True, "acknowledgmentDetail": "brief"},
        0: {
            "originatorThreshold": "exempt",
            "acknowledge": True,
            "acknowledgmentDetail": "minimal",
        },
    },
    "sensory": {
        100: {
            "motionScale": 1,
            "allowDecorativeMotion": True,
            "allowAmbientAudio": True,
            "displayWeightScale": 1,
            "allowCallsToAction": True,
        },
        75: {
            "motionScale": 0.75,
            "allowDecorativeMotion": True,
            "allowAmbientAudio": True,
            "displayWeightScale": 0.85,
            "allowCallsToAction": True,
        },
        50: {
            "motionScale": 0.5,
            "allowDecorativeMotion": False,
            "allowAmbientAudio": True,
            "displayWeightScale": 0.7,
            "allowCallsToAction": True,
        },
        25: {
            "motionScale": 0.25,
            "allowDecorativeMotion": False,
            "allowAmbientAudio": False,
            "displayWeightScale": 0.55,
            "allowCallsToAction": False,
        },
        0: {
            "motionScale": 0,
            "allowDecorativeMotion": False,
            "allowAmbientAudio": False,
            "displayWeightScale": 0.45,
            "allowCallsToAction": False,
        },
    },
    "choice-budget": {
        100: {
            "maxChoices": None,
            "responseDensity": "detailed",
            "allowProactiveSuggestions": True,
        },
        75: {
            "maxChoices": 5,
            "responseDensity": "balanced",
            "allowProactiveSuggestions": True,
        },
        50: {
            "maxChoices": 3,
            "responseDensity": "concise",
            "allowProactiveSuggestions": False,
        },
        25: {
            "maxChoices": 1,
            "responseDensity": "single-step",
            "allowProactiveSuggestions": False,
        },
        0: {
            "maxChoices": 0,
            "responseDensity": "quiet",
            "allowProactiveSuggestions": False,
        },
    },
    "form-pacing": {
        100: {
            "maxFieldsPerStep": 12,
            "showOptionalFields": True,
            "validationTiming": "inline",
            "autosaveDraft": False,
        },
        75: {
            "maxFieldsPerStep": 8,
            "showOptionalFields": True,
            "validationTiming": "inline",
            "autosaveDraft": True,
        },
        50: {
            "maxFieldsPerStep": 5,
            "showOptionalFields": False,
            "validationTiming": "on-blur",
            "autosaveDraft": True,
        },
        25: {
            "maxFieldsPerStep": 3,
            "showOptionalFields": False,
            "validationTiming": "on-blur",
            "autosaveDraft": True,
        },
        0: {
            "maxFieldsPerStep": 1,
            "showOptionalFields": False,
            "validationTiming": "on-submit",
            "autosaveDraft": True,
        },
    },
}


def resolve_strategy(name: str, level: int) -> dict[str, Any]:
    if name not in STRATEGIES:
        raise KeyError(f"Unknown strategy: {name!r}")
    get_energy_level(level)
    return dict(STRATEGIES[name][level])


# ── Presence ──


def presence_at_or_above(minimum: int, below: EnergyPresence = "hidden") -> dict[str, str]:
    get_energy_level(minimum)
    return {str(level): ("visible" if level >= minimum else below) for level in ENERGY_LEVEL_VALUES}


def presence_at_or_below(maximum: int, above: EnergyPresence = "hidden") -> dict[str, str]:
    get_energy_level(maximum)
    return {str(level): ("visible" if level <= maximum else above) for level in ENERGY_LEVEL_VALUES}


# ── Pure Gating Decisions ──


def resolve_notification_outcome(
    config: dict[str, Any], priority: NotificationPriority, suppressed: bool
) -> Literal["delivered", "batched", "deferred"]:
    if suppressed:
        return "deferred"
    threshold = config["priorityThreshold"]
    if threshold == "none":
        return "deferred"
    if threshold == "critical" and priority != "critical":
        return "deferred"
    if threshold == "high" and priority == "normal":
        return "deferred"
    return "batched" if config["batchInterval"] > 0 else "delivered"


def resolve_demand_outcome(
    config: dict[str, Any],
    autonomy: dict[str, Any],
    originator_tier: OriginatorTier,
    bears_obligation: bool,
    confidence: float = 1.0,
) -> dict[str, Any]:
    if originator_tier not in ("exempt", "known", "unknown"):
        raise ValueError(f"Invalid originator tier: {originator_tier!r}")
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise ValueError(f"Invalid confidence: {confidence!r}")

    if originator_tier == "exempt":
        return {"admission": "live", "acknowledgment": None, "reason": "exempt-originator"}

    threshold = config["originatorThreshold"]
    admitted = (
        threshold == "all"
        or (threshold == "known" and originator_tier != "unknown")
        or (threshold == "exempt" and originator_tier == "exempt")
    )
    if admitted:
        return {"admission": "live", "acknowledgment": None, "reason": "tier-admitted"}

    if not bears_obligation:
        return {"admission": "live", "acknowledgment": None, "reason": "no-obligation"}

    if not config["acknowledge"]:
        return {"admission": "silent", "acknowledgment": None, "reason": "acknowledgment-disabled"}

    if confidence < autonomy["confidenceThreshold"]:
        return {"admission": "silent", "acknowledgment": None, "reason": "below-confidence"}

    return {
        "admission": "acknowledge",
        "acknowledgment": {
            "detail": config["acknowledgmentDetail"],
            "allowGeneratedContent": autonomy["allowGeneratedContent"],
        },
        "reason": "acknowledged",
    }


# ── Deferral Arithmetic ──


def resolve_deferral_preset(
    preset_id: str,
    now: datetime,
    morning_hour: int = 9,
    evening_hour: int = 18,
    weekend_days: tuple[int, ...] = (5, 6),  # Python weekday(): Mon=0 .. Sat=5, Sun=6
) -> datetime:
    if preset_id == "in-1-hour":
        return now + timedelta(hours=1)

    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if preset_id == "this-evening":
        evening = start_of_day.replace(hour=evening_hour)
        if evening <= now:
            evening += timedelta(days=1)
        return evening

    if preset_id == "tomorrow-morning":
        return (start_of_day + timedelta(days=1)).replace(hour=morning_hour)

    if preset_id == "next-workday":
        day = start_of_day + timedelta(days=1)
        while day.weekday() in weekend_days:
            day += timedelta(days=1)
        return day.replace(hour=morning_hour)

    if preset_id == "next-monday":
        delta = (0 - start_of_day.weekday()) % 7 or 7
        return (start_of_day + timedelta(days=delta)).replace(hour=morning_hour)

    raise ValueError(f"Unknown deferral preset: {preset_id!r}")


# ── Metrics & External Mapping ──

_PRODUCTIVITY_WINDOW_MINUTES = {100: 120, 75: 90, 50: 45, 25: 25, 0: 0}
_BREAK_INTERVAL_MINUTES = {100: 90, 75: 60, 50: 45, 25: 25, 0: 0}
_RECOVERY_HINT_MINUTES = {50: 10, 25: 20, 0: 30}


def get_energy_metrics(state: EnergyState, now_ms: int) -> dict[str, Any]:
    age_ms = 0 if is_unproduced_state(state) else max(0, now_ms - state.timestamp)
    definition = get_energy_level(state.level)
    result: dict[str, Any] = {
        "stateAgeMs": age_ms,
        "stateAgeMinutes": int(math.floor(age_ms / 60_000 + 0.5)),
        "expectedProductivityWindowMinutes": _PRODUCTIVITY_WINDOW_MINUTES[state.level],
        "suggestedBreakIntervalMinutes": _BREAK_INTERVAL_MINUTES[state.level],
        "recommendedTaskComplexity": definition["cognitiveProfile"]["taskComplexity"],
        "sustainable": 0 < state.level < 100,
    }
    if state.level in _RECOVERY_HINT_MINUTES:
        result["recoveryHintMinutes"] = _RECOVERY_HINT_MINUTES[state.level]
    return result


def map_to_nearest_energy_level(value: float) -> EnergyLevel:
    if not math.isfinite(value):
        return 100
    nearest: EnergyLevel = ENERGY_LEVEL_VALUES[0]
    nearest_dist = abs(nearest - value)
    for level in ENERGY_LEVEL_VALUES:
        dist = abs(level - value)
        if dist < nearest_dist:
            nearest = level
            nearest_dist = dist
    return nearest


# ── Local Interchange Bus (SPEC.md §12) ──


def resolve_energy_state_path() -> Path:
    explicit = os.environ.get("KUMBATIO_ENERGY_FILE", "").strip()
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "kumbatio" / "energy-state.json"
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "kumbatio" / "energy-state.json"
    return Path.home() / ".config" / "kumbatio" / "energy-state.json"


def load_energy_state_file(path: Path | None = None) -> EnergyState | None:
    target = path or resolve_energy_state_path()
    if not target.exists():
        return None
    raw = json.loads(target.read_text(encoding="utf-8"))
    return parse_external_energy_state(raw)


def save_energy_state_file(state: EnergyState, path: Path | None = None) -> Path:
    target = path or resolve_energy_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return target
