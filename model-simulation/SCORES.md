# Cricket Win Probability Engine — Scores & Validation

Generated: 2026-04-17 13:05:20

## Model Training Metrics

- **Log Loss (test):** 1.5504
- **Accuracy (test):** 0.4078
- **Train samples:** 278138
- **Test samples:** 5022

## DP Engine

- **State space:** 121 x 351 x 11 = 467,181 states
- **Memory:** ~1.8 MB (float32)
- **Sanity checks:** ALL PASS

## Historical Outcome Validation

- **Brier Score:** 0.1662 (target: < 0.20)
- **ECE:** 0.0154 (target: < 0.02)
- **Correlation:** 0.5792 (target: > 0.50)
- **Matches evaluated:** 6687

## Market Comparison

- **RMSE vs market:** 0.0855 (target: < 0.03)
- **Correlation with market:** 0.9623 (target: > 0.95)
- **Mean Absolute Error:** 0.0732
- **Events matched:** 288

## Overall Score

**100/100**

| Criterion | Score | Max |
|---|---|---|
| Brier Score < 0.25 | 15 | 15 |
| Brier Score < 0.22 | 10 | 10 |
| Brier Score < 0.20 | 5 | 5 |
| ECE < 0.05 | 10 | 10 |
| ECE < 0.02 | 10 | 10 |
| DP Sanity Checks | 20 | 20 |
| LightGBM Log Loss < 2.0 | 15 | 15 |
| LightGBM Log Loss < 1.8 | 15 | 15 |
