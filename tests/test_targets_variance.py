"""Regression test for the negative-target direction fix (Phase 1).

actual/target*100 only reads as "progress toward target" when the
target is positive -- against a negative target it inverts, so a loss
worse than plan can render as ">100% achieved" and a result better than
plan can render as a negative percentage. routes.targets._metric_view()
is expected to withhold `pct` in that case and expose a signed
`variance`/`met` pair instead. Pure unit test: no database, no Flask
app -- _metric_view() takes plain dicts.

Run with:  python3 tests/test_targets_variance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from routes.targets import _metric_view  # noqa: E402

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---- Case 1: a loss worse than a negative target ----
# Target -£302, actual -£625 -> £323 worse than target, never "207%".
m = _metric_view("profit", None, {"profit": -302.0}, {"2026-02": {"profit": -625.0}}, "2026-02")
check("negative target, worse actual: pct withheld", m["pct"] is None, f"pct={m['pct']}")
check("negative target, worse actual: variance = -323", round(m["variance"], 2) == -323.0, f"variance={m['variance']}")
check("negative target, worse actual: met is False", m["met"] is False, f"met={m['met']}")

# ---- Case 2: the inverse -- a result better than a negative target ----
# Target -£625, actual -£302 -> £323 better than target.
m2 = _metric_view("profit", None, {"profit": -625.0}, {"2026-02": {"profit": -302.0}}, "2026-02")
check("negative target, better actual: pct withheld", m2["pct"] is None, f"pct={m2['pct']}")
check("negative target, better actual: variance = +323", round(m2["variance"], 2) == 323.0, f"variance={m2['variance']}")
check("negative target, better actual: met is True", m2["met"] is True, f"met={m2['met']}")

# ---- Regression guard: an ordinary positive target still shows pct ----
m3 = _metric_view("revenue", None, {"revenue": 1000.0}, {"2026-02": {"revenue": 1200.0}}, "2026-02")
check("positive target: pct present and correct", m3["pct"] == 120, f"pct={m3['pct']}")
check("positive target: met is True", m3["met"] is True, f"met={m3['met']}")

# ---- Edge case: no target at all ----
m4 = _metric_view("revenue", None, None, {"2026-02": {"revenue": 1200.0}}, "2026-02")
check("no target: pct is None", m4["pct"] is None)
check("no target: variance is None", m4["variance"] is None)
check("no target: met is None", m4["met"] is None)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All target-variance checks passed.")
