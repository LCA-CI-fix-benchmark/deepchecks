# ----------------------------------------------------------------------------
# Copyright (C) 2021-2023 Deepchecks (https://www.deepchecks.com)
#
# This file is part of Deepchecks.
# Deepchecks is distributed under the terms of the GNU Affero General
# Public License (version 3 or later).
# You should have received a copy of the GNU Affero General Public License
# along with Deepchecks.  If not, see <http://www.gnu.org/licenses/>.
# ----------------------------------------------------------------------------
#
"""Module for base recsys context."""
import typing as t

import numpy as np
import pandas as pd
from deepchecks.core.errors import DeepchecksValueError

from deepchecks.tabular.context import Context as TabularContext
from deepchecks.tabular._shared_docs import docstrings
from deepchecks.tabular.dataset import Dataset
from deepchecks.tabular.utils.validation import ensure_predictions_shape
from deepchecks.utils.logger import get_logger

__all__ = [
    'Context'
]


class _DummyModel:
    """Dummy model class used for inference with static predictions from the user.

    Parameters
    ----------
    train: Dataset
        Dataset, representing data an estimator was fitted on.
    test: Dataset
        Dataset, representing data an estimator predicts on.
    y_pred_train: t.Optional[np.ndarray]
        Array of the model prediction over the train dataset.
    y_pred_test: t.Optional[np.ndarray]
        Array of the model prediction over the test dataset.
    y_proba_train: np.ndarray
        Array of the model prediction probabilities over the train dataset.
    y_proba_test: np.ndarray
        Array of the model prediction probabilities over the test dataset.
    validate_data_on_predict: bool, default = True
        If true, before predicting validates that the received data samples have the same index as in original data.
    """

    feature_df_list: t.List[pd.DataFrame]
    predictions: pd.DataFrame
    proba: pd.DataFrame

    def __init__(self,
                 train: t.Union[Dataset, None] = None,
                 test: t.Union[Dataset, None] = None,
                 y_pred_train: t.Optional[t.Sequence[t.Hashable]] = None,
                 y_pred_test: t.Optional[t.Sequence[t.Hashable]] = None,
                 validate_data_on_predict: bool = True):

        if train is not None and test is not None:
            # check if datasets have same indexes
            if set(train.data.index) & set(test.data.index):
                train.data.index = map(lambda x: f'train-{x}', list(train.data.index))
                test.data.index = map(lambda x: f'test-{x}', list(test.data.index))
                get_logger().warning('train and test datasets have common index - adding "train"/"test"'
                                     ' prefixes. To avoid that provide datasets with no common indexes '
                                     'or pass the model object instead of the predictions.')

        feature_df_list = []
        predictions = []
        probas = []

        for dataset, y_pred in zip([train, test],
                                   [y_pred_train, y_pred_test]):
            if y_pred is not None and not isinstance(y_pred, np.ndarray):
                y_pred = np.array(y_pred)
            if y_proba is not None and not isinstance(y_proba, np.ndarray):
                y_proba = np.array(y_proba)
            if dataset is not None:
                feature_df_list.append(dataset.features_columns)
                if y_pred is not None:
                    ensure_predictions_shape(y_pred, dataset.data)
                    y_pred_ser = pd.Series(y_pred, index=dataset.data.index)
                    predictions.append(y_pred_ser)

        self.predictions = pd.concat(predictions, axis=0) if predictions else None
        self.probas = pd.concat(probas, axis=0) if probas else None
        self.feature_df_list = feature_df_list
        self.validate_data_on_predict = validate_data_on_predict

        if self.predictions is not None:
            self.predict = self._predict

    def _validate_data(self, data: pd.DataFrame):
        data = data.sample(min(100, len(data)))
        for feature_df in self.feature_df_list:
            # If all indices are found than test for equality in actual data (statistically significant portion)
            if set(data.index).issubset(set(feature_df.index)):
                sample_data = np.unique(np.random.choice(data.index, 30))
                if feature_df.loc[sample_data].equals(data.loc[sample_data]):
                    return
                else:
                    break
        raise DeepchecksValueError('Data that has not been seen before passed for inference with static '
                                   'predictions. Pass a real model to resolve this')

    def _predict(self, data: pd.DataFrame):
        """Predict on given data by the data indexes."""
        if self.validate_data_on_predict:
            self._validate_data(data)
        return self.predictions.loc[data.index].to_numpy()


@docstrings
class Context(TabularContext):
    """Contains all the data + properties the user has passed to a check/suite, and validates it seamlessly.

    Parameters
    ----------
    train: Union[Dataset, pd.DataFrame, None] , default: None
        Dataset or DataFrame object, representing data an estimator was fitted on
    test: Union[Dataset, pd.DataFrame, None] , default: None
        Dataset or DataFrame object, representing data an estimator predicts on
    model: Optional[BasicModel] , default: None
        A scikit-learn-compatible fitted estimator instance
    {additional_context_params:indent}
    """

    def __init__(
        self,
        train: t.Union[Dataset, pd.DataFrame, None] = None,
        test: t.Union[Dataset, pd.DataFrame, None] = None,
        feature_importance: t.Optional[pd.Series] = None,
        feature_importance_force_permutation: bool = False,
        feature_importance_timeout: int = 120,
        with_display: bool = True,
        y_pred_train: t.Optional[t.Sequence[t.Hashable]] = None,
        y_pred_test: t.Optional[t.Sequence[t.Hashable]] = None,
    ):

        super().__init__(train=train,
                         test=test,
                         feature_importance=feature_importance,
                         feature_importance_force_permutation=feature_importance_force_permutation,
                         feature_importance_timeout=feature_importance_timeout,
                         with_display=with_display)
        self._model = _DummyModel(train=train, test=test, y_pred_train=y_pred_train, y_pred_test=y_pred_test)
