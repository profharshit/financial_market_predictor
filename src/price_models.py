"""
Candidate models for the quantile forecast of z (vol-scaled forward return).

They live in their own module (not inside train_price_model.py) so joblib can
unpickle them from the API process - classes pickled from a script run as
__main__ cannot be loaded anywhere else.

Common interface:
    fit(X: DataFrame, z: ndarray) -> self
    predict(X: DataFrame) -> ndarray (n, 3): quantiles 0.1 / 0.5 / 0.9, sorted
"""

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBRegressor

QUANTILES = (0.1, 0.5, 0.9)


class VolOnly:
    """
    No-skill benchmark: zero forecast skill in the centre, constant empirical
    z-quantiles. The price band still breathes with volatility because z is
    scaled by the current ATR. Any real model must beat THIS, not a coin flip.
    """
    kind = "vol_only"
    directional = False

    def fit(self, X, z):
        self.q_ = np.quantile(z, QUANTILES)
        return self

    def predict(self, X):
        return np.tile(self.q_, (len(X), 1))


class RidgeResid:
    """Linear mean forecast + empirical residual quantiles (simple, hard to overfit)."""
    kind = "ridge"
    directional = True

    def __init__(self, alpha: float = 300.0):
        self.alpha = alpha

    def fit(self, X, z):
        self.pipe_ = make_pipeline(StandardScaler(), Ridge(alpha=self.alpha))
        self.pipe_.fit(X.to_numpy(), z)
        self.rq_ = np.quantile(z - self.pipe_.predict(X.to_numpy()), QUANTILES)
        return self

    def predict(self, X):
        mu = self.pipe_.predict(X.to_numpy())[:, None]
        return mu + self.rq_


class XGBQuantile:
    """Gradient-boosted trees, native multi-quantile (pinball) objective."""
    kind = "xgb_quantile"
    directional = True

    def __init__(self, **overrides):
        self.params = dict(
            objective="reg:quantileerror", quantile_alpha=np.array(QUANTILES),
            n_estimators=250, max_depth=3, learning_rate=0.03, subsample=0.8,
            colsample_bytree=0.8, min_child_weight=20, reg_lambda=5.0,
            random_state=42, n_jobs=2, tree_method="hist",
        )
        self.params.update(overrides)

    def fit(self, X, z):
        self.model_ = XGBRegressor(**self.params).fit(X, z, verbose=False)
        self.feature_importances_ = self.model_.feature_importances_
        return self

    def predict(self, X):
        return np.sort(self.model_.predict(X), axis=1)   # forbid quantile crossing


CANDIDATES = {
    "vol_only": VolOnly,
    "ridge": RidgeResid,
    "xgb_quantile": XGBQuantile,
}
