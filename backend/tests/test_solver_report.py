"""Tests for services.solver_report's helpers.

The Solver Report payload itself is covered through _solver_window_stats in
test_solver_stats.py.
"""

from services.solver_report import _median_p90


class TestMedianP90:
    """Nearest rank with no interpolation: xs[n // 2] and xs[int(0.9 * (n - 1))]."""

    def test_empty_is_none_for_both(self):
        assert _median_p90([]) == (None, None)

    def test_single_value_is_both(self):
        assert _median_p90([3.5]) == (3.5, 3.5)

    def test_two_values_take_the_upper_median_and_the_lower_p90(self):
        # int(0.9 * 1) == 0, so the p90 of two values sits below their median.
        assert _median_p90([1.0, 2.0]) == (2.0, 1.0)

    def test_ten_values(self):
        # xs[5] and xs[int(8.1)] == xs[8].
        assert _median_p90([float(i) for i in range(10)]) == (5.0, 8.0)

    def test_input_order_does_not_matter(self):
        assert _median_p90(iter([9, 3, 7, 1, 5])) == (5, 7)
