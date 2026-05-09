"""Polymarket CLOB replay engine for backtesting cricket strategies.

Public API:

    from backtest.engine    import Engine
    from backtest.replay    import load_market, stream_events
    from backtest.strategy  import Strategy, ScheduledStrategy, ScheduledOrder
    from backtest.enums     import Side, OrderType, CricketSignal, MarketCategory
    from backtest.suite     import discover_captures, run_suite, SuiteConfig

A run is:

    market = load_market(db_path)
    engine = Engine(market)
    engine.register(my_strategy)
    engine.run(stream_events(db_path))
    report = engine.metrics.report()
"""
