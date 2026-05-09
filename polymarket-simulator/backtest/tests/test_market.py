from backtest.enums import MarketCategory
from backtest.market import Market


def _mk():
    return Market(
        slug="t", condition_id="c", token_ids=("A", "B"),
        outcome_names=("Yes", "No"), category=MarketCategory.SPORTS,
    )


def test_initial_tick_is_none():
    m = _mk()
    assert m.tick() is None


def test_observe_tick_sets_value():
    m = _mk()
    m.observe_tick([0.50, 0.45, 0.99])
    assert m.tick() == 0.01


def test_observe_tick_finer_replaces_coarser():
    m = _mk()
    m.observe_tick([0.50])              # tick 0.01
    m.observe_tick([0.5234])            # tick 0.0001
    assert m.tick() == 0.0001


def test_observe_tick_coarser_does_not_replace_finer():
    """Once we've seen 3-decimal prices, a follow-up snapshot of round
    prices does not coarsen the tick — Polymarket keeps the finer grid."""
    m = _mk()
    m.observe_tick([0.523])             # tick 0.001
    m.observe_tick([0.50, 0.49])        # would suggest 0.01 alone
    assert m.tick() == 0.001


def test_other_token():
    m = _mk()
    assert m.other_token("A") == "B"
    assert m.other_token("B") == "A"


def test_default_rate_sports():
    m = _mk()
    assert m.default_rate() == 0.03
