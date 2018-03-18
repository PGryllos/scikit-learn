"""Calibration of predicted probabilities."""

# Author: Alexandre Gramfort <alexandre.gramfort@telecom-paristech.fr>
#         Balazs Kegl <balazs.kegl@gmail.com>
#         Jan Hendrik Metzen <jhm@informatik.uni-bremen.de>
#         Mathieu Blondel <mathieu@mblondel.org>
#         Prokopios Gryllos <prokopis.gryllos@sentiance.com>
#
# License: BSD 3 clause

from __future__ import division
import warnings

from math import log
import numpy as np

from scipy.optimize import fmin_bfgs
from sklearn.preprocessing import LabelEncoder

from .base import BaseEstimator, ClassifierMixin, RegressorMixin,\
    MetaEstimatorMixin, clone
from .preprocessing import label_binarize, LabelBinarizer
from .utils import check_X_y, check_array, indexable, column_or_1d
from .utils.validation import check_is_fitted, check_consistent_length
from .utils.fixes import signature
from .isotonic import IsotonicRegression
from .svm import LinearSVC
from .model_selection import check_cv
from .metrics.classification import _check_binary_probabilistic_predictions
from .metrics.ranking import roc_curve
from .utils.multiclass import type_of_target


class CutoffClassifier(BaseEstimator, ClassifierMixin, MetaEstimatorMixin):
    """Decision threshold calibration for binary classification

    Meta estimator that calibrates the decision threshold (cutoff point)
    that is used for prediction. The methods for picking cutoff points are
    inferred from ROC analysis; making use of true positive and true negative
    rates and their corresponding thresholds.

    If cv="prefit" the base estimator is assumed to be fitted and all data will
    be used for the selection of the cutoff point. Otherwise the decision
    threshold is calculated as the average of the thresholds resulting from the
    cross-validation loop.

    Parameters
    ----------
    base_estimator : obj
        The classifier whose decision threshold will be adapted according to
        the acquired cutoff point. The estimator must have a decision_function
        or a predict_proba.

    method : str
        The method to use for choosing the cutoff point.

        - 'roc', selects the point on the roc_curve that is closest to the
        ideal corner (0, 1)

        - 'max_tpr', selects the point that yields the highest true positive
        rate with true negative rate at least equal to the value of the
        parameter min_tnr

        - 'max_tnr', selects the point that yields the highest true negative
        rate with true positive rate at least equal to the value of the
        parameter min_tpr

    scoring : str or None, optional (default=None)
        The method to be used for acquiring the score.

        - 'decision_function'. base_estimator.decision_function will be used
        for scoring.

        - 'predict_proba'. base_estimator.predict_proba will be used for
        scoring

        - None. base_estimator.decision_function will be used first and if not
        available base_estimator.predict_proba.

    pos_label : object, optional (default=1)
        Object representing the positive label

    cv : int, cross-validation generator, iterable or 'prefit', optional
        (default='prefit'). Determines the cross-validation splitting strategy.
        If cv='prefit' the base estimator is assumed to be fitted and all data
        will be used for the calibration of the probability threshold.

    threshold : float in [0, 1] or None, (default=None)
        In case method is 'max_tpr' or 'max_tnr' this parameter must be set to
        specify the threshold for the true negative rate or true positive rate
        respectively that needs to be achieved

    Attributes
    ----------
    decision_threshold_ : float
        Decision threshold for the positive class. Determines the output of
        predict

    References
    ----------
    .. [1] Receiver-operating characteristic (ROC) plots: a fundamental
           evaluation tool in clinical medicine, MH Zweig, G Campbell -
           Clinical chemistry, 1993

    """
    def __init__(self, base_estimator, method='roc', scoring=None, pos_label=1,
                 cv=3, threshold=None):
        self.base_estimator = base_estimator
        self.method = method
        self.scoring = scoring
        self.pos_label = pos_label
        self.cv = cv
        self.threshold = threshold

    def fit(self, X, y):
        """Fit model

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data

        y : array-like, shape (n_samples,)
            Target values. There must be two 2 distinct values.

        Returns
        -------
        self : object
            Instance of self.
        """
        if not hasattr(self.base_estimator, 'decision_function') and \
                not hasattr(self.base_estimator, 'predict_proba'):
            raise TypeError('The base_estimator needs to implement either a '
                            'decision_function or a predict_proba method')

        if self.method not in ['roc', 'max_tpr', 'max_tnr']:
            raise ValueError('method can either be "roc" or "max_tpr" or '
                             '"max_tnr. Got %s instead' % self.method)

        if self.scoring not in [None, 'decision_function', 'predict_proba']:
            raise ValueError('scoring param can either be "decision_function" '
                             'or "predict_proba" or None. Got %s instead' %
                             self.scoring)

        if self.method == 'max_tpr' or self.method == 'max_tnr':
            if not self.threshold or not isinstance(self.threshold, float) \
                    or not self.threshold >= 0 or not self.threshold <= 1:
                raise ValueError('threshold must be a number in [1, 0]. '
                                 'Got %s instead' % repr(self.threshold))

        X, y = check_X_y(X, y)

        y_type = type_of_target(y)
        if y_type != 'binary':
            raise ValueError('Expected target of binary type. Got %s ' %
                             y_type)

        self.label_encoder_ = LabelEncoder().fit(y)

        y = self.label_encoder_.transform(y)
        self.pos_label = self.label_encoder_.transform([self.pos_label])[0]

        if self.cv == 'prefit':
            self.decision_threshold_ = _CutoffClassifier(
                self.base_estimator, self.method, self.scoring, self.pos_label,
                self.threshold
            ).fit(X, y).decision_threshold_
        else:
            cv = check_cv(self.cv, y, classifier=True)
            decision_thresholds = []

            for train, test in cv.split(X, y):
                estimator = clone(self.base_estimator).fit(X[train], y[train])
                decision_thresholds.append(
                    _CutoffClassifier(estimator,
                                      self.method,
                                      self.scoring,
                                      self.pos_label,
                                      self.threshold).fit(
                        X[test], y[test]
                    ).decision_threshold_
                )
            self.decision_threshold_ = sum(decision_thresholds) /\
                len(decision_thresholds)
            self.base_estimator.fit(X, y)
        return self

    def predict(self, X):
        """Predict using the calibrated decision threshold

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            The samples.

        Returns
        -------
        C : array, shape (n_samples,)
            The predicted class.
        """
        X = check_array(X)
        check_is_fitted(self, ["label_encoder_", "decision_threshold_"])

        y_score = _get_binary_score(self.base_estimator, X, self.scoring,
                                    self.pos_label)
        return self.label_encoder_.inverse_transform(
            (y_score > self.decision_threshold_).astype(int)
        )


class _CutoffClassifier(object):
    """Cutoff point selection.

    It assumes that base_estimator has already been fit, and uses the input set
    of the fit function to select a cutoff point. Note that this class should
    not be used as an estimator directly. Use the CutoffClassifier with
    cv="prefit" instead.

    Parameters
    ----------
    base_estimator : obj
        The classifier whose decision threshold will be adapted according to
        the acquired cutoff point. The estimator must have a decision_function
        or a predict_proba.

    method : 'roc' or 'max_tpr' or 'max_tnr'
        The method to use for choosing the cutoff point.

    scoring : str or None, optional (default=None)
        The method to be used for acquiring the score. Can either be
        "decision_function" or "predict_proba" or None. If None then
        decision_function will be used first and if not available
        predict_proba.

    pos_label : object
        Label considered as positive during the roc_curve construction.

    threshold : float in [0, 1]
        minimum required value for the true negative rate (specificity) in case
        method 'max_tpr' is used or for the true positive rate (sensitivity) in
        case method 'max_tnr' is used

    Attributes
    ----------
    decision_threshold_ : float
        Acquired decision threshold for the positive class
    """
    def __init__(self, base_estimator, method, scoring, pos_label, threshold):
        self.base_estimator = base_estimator
        self.method = method
        self.scoring = scoring
        self.pos_label = pos_label
        self.threshold = threshold

    def fit(self, X, y):
        """Select a decision threshold for the fitted model's positive class
        using one of the available methods

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data.

        y : array-like, shape (n_samples,)
            Target values.

        Returns
        -------
        self : object
            Instance of self.
        """
        y_score = _get_binary_score(self.base_estimator, X, self.scoring,
                                    self.pos_label)
        fpr, tpr, thresholds = roc_curve(y, y_score, self.pos_label)

        if self.method == 'roc':
            # we find the threshold of the point (fpr, tpr) with the smallest
            # euclidean distance from the "ideal" corner (0, 1)
            self.decision_threshold_ = thresholds[
                np.argmin(fpr ** 2 + (tpr - 1) ** 2)
            ]
        elif self.method == 'max_tpr':
            indices = np.where(1 - fpr >= self.threshold)[0]
            max_tpr_index = np.argmax(tpr[indices])
            self.decision_threshold_ = thresholds[indices[max_tpr_index]]
        else:
            indices = np.where(tpr >= self.threshold)[0]
            max_tnr_index = np.argmax(1 - fpr[indices])
            self.decision_threshold_ = thresholds[indices[max_tnr_index]]
        return self


def _get_binary_score(clf, X, scoring=None, pos_label=1):
    """Binary classification score for the positive label (0 or 1)

    Returns the score that a binary classifier outputs for the positive label
    acquired either from decision_function or predict_proba

    Parameters
    ----------
    clf : object
        Classifier object to be used for acquiring the scores. Needs to have
        a decision_function or a predict_proba method.

    X : array-like, shape (n_samples, n_features)
        The samples.

    pos_label : int, optional (default=1)
        The positive label. Can either be 0 or 1.

    scoring : str or None, optional (default=None)
        The method to be used for acquiring the score. Can either be
        "decision_function" or "predict_proba" or None. If None then
        decision_function will be used first and if not available
        predict_proba.
    """
    if not scoring:
        try:
            y_score = clf.decision_function(X)
            if pos_label == 0:
                y_score = - y_score
        except (NotImplementedError, AttributeError):
            y_score = clf.predict_proba(X)[:, pos_label]
    elif scoring == 'decision_function':
        y_score = clf.decision_function(X)
        if pos_label == 0:
            y_score = - y_score
    else:
        y_score = clf.predict_proba(X)[:, pos_label]
    return y_score


class CalibratedClassifierCV(BaseEstimator, ClassifierMixin):
    """Probability calibration with isotonic regression or sigmoid.

    With this class, the base_estimator is fit on the train set of the
    cross-validation generator and the test set is used for calibration.
    The probabilities for each of the folds are then averaged
    for prediction. In case that cv="prefit" is passed to __init__,
    it is assumed that base_estimator has been fitted already and all
    data is used for calibration. Note that data for fitting the
    classifier and for calibrating it must be disjoint.

    Read more in the :ref:`User Guide <calibration>`.

    Parameters
    ----------
    base_estimator : instance BaseEstimator
        The classifier whose output decision function needs to be calibrated
        to offer more accurate predict_proba outputs. If cv=prefit, the
        classifier must have been fit already on data.

    method : 'sigmoid' or 'isotonic'
        The method to use for calibration. Can be 'sigmoid' which
        corresponds to Platt's method or 'isotonic' which is a
        non-parametric approach. It is not advised to use isotonic calibration
        with too few calibration samples ``(<<1000)`` since it tends to
        overfit.
        Use sigmoids (Platt's calibration) in this case.

    cv : integer, cross-validation generator, iterable or "prefit", optional
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - An object to be used as a cross-validation generator.
        - An iterable yielding train/test splits.

        For integer/None inputs, if ``y`` is binary or multiclass,
        :class:`sklearn.model_selection.StratifiedKFold` is used. If ``y`` is
        neither binary nor multiclass, :class:`sklearn.model_selection.KFold`
        is used.

        Refer :ref:`User Guide <cross_validation>` for the various
        cross-validation strategies that can be used here.

        If "prefit" is passed, it is assumed that base_estimator has been
        fitted already and all data is used for calibration.

    Attributes
    ----------
    classes_ : array, shape (n_classes)
        The class labels.

    calibrated_classifiers_ : list (len() equal to cv or 1 if cv == "prefit")
        The list of calibrated classifiers, one for each crossvalidation fold,
        which has been fitted on all but the validation fold and calibrated
        on the validation fold.

    References
    ----------
    .. [1] Obtaining calibrated probability estimates from decision trees
           and naive Bayesian classifiers, B. Zadrozny & C. Elkan, ICML 2001

    .. [2] Transforming Classifier Scores into Accurate Multiclass
           Probability Estimates, B. Zadrozny & C. Elkan, (KDD 2002)

    .. [3] Probabilistic Outputs for Support Vector Machines and Comparisons to
           Regularized Likelihood Methods, J. Platt, (1999)

    .. [4] Predicting Good Probabilities with Supervised Learning,
           A. Niculescu-Mizil & R. Caruana, ICML 2005
    """
    def __init__(self, base_estimator=None, method='sigmoid', cv=3):
        self.base_estimator = base_estimator
        self.method = method
        self.cv = cv

    def fit(self, X, y, sample_weight=None):
        """Fit the calibrated model

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data.

        y : array-like, shape (n_samples,)
            Target values.

        sample_weight : array-like, shape = [n_samples] or None
            Sample weights. If None, then samples are equally weighted.

        Returns
        -------
        self : object
            Returns an instance of self.
        """
        X, y = check_X_y(X, y, accept_sparse=['csc', 'csr', 'coo'],
                         force_all_finite=False)
        X, y = indexable(X, y)
        le = LabelBinarizer().fit(y)
        self.classes_ = le.classes_

        # Check that each cross-validation fold can have at least one
        # example per class
        n_folds = self.cv if isinstance(self.cv, int) \
            else self.cv.n_folds if hasattr(self.cv, "n_folds") else None
        if n_folds and \
                np.any([np.sum(y == class_) < n_folds for class_ in
                        self.classes_]):
            raise ValueError("Requesting %d-fold cross-validation but provided"
                             " less than %d examples for at least one class."
                             % (n_folds, n_folds))

        self.calibrated_classifiers_ = []
        if self.base_estimator is None:
            # we want all classifiers that don't expose a random_state
            # to be deterministic (and we don't want to expose this one).
            base_estimator = LinearSVC(random_state=0)
        else:
            base_estimator = self.base_estimator

        if self.cv == "prefit":
            calibrated_classifier = _CalibratedClassifier(
                base_estimator, method=self.method)
            if sample_weight is not None:
                calibrated_classifier.fit(X, y, sample_weight)
            else:
                calibrated_classifier.fit(X, y)
            self.calibrated_classifiers_.append(calibrated_classifier)
        else:
            cv = check_cv(self.cv, y, classifier=True)
            fit_parameters = signature(base_estimator.fit).parameters
            estimator_name = type(base_estimator).__name__
            if (sample_weight is not None
                    and "sample_weight" not in fit_parameters):
                warnings.warn("%s does not support sample_weight. Samples"
                              " weights are only used for the calibration"
                              " itself." % estimator_name)
                base_estimator_sample_weight = None
            else:
                if sample_weight is not None:
                    sample_weight = check_array(sample_weight, ensure_2d=False)
                    check_consistent_length(y, sample_weight)
                base_estimator_sample_weight = sample_weight
            for train, test in cv.split(X, y):
                this_estimator = clone(base_estimator)
                if base_estimator_sample_weight is not None:
                    this_estimator.fit(
                        X[train], y[train],
                        sample_weight=base_estimator_sample_weight[train])
                else:
                    this_estimator.fit(X[train], y[train])

                calibrated_classifier = _CalibratedClassifier(
                    this_estimator, method=self.method,
                    classes=self.classes_)
                if sample_weight is not None:
                    calibrated_classifier.fit(X[test], y[test],
                                              sample_weight[test])
                else:
                    calibrated_classifier.fit(X[test], y[test])
                self.calibrated_classifiers_.append(calibrated_classifier)

        return self

    def predict_proba(self, X):
        """Posterior probabilities of classification

        This function returns posterior probabilities of classification
        according to each class on an array of test vectors X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            The samples.

        Returns
        -------
        C : array, shape (n_samples, n_classes)
            The predicted probas.
        """
        check_is_fitted(self, ["classes_", "calibrated_classifiers_"])
        X = check_array(X, accept_sparse=['csc', 'csr', 'coo'],
                        force_all_finite=False)
        # Compute the arithmetic mean of the predictions of the calibrated
        # classifiers
        mean_proba = np.zeros((X.shape[0], len(self.classes_)))
        for calibrated_classifier in self.calibrated_classifiers_:
            proba = calibrated_classifier.predict_proba(X)
            mean_proba += proba

        mean_proba /= len(self.calibrated_classifiers_)

        return mean_proba

    def predict(self, X):
        """Predict the target of new samples. Can be different from the
        prediction of the uncalibrated classifier.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            The samples.

        Returns
        -------
        C : array, shape (n_samples,)
            The predicted class.
        """
        check_is_fitted(self, ["classes_", "calibrated_classifiers_"])
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]


class _CalibratedClassifier(object):
    """Probability calibration with isotonic regression or sigmoid.

    It assumes that base_estimator has already been fit, and trains the
    calibration on the input set of the fit function. Note that this class
    should not be used as an estimator directly. Use CalibratedClassifierCV
    with cv="prefit" instead.

    Parameters
    ----------
    base_estimator : instance BaseEstimator
        The classifier whose output decision function needs to be calibrated
        to offer more accurate predict_proba outputs. No default value since
        it has to be an already fitted estimator.

    method : 'sigmoid' | 'isotonic'
        The method to use for calibration. Can be 'sigmoid' which
        corresponds to Platt's method or 'isotonic' which is a
        non-parametric approach based on isotonic regression.

    classes : array-like, shape (n_classes,), optional
            Contains unique classes used to fit the base estimator.
            if None, then classes is extracted from the given target values
            in fit().

    See also
    --------
    CalibratedClassifierCV

    References
    ----------
    .. [1] Obtaining calibrated probability estimates from decision trees
           and naive Bayesian classifiers, B. Zadrozny & C. Elkan, ICML 2001

    .. [2] Transforming Classifier Scores into Accurate Multiclass
           Probability Estimates, B. Zadrozny & C. Elkan, (KDD 2002)

    .. [3] Probabilistic Outputs for Support Vector Machines and Comparisons to
           Regularized Likelihood Methods, J. Platt, (1999)

    .. [4] Predicting Good Probabilities with Supervised Learning,
           A. Niculescu-Mizil & R. Caruana, ICML 2005
    """
    def __init__(self, base_estimator, method='sigmoid', classes=None):
        self.base_estimator = base_estimator
        self.method = method
        self.classes = classes

    def _preproc(self, X):
        n_classes = len(self.classes_)
        if hasattr(self.base_estimator, "decision_function"):
            df = self.base_estimator.decision_function(X)
            if df.ndim == 1:
                df = df[:, np.newaxis]
        elif hasattr(self.base_estimator, "predict_proba"):
            df = self.base_estimator.predict_proba(X)
            if n_classes == 2:
                df = df[:, 1:]
        else:
            raise RuntimeError('classifier has no decision_function or '
                               'predict_proba method.')

        idx_pos_class = self.label_encoder_.\
            transform(self.base_estimator.classes_)

        return df, idx_pos_class

    def fit(self, X, y, sample_weight=None):
        """Calibrate the fitted model

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data.

        y : array-like, shape (n_samples,)
            Target values.

        sample_weight : array-like, shape = [n_samples] or None
            Sample weights. If None, then samples are equally weighted.

        Returns
        -------
        self : object
            Returns an instance of self.
        """

        self.label_encoder_ = LabelEncoder()
        if self.classes is None:
            self.label_encoder_.fit(y)
        else:
            self.label_encoder_.fit(self.classes)

        self.classes_ = self.label_encoder_.classes_
        Y = label_binarize(y, self.classes_)

        df, idx_pos_class = self._preproc(X)
        self.calibrators_ = []

        for k, this_df in zip(idx_pos_class, df.T):
            if self.method == 'isotonic':
                calibrator = IsotonicRegression(out_of_bounds='clip')
            elif self.method == 'sigmoid':
                calibrator = _SigmoidCalibration()
            else:
                raise ValueError('method should be "sigmoid" or '
                                 '"isotonic". Got %s.' % self.method)
            calibrator.fit(this_df, Y[:, k], sample_weight)
            self.calibrators_.append(calibrator)

        return self

    def predict_proba(self, X):
        """Posterior probabilities of classification

        This function returns posterior probabilities of classification
        according to each class on an array of test vectors X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            The samples.

        Returns
        -------
        C : array, shape (n_samples, n_classes)
            The predicted probas. Can be exact zeros.
        """
        n_classes = len(self.classes_)
        proba = np.zeros((X.shape[0], n_classes))

        df, idx_pos_class = self._preproc(X)

        for k, this_df, calibrator in \
                zip(idx_pos_class, df.T, self.calibrators_):
            if n_classes == 2:
                k += 1
            proba[:, k] = calibrator.predict(this_df)

        # Normalize the probabilities
        if n_classes == 2:
            proba[:, 0] = 1. - proba[:, 1]
        else:
            proba /= np.sum(proba, axis=1)[:, np.newaxis]

        # XXX : for some reason all probas can be 0
        proba[np.isnan(proba)] = 1. / n_classes

        # Deal with cases where the predicted probability minimally exceeds 1.0
        proba[(1.0 < proba) & (proba <= 1.0 + 1e-5)] = 1.0

        return proba


def _sigmoid_calibration(df, y, sample_weight=None):
    """Probability Calibration with sigmoid method (Platt 2000)

    Parameters
    ----------
    df : ndarray, shape (n_samples,)
        The decision function or predict proba for the samples.

    y : ndarray, shape (n_samples,)
        The targets.

    sample_weight : array-like, shape = [n_samples] or None
        Sample weights. If None, then samples are equally weighted.

    Returns
    -------
    a : float
        The slope.

    b : float
        The intercept.

    References
    ----------
    Platt, "Probabilistic Outputs for Support Vector Machines"
    """
    df = column_or_1d(df)
    y = column_or_1d(y)

    F = df  # F follows Platt's notations
    tiny = np.finfo(np.float).tiny  # to avoid division by 0 warning

    # Bayesian priors (see Platt end of section 2.2)
    prior0 = float(np.sum(y <= 0))
    prior1 = y.shape[0] - prior0
    T = np.zeros(y.shape)
    T[y > 0] = (prior1 + 1.) / (prior1 + 2.)
    T[y <= 0] = 1. / (prior0 + 2.)
    T1 = 1. - T

    def objective(AB):
        # From Platt (beginning of Section 2.2)
        E = np.exp(AB[0] * F + AB[1])
        P = 1. / (1. + E)
        l = -(T * np.log(P + tiny) + T1 * np.log(1. - P + tiny))
        if sample_weight is not None:
            return (sample_weight * l).sum()
        else:
            return l.sum()

    def grad(AB):
        # gradient of the objective function
        E = np.exp(AB[0] * F + AB[1])
        P = 1. / (1. + E)
        TEP_minus_T1P = P * (T * E - T1)
        if sample_weight is not None:
            TEP_minus_T1P *= sample_weight
        dA = np.dot(TEP_minus_T1P, F)
        dB = np.sum(TEP_minus_T1P)
        return np.array([dA, dB])

    AB0 = np.array([0., log((prior0 + 1.) / (prior1 + 1.))])
    AB_ = fmin_bfgs(objective, AB0, fprime=grad, disp=False)
    return AB_[0], AB_[1]


class _SigmoidCalibration(BaseEstimator, RegressorMixin):
    """Sigmoid regression model.

    Attributes
    ----------
    a_ : float
        The slope.

    b_ : float
        The intercept.
    """
    def fit(self, X, y, sample_weight=None):
        """Fit the model using X, y as training data.

        Parameters
        ----------
        X : array-like, shape (n_samples,)
            Training data.

        y : array-like, shape (n_samples,)
            Training target.

        sample_weight : array-like, shape = [n_samples] or None
            Sample weights. If None, then samples are equally weighted.

        Returns
        -------
        self : object
            Returns an instance of self.
        """
        X = column_or_1d(X)
        y = column_or_1d(y)
        X, y = indexable(X, y)

        self.a_, self.b_ = _sigmoid_calibration(X, y, sample_weight)
        return self

    def predict(self, T):
        """Predict new data by linear interpolation.

        Parameters
        ----------
        T : array-like, shape (n_samples,)
            Data to predict from.

        Returns
        -------
        T_ : array, shape (n_samples,)
            The predicted data.
        """
        T = column_or_1d(T)
        return 1. / (1. + np.exp(self.a_ * T + self.b_))


def calibration_curve(y_true, y_prob, normalize=False, n_bins=5):
    """Compute true and predicted probabilities for a calibration curve.

     The method assumes the inputs come from a binary classifier.

     Calibration curves may also be referred to as reliability diagrams.

    Read more in the :ref:`User Guide <calibration>`.

    Parameters
    ----------
    y_true : array, shape (n_samples,)
        True targets.

    y_prob : array, shape (n_samples,)
        Probabilities of the positive class.

    normalize : bool, optional, default=False
        Whether y_prob needs to be normalized into the bin [0, 1], i.e. is not
        a proper probability. If True, the smallest value in y_prob is mapped
        onto 0 and the largest one onto 1.

    n_bins : int
        Number of bins. A bigger number requires more data.

    Returns
    -------
    prob_true : array, shape (n_bins,)
        The true probability in each bin (fraction of positives).

    prob_pred : array, shape (n_bins,)
        The mean predicted probability in each bin.

    References
    ----------
    Alexandru Niculescu-Mizil and Rich Caruana (2005) Predicting Good
    Probabilities With Supervised Learning, in Proceedings of the 22nd
    International Conference on Machine Learning (ICML).
    See section 4 (Qualitative Analysis of Predictions).
    """
    y_true = column_or_1d(y_true)
    y_prob = column_or_1d(y_prob)

    if normalize:  # Normalize predicted values into interval [0, 1]
        y_prob = (y_prob - y_prob.min()) / (y_prob.max() - y_prob.min())
    elif y_prob.min() < 0 or y_prob.max() > 1:
        raise ValueError("y_prob has values outside [0, 1] and normalize is "
                         "set to False.")

    y_true = _check_binary_probabilistic_predictions(y_true, y_prob)

    bins = np.linspace(0., 1. + 1e-8, n_bins + 1)
    binids = np.digitize(y_prob, bins) - 1

    bin_sums = np.bincount(binids, weights=y_prob, minlength=len(bins))
    bin_true = np.bincount(binids, weights=y_true, minlength=len(bins))
    bin_total = np.bincount(binids, minlength=len(bins))

    nonzero = bin_total != 0
    prob_true = (bin_true[nonzero] / bin_total[nonzero])
    prob_pred = (bin_sums[nonzero] / bin_total[nonzero])

    return prob_true, prob_pred
