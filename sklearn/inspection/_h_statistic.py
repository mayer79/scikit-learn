"""Friedman and Popescu's H-Statistic"""

import itertools

import numpy as np
from scipy import sparse

from ..base import is_classifier, is_regressor
from ..utils import (
    Bunch,
    _get_column_indices,
    _safe_assign,
    _safe_indexing,
    check_array,
)
from ..utils._param_validation import (
    HasMethods,
    Integral,
    Interval,
    Real,
    validate_params,
)
from ..utils.random import sample_without_replacement
from ..utils.validation import _check_sample_weight, check_is_fitted


def _calculate_pd_brute_fast(estimator, X, feature_indices, grid, sample_weight=None):
    """Fast version of _calculate_partial_dependence_brute()

    Returns np.array of size (n_grid, ) or (n_ngrid, output_dim).
    """

    if is_regressor(estimator):
        if hasattr(estimator, "predict"):
            pred_fun = estimator.predict
        else:
            raise ValueError("The regressor has no predict() method.")
    elif is_classifier(estimator):
        if hasattr(estimator, "predict_proba"):
            pred_fun = estimator.predict_proba
        else:
            raise ValueError("The classifier has no predict_proba() method.")

    # X is stacked n_grid times, and grid columns are replaced by replicated grid
    n = X.shape[0]
    n_grid = grid.shape[0]
    X_eval = X.copy()

    X_stacked = _safe_indexing(X_eval, np.tile(np.arange(n), n_grid), axis=0)
    grid_stacked = _safe_indexing(grid, np.repeat(np.arange(n_grid), n), axis=0)
    _safe_assign(X_stacked, values=grid_stacked, column_indexer=feature_indices)

    # Predict on stacked data. Pick positive class probs for binary classification
    preds = pred_fun(X_stacked)
    if is_classifier(estimator) and preds.shape[1] == 2:
        preds = preds[:, 1]

    # Partial dependences are averages per grid block
    pd_values = [
        np.average(Z, axis=0, weights=sample_weight) for Z in np.split(preds, n_grid)
    ]

    return np.array(pd_values)


def _calculate_pd_over_data(estimator, X, feature_indices, sample_weight=None):
    """Calculates centered partial dependence over the data distribution.

    It returns a numpy array of size (n, ) or (n, output_dim).
    """

    # Select grid columns and remove duplicates (will compensate below)
    grid = _safe_indexing(X, feature_indices, axis=1)

    # np.unique() fails for mixed type and sparse objects
    try:
        ax = 0 if grid.shape[1] > 1 else None  # np.unique works better in 1 dim
        _, ix, ix_reconstruct = np.unique(
            grid, return_index=True, return_inverse=True, axis=ax
        )
        grid = _safe_indexing(grid, ix, axis=0)
        compressed_grid = True
    except (TypeError, np.AxisError):
        compressed_grid = False

    pd_values = _calculate_pd_brute_fast(
        estimator,
        X=X,
        feature_indices=feature_indices,
        grid=grid,
        sample_weight=sample_weight,
    )

    if compressed_grid:
        pd_values = pd_values[ix_reconstruct]

    # H-statistics are based on *centered* partial dependences
    column_means = np.average(pd_values, axis=0, weights=sample_weight)

    return pd_values - column_means


@validate_params(
    {
        "estimator": [
            HasMethods(["fit", "predict"]),
            HasMethods(["fit", "predict_proba"]),
        ],
        "X": ["array-like", "sparse matrix"],
        "features": ["array-like", list, None],
        "sample_weight": ["array-like", None],
        "n_max": [Interval(Integral, 1, None, closed="left")],
        "random_state": ["random_state"],
        "eps": [Interval(Real, 0, None, closed="left")],
    },
    prefer_skip_nested_validation=True,
)
def h_statistic(
    estimator,
    X,
    features=None,
    *,
    sample_weight=None,
    n_max=500,
    random_state=None,
    eps=1e-10,
):
    """Friedman and Popescu's H-statistic of pairwise interaction strength.

    Calculates Friedman and Popescu's H-statistic of interaction strength
    for each feature pair j, k, see [FRI]_. The statistic is defined as::

        H_jk^2 = Numerator_jk / Denominator_jk, where

        - Numerator_jk = 1/n * sum(PD_{jk}(x_ij, x_ik) - PD_j(x_ij) - PD_k(x_ik)^2,
        - Denominator_jk = 1/n * sum(PD_{jk}(x_ij, x_ik)^2),
        - PD_j and PD_jk are the one- and two-dimensional partial dependence
          functions centered to mean 0,
        - and the sums run over 1 <= i <= n, where n is the sample size.

    It equals the proportion of effect variability between two features that cannot
    be explained by their main effects. When there is no interaction, the value is
    exactly 0. The numerator (or its square root) provides an absolute measure
    of interaction strength, enabling direct comparison across feature pairs.

    The computational complexity of the function is `O(p^2 n^2)`,
    where `p` denotes the number of features considered. The size of `n` is
    automatically controlled via `n_max=500`, while it is the user's responsibility
    to select only a subset of *important* features. It is crucial to focus on important
    features because for weak predictors, the denominator might be small, and
    even a weak interaction could result in a high Friedman's H, sometimes exceeding 1.

    Parameters
    ----------
    estimator : object
        An estimator that has already been :term:`fitted`.

    X : {array-like or dataframe} of shape (n_samples, n_features)
        Data for which :term:`estimator` is able to calculate predictions.

    features : array-like of {int, str}, default=None
        List of feature names or column indices used to calculate pairwise statistics.
        The default, None, will use all column indices of X.

    sample_weight : array-like of shape (n_samples,), default=None
        Sample weights used in calculating partial dependencies.

    n_max : int, default=500
        The number of rows to draw without replacement from X (and `sample_weight`).

    random_state : int, RandomState instance, default=None
        Pseudo-random number generator used for subsampling via `n_max`.
        See :term:`Glossary <random_state>`.

    eps : float, default=1e-10
        Threshold below which numerator values are set to 0.

    Returns
    -------
    result : :class:`~sklearn.utils.Bunch`
        Dictionary-like object, with the following attributes.

        feature_pairs : list of length n_feature_pairs
            The list contains tuples of feature pairs (indices) in the same order
            as all pairwise statistics.

        h_squared_pairwise : ndarray of shape (n_pairs, ) or (n_pairs, output_dim)
            Pairwise H-squared statistic. Useful to see which feature pair has
            strongest relative interation (relative with respect to joint effect).
            Calculated as numerator_pairwise / denominator_pairwise.

        numerator_pairwise : ndarray of shape (n_pairs, ) or (n_pairs, output_dim)
            Numerator of pairwise H-squared statistic.
            Useful to see which feature pair has strongest absolute interaction.
            Take square-root to get values on the scale of the predictions.

        denominator_pairwise : ndarray of shape (n_pairs, ) or (n_pairs, output_dim)
            Denominator of pairwise H-squared statistic. Used for appropriate
            normalization of H.

    References
    ----------
    .. [FRI] :doi:`J. H. Friedman and B. E. Popescu,
            "Predictive Learning via Rule Ensembles",
            The Annals of Applied Statistics, 2(3), 916-954,
            2008. <10.1214/07-AOAS148>`

    Examples
    --------
    >>> import numpy as np
    >>> from sklearn.ensemble import HistGradientBoostingRegressor
    >>> from sklearn.inspection import permutation_importance, h_statistic
    >>> from sklearn.datasets import load_diabetes
    >>>
    >>> X, y = load_diabetes(return_X_y=True)
    >>> est = HistGradientBoostingRegressor(max_iter=5, max_depth=2).fit(X, y)
    >>>
    >>> # Get Friedman's H-squared for top m=3 predictors
    >>> m = 3
    >>> imp = permutation_importance(est, X, y, random_state=0)
    >>> top_m = np.argsort(imp.importances_mean)[-m:]
    >>> h_statistic(est, X=X, features=top_m, random_state=4)

    >>> # For features (8, 2), 3.4% of the joint effect variability comes from
    >>> # their interaction. These two features also have strongest absolute
    >>> # interaction, see "numerator_pairwise":
    >>> # {'feature_pairs': [(3, 8), (3, 2), (8, 2)],
    >>> # 'h_squared_pairwise': array([0.00985985, 0.00927104, 0.03439926]),
    >>> # 'numerator_pairwise': array([ 1.2955532 ,  1.2419687 , 11.13358385]),
    >>> # 'denominator_pairwise': array([131.39690331, 133.96210997, 323.6576595 ])}

    """
    check_is_fitted(estimator)

    if not (is_classifier(estimator) or is_regressor(estimator)):
        raise ValueError("'estimator' must be a fitted regressor or classifier.")

    if is_classifier(estimator) and isinstance(estimator.classes_[0], np.ndarray):
        raise ValueError("Multiclass-multioutput estimators are not supported")

    # Use check_array only on lists and other non-array-likes / sparse. Do not
    # convert DataFrame into a NumPy array.
    if not (hasattr(X, "__array__") or sparse.issparse(X)):
        X = check_array(X, force_all_finite="allow-nan", dtype=object)

    if sample_weight is not None:
        sample_weight = _check_sample_weight(sample_weight, X)

    # Usually, the data is too large and we need subsampling
    if X.shape[0] > n_max:
        row_indices = sample_without_replacement(
            n_population=X.shape[0], n_samples=n_max, random_state=random_state
        )
        X = _safe_indexing(X, row_indices, axis=0)
        if sample_weight is not None:
            sample_weight = _safe_indexing(sample_weight, row_indices, axis=0)
    else:
        X = X.copy()

    if features is None:
        features = feature_indices = np.arange(X.shape[1])
    else:
        feature_indices = np.asarray(
            _get_column_indices(X, features), dtype=np.intp, order="C"
        ).ravel()

    # CALCULATIONS
    pd_univariate = []
    for idx in feature_indices:
        pd_univariate.append(
            _calculate_pd_over_data(
                estimator, X=X, feature_indices=[idx], sample_weight=sample_weight
            )
        )

    num = []
    denom = []

    for j, k in itertools.combinations(range(len(feature_indices)), 2):
        pd_bivariate = _calculate_pd_over_data(
            estimator,
            X=X,
            feature_indices=feature_indices[[j, k]],
            sample_weight=sample_weight,
        )
        num.append(
            np.average(
                (pd_bivariate - pd_univariate[j] - pd_univariate[k]) ** 2,
                axis=0,
                weights=sample_weight,
            )
        )
        denom.append(np.average(pd_bivariate**2, axis=0, weights=sample_weight))

    num = np.array(num)
    num[np.abs(num) < eps] = 0  # Round small numerators to 0
    denom = np.array(denom)
    h2_stat = np.divide(num, denom, out=np.zeros_like(num), where=denom > 0)

    return Bunch(
        feature_pairs=list(itertools.combinations(features, 2)),
        h_squared_pairwise=h2_stat,
        numerator_pairwise=num,
        denominator_pairwise=denom,
    )
