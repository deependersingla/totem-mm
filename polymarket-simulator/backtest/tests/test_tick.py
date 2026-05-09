import pytest

from backtest.tick import infer_tick_from_prices, is_multiple_of_tick


def test_only_whole_cents_yields_001():
    assert infer_tick_from_prices([0.50, 0.45, 0.99]) == 0.01


def test_three_decimals_yields_0001():
    assert infer_tick_from_prices([0.5, 0.453, 0.501]) == 0.001


def test_four_decimals_yields_00001():
    assert infer_tick_from_prices([0.5234, 0.50, 0.99]) == 0.0001


def test_one_decimal_only():
    assert infer_tick_from_prices([0.1, 0.5, 0.9]) == 0.1


def test_empty_yields_coarsest():
    assert infer_tick_from_prices([]) == 0.1


def test_rounding_quirk_is_handled():
    # 0.585 → some snapshots store as 0.5850000000000001
    assert infer_tick_from_prices([0.585, 0.50]) == 0.001


def test_is_multiple_of_tick():
    assert is_multiple_of_tick(0.50, 0.01)
    assert is_multiple_of_tick(0.5234, 0.0001)
    assert not is_multiple_of_tick(0.505, 0.01)


def test_invalid_tick_rejected():
    with pytest.raises(ValueError):
        is_multiple_of_tick(0.5, 0.005)
