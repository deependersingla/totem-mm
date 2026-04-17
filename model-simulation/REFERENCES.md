# References

## Academic Papers

### Core (Must-Read)
- **Davis, Perera & Swartz (2015)** — "A Simulator for Twenty20 Cricket" — Ball-by-ball T20 simulator with hierarchical Bayesian player models. The closest published work to this system. [PDF](https://www.sfu.ca/~tswartz/papers/t20sim.pdf)
- **Norton, Gray & Faff (2015)** — "Yes, One-Day International Cricket In-Play Trading Strategies Can Be Profitable!" — Proves 20.8% returns from wicket overreaction on Betfair. Journal of Banking & Finance. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2465536)
- **Brooker & Hogan (2011)** — WASP model. DP for cricket win probability. University of Canterbury.
- **Valerio (2021)** — "Markov Cricket" — Forward + inverse RL for ODI cricket. Outperforms DLS by 3-10x. [arXiv:2103.04349](https://arxiv.org/abs/2103.04349)

### Supporting
- **Asif & McHale (2016)** — "In-play forecasting of win probability in ODI cricket" — Dynamic logistic regression, validated against Betfair. [PDF](https://isidl.com/wp-content/uploads/2017/06/E4034-ISIDL.pdf)
- **Mysore et al. (2023)** — "DynaSim: Ball-by-Ball Simulation of IPL T20" — 57 outcomes per ball, 20 features. [Springer](https://link.springer.com/chapter/10.1007/978-981-99-2468-4_29)
- **Swartz et al. (2006)** — "Optimal batting orders in one-day cricket" — Ball-by-ball simulation with player-specific models. Computers & Operations Research.
- **Perera & Swartz (2013)** — "Resource Estimation in T20 Cricket" — DLS adaptation for T20.
- **Duckworth & Lewis (1998)** — "A fair method for resetting targets in one-day cricket" — Original DLS paper. JORS.
- **Higher-Order Markov T20 Chase (2025)** — Pressure Index model for T20. [arXiv:2505.01849](https://arxiv.org/abs/2505.01849)

### ML/Calibration
- **Grinsztajn et al. (2022)** — "Why do tree-based models still outperform deep learning on tabular data?" — NeurIPS. Justifies LightGBM over neural nets for this problem.
- **Bayesian Priors in Cricket Prediction** — [arXiv:2203.10706](https://arxiv.org/abs/2203.10706)

### RL (Evaluated and Rejected for This Use Case)
- **Stochastic MuZero (2022)** — [ICLR](https://openreview.net/forum?id=X6D9bAHhBQ1)
- **Multi-Agent DQN for Cricket (2026)** — [TechRxiv](https://www.techrxiv.org/users/898548/articles/1275062)
- **MONEYBaRL (2014)** — RL for baseball pitcher exploitation. [arXiv:1407.8392](https://arxiv.org/abs/1407.8392)

## Data Sources

### Training Data
- **Cricsheet.org** — Ball-by-ball JSON, all cricket formats. [Downloads](https://cricsheet.org/downloads/)
- **Cricsheet Register** — Player IDs → ESPNCricinfo mapping. [Register](https://cricsheet.org/register/)
- **python-espncricinfo** — Player metadata (bowling style, batting style). [GitHub](https://github.com/outside-edge/python-espncricinfo)

### Market Data
- **Captured Polymarket order books** — IPL 2026 matches in `captures/` directory

### Live Feeds
- Cricbuzz scraper (existing: `polymarket-simulator/cricket_score.py`)
- ESPN API (existing: `polymarket-simulator/combined_score.py`)

## Open Source References

### Cricket Prediction
- **dr00bot T20 Ball Simulation** — Neural net + MC, trained on 677K balls. [Blog](https://dr00bot.com/blog/t20-cricket-simulation-engine)
- **IPLSimulator** — Ball-by-ball IPL simulator. [GitHub](https://github.com/Aducj1910/IPLSimulator)
- **WASP Implementation** — DP-based win prediction. [GitHub](https://github.com/sodhanipranav/WASP)
- **IPL Win Probability (LightGBM)** — Walk-forward backtested. [GitHub](https://github.com/Goodest-ai/IPL-Win-Probability-Predictor)

### Trading Frameworks
- **flumine** — Betting framework with Polymarket + CricketMatch support. [GitHub](https://github.com/betcode-org/flumine)
- **betfairlightweight** — Betfair API wrapper. [GitHub](https://github.com/betcode-org/betfair)
- **Polymarket poly-market-maker** — Official MM reference. [GitHub](https://github.com/Polymarket/poly-market-maker)

### Stats/Analytics
- **cricketstats** — Cricsheet data analysis. [GitHub](https://github.com/nsaranga/cricketstats)
- **Cricmetric** — Head-to-head matchup tool. [Web](https://www.cricmetric.com/matchup.py)

### ML Tools
- **LightGBM** — Gradient boosting. [GitHub](https://github.com/microsoft/LightGBM)
- **ONNX Runtime** — Fast inference. [GitHub](https://github.com/microsoft/onnxruntime)

## Polymarket Infrastructure
- [CLOB Documentation](https://docs.polymarket.com/developers/CLOB/introduction)
- [Fees Documentation](https://docs.polymarket.com/trading/fees)
- [Liquidity Rewards](https://docs.polymarket.com/market-makers/liquidity-rewards)
- [Server Location (AWS eu-west-2)](https://newyorkcityservers.com/blog/polymarket-server-location-latency-guide)

## Industry Analysis
- [Opta Cricket Simulation Models](https://theanalyst.com/2022/06/introducing-cricket-simulation-models)
- [Opta Next Ball Predictor](https://theanalyst.com/articles/opta-next-ball-predictor)
- [Tim Swartz Paper Collection (SFU)](https://www.sfu.ca/~tswartz/papers/)
- [BetAngel Forum: Pitchsiding Discussion](https://forum.betangel.com/viewtopic.php?t=20424)
