from __future__ import annotations

from pathlib import Path

from src.refinery.router import ExtractionRouter
from src.refinery.triage import DocumentTriageAgent


def test_router_escalates_on_low_confidence(tmp_path: Path) -> None:
    doc = tmp_path / "thin.txt"
    doc.write_text("short line", encoding="utf-8")

    triage = DocumentTriageAgent(profiles_dir=tmp_path / "profiles")
    profile = triage.profile_document(doc)

    rules = tmp_path / "rules.yaml"
    rules.write_text("confidence_gate: 0.95\n", encoding="utf-8")

    router = ExtractionRouter(rules_path=rules, ledger_path=tmp_path / "ledger.jsonl")
    routed = router.extract(doc, profile)

    assert len(routed.attempted_strategies) >= 1
    assert routed.attempted_strategies[0] in {"fast_text", "layout_aware", "vision_augmented"}
    assert routed.extracted.strategy_used in {"layout_aware", "vision_augmented"}
    assert (tmp_path / "ledger.jsonl").exists()


def test_router_uses_strict_stage_gates_by_default(tmp_path: Path) -> None:
    router = ExtractionRouter(
        rules_path=tmp_path / "missing-rules.yaml",
        ledger_path=tmp_path / "ledger.jsonl",
    )

    assert router._confidence_gate_for("fast_text") == 0.9
    assert router._confidence_gate_for("layout_aware") == 0.88
    assert router._confidence_gate_for("vision_augmented") == 0.72


def test_router_global_confidence_gate_overrides_stage_specific(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text("confidence_gate: 0.96\n", encoding="utf-8")
    router = ExtractionRouter(rules_path=rules, ledger_path=tmp_path / "ledger.jsonl")

    assert router._confidence_gate_for("fast_text") == 0.96
    assert router._confidence_gate_for("layout_aware") == 0.96
    assert router._confidence_gate_for("vision_augmented") == 0.96
