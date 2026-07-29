import math

import pytest

from analogcoder.experiment_stats import (
    SPRT,
    informative,
    mcnemar_exact,
    required_discordant_pairs,
    required_pairs,
)


# ---------------------------------------------------------------------------
# mcnemar_exact
# ---------------------------------------------------------------------------


def test_mcnemar_exact_matches_a_hand_computed_value():
    # n=10, k=min(1,9)=1. Two-sided exact p = 2 * P(X<=1) for X~Binomial(10, 0.5)
    # = 2 * (C(10,0) + C(10,1)) / 2**10 = 2 * 11/1024 = 22/1024 = 0.021484375.
    # Hand-computed independently of the implementation, not copied from it.
    assert mcnemar_exact(1, 9) == pytest.approx(22 / 1024)


def test_mcnemar_exact_is_symmetric_in_b_and_c():
    assert mcnemar_exact(1, 9) == pytest.approx(mcnemar_exact(9, 1))
    assert mcnemar_exact(3, 12) == pytest.approx(mcnemar_exact(12, 3))


def test_mcnemar_exact_returns_one_when_b_equals_c():
    # No asymmetry in the discordant pairs at all - no evidence against H0.
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(0, 0) == 1.0


def test_mcnemar_exact_returns_one_when_there_are_no_discordant_pairs():
    # n=0 must be "no evidence", not a ZeroDivisionError from 2**0 handling
    # gone wrong, and not something silently below 1.0.
    assert mcnemar_exact(0, 0) == 1.0


def test_mcnemar_exact_differs_from_the_chi_square_approximation_on_small_n():
    # This is the guard against a plausible wrong implementation: swap the
    # exact binomial tail for the normal/chi-square approximation to
    # McNemar's test and this test must fail, because the spec requires the
    # exact form specifically for small samples where the approximation is
    # known to be inaccurate.
    #
    # Standard continuity-corrected McNemar chi-square, computed independently
    # here (not imported from the module under test):
    #   chi2 = (|b-c| - 1)^2 / (b+c),  z = sqrt(chi2),  p = 2*(1-Phi(z))
    b, c = 1, 9
    n = b + c
    stat = (abs(b - c) - 1) ** 2 / n
    z = math.sqrt(stat)
    chi_square_p = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))

    exact_p = mcnemar_exact(b, c)

    assert exact_p != pytest.approx(chi_square_p)
    assert abs(exact_p - chi_square_p) > 1e-3


def test_mcnemar_exact_rejects_negative_counts():
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 3)
    with pytest.raises(ValueError):
        mcnemar_exact(3, -1)


# ---------------------------------------------------------------------------
# required_discordant_pairs
# ---------------------------------------------------------------------------


def test_required_discordant_pairs_is_monotone_in_effect_size():
    # A smaller true discordance split (p1 closer to 0.5, i.e. a smaller
    # effect) must require MORE discordant pairs to detect at the same
    # alpha/power - not fewer, and not the same (a wrong implementation that
    # e.g. drops the (p1-0.5)**2 denominator would flatten this).
    n_small_effect = required_discordant_pairs(0.6)
    n_medium_effect = required_discordant_pairs(0.7)
    n_large_effect = required_discordant_pairs(0.9)
    assert n_small_effect > n_medium_effect > n_large_effect


def test_required_discordant_pairs_rejects_zero_effect_size():
    # p1 == 0.5 means the discordant pairs split exactly like H0 - detecting
    # that "effect" needs infinitely many pairs. Returning a large-but-finite
    # int here would silently misrepresent that as a feasible experiment.
    with pytest.raises(ValueError):
        required_discordant_pairs(0.5)


def test_required_discordant_pairs_returns_a_positive_int():
    n = required_discordant_pairs(0.8)
    assert isinstance(n, int)
    assert n > 0


# ---------------------------------------------------------------------------
# required_pairs
# ---------------------------------------------------------------------------


def test_required_pairs_halving_discordance_rate_at_least_doubles_the_requirement():
    # This is the function that would have caught the D1 n=3 failure before
    # the run: it converts "pairs where the two arms could possibly disagree"
    # into "total pairs to run", and a rarer discordance event needs
    # proportionally (here: at least) more total pairs to see enough of them.
    # A wrong implementation that used discordance_rate as a multiplier
    # instead of a divisor would make the requirement SMALLER when the rate
    # halves - the opposite direction - so this test distinguishes the two.
    # p1=0.8 makes required_discordant_pairs(0.8) == 20 (an exact multiple of
    # both rates below), so the ceil() rounding in required_pairs cannot mask
    # a rounding-driven near-miss the way an arbitrary p1 could.
    n_common = required_pairs(0.8, discordance_rate=0.5)
    n_rare = required_pairs(0.8, discordance_rate=0.25)
    assert n_rare >= 2 * n_common


def test_required_pairs_rejects_non_positive_or_over_one_discordance_rate():
    with pytest.raises(ValueError):
        required_pairs(0.7, discordance_rate=0.0)
    with pytest.raises(ValueError):
        required_pairs(0.7, discordance_rate=-0.1)
    with pytest.raises(ValueError):
        required_pairs(0.7, discordance_rate=1.1)


def test_required_pairs_at_discordance_rate_one_equals_required_discordant_pairs():
    # Every pair discordant is the degenerate case where the two quantities
    # coincide - a useful sanity anchor distinct from the halving test above.
    assert required_pairs(0.8, discordance_rate=1.0) == required_discordant_pairs(0.8)


# ---------------------------------------------------------------------------
# SPRT
# ---------------------------------------------------------------------------


def test_sprt_a_stream_of_all_successes_reaches_h1():
    test = SPRT(p0=0.3, p1=0.7, alpha=0.05, beta=0.2, max_observations=200)
    verdict = "continue"
    for _ in range(200):
        verdict = test.update(True)
        if verdict != "continue":
            break
    assert verdict == "H1"
    assert test.verdict == "H1"


def test_sprt_a_stream_of_all_failures_reaches_h0():
    test = SPRT(p0=0.3, p1=0.7, alpha=0.05, beta=0.2, max_observations=200)
    verdict = "continue"
    for _ in range(200):
        verdict = test.update(False)
        if verdict != "continue":
            break
    assert verdict == "H0"
    assert test.verdict == "H0"


def test_sprt_a_balanced_alternating_stream_stays_undecided_then_hits_the_cap():
    # Alternating success/failure keeps the log-likelihood ratio oscillating
    # near zero (the increments are +log(p1/p0) and -log(p1/p0)-shifted terms
    # that do not accumulate in one direction), so neither boundary is ever
    # crossed. A version without max_observations would loop forever on data
    # shaped like this - the cap is what makes the harness usable at all.
    test = SPRT(p0=0.3, p1=0.7, alpha=0.05, beta=0.2, max_observations=20)
    verdict = "continue"
    seen_continue = False
    for i in range(20):
        verdict = test.update(i % 2 == 0)
        if verdict == "continue":
            seen_continue = True
        else:
            break
    assert seen_continue  # it really did stay undecided for a while
    assert verdict == "inconclusive"
    assert test.verdict == "inconclusive"


def test_sprt_exposes_the_accumulated_log_likelihood_ratio():
    test = SPRT(p0=0.3, p1=0.7, alpha=0.05, beta=0.2, max_observations=200)
    assert test.llr == 0.0
    test.update(True)
    assert test.llr == pytest.approx(math.log(0.7 / 0.3))
    test.update(False)
    assert test.llr == pytest.approx(
        math.log(0.7 / 0.3) + math.log(0.3 / 0.7)
    )


def test_sprt_rejects_equal_hypotheses():
    with pytest.raises(ValueError):
        SPRT(p0=0.5, p1=0.5)


# ---------------------------------------------------------------------------
# informative
# ---------------------------------------------------------------------------


def test_informative_is_false_when_condition_count_is_zero():
    ok, reason = informative(0, what="선행 실패 이벤트")
    assert ok is False
    assert "선행 실패 이벤트" in reason
    assert "0" in reason


def test_informative_is_true_when_condition_count_is_at_least_one():
    ok, reason = informative(1, what="선행 실패 이벤트")
    assert ok is True
    assert reason == ""


def test_informative_pins_the_measured_d1_case():
    # runs/measure/before/two_stage-1 had 4 proposal events and 0 gate
    # rejections, 0 verify_pre rejections, 0 rollbacks - the repeat-proposal
    # metric was arithmetically pinned at 0.000 on that run, and the first D1
    # comparison used it anyway. This is the case informative() exists to catch.
    gate_rejections = 0
    verify_pre_rejections = 0
    rollbacks = 0
    condition_count = gate_rejections + verify_pre_rejections + rollbacks
    ok, reason = informative(condition_count, what="선행 실패 이벤트")
    assert ok is False
    assert reason != ""
