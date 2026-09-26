"""Generator determinism and a small benchmark run (regression guard, not an accuracy claim)."""
import hashlib

from tests.evaluation import benchmark
from tests.evaluation.generator import generate


def _h(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_generator_is_deterministic_and_truth_is_separate(tmp_path):
    a = generate(300, 42, "default", tmp_path / "d1", tmp_path / "t1")
    b = generate(300, 42, "default", tmp_path / "d2", tmp_path / "t2")
    c = generate(300, 43, "default", tmp_path / "d3", tmp_path / "t3")
    for f in ("ledger.csv", "psp.csv", "bank.csv"):
        assert _h(a.data_dir / f) == _h(b.data_dir / f)
        assert _h(a.data_dir / f) != _h(c.data_dir / f)
    assert sorted(p.name for p in a.data_dir.iterdir()) == ["bank.csv", "ledger.csv", "psp.csv"]
    assert not any("truth" in p.name for p in a.data_dir.iterdir())
    for f in ("ledger.csv", "psp.csv", "bank.csv"):
        assert "clean" not in (a.data_dir / f).read_text()   # no labels leak into engine-visible files


def test_small_benchmark_runs_with_defined_denominators(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "DATA", tmp_path / "data")
    monkeypatch.setattr(benchmark, "TRUTH", tmp_path / "truth")
    for prof, seed in (("default", 42), ("adversarial", 99)):
        r = benchmark.run_one(f"t_{prof}", seed, prof, 400)
        a, b = r["chain_a"], r["chain_b"]
        assert a["false_match_denominator"] == a["auto_matched_groups"] > 0
        assert b["false_match_denominator"] == b["auto_matched_groups"] > 0
        assert a["clean_pairs"] > 0 and b["v1_matchable_truth_groups"] > 0
        # regression guard for this generator: the conservative rules produced no false matches when written
        assert a["false_matches"] == 0 and b["false_matches"] == 0
