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
from deepchecks.utils.function import run_available_kwargs

from deepchecks.core.errors import DeepchecksValueError
from deepchecks.recommender.dataset import RecDataset

from deepchecks.tabular.context import Context as TabularContext
from deepchecks.tabular._shared_docs import docstrings
from deepchecks.tabular.dataset import Dataset
from deepchecks.tabular.metric_utils.scorers import DeepcheckScorer
from deepchecks.utils.logger import get_logger

from . import ranking

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
    validate_data_on_predict: bool, default = True
        If true, before predicting validates that the received data samples have the same index as in original data.
    """

    feature_df_list: t.List[pd.DataFrame]
    predictions: pd.DataFrame

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

        for dataset, y_pred in zip([train, test],
                                   [y_pred_train, y_pred_test]):
            if dataset is not None:
                feature_df_list.append(dataset.features_columns)
                if y_pred is not None:
                    y_pred_ser = pd.Series(y_pred, index=dataset.data.index)
                    predictions.append(y_pred_ser)

        self.predictions = pd.concat(predictions, axis=0) if predictions else None
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


class Scorer(DeepcheckScorer):
    def __init__(self, metric, name, to_avg=True, item_item_similarity=None, item_popularity=None,
                 item_to_index=None):
        if isinstance(metric, t.Callable):
            self.per_sample_metric = metric
        elif isinstance(metric, str):
            self.per_sample_metric = getattr(ranking, metric)
        else:
            raise DeepchecksValueError('Wrong scorer type')

        super().__init__(self.per_sample_metric, name=name, model_classes=None, observed_classes=None)
        self.to_avg = to_avg
        self.item_item_similarity = item_item_similarity
        self.item_popularity = item_popularity
        self.item_to_index = item_to_index

    def run_rec_metric(self, y_true, y_pred):
        additional_wkargs = {}
        if self.item_popularity is not None:
            additional_wkargs['item_popularity'] = self.item_popularity
        if self.item_item_similarity is not None:
            additional_wkargs['item_item_similarity'] = self.item_item_similarity
        scores = [run_available_kwargs(self.per_sample_metric, relevant_items=label, relevant_item=label,
                                       recommendation=pred, **additional_wkargs)
                  for label, pred in zip(y_true, y_pred)]
        return scores

    def __call__(self, model, dataset: RecDataset):
        dataset_without_nulls = self.filter_nulls(dataset)
        y_true = dataset_without_nulls.label_col
        y_pred = model.predict(dataset_without_nulls.features_columns)
        scores = self.run_rec_metric(y_true, y_pred)
        if self.to_avg:
            return np.nanmean(scores)
        return scores

    def _run_score(self, model, data: pd.DataFrame, label_col: pd.Series):
        y_pred = model.predict(data)
        scores = self.run_rec_metric(label_col, y_pred)
        if self.to_avg:
            return np.nanmean(scores)
        return scores


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

    _item_item_similarity: t.Optional[np.array]

    def __init__(
        self,
        train: t.Union[Dataset, pd.DataFrame, None] = None,
        test: t.Union[Dataset, pd.DataFrame, None] = None,
        feature_importance: t.Optional[pd.Series] = None,
        feature_importance_force_permutation: bool = False,
        feature_importance_timeout: int = 120,
        item_item_similarity: t.Optional[np.array] = None,
        with_display: bool = True,
        y_pred_train: t.Optional[t.Sequence[t.Hashable]] = None,
        y_pred_test: t.Optional[t.Sequence[t.Hashable]] = None,
        **kwargs
    ):

        super().__init__(train=train,
                         test=test,
                         feature_importance=feature_importance,
                         feature_importance_force_permutation=feature_importance_force_permutation,
                         feature_importance_timeout=feature_importance_timeout,
                         with_display=with_display)
        self._model = _DummyModel(train=train, test=test, y_pred_train=y_pred_train, y_pred_test=y_pred_test)
        self._item_item_similarity = item_item_similarity

    @property
    def item_to_index(self) -> t.Mapping[t.Hashable, int]:
        if self.train.item_to_index is not None:
            return self.train.item_to_index

    def get_scorer_kwargs(self) -> t.Dict:
        return {'item_item_similarity': self._item_item_similarity, 'item_to_index': self.item_to_index}

    def get_scorers(self, scorers: t.Union[t.Mapping[str, t.Union[str, t.Callable]],
                                           t.List[str]] = None, use_avg_defaults=True) -> t.List[Scorer]:
        if scorers is None:
            return [Scorer('reciprocal_rank', to_avg=use_avg_defaults, name=None, **self.get_scorer_kwargs()),
                    Scorer('rank', to_avg=use_avg_defaults, name=None, **self.get_scorer_kwargs())]
        if isinstance(scorers, t.Mapping):
            scorers = [Scorer(scorer, name, to_avg=use_avg_defaults, **self.get_scorer_kwargs())
                       for name, scorer in scorers.items()]
        else:
            scorers = [Scorer(scorer, to_avg=use_avg_defaults, name=None, **self.get_scorer_kwargs())
                       for scorer in scorers]
        return scorers

    def get_single_scorer(self, scorer: t.Mapping[str, t.Union[str, t.Callable]] = None,
                          use_avg_defaults=True) -> DeepcheckScorer:
        if scorer is None:
            return Scorer('reciprocal_rank', to_avg=use_avg_defaults, name=None, **self.get_scorer_kwargs())
        return Scorer(scorer, to_avg=use_avg_defaults, name=None, **self.get_scorer_kwargs())
