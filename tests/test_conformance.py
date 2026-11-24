"""Replay every conformance vector from energy-system/conformance.json against kumbatio_energy."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from kumbatio_energy import (
    EnergyState,
    cycle_energy_level,
    get_energy_levels,
    get_energy_metrics,
    is_preferred_energy_state,
    load_energy_state_file,
    map_to_nearest_energy_level,
    parse_external_energy_state,
    presence_at_or_above,
    presence_at_or_below,
    resolve_deferral_preset,
    resolve_demand_outcome,
    resolve_notification_outcome,
    resolve_strategy,
    save_energy_state_file,
)

CONFORMANCE_PATH = Path(__file__).resolve().parent / "conformance.json"


class ConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vectors = json.loads(CONFORMANCE_PATH.read_text(encoding="utf-8"))

    def test_levels_and_cycle(self) -> None:
        self.assertEqual(list(get_energy_levels()), self.vectors["levels"])
        for row in self.vectors["cycle"]:
            self.assertEqual(cycle_energy_level(row["from"]), row["to"])

    def test_all_strategy_tables(self) -> None:
        for name, table in self.vectors["strategies"].items():
            for level_str, expected in table.items():
                self.assertEqual(resolve_strategy(name, int(level_str)), expected, f"{name}@{level_str}")

    def test_presence_tables(self) -> None:
        for threshold_str, expected in self.vectors["presence"]["atOrAbove"].items():
            self.assertEqual(presence_at_or_above(int(threshold_str)), expected)
        for threshold_str, expected in self.vectors["presence"]["atOrAboveMuted"].items():
            self.assertEqual(presence_at_or_above(int(threshold_str), "muted"), expected)
        for threshold_str, expected in self.vectors["presence"]["atOrBelow"].items():
            self.assertEqual(presence_at_or_below(int(threshold_str)), expected)

    def test_notification_decisions(self) -> None:
        for vec in self.vectors["decisions"]["notification"]:
            cfg = resolve_strategy("notifications", vec["level"])
            actual = resolve_notification_outcome(cfg, vec["priority"], vec["suppressed"])
            self.assertEqual(actual, vec["outcome"])

    def test_demand_decisions(self) -> None:
        for vec in self.vectors["decisions"]["demand"]:
            admission = resolve_strategy("demand-admission", vec["level"])
            autonomy = resolve_strategy("autonomy", vec["level"])
            actual = resolve_demand_outcome(
                admission,
                autonomy,
                vec["originatorTier"],
                vec["bearsObligation"],
                vec["confidence"],
            )
            self.assertEqual(actual, vec["outcome"])

    def test_deferral_vectors_in_utc(self) -> None:
        morning = self.vectors["deferral"]["morningHour"]
        evening = self.vectors["deferral"]["eveningHour"]
        for ref in self.vectors["deferral"]["references"]:
            now = datetime.fromtimestamp(ref["epochMs"] / 1000, tz=timezone.utc)
            for preset_id, expected in ref["resolved"].items():
                actual = resolve_deferral_preset(preset_id, now, morning_hour=morning, evening_hour=evening)
                self.assertEqual(int(actual.timestamp() * 1000), expected["epochMs"], f"{ref['label']} / {preset_id}")

    def test_reconciliation_pairs_both_directions(self) -> None:
        for pair in self.vectors["reconciliation"]["pairs"]:
            candidate = parse_external_energy_state(pair["candidate"])
            current = parse_external_energy_state(pair["current"])
            self.assertEqual(is_preferred_energy_state(candidate, current), pair["expected"], pair["rule"])
            self.assertEqual(
                is_preferred_energy_state(current, candidate),
                pair["expectedReversed"],
                f"{pair['rule']} (reversed)",
            )

    def test_metrics_and_external_level_mapping(self) -> None:
        for vec in self.vectors["metrics"]:
            st = EnergyState(
                level=vec["level"],
                timestamp=vec["stateTimestamp"],
                source="manual",
                revision=0,
                origin="origin-a",
            )
            self.assertEqual(get_energy_metrics(st, vec["now"]), vec["metrics"])

        for vec in self.vectors["externalLevelMapping"]:
            self.assertEqual(map_to_nearest_energy_level(vec["input"]), vec["level"])

    def test_local_interchange_atomic_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "energy-state.json"
            self.assertIsNone(load_energy_state_file(path))
            state = EnergyState(level=25, timestamp=1_786_060_800_000, source="manual", revision=2, origin="py-app")
            save_energy_state_file(state, path)
            self.assertEqual(load_energy_state_file(path), state)


if __name__ == "__main__":
    unittest.main()
