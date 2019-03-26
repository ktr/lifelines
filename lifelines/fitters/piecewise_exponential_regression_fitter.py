# -*- coding: utf-8 -*-
from textwrap import dedent
import warnings
from datetime import datetime
from autograd import numpy as np
from scipy.special import gamma
import pandas as pd
from autograd import hessian, value_and_grad, elementwise_grad as egrad, grad
from lifelines.utils import _get_index, coalesce, qth_survival_times
from lifelines.fitters import BaseFitter
from lifelines.plotting import _plot_estimate, set_kwargs_drawstyle, set_kwargs_ax

from lifelines.utils import (
    qth_survival_times,
    _to_array,
    _to_list,
    dataframe_interpolate_at_times,
    ConvergenceError,
    inv_normal_cdf,
    string_justify,
    format_floats,
    format_p_value,
    format_exp_floats,
    coalesce,
    check_nans_or_infs,
    pass_for_numeric_dtypes_or_raise_array,
    check_for_numeric_dtypes_or_raise,
    check_complete_separation,
    check_low_var,
    StatisticalWarning,
    StatError,
    median_survival_times,
    normalize,
    concordance_index,
)
from autograd.differential_operators import make_jvp_reversemode
from scipy.optimize import minimize
from scipy import stats
import pandas as pd
from numpy.linalg import inv, pinv


class PiecewiseExponentialRegressionFitter(BaseFitter):
    r"""
    This class implements an Piecewise Exponential model for univariate data. The model has parameterized
    hazard rate:

    .. math::  h(t\;|\;x) = \begin{cases}
                        1/\lambda_0(x),  & \text{if $t \le \tau_0$} \\
                        1/\lambda_1(x) & \text{if $\tau_0 < t \le \tau_1$} \\
                        1/\lambda_2(x) & \text{if $\tau_1 < t \le \tau_2$} \\
                        ...
                      \end{cases}

    You specify the breakpoints, :math:`\tau_i`, and *lifelines* will find the
    optional values for the parameters.

    After calling the `.fit` method, you have access to properties like: ``params_``
    A summary of the fit is available with the method ``print_summary()``


    Attributes
    ----------
    cumulative_hazard_ : DataFrame
        The estimated cumulative hazard (with custom timeline if provided)
    confidence_interval_cumulative_hazard_ : DataFrame
        The lower and upper confidence intervals for the cumulative hazard
    hazard_ : DataFrame
        The estimated hazard (with custom timeline if provided)
    confidence_interval_hazard_ : DataFrame
        The lower and upper confidence intervals for the hazard
    survival_function_ : DataFrame
        The estimated survival function (with custom timeline if provided)
    confidence_interval_survival_function_ : DataFrame
        The lower and upper confidence intervals for the survival function
    cumumlative_density_ : DataFrame
        The estimated cumulative density function (with custom timeline if provided)
    confidence_interval_cumumlative_density_ : DataFrame
        The lower and upper confidence intervals for the cumulative density
    variance_matrix_ : numpy array
        The variance matrix of the coefficients
    median_: float
        The median time to event
    lambda_i_: float
        The fitted parameter in the model, for i = 0, 1 ... n-1 breakpoints
    durations: array
        The durations provided
    event_observed: array
        The event_observed variable provided
    timeline: array
        The time line to use for plotting and indexing
    entry: array or None
        The entry array provided, or None
    breakpoints: array
        The provided breakpoints

    """

    def __init__(self, breakpoints, alpha=0.05, penalizer=0.0, fit_intercept=True, *args, **kwargs):
        super(PiecewiseExponentialRegressionFitter, self).__init__(alpha=alpha)

        breakpoints = np.sort(breakpoints)
        if len(breakpoints) > 0 and not (breakpoints[-1] < np.inf):
            raise ValueError("Do not add inf to the breakpoints.")

        if len(breakpoints) > 0 and breakpoints[0] < 0:
            raise ValueError("First breakpoint must be greater than 0.")

        self.breakpoints = np.append(breakpoints, [np.inf])
        self.n_breakpoints = len(self.breakpoints)

        self._hazard = egrad(self._cumulative_hazard, argnum=1)  # pylint: disable=unexpected-keyword-arg
        self.penalizer = penalizer
        self.fit_intercept = fit_intercept
        self._fitted_parameter_names = ["lambda_%d_" % i for i in range(self.n_breakpoints)]

    def _cumulative_hazard(self, params, T, X):
        n = T.shape[0]
        T = T.reshape((n, 1))
        bp = self.breakpoints
        M = np.minimum(np.tile(bp, (n, 1)), T)
        M = np.hstack([M[:, tuple([0])], np.diff(M, axis=1)])
        lambdas_ = np.array(
            [np.exp(-np.dot(X, params[self._LOOKUP_SLICE["lambda_%d_" % i]])) for i in range(self.n_breakpoints)]
        )
        return M * lambdas_.T

    def _log_hazard(self, params, T, X):
        # can be overwritten to improve convergence, see WeibullAFTFitter
        hz = self._hazard(params, T, X)
        hz = np.clip(hz, 1e-20, np.inf)
        return np.log(hz)

    def _negative_log_likelihood(self, params, T, E, W, X):
        warnings.simplefilter(action="ignore", category=FutureWarning)
        ll = (W[E] * self._log_hazard(params, T[E], X[E, :])).sum() - (
            W[:, None] * self._cumulative_hazard(params, T, X)
        ).sum()
        if self.penalizer > 0:
            coef_penalty = 0
            for i in range(X.shape[1]):
                coef_penalty = coef_penalty + (params[i :: X.shape[1]]).var()
        else:
            coef_penalty = 0

        ll = ll / np.sum(W)
        return -ll + self.penalizer * coef_penalty

    def fit(
        self,
        df,
        duration_col=None,
        event_col=None,
        show_progress=False,
        timeline=None,
        weights_col=None,
        robust=False,
        initial_point=None,
    ):
        """
        Fit the accelerated failure time model to a dataset.

        Parameters
        ----------
        df: DataFrame
            a Pandas DataFrame with necessary columns `duration_col` and
            `event_col` (see below), covariates columns, and special columns (weights).
            `duration_col` refers to
            the lifetimes of the subjects. `event_col` refers to whether
            the 'death' events was observed: 1 if observed, 0 else (censored).

        duration_col: string
            the name of the column in DataFrame that contains the subjects'
            lifetimes.

        event_col: string, optional
            the  name of the column in DataFrame that contains the subjects' death
            observation. If left as None, assume all individuals are uncensored.

        show_progress: boolean, optional (default=False)
            since the fitter is iterative, show convergence
            diagnostics. Useful if convergence is failing.

        timeline: array, optional
            Specify a timeline that will be used for plotting and prediction

        weights_col: string
            the column in df that specifies weights per observation.

        robust: boolean, optional (default=False)
            Compute the robust errors using the Huber sandwich estimator.

        initial_point: (d,) numpy array, optional
            initialize the starting point of the iterative
            algorithm. Default is the zero vector.

        Returns
        -------
        self:
            self with additional new properties: ``print_summary``, ``params_``, ``confidence_intervals_`` and more


        Examples
        --------
        TODO
        >>> from lifelines import WeibullAFTFitter
        >>>
        >>> df = pd.DataFrame({
        >>>     'T': [5, 3, 9, 8, 7, 4, 4, 3, 2, 5, 6, 7],
        >>>     'E': [1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 0],
        >>>     'var': [0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2],
        >>>     'age': [4, 3, 9, 8, 7, 4, 4, 3, 2, 5, 6, 7],
        >>> })
        >>>
        >>> aft = WeibullAFTFitter()
        >>> aft.fit(df, 'T', 'E')
        >>> aft.print_summary()
        >>> aft.predict_median(df)
        >>>
        >>> aft = WeibullAFTFitter()
        >>> aft.fit(df, 'T', 'E', ancillary_df=df)
        >>> aft.print_summary()
        >>> aft.predict_median(df)

        """
        if duration_col is None:
            raise TypeError("duration_col cannot be None.")

        self._time_fit_was_called = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC"
        self.duration_col = duration_col
        self.event_col = event_col
        self.weights_col = weights_col
        self._n_examples = df.shape[0]
        self.timeline = timeline
        self.robust = robust

        df = df.copy()

        T = pass_for_numeric_dtypes_or_raise_array(df.pop(duration_col)).astype(float)
        E = (
            pass_for_numeric_dtypes_or_raise_array(df.pop(self.event_col)).astype(bool)
            if (self.event_col is not None)
            else pd.Series(np.ones(self._n_examples, dtype=bool), index=df.index, name="E")
        )
        weights = (
            pass_for_numeric_dtypes_or_raise_array(df.pop(self.weights_col)).astype(float)
            if (self.weights_col is not None)
            else pd.Series(np.ones(self._n_examples, dtype=float), index=df.index, name="weights")
        )
        # check to make sure their weights are okay
        if self.weights_col:
            if (weights.astype(int) != weights).any() and not self.robust:
                warnings.warn(
                    dedent(
                        """It appears your weights are not integers, possibly propensity or sampling scores then?
                                        It's important to know that the naive variance estimates of the coefficients are biased. Instead a) set `robust=True` in the call to `fit`, or b) use Monte Carlo to
                                        estimate the variances. See paper "Variance estimation when using inverse probability of treatment weighting (IPTW) with survival analysis"""
                    ),
                    StatisticalWarning,
                )
            if (weights <= 0).any():
                raise ValueError("values in weight column %s must be positive." % self.weights_col)

        self.durations = T.copy()
        self.event_observed = E.copy()
        self.weights = weights.copy()

        if np.any(self.durations <= 0):
            raise ValueError(
                "This model does not allow for non-positive durations. Suggestion: add a small positive value to zero elements."
            )

        df = df.astype(float)
        self._check_values(df, T, E, self.event_col)

        if self.fit_intercept:
            assert "_intercept" not in df
            df["_intercept"] = 1.0

        self._LOOKUP_SLICE = self._create_slicer(len(df.columns))  # TODO

        _norm_std = df.std(0)
        self._norm_mean = df.mean(0)

        # if we included an intercept, we need to fix not divide by zero.
        if self.fit_intercept:
            _norm_std["_intercept"] = 1.0
        else:
            _norm_std[_norm_std < 1e-8] = 1.0

        _index = pd.MultiIndex.from_tuples(
            sum([[(name, c) for c in df.columns] for name in self._fitted_parameter_names], [])
        )

        self._norm_std = pd.Series(np.concatenate([_norm_std.values] * self.n_breakpoints), index=_index)

        _params, self._log_likelihood, self._hessian_ = self._fit_model(
            T.values,
            E.values,
            weights.values,
            normalize(df, 0, _norm_std).values,
            show_progress=show_progress,
            initial_point=initial_point,
        )
        self.params_ = _params / self._norm_std

        self.variance_matrix_ = self._compute_variance_matrix()
        self.standard_errors_ = self._compute_standard_errors(T.values, E.values, weights.values, df.values)
        self.confidence_intervals_ = self._compute_confidence_intervals()
        self._predicted_cumulative_hazard_ = self.predict_cumulative_hazard(df, times=[np.percentile(T, 75)]).T

        return self

    def _check_values(self, df, T, E, event_col):
        check_for_numeric_dtypes_or_raise(df)
        check_nans_or_infs(T)
        check_nans_or_infs(E)
        check_nans_or_infs(df)
        check_complete_separation(df, E, T, event_col)

        if self.fit_intercept:
            check_low_var(df)

    def _fit_model(self, T, E, weights, X, show_progress=False, initial_point=None):

        if initial_point is None:
            initial_point = np.zeros((X.shape[1] * self.n_breakpoints))

        results = minimize(
            # using value_and_grad is much faster (takes advantage of shared computations) than spitting.
            value_and_grad(self._negative_log_likelihood),
            initial_point,
            method=None,
            jac=True,
            args=(T, E, weights, X),
            options={"disp": show_progress},
        )
        if show_progress or not results.success:
            print(results)

        if results.success:
            sum_weights = weights.sum()
            # pylint: disable=no-value-for-parameter
            hessian_ = hessian(self._negative_log_likelihood)(results.x, T, E, weights, X)
            return results.x, -sum_weights * results.fun, sum_weights * hessian_

        name = self.__class__.__name__
        raise ConvergenceError(
            dedent(
                """\
            Fitting did not converge. This could be a problem with your data:
            1. Does a column have extremely high mean or variance? Try standardizing it.
            2. Are there any extreme outliers? Try modeling them or dropping them to see if it helps convergence
            3. Trying adding a small penalizer (or changing it, if already present). Example: `%s(penalizer=0.01).fit(...)`
        """
                % name
            )
        )

    def _create_slicer(self, size_of_X):
        lookup = {}
        position = 0

        for name in self._fitted_parameter_names:
            lookup[name] = slice(position, position + size_of_X)
            position += size_of_X

        return lookup

    def _compute_variance_matrix(self):
        try:
            unit_scaled_variance_matrix_ = np.linalg.inv(self._hessian_)
        except np.linalg.LinAlgError:
            unit_scaled_variance_matrix_ = np.linalg.pinv(self._hessian_)
            warning_text = dedent(
                """\
                The hessian was not invertable. We will instead approximate it using the psuedo-inverse.

                It's advisable to not trust the variances reported, and to be suspicious of the
                fitted parameters too. Perform plots of the cumulative hazard to help understand
                the latter's bias.
                """
            )
            warnings.warn(warning_text, StatisticalWarning)

        return unit_scaled_variance_matrix_ / np.outer(self._norm_std, self._norm_std)

    def _compute_z_values(self):
        return self.params_ / self.standard_errors_

    def _compute_p_values(self):
        U = self._compute_z_values() ** 2
        return stats.chi2.sf(U, 1)

    def _compute_standard_errors(self, T, E, weights, X):
        if self.robust:
            se = np.sqrt(self._compute_sandwich_errors(T, E, weights, X).diagonal())
        else:
            se = np.sqrt(self.variance_matrix_.diagonal())
        return pd.Series(se, name="se", index=self.params_.index)

    def _compute_sandwich_errors(self, T, E, weights, X):
        with np.errstate(all="ignore"):
            # convergence will fail catastrophically elsewhere.

            ll_gradient = grad(self._negative_log_likelihood)
            params = self.params_.values
            n_params = params.shape[0]
            J = np.zeros((n_params, n_params))

            for t, e, w, x in zip(T, E, weights, X):
                score_vector = ll_gradient(params, t, e, w, x)
                J += np.outer(score_vector, score_vector)

            return self.variance_matrix_ @ J @ self.variance_matrix_

    def _compute_confidence_intervals(self):
        z = inv_normal_cdf(1 - self.alpha / 2)
        se = self.standard_errors_
        params = self.params_.values
        return pd.DataFrame(
            np.c_[params - z * se, params + z * se], index=self.params_.index, columns=["lower-bound", "upper-bound"]
        )

    @property
    def _ll_null(self):
        if hasattr(self, "_ll_null_"):
            return self._ll_null_

        initial_point = np.zeros(len(self._fitted_parameter_names))
        self._ll_null_ = (
            self.__class__(breakpoints=self.breakpoints[:-1], penalizer=self.penalizer, fit_intercept=True)
            .fit(pd.DataFrame({"T": self.durations, "E": self.event_observed}), "T", "E", initial_point=initial_point)
            ._log_likelihood
        )
        return self._ll_null_

    def _compute_likelihood_ratio_test(self):
        """
        This function computes the likelihood ratio test for the model. We
        compare the existing model (with all the covariates) to the trivial model
        of no covariates.

        """
        from lifelines.statistics import chisq_test

        ll_null = self._ll_null
        ll_alt = self._log_likelihood

        test_stat = 2 * ll_alt - 2 * ll_null
        degrees_freedom = self.params_.shape[0] - 2  # delta in number of parameters between models
        p_value = chisq_test(test_stat, degrees_freedom=degrees_freedom)
        with np.errstate(invalid="ignore", divide="ignore"):
            return test_stat, degrees_freedom, -np.log2(p_value)

    @property
    def summary(self):
        """Summary statistics describing the fit.

        Returns
        -------
        df : DataFrame
            Contains columns coef, np.exp(coef), se(coef), z, p, lower, upper"""
        ci = 1 - self.alpha
        with np.errstate(invalid="ignore", divide="ignore"):
            df = pd.DataFrame(index=self.params_.index)
            df["coef"] = self.params_
            df["exp(coef)"] = np.exp(self.params_)
            df["se(coef)"] = self.standard_errors_
            df["z"] = self._compute_z_values()
            df["p"] = self._compute_p_values()
            df["-log2(p)"] = -np.log2(df["p"])
            df["lower %g" % ci] = self.confidence_intervals_["lower-bound"]
            df["upper %g" % ci] = self.confidence_intervals_["upper-bound"]
            return df

    def print_summary(self, decimals=2, **kwargs):
        """
        Print summary statistics describing the fit, the coefficients, and the error bounds.

        Parameters
        -----------
        decimals: int, optional (default=2)
            specify the number of decimal places to show
        alpha: float or iterable
            specify confidence intervals to show
        kwargs:
            print additional metadata in the output (useful to provide model names, dataset names, etc.) when comparing
            multiple outputs.

        """

        # Print information about data first
        justify = string_justify(18)
        print(self)
        print("{} = '{}'".format(justify("duration col"), self.duration_col))
        if self.event_col:
            print("{} = '{}'".format(justify("event col"), self.event_col))
        if self.weights_col:
            print("{} = '{}'".format(justify("weights col"), self.weights_col))
        if self.penalizer > 0:
            print("{} = {}".format(justify("penalizer"), self.penalizer))

        if self.robust:
            print("{} = {}".format(justify("robust variance"), True))

        print("{} = {}".format(justify("number of subjects"), self._n_examples))
        print("{} = {}".format(justify("number of events"), self.event_observed.sum()))
        print("{} = {:.{prec}f}".format(justify("log-likelihood"), self._log_likelihood, prec=decimals))
        print("{} = {}".format(justify("time fit was run"), self._time_fit_was_called))

        for k, v in kwargs.items():
            print("{} = {}\n".format(justify(k), v))

        print(end="\n")
        print("---")

        df = self.summary
        # Significance codes as last column
        print(
            df.to_string(
                float_format=format_floats(decimals),
                formatters={"p": format_p_value(decimals), "exp(coef)": format_exp_floats(decimals)},
            )
        )

        # Significance code explanation
        print("---")
        print("Concordance = {:.{prec}f}".format(self.score_, prec=decimals))
        print(
            "Log-likelihood ratio test = {:.{prec}f} on {} df, -log2(p)={:.{prec}f}".format(
                *self._compute_likelihood_ratio_test(), prec=decimals
            )
        )

    def predict_survival_function(self, X, times=None, ancillary_X=None):
        """
        Predict the survival function for individuals, given their covariates. This assumes that the individual
        just entered the study (that is, we do not condition on how long they have already lived for.)

        Parameters
        ----------

        X: numpy array or DataFrame
            a (n,d) covariate numpy array or DataFrame. If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.
        ancillary_X: numpy array or DataFrame, optional
            a (n,d) covariate numpy array or DataFrame. If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.
        times: iterable, optional
            an iterable of increasing times to predict the cumulative hazard at. Default
            is the set of all durations (observed and unobserved). Uses a linear interpolation if
            points in time are not in the index.


        Returns
        -------
        survival_function : DataFrame
            the survival probabilities of individuals over the timeline
        """
        return np.exp(-self.predict_cumulative_hazard(X, times=times))

    def predict_median(self, X):
        """
        Predict the median lifetimes for the individuals. If the survival curve of an
        individual does not cross 0.5, then the result is infinity.

        Parameters
        ----------
        X: numpy array or DataFrame
            a (n,d) covariate numpy array or DataFrame. If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.


        Returns
        -------
        percentiles: DataFrame
            the median lifetimes for the individuals. If the survival curve of an
            individual does not cross 0.5, then the result is infinity.


        See Also
        --------
        predict_percentile, predict_expectation

        """
        return self.predict_percentile(X, p=0.5)

    def predict_percentile(self, X, p=0.5):
        """
        Returns the median lifetimes for the individuals, by default. If the survival curve of an
        individual does not cross 0.5 in the timeline (set in ``fit``), then the result is infinity.
        http://stats.stackexchange.com/questions/102986/percentile-loss-functions

        Parameters
        ----------
        X:  numpy array or DataFrame
            a (n,d) covariate numpy array or DataFrame. If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.
        p: float, optional (default=0.5)
            the percentile, must be between 0 and 1.

        Returns
        -------
        percentiles: DataFrame

        See Also
        --------
        predict_median

        """
        subjects = _get_index(X)
        return qth_survival_times(p, self.predict_survival_function(X)[subjects]).T

    def predict_cumulative_hazard(self, X, times=None):
        """
        Return the cumulative hazard rate of subjects in X at time points.

        Parameters
        ----------
        X: numpy array or DataFrame
            a (n,d) covariate numpy array or DataFrame. If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.
        times: iterable, optional
            an iterable of increasing times to predict the cumulative hazard at. Default
            is the set of all durations (observed and unobserved). Uses a linear interpolation if
            points in time are not in the index.
        ancillary_X: numpy array or DataFrame, optional
            a (n,d) covariate numpy array or DataFrame. If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.

        Returns
        -------
        cumulative_hazard_ : DataFrame
            the cumulative hazard of individuals over the timeline
        """
        times = np.asarray(coalesce(times, self.timeline, np.unique(self.durations)))
        n = times.shape[0]
        times = times.reshape((n, 1))

        lambdas_ = self._prep_inputs_for_prediction_and_return_parameters(X)

        bp = self.breakpoints
        M = np.minimum(np.tile(bp, (n, 1)), times)
        M = np.hstack([M[:, tuple([0])], np.diff(M, axis=1)])

        return pd.DataFrame(np.dot(M, (1 / lambdas_)), columns=_get_index(X), index=times[:, 0])

    @property
    def score_(self):
        """
        The concordance score (also known as the c-index) of the fit.  The c-index is a generalization of the ROC AUC
        to survival data, including censorships.

        For this purpose, the ``score_`` is a measure of the predictive accuracy of the fitted model
        onto the training dataset.

        """
        # pylint: disable=access-member-before-definition
        if hasattr(self, "_predicted_cumulative_hazard_"):
            self._concordance_score_ = concordance_index(
                self.durations, -self._predicted_cumulative_hazard_, self.event_observed
            )
            del self._predicted_cumulative_hazard_
            return self._concordance_score_
        return self._concordance_score_

    @property
    def median_survival_time_(self):
        return self.predict_median(self._norm_mean.to_frame().T).squeeze()

    @property
    def mean_survival_time_(self):
        return self.predict_expectation(self._norm_mean.to_frame().T).squeeze()

    def plot(self, columns=None, parameter=None, **errorbar_kwargs):
        """
        Produces a visual representation of the coefficients, including their standard errors and magnitudes.

        Parameters
        ----------
        columns : list, optional
            specify a subset of the columns to plot
        errorbar_kwargs:
            pass in additional plotting commands to matplotlib errorbar command

        Returns
        -------
        ax: matplotlib axis
            the matplotlib axis that be edited.

        """
        from matplotlib import pyplot as plt

        set_kwargs_ax(errorbar_kwargs)
        ax = errorbar_kwargs.pop("ax")
        errorbar_kwargs.setdefault("c", "k")
        errorbar_kwargs.setdefault("fmt", "s")
        errorbar_kwargs.setdefault("markerfacecolor", "white")
        errorbar_kwargs.setdefault("markeredgewidth", 1.25)
        errorbar_kwargs.setdefault("elinewidth", 1.25)
        errorbar_kwargs.setdefault("capsize", 3)

        z = inv_normal_cdf(1 - self.alpha / 2)

        params_ = self.params_.copy()
        standard_errors_ = self.standard_errors_.copy()

        if columns is not None:
            params_ = params_.loc[:, columns]
            standard_errors_ = standard_errors_.loc[:, columns]
        if parameter is not None:
            params_ = params_.loc[parameter]
            standard_errors_ = standard_errors_.loc[parameter]

        columns = params_.index

        hazards = params_.loc[columns].to_frame(name="coefs")

        hazards["se"] = z * standard_errors_.loc[columns]
        hazards = hazards.swaplevel(1, 0).sort_index()

        yaxis_locations = list(range(len(columns)))

        ax.errorbar(hazards["coefs"], yaxis_locations, xerr=hazards["se"], **errorbar_kwargs)
        best_ylim = ax.get_ylim()
        ax.vlines(0, -2, len(columns) + 1, linestyles="dashed", linewidths=1, alpha=0.65)
        ax.set_ylim(best_ylim)

        if isinstance(columns[0], tuple):
            tick_labels = ["%s: %s" % (p, c) for (p, c) in hazards.index]
        else:
            tick_labels = [i for i in hazards.index]

        plt.yticks(yaxis_locations, tick_labels)
        plt.xlabel("log(accelerated failure rate) (%g%% CI)" % ((1 - self.alpha) * 100))

        return ax

    def plot_covariate_groups(self, covariates, values, plot_baseline=True, **kwargs):
        """
        Produces a visual representation comparing the baseline survival curve of the model versus
        what happens when a covariate(s) is varied over values in a group. This is useful to compare
        subjects' survival as we vary covariate(s), all else being held equal. The baseline survival
        curve is equal to the predicted survival curve at all average values in the original dataset.

        Parameters
        ----------
        covariates: string or list
            a string (or list of strings) of the covariate in the original dataset that we wish to vary.
        values: 1d or 2d iterable
            an iterable of the values we wish the covariate to take on.
        plot_baseline: bool
            also display the baseline survival, defined as the survival at the mean of the original dataset.
        kwargs:
            pass in additional plotting commands

        Returns
        -------
        ax: matplotlib axis, or list of axis'
            the matplotlib axis that be edited.

        Examples
        ---------

        >>> from lifelines import datasets, WeibullAFTFitter
        >>> rossi = datasets.load_rossi()
        >>> wf = WeibullAFTFitter().fit(rossi, 'week', 'arrest')
        >>> wf.plot_covariate_groups('prio', values=np.arange(0, 15), cmap='coolwarm')

        >>> # multiple variables at once
        >>> wf.plot_covariate_groups(['prio', 'paro'], values=[[0, 0], [5, 0], [10, 0], [0, 1], [5, 1], [10, 1]], cmap='coolwarm')

        >>> # if you have categorical variables, you can simply things:
        >>> wf.plot_covariate_groups(['dummy1', 'dummy2', 'dummy3'], values=np.eye(3))


        """
        raise NotImplementedError()

    def _prep_inputs_for_prediction_and_return_parameters(self, X):
        X = X.copy()

        if isinstance(X, pd.DataFrame):
            X = X[self.params_["lambda_0_"].index]
            if self.fit_intercept:
                X["_intercept"] = 1.0

        return np.array([np.exp(np.dot(X, self.params_["lambda_%d_" % i])) for i in range(self.n_breakpoints)])