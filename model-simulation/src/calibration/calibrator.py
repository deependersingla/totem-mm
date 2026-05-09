"""Probability calibration and validation metrics."""

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.isotonic import IsotonicRegression


class PhaseCalibrator:
    """Phase-specific isotonic regression calibrator."""

    def __init__(self):
        self.calibrators = {}  # phase -> IsotonicRegression
        self.global_calibrator = None

    def fit(self, y_true: np.ndarray, y_pred: np.ndarray, phases: np.ndarray = None):
        """Fit calibration models."""
        # Global calibrator
        self.global_calibrator = IsotonicRegression(y_min=0.001, y_max=0.999, out_of_bounds="clip")
        self.global_calibrator.fit(y_pred, y_true)

        # Phase-specific
        if phases is not None:
            for phase in np.unique(phases):
                mask = phases == phase
                if mask.sum() < 100:
                    continue
                cal = IsotonicRegression(y_min=0.001, y_max=0.999, out_of_bounds="clip")
                cal.fit(y_pred[mask], y_true[mask])
                self.calibrators[phase] = cal

    def transform(self, y_pred: float | np.ndarray, phase: str = None) -> float | np.ndarray:
        """Apply calibration."""
        if phase and phase in self.calibrators:
            return self.calibrators[phase].predict(np.atleast_1d(y_pred)).item() if np.isscalar(y_pred) else self.calibrators[phase].predict(y_pred)
        if self.global_calibrator:
            return self.global_calibrator.predict(np.atleast_1d(y_pred)).item() if np.isscalar(y_pred) else self.global_calibrator.predict(y_pred)
        return y_pred


def brier_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean squared error of predicted probabilities."""
    return float(np.mean((y_pred - y_true) ** 2))


def expected_calibration_error(y_true: np.ndarray, y_pred: np.ndarray, n_bins: int = 10) -> float:
    """Expected Calibration Error (ECE)."""
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    total = len(y_true)

    for i in range(n_bins):
        mask = (y_pred >= bin_edges[i]) & (y_pred < bin_edges[i + 1])
        if i == n_bins - 1:
            mask = (y_pred >= bin_edges[i]) & (y_pred <= bin_edges[i + 1])
        if mask.sum() == 0:
            continue
        bin_acc = y_true[mask].mean()
        bin_conf = y_pred[mask].mean()
        bin_size = mask.sum()
        ece += (bin_size / total) * abs(bin_acc - bin_conf)

    return float(ece)


def compute_all_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """Compute all calibration and accuracy metrics."""
    bs = brier_score(y_true, y_pred)
    ece = expected_calibration_error(y_true, y_pred)
    rmse = float(np.sqrt(np.mean((y_pred - y_true) ** 2)))
    correlation = float(np.corrcoef(y_pred, y_true)[0, 1]) if len(y_true) > 1 else 0.0
    mean_abs_error = float(np.mean(np.abs(y_pred - y_true)))

    return {
        "brier_score": bs,
        "ece": ece,
        "rmse": rmse,
        "correlation": correlation,
        "mean_abs_error": mean_abs_error,
        "n_samples": len(y_true),
    }
