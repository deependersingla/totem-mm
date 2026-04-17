"""LightGBM-based ball outcome prediction model."""

import numpy as np
import pandas as pd


OUTCOME_CLASSES = ["dot", "single", "double", "triple", "four", "six", "wicket", "wide", "noball"]
OUTCOME_TO_IDX = {o: i for i, o in enumerate(OUTCOME_CLASSES)}


def build_training_features(df: pd.DataFrame, batting_stats: pd.DataFrame, bowling_stats: pd.DataFrame) -> pd.DataFrame:
    """Build feature matrix for LightGBM training from deliveries + feature store."""
    features = df[["match_id", "match_date", "season", "innings", "over", "phase", "venue",
                    "batter", "bowler", "batting_team", "bowling_team",
                    "is_legal", "outcome_class",
                    "cumulative_runs", "cumulative_wickets", "cumulative_legal_balls",
                    "toss_decision", "batting_team_won"]].copy()

    # Match state features
    features["balls_bowled"] = features["cumulative_legal_balls"]
    features["balls_remaining"] = 120 - features["balls_bowled"]
    features["current_run_rate"] = np.where(
        features["balls_bowled"] > 0,
        features["cumulative_runs"] / (features["balls_bowled"] / 6),
        0,
    )

    # Phase encoding
    features["phase_num"] = features["phase"].map({"powerplay": 0, "middle": 1, "death": 2})

    # Innings number
    features["is_second_innings"] = (features["innings"] == 2).astype(int)

    # Merge batter stats (overall, not phase-specific for simplicity)
    batter_overall = batting_stats.groupby("batter").agg(
        batter_sr=("strike_rate", "mean"),
        batter_dot_pct=("dot_pct", "mean"),
        batter_boundary_pct=("boundary_pct", "mean"),
        batter_dismissal_rate=("dismissal_rate", "mean"),
        batter_balls_career=("balls_faced", "sum"),
    ).reset_index()

    features = features.merge(batter_overall, on="batter", how="left")

    # Merge batter phase-specific stats
    batter_phase = batting_stats[["batter", "phase", "strike_rate", "boundary_pct", "dismissal_rate"]].copy()
    batter_phase.columns = ["batter", "phase", "batter_phase_sr", "batter_phase_boundary_pct", "batter_phase_dismissal_rate"]
    features = features.merge(batter_phase, on=["batter", "phase"], how="left")

    # Merge bowler stats
    bowler_overall = bowling_stats.groupby("bowler").agg(
        bowler_economy=("economy", "mean"),
        bowler_dot_pct=("dot_pct", "mean"),
        bowler_boundary_rate=("boundary_concession_rate", "mean"),
        bowler_wpm=("wickets_per_match", "mean"),
        bowler_balls_career=("legal_balls", "sum"),
    ).reset_index()

    features = features.merge(bowler_overall, on="bowler", how="left")

    # Bowler phase-specific
    bowler_phase = bowling_stats[["bowler", "phase", "economy", "dot_pct"]].copy()
    bowler_phase.columns = ["bowler", "phase", "bowler_phase_economy", "bowler_phase_dot_pct"]
    features = features.merge(bowler_phase, on=["bowler", "phase"], how="left")

    # Fill NaN with median values
    numeric_cols = features.select_dtypes(include=[np.number]).columns
    features[numeric_cols] = features[numeric_cols].fillna(features[numeric_cols].median())

    # Target
    features["target"] = features["outcome_class"].map(OUTCOME_TO_IDX)

    # Drop rows with unmapped outcomes (e.g., "other_5" for 5 runs off bat)
    n_before = len(features)
    features = features.dropna(subset=["target"])
    features["target"] = features["target"].astype(int)
    n_dropped = n_before - len(features)
    if n_dropped > 0:
        print(f"  Dropped {n_dropped} rows with unmapped outcome classes")

    return features


def get_feature_columns() -> list[str]:
    """Feature columns for the model."""
    return [
        "phase_num",
        "balls_remaining",
        "cumulative_runs",
        "cumulative_wickets",
        "current_run_rate",
        "is_second_innings",
        "batter_sr",
        "batter_dot_pct",
        "batter_boundary_pct",
        "batter_dismissal_rate",
        "batter_balls_career",
        "batter_phase_sr",
        "batter_phase_boundary_pct",
        "batter_phase_dismissal_rate",
        "bowler_economy",
        "bowler_dot_pct",
        "bowler_boundary_rate",
        "bowler_wpm",
        "bowler_balls_career",
        "bowler_phase_economy",
        "bowler_phase_dot_pct",
    ]


def train_model(features: pd.DataFrame, test_season: str = None):
    """Train LightGBM multinomial classifier."""
    import lightgbm as lgb

    feature_cols = get_feature_columns()

    # Temporal split
    if test_season:
        train_mask = features["season"].astype(str) != str(test_season)
        test_mask = features["season"].astype(str) == str(test_season)
    else:
        # Use last 20% of data by date
        dates = features["match_date"].sort_values().unique()
        split_date = dates[int(len(dates) * 0.8)]
        train_mask = features["match_date"] < split_date
        test_mask = features["match_date"] >= split_date

    X_train = features.loc[train_mask, feature_cols].values
    y_train = features.loc[train_mask, "target"].values
    X_test = features.loc[test_mask, feature_cols].values
    y_test = features.loc[test_mask, "target"].values

    print(f"Train: {len(X_train)} samples, Test: {len(X_test)} samples")

    train_data = lgb.Dataset(X_train, label=y_train)
    test_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

    params = {
        "objective": "multiclass",
        "num_class": len(OUTCOME_CLASSES),
        "metric": "multi_logloss",
        "num_leaves": 63,
        "learning_rate": 0.03,
        "min_child_samples": 50,
        "feature_fraction": 0.7,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "reg_alpha": 0.1,
        "reg_lambda": 1.0,
        "verbose": -1,
    }

    callbacks = [
        lgb.log_evaluation(200),
        lgb.early_stopping(50),
    ]

    model = lgb.train(
        params,
        train_data,
        num_boost_round=1500,
        valid_sets=[test_data],
        callbacks=callbacks,
    )

    # Evaluate
    y_pred_proba = model.predict(X_test)
    y_pred = y_pred_proba.argmax(axis=1)
    accuracy = (y_pred == y_test).mean()

    from sklearn.metrics import log_loss
    logloss = log_loss(y_test, y_pred_proba, labels=list(range(len(OUTCOME_CLASSES))))

    print(f"\nTest accuracy: {accuracy:.4f}")
    print(f"Test log loss: {logloss:.4f}")

    # Per-class accuracy
    for i, cls in enumerate(OUTCOME_CLASSES):
        mask = y_test == i
        if mask.sum() > 0:
            cls_acc = (y_pred[mask] == i).mean()
            cls_count = mask.sum()
            pred_prob = y_pred_proba[mask, i].mean()
            actual_rate = mask.mean()
            print(f"  {cls:>8}: count={cls_count:>6}, acc={cls_acc:.3f}, pred_prob={pred_prob:.3f}, actual_rate={actual_rate:.3f}")

    return model, {
        "accuracy": accuracy,
        "log_loss": logloss,
        "train_size": len(X_train),
        "test_size": len(X_test),
        "feature_cols": feature_cols,
    }


def predict_transition_probs(model, features_row: dict) -> dict[str, float]:
    """Predict outcome probabilities for a single delivery context."""
    feature_cols = get_feature_columns()
    X = np.array([[features_row.get(c, 0) for c in feature_cols]])
    probs = model.predict(X)[0]
    return {cls: float(probs[i]) for i, cls in enumerate(OUTCOME_CLASSES)}
