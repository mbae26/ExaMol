"""Interfaces for selector classes"""
import heapq
import logging
from itertools import chain
from typing import Iterator, Sequence

import numpy as np

from examol.store.models import MoleculeRecord
from examol.store.recipes import PropertyRecipe

logger = logging.getLogger(__name__)


def _extract_observations(database: dict[str, MoleculeRecord], recipes: Sequence[PropertyRecipe]) -> np.ndarray:
    """Get an array of observations from the training set

    Args:
        database: Database of molecular records to process
        recipes: List of recipes to extract
    Returns:
        Properties for all molecules which have values for all recipes. Shape: (num molecules) x (num recipes)
    """

    output = []
    for record in database.values():
        if not all(recipe.lookup(record) is not None for recipe in recipes):
            continue
        output.append([recipe.lookup(record) for recipe in recipes])
    return np.array(output)


class Selector:
    """Base class for selection algorithms

    **Using a Selector**

    Selectors function in two phases: gathering and dispensing.

    Selectors are in the gathering phase when first created.
    Add potential computations in batches with :meth:`add_possibilities`,
    which takes a list of keys describing the computations
    and a distribution of probable scores (e.g., predictions from different models in an ensemble) for each computation.
    Sample arrays are 3D and shaped ``num_recipes x num_samples x num_models``

    The dispensing phase starts by calling :meth:`dispense`. ``dispense`` generates a selected computation from
    the list of keys acquired during gathering phase paired with a score. Selections are generated from highest
    to lowest priority.

    **Creating a Selector**

    You must implement three operations:

    - :meth:`start_gathering`, which is called at the beginning of a gathering phase and
      must clear state from the previous selection round.
    - :meth:`add_possibilities` updates the state of a selection to account for a new batch of computations.
      For example, you could update an ranked list of best-scored computations.
    - :meth:`dispense` generates a list of :attr:`to_select` in ranked order from best to worst
    """

    multiobjective: bool = False
    """Whether the selector supports multi-objective optimization"""

    def __init__(self, to_select: int):
        """

        Args:
            to_select: Target number of computations to select
        """
        self.to_select: int = to_select
        """Number of computations to select"""
        self.gathering: bool = True
        """Whether the selector is waiting to accept more possibilities."""
        self.start_gathering()

    def start_gathering(self):
        """Prepare to gather new batches potential computations"""
        self.gathering = True

    def add_possibilities(self, keys: list, samples: np.ndarray, **kwargs):
        """Add potential options to be selected

        Args:
            keys: Labels by which to identify the records being evaluated
            samples: A distribution of scores for each record.
                Expects a 3-dimensional array of shape (num recipes) x (num records) x (num models)
        """
        # Test for error conditions
        if samples.shape[0] > 1 and not self.multiobjective:
            raise ValueError(f'Provided {samples.shape[0]} objectives but the class does not support multi-objective selection')
        if samples.ndim != 3:  # pragma: no-coverage
            raise ValueError(f'Expected samples dimension of 3. Found {samples.ndim}. Array should be (recipe, records, model)')
        if samples.shape[1] != len(keys):  # pragma: no-coverage
            raise ValueError(f'Number of keys and number of samples differ. Keys={len(keys)}. Samples={samples.shape[1]}')

        # Do the work
        if not self.gathering:
            logger.info('Switching selector back to gathering phase. Clearing any previous selection information')
            self.start_gathering()
        self._add_possibilities(keys, samples, **kwargs)

    def _add_possibilities(self, keys: list, samples: np.ndarray, **kwargs):
        raise NotImplementedError()

    def update(self, database: dict[str, MoleculeRecord], recipes: Sequence[PropertyRecipe]):
        """Update the selector given the current database

        Args:
            database: Known molecules
            recipes: Recipe being optimized
        """
        pass

    def dispense(self) -> Iterator[tuple[object, float]]:
        """Dispense selected computations from highest- to least-rated.

        Yields:
            A pair of "selected computation" (as identified by the keys provided originally)
            and a score.
        """
        self.gathering = False
        yield from self._dispense()

    def _dispense(self) -> Iterator[tuple[object, float]]:
        raise NotImplementedError()


class RankingSelector(Selector):
    """Base class where we assign an independent score to each possibility.

    Implementations should assume that the goal is maximization
    because this abstract class negates the samples for objective
    is to minimize.

    Args:
        to_select: How many computations to select per batch
        maximize: Whether to select entries with high or low values of the samples.
            Provide either a single value if maximizing or minimizing all objectives,
            or a list for whether to maximize each objectives.
    """

    def __init__(self, to_select: int, maximize: bool | Sequence[bool] = True):
        self._options: list[tuple[object, float]] = []
        self.maximize = maximize
        super().__init__(to_select)

    def _add_possibilities(self, keys: list, samples: np.ndarray, **kwargs):
        # Determine user options for minimization
        n_objectives = samples.shape[0]
        maximize = self.maximize
        if isinstance(maximize, bool):
            maximize = [maximize] * n_objectives
        elif len(maximize) != n_objectives:  # pragma: no-cover
            raise ValueError(f'Different number of recipes ({n_objectives} and number of maximization selections ({len(maximize)})')

        # Negate, if needed
        if not all(maximize):
            samples = samples.copy()
            for i, m in enumerate(maximize):
                if not m:
                    samples[i, :, :] *= -1

        score = self._assign_score(samples)
        self._options = heapq.nlargest(self.to_select, chain(self._options, zip(keys, score)), key=lambda x: x[1])

    def _dispense(self) -> Iterator[tuple[object, float]]:
        yield from self._options

    def start_gathering(self):
        super().start_gathering()
        self._options.clear()

    def _assign_score(self, samples: np.ndarray) -> np.ndarray:
        raise NotImplementedError()
