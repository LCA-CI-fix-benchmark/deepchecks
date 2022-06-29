# ----------------------------------------------------------------------------
# Copyright (C) 2021-2022 Deepchecks (https://www.deepchecks.com)
#
# This file is part of Deepchecks.
# Deepchecks is distributed under the terms of the GNU Affero General
# Public License (version 3 or later).
# You should have received a copy of the GNU Affero General Public License
# along with Deepchecks.  If not, see <http://www.gnu.org/licenses/>.
# ----------------------------------------------------------------------------
#
"""Module of weak segments performance check."""
from collections import defaultdict
from typing import Callable, Dict, List, Union

import numpy as np
import pandas as pd
import plotly.express as px
import sklearn
from category_encoders import TargetEncoder
from packaging import version
from sklearn.model_selection import GridSearchCV
from sklearn.tree import DecisionTreeRegressor

from deepchecks import ConditionCategory, ConditionResult, Dataset
from deepchecks.core import CheckResult
from deepchecks.core.check_result import DisplayMap
from deepchecks.core.errors import DeepchecksNotSupportedError, DeepchecksProcessError
from deepchecks.tabular import Context, SingleDatasetCheck
from deepchecks.tabular.context import _DummyModel
from deepchecks.tabular.utils.task_type import TaskType
from deepchecks.utils.performance.partition import (convert_tree_leaves_into_filters,
                                                    partition_numeric_feature_around_segment)
from deepchecks.utils.single_sample_metrics import calculate_per_sample_loss
from deepchecks.utils.strings import format_number, format_percent
from deepchecks.utils.typing import Hashable

__all__ = ['WeakSegmentsPerformance']


class WeakSegmentsPerformance(SingleDatasetCheck):
    """Search for 2 feature based segments with the lowest performance score.

    Parameters
    ----------
    columns : Union[Hashable, List[Hashable]] , default: None
        Columns to check, if none are given checks all columns except ignored ones.
    ignore_columns : Union[Hashable, List[Hashable]] , default: None
        Columns to ignore, if none given checks based on columns variable
    n_top_features : int , default: 5
        Number of features to use for segment search. Top columns are selected based on feature importance.
    segment_minimum_size_ratio: float , default: 0.01
        Minimum size ratio for segments. Will only search for segments of
        size >= segment_minimum_size_ratio * data_size.
    alternative_scorer : Tuple[str, Union[str, Callable]] , default: None
        Score to show, either function or sklearn scorer name.
        If None, a default scorer (per the model type) will be used.
    loss_per_sample: Union[np.array, pd.Series, None], default: None
        Loss per sample used to detect relevant weak segments. If None, the check calculates loss per sample by using
         for classification and mean square error for regression.
    n_samples : int , default: 10_000
        number of samples to use for this check.
    n_to_show : int , default: 3
        number of segments with the weakest performance to show.
    categorical_aggregation_threshold : float , default: 0.05
        Categories with appearance ratio below threshold will be merged into "Other" category.
    random_state : int, default: 42
        random seed for all check internals.
    """

    def __init__(
            self,
            columns: Union[Hashable, List[Hashable], None] = None,
            ignore_columns: Union[Hashable, List[Hashable], None] = None,
            n_top_features: int = 5,
            segment_minimum_size_ratio: float = 0.01,
            alternative_scorer: Dict[str, Callable] = None,
            loss_per_sample: Union[np.array, pd.Series, None] = None,
            classes_index_order: Union[np.array, pd.Series, None] = None,
            n_samples: int = 10_000,
            categorical_aggregation_threshold: float = 0.05,
            n_to_show: int = 3,
            random_state: int = 42,
            **kwargs
    ):
        super().__init__(**kwargs)
        self.columns = columns
        self.ignore_columns = ignore_columns
        self.n_top_features = n_top_features
        self.segment_minimum_size_ratio = segment_minimum_size_ratio
        self.n_samples = n_samples
        self.n_to_show = n_to_show
        self.random_state = random_state
        self.loss_per_sample = loss_per_sample
        self.classes_index_order = classes_index_order
        self.user_scorer = alternative_scorer if alternative_scorer else None
        self.categorical_aggregation_threshold = categorical_aggregation_threshold

    def run_logic(self, context: Context, dataset_kind) -> CheckResult:
        """Run check."""
        dataset = context.get_data_by_kind(dataset_kind)
        dataset.assert_features()
        dataset = dataset.sample(self.n_samples, random_state=self.random_state, drop_na_label=True)
        predictions = context.model.predict(dataset.features_columns)
        y_proba = context.model.predict_proba(dataset.features_columns) if \
            context.task_type in [TaskType.MULTICLASS, TaskType.BINARY] else None

        if self.loss_per_sample is not None:
            loss_per_sample = self.loss_per_sample[list(dataset.data.index)]
        else:
            loss_per_sample = calculate_per_sample_loss(context.model, context.task_type, dataset,
                                                        self.classes_index_order)
        dataset = dataset.select(self.columns, self.ignore_columns, keep_label=True)
        if len(dataset.features) < 2:
            raise DeepchecksNotSupportedError('Check requires data to have at least two features in order to run.')
        encoded_dataset = self._target_encode_categorical_features_fill_na(dataset)
        dummy_model = _DummyModel(test=encoded_dataset, y_pred_test=predictions, y_proba_test=y_proba,
                                  validate_data_on_predict=False)

        if context.features_importance is not None:
            feature_rank = context.features_importance.sort_values(ascending=False).keys()
            feature_rank = np.asarray([col for col in feature_rank if col in encoded_dataset.features])
        else:
            feature_rank = np.asarray(encoded_dataset.features)

        scorer = context.get_single_scorer(self.user_scorer)
        weak_segments = self._weak_segments_search(dummy_model, encoded_dataset, feature_rank,
                                                   loss_per_sample, scorer)
        if len(weak_segments) == 0:
            raise DeepchecksProcessError('Unable to train a error model to find weak segments.')
        avg_score = round(scorer(dummy_model, encoded_dataset), 3)

        display = self._create_heatmap_display(dummy_model, encoded_dataset, weak_segments, avg_score,
                                               scorer) if context.with_display else []

        for idx, segment in weak_segments.copy().iterrows():
            if segment['Feature1'] in encoded_dataset.cat_features:
                weak_segments['Feature1 range'][idx] = \
                    self._format_partition_vec_for_display(segment['Feature1 range'], segment['Feature1'], None)[0]
            if segment['Feature2'] in encoded_dataset.cat_features:
                weak_segments['Feature2 range'][idx] = \
                    self._format_partition_vec_for_display(segment['Feature2 range'], segment['Feature2'], None)[0]

        return CheckResult({'segments': weak_segments, 'avg_score': avg_score, 'scorer_name': scorer.name},
                           display=[DisplayMap(display)])

    def _target_encode_categorical_features_fill_na(self, dataset: Dataset) -> Dataset:
        values_mapping = defaultdict(list)
        df_aggregated = dataset.features_columns.copy()
        for col in dataset.cat_features:
            categories_to_mask = [k for k, v in df_aggregated[col].value_counts().items() if
                                  v / dataset.n_samples < self.categorical_aggregation_threshold]
            df_aggregated.loc[np.isin(df_aggregated[col], categories_to_mask), col] = 'other'

        if len(dataset.cat_features) > 0:
            t_encoder = TargetEncoder(cols=dataset.cat_features)
            df_encoded = t_encoder.fit_transform(df_aggregated, dataset.label_col)
            for col in dataset.cat_features:
                values_mapping[col] = pd.concat([df_encoded[col], df_aggregated[col]], axis=1).drop_duplicates()
        else:
            df_encoded = df_aggregated
        self.encoder_mapping = values_mapping
        df_encoded.fillna(df_encoded.mode(axis=0), inplace=True)
        return Dataset(df_encoded, cat_features=dataset.cat_features, label=dataset.label_col)

    def _create_heatmap_display(self, dummy_model, encoded_dataset, weak_segments, avg_score, scorer):
        display_tabs = {}
        data = encoded_dataset.data
        idx = -1
        while len(display_tabs.keys()) < self.n_to_show and idx + 1 < len(weak_segments):
            idx += 1
            segment = weak_segments.iloc[idx, :]
            feature1, feature2 = data[segment['Feature1']], data[segment['Feature2']]
            segments_f1 = partition_numeric_feature_around_segment(feature1, segment['Feature1 range'])
            segments_f2 = partition_numeric_feature_around_segment(feature2, segment['Feature2 range'])
            if len(segments_f1) <= 2 or len(segments_f2) <= 2:
                continue
            scores = np.empty((len(segments_f1) - 1, len(segments_f2) - 1), dtype=float)
            counts = np.empty((len(segments_f1) - 1, len(segments_f2) - 1), dtype=int)
            for f1_idx in range(len(segments_f1) - 1):
                for f2_idx in range(len(segments_f2) - 1):
                    segment_data = data[
                        np.asarray(feature1.between(segments_f1[f1_idx], segments_f1[f1_idx + 1])) * np.asarray(
                            feature2.between(segments_f2[f2_idx], segments_f2[f2_idx + 1]))]
                    if segment_data.empty:
                        scores[f1_idx, f2_idx] = np.NaN
                        counts[f1_idx, f2_idx] = 0
                    else:
                        scores[f1_idx, f2_idx] = scorer.run_on_data_and_label(dummy_model, segment_data,
                                                                              segment_data[encoded_dataset.label_name])
                        counts[f1_idx, f2_idx] = len(segment_data)

            f1_labels = self._format_partition_vec_for_display(segments_f1, segment['Feature1'])
            f2_labels = self._format_partition_vec_for_display(segments_f2, segment['Feature2'])

            scores_text = [[0] * scores.shape[1] for _ in range(scores.shape[0])]
            counts = np.divide(counts, len(data))
            for i in range(len(f1_labels)):
                for j in range(len(f2_labels)):
                    score = scores[i, j]
                    if not np.isnan(score):
                        scores_text[i][j] = f'{format_number(score)}\n({format_percent(counts[i, j])})'
                    elif counts[i, j] == 0:
                        scores_text[i][j] = ''
                    else:
                        scores_text[i][j] = f'{score}\n({format_percent(counts[i, j])})'

            # Plotly FigureWidget have bug with numpy nan, so replacing with python None
            scores = scores.astype(np.object)
            scores[np.isnan(scores.astype(np.float_))] = None

            labels = dict(x=segment['Feature2'], y=segment['Feature1'], color=f'{scorer.name} score')
            fig = px.imshow(scores, x=f2_labels, y=f1_labels, labels=labels, color_continuous_scale='rdylgn')
            fig.update_traces(text=scores_text, texttemplate='%{text}')
            fig.update_layout(
                title=f'{scorer.name} score (percent of data) {segment["Feature1"]} vs {segment["Feature2"]}',
                height=600
            )
            msg = f'Average {scorer.name} score on data sampled is {format_number(avg_score)}'
            display_tabs[f'{segment["Feature1"]} vs {segment["Feature2"]}'] = [fig, msg]

        return display_tabs

    def _weak_segments_search(self, dummy_model, encoded_dataset, feature_rank_for_search, loss_per_sample, scorer):
        """Search for weak segments based on scorer."""
        weak_segments = pd.DataFrame(
            columns=[f'{scorer.name} score', 'Feature1', 'Feature1 range', 'Feature2', 'Feature2 range', '% of data'])
        for i in range(min(len(feature_rank_for_search), self.n_top_features)):
            for j in range(i + 1, min(len(feature_rank_for_search), self.n_top_features)):
                feature1, feature2 = feature_rank_for_search[[i, j]]
                weak_segment_score, weak_segment_filter = self._find_weak_segment(dummy_model, encoded_dataset,
                                                                                  [feature1, feature2], scorer,
                                                                                  loss_per_sample)
                if weak_segment_score is None:
                    continue
                data_size = 100 * weak_segment_filter.filter(encoded_dataset.data).shape[0] / encoded_dataset.n_samples
                weak_segments.loc[len(weak_segments)] = [weak_segment_score, feature1,
                                                         weak_segment_filter.filters[feature1], feature2,
                                                         weak_segment_filter.filters[feature2], data_size]
        return weak_segments.sort_values(f'{scorer.name} score')

    def _find_weak_segment(self, dummy_model, dataset, features_for_segment, scorer, loss_per_sample):
        """Find weak segment based on scorer for specified features."""
        if version.parse(sklearn.__version__) < version.parse('1.0.0'):
            criterion = ['mse', 'mae']
        else:
            criterion = ['squared_error', 'absolute_error']
        search_space = {
            'max_depth': [5],
            'min_weight_fraction_leaf': [self.segment_minimum_size_ratio],
            'min_samples_leaf': [10],
            'criterion': criterion
        }

        def get_worst_leaf_filter(tree):
            leaves_filters = convert_tree_leaves_into_filters(tree, features_for_segment)
            min_score, min_score_leaf_filter = np.inf, None
            for leaf_filter in leaves_filters:
                leaf_data = leaf_filter.filter(dataset.data)
                leaf_score = scorer.run_on_data_and_label(dummy_model, leaf_data, leaf_data[dataset.label_name])
                if leaf_score < min_score:
                    min_score, min_score_leaf_filter = leaf_score, leaf_filter
            return min_score, min_score_leaf_filter

        def neg_worst_segment_score(clf: DecisionTreeRegressor, x, y) -> float:  # pylint: disable=unused-argument
            return -get_worst_leaf_filter(clf.tree_)[0]

        grid_searcher = GridSearchCV(DecisionTreeRegressor(), scoring=neg_worst_segment_score,
                                     param_grid=search_space, n_jobs=-1, cv=3)
        try:
            grid_searcher.fit(dataset.features_columns[features_for_segment], loss_per_sample)
            segment_score, segment_filter = get_worst_leaf_filter(grid_searcher.best_estimator_.tree_)
        except ValueError:
            return None, None

        if features_for_segment[0] not in segment_filter.filters.keys():
            segment_filter.filters[features_for_segment[0]] = [np.NINF, np.inf]
        if features_for_segment[1] not in segment_filter.filters.keys():
            segment_filter.filters[features_for_segment[1]] = [np.NINF, np.inf]
        return segment_score, segment_filter

    def _format_partition_vec_for_display(self, partition_vec: np.array, feature_name: str,
                                          seperator: Union[str, None] = '\n') -> List[Union[List, str]]:
        """Format partition vector for display. If seperator is None returns a list instead of a string."""
        result = []
        if feature_name in self.encoder_mapping.keys():
            feature_map_df = self.encoder_mapping[feature_name]
            encodings = feature_map_df.iloc[:, 0]
            for lower, upper in zip(partition_vec[:-1], partition_vec[1:]):
                if lower == partition_vec[0]:
                    values_in_range = np.where(np.logical_and(encodings >= lower, encodings <= upper))[0]
                else:
                    values_in_range = np.where(np.logical_and(encodings > lower, encodings <= upper))[0]
                if seperator is None:
                    result.append(feature_map_df.iloc[values_in_range, 1].to_list())
                else:
                    result.append(seperator.join(feature_map_df.iloc[values_in_range, 1].to_list()))

        else:
            for lower, upper in zip(partition_vec[:-1], partition_vec[1:]):
                result.append(f'({format_number(lower)}, {format_number(upper)}]')
            result[0] = '[' + result[0][1:]

        return result

    def add_condition_segments_performance_relative_difference_greater_than(self, max_ratio_change: float = 0.20):
        """Add condition - check that the score of the weakest segment is at least (1 - ratio) * average score.

        Parameters
        ----------
        max_ratio_change : float , default: 0.20
            maximal ratio of change between the average score and the score of the weakest segment.
        """

        def condition(result: Dict) -> ConditionResult:
            weakest_segment_score = result['segments'].iloc[0, 0]
            msg = f'Found a segment with {result["scorer_name"]} score of {format_number(weakest_segment_score, 3)} ' \
                  f'in comparison to an average score of {format_number(result["avg_score"], 3)} in sampled data.'
            if weakest_segment_score < (1 - max_ratio_change) * result['avg_score']:
                return ConditionResult(ConditionCategory.WARN, msg)
            else:
                return ConditionResult(ConditionCategory.PASS, msg)

        return self.add_condition(f'The performance of weakest segment is greater than '
                                  f'{format_percent(1 - max_ratio_change)} of average model performance.', condition)
