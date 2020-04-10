import logging
import math
from typing import Optional, Tuple

import click
import numpy as np
from scipy.spatial import cKDTree as KDTree
from shapely.geometry import Polygon, LineString

from vpype import as_vector, LineCollection, Length, layer_processor
from .cli import cli


class LineIndex:
    """Wrapper to rtree to facilitate systematic processing of a LineCollection. This
    class has many avenue for optimisation, which shan't be done until profiling says so.

    Implementation note: we use the `available` bool array because deleting stuff from the
    index is very costly.
    """

    def __init__(self, lines: LineCollection, reverse: bool = False):
        self.lines = [line for line in lines]
        self.reverse = reverse
        self._make_index()

    def _make_index(self) -> None:
        logging.info(f"LineIndex: creating index for {len(self.lines)} lines")
        self.available = np.ones(shape=len(self.lines), dtype=bool)

        # create rtree index
        self.index = KDTree(np.array([(line[0].real, line[0].imag) for line in self.lines]))

        # create reverse index
        if self.reverse:
            self.rindex = KDTree(
                np.array([(line[-1].real, line[-1].imag) for line in self.lines])
            )

    def _reindex(self) -> None:
        self.lines = [line for idx, line in enumerate(self.lines) if self.available[idx]]
        self._make_index()

    def __len__(self) -> int:
        return np.count_nonzero(self.available)

    def __getitem__(self, item):
        return self.lines[item]

    def pop_front(self) -> Optional[np.ndarray]:
        if len(self) == 0:
            return None
        idx = int(np.argmax(self.available))
        self.available[idx] = False
        return self.lines[idx]

    def pop(self, idx: int) -> Optional[np.ndarray]:
        if not self.available[idx]:
            return None
        self.available[idx] = False
        return self.lines[idx]

    def find_nearest_within(self, p: complex, max_dist: float) -> Tuple[Optional[int], bool]:
        """Find the closest line, assuming a maximum admissible distance.
        Returns a tuple of (idx, reverse), where `idx` may be None if nothing is found.
        `reverse` indicates whether or not a line ending has been matched instead of a start.
        False is always returned if index was created with `reverse=False`.s
        """

        ridx = None
        rdist = 0

        while True:
            reindex, idx, dist = self._find_nearest_within_in_index(p, max_dist, self.index)
            if reindex:
                self._reindex()
                continue

            if self.reverse:
                reindex, ridx, rdist = self._find_nearest_within_in_index(
                    p, max_dist, self.rindex
                )
                if reindex:
                    self._reindex()
                    continue
            break

        if self.reverse:
            if idx is None and ridx is None:
                return None, False
            elif idx is not None and ridx is None:
                return idx, False
            elif idx is None and ridx is not None:
                return ridx, True
            elif rdist < dist:
                return ridx, True
            else:
                return idx, False
        else:
            return idx, False

    def _find_nearest_within_in_index(
        self, p: complex, max_dist: float, index: KDTree
    ) -> Tuple[bool, Optional[int], Optional[float]]:
        """Find nearest in specific index. Return (reindex, idx, dist) tuple, where
        reindex indicates if a reindex is needed.
        """

        # For performance reason, we query only a max of k candidates. In the special case
        # where all distances are not inf and none are available, we might have more than k
        # suitable candidate, so we reindex and loop. Otherwise, we check the query results
        # for availability and not inf and return anything found
        dists, idxs = index.query((p.real, p.imag), k=50, distance_upper_bound=max_dist)
        dists = np.array(dists)

        not_inf = ~np.isinf(dists)
        if np.all(not_inf) and np.all(~self.available[idxs[not_inf]]):
            return True, None, 0

        candidates = self.available[idxs[not_inf]]

        if np.any(candidates):
            idx = np.argmax(candidates)
            return False, idxs[not_inf][idx], dists[not_inf][idx]
        else:
            return False, None, 0

    # noinspection PyUnboundLocalVariable
    def find_nearest(self, p: complex) -> Tuple[int, bool]:
        while True:
            idx, dist = self._find_nearest_in_index(p, self.index)
            if self.reverse:
                ridx, rdist = self._find_nearest_in_index(p, self.rindex)

                if ridx is not None and idx is not None:
                    break
            elif idx is not None:
                break
            self._reindex()

        if self.reverse:
            if rdist < dist:
                return ridx, True
            else:
                return idx, False
        else:
            return idx, False

    def _find_nearest_in_index(self, p: complex, index: KDTree) -> Tuple[Optional[int], float]:
        """Check the N nearest lines, hopefully find one that is active."""

        dists, idxs = index.query((p.real, p.imag), k=100)
        for dist, idx in zip(dists, idxs):
            if ~np.isinf(dist) and self.available[idx]:
                return idx, dist

        return None, 0.0


@cli.command(group="Operations")
@click.argument("x", type=Length(), required=True)
@click.argument("y", type=Length(), required=True)
@click.argument("width", type=Length(), required=True)
@click.argument("height", type=Length(), required=True)
@layer_processor
def crop(lines: LineCollection, x: float, y: float, width: float, height: float):
    """
    Crop the geometries.

    The crop area is defined by the (X, Y) top-left corner and the WIDTH and HEIGHT arguments.
    All arguments understand supported units.
    """
    if lines.is_empty():
        return lines

    # Because of this bug, we cannot use shapely at MultiLineString level
    # https://github.com/Toblerity/Shapely/issues/779
    # I should probably implement it directly anyways...
    p = Polygon([(x, y), (x + width, y), (x + width, y + height), (x, y + height)])
    new_lines = LineCollection()
    for line in lines:
        res = LineString(as_vector(line)).intersection(p)
        if res.is_empty:
            continue
        if res.geom_type == "MultiLineString":
            new_lines.extend(res)
        elif res.geom_type == "LineString":
            new_lines.append(res)

    return new_lines


@cli.command(group="Operations")
@click.option(
    "-t",
    "--tolerance",
    type=Length(),
    default="0.05mm",
    help="Maximum distance between two line endings that should be merged.",
)
@click.option(
    "-f", "--no-flip", is_flag=True, help="Disable reversing stroke direction for merging."
)
@layer_processor
def linemerge(lines: LineCollection, tolerance: float, no_flip: bool = True):
    """
    Merge lines whose endings overlap or are very close.

    Stroke direction is preserved by default, so `linemerge` looks at joining a line's end with
    another line's start. With the `--flip` stroke direction will be reversed as required to
    further the merge.

    By default, gaps of maximum 0.05mm are considered for merging. This can be controlled with
    the `--tolerance` option.
    """
    if len(lines) < 2:
        return lines

    index = LineIndex(lines, reverse=not no_flip)
    new_lines = LineCollection()

    while len(index) > 0:
        line = index.pop_front()

        # we append to `line` until we dont find anything to add
        while True:
            idx, reverse = index.find_nearest_within(line[-1], tolerance)
            if idx is None and not no_flip:
                idx, reverse = index.find_nearest_within(line[0], tolerance)
                line = np.flip(line)
            if idx is None:
                break
            new_line = index.pop(idx)
            if reverse:
                new_line = np.flip(new_line)
            line = np.hstack([line[:-1], 0.5 * (line[-1] + new_line[0]), new_line[1:]])

        new_lines.append(line)

    logging.info(f"linemerge: reduced line count from {len(lines)} to {len(new_lines)}")
    return new_lines


@cli.command(group="Operations")
@click.option(
    "-f",
    "--no-flip",
    is_flag=True,
    help="Disable reversing stroke direction for optimization.",
)
@layer_processor
def linesort(lines: LineCollection, no_flip: bool = True):
    """
    Sort lines to minimize the pen-up travel distance.

    Note: this process can be lengthy depending on the total number of line. Consider using
    `linemerge` before `linesort` to reduce the total number of line and thus significantly
    optimizing the overall plotting time.
    """
    if len(lines) < 2:
        return lines

    index = LineIndex(lines[1:], reverse=not no_flip)
    new_lines = LineCollection([lines[0]])

    while len(index) > 0:
        idx, reverse = index.find_nearest(new_lines[-1][-1])
        line = index.pop(idx)
        if reverse:
            line = np.flip(line)
        new_lines.append(line)

    logging.info(
        f"optimize: reduced pen-up (distance, mean, median) from {lines.pen_up_length()} to "
        f"{new_lines.pen_up_length()}"
    )

    return new_lines


@cli.command(group="Operations")
@click.option(
    "-t",
    "--tolerance",
    type=Length(),
    default="0.05mm",
    help="Controls how far from the original geometry simplified points may lie.",
)
@layer_processor
def linesimplify(lines: LineCollection, tolerance):
    """
    Reduce the number of segments in the geometries.

    The resulting geometries' points will be at a maximum distance from the original controlled
    by the `--tolerance` parameter (0.05mm by default).
    """
    if len(lines) < 2:
        return lines

    mls = lines.as_mls().simplify(tolerance=tolerance)
    new_lines = LineCollection(mls)

    logging.info(
        f"simplify: reduced segment count from {lines.segment_count()} to "
        f"{new_lines.segment_count()}"
    )

    return new_lines


@cli.command(group="Operations")
@click.option(
    "-t",
    "--tolerance",
    type=Length(),
    default="0.05mm",
    help="Controls how close the path beginning and end must be to consider it closed ("
    "default: 0.05mm).",
)
@layer_processor
def reloop(lines: LineCollection, tolerance):
    """
    Randomize the seam location for closed paths. Paths are considered closed when their
    beginning and end points are closer than the provided tolerance.
    """

    lines.reloop(tolerance=tolerance)
    return lines


@cli.command(group="Operations")
@click.option(
    "-n", "--count", type=int, default=2, help="How many pass for each line (default: 2).",
)
@layer_processor
def multipass(lines: LineCollection, count: int):
    """
    Add multiple passes to each line

    Each line is extended with a mirrored copy of itself, optionally multiple times. This is
    useful for pens that need several passes to ensure a good quality.
    """
    if count < 2:
        return lines

    new_lines = LineCollection()
    for line in lines:
        new_lines.append(
            np.hstack(
                [line] + [line[-2::-1] if i % 2 == 0 else line[1:] for i in range(count - 1)]
            )
        )

    return new_lines


@cli.command(group="Operations")
@layer_processor
def splitall(lines: LineCollection) -> LineCollection:
    """
    TODO doc
    """

    new_lines = LineCollection()
    for line in lines:
        new_lines.extend([line[i : i + 2] for i in range(len(line) - 1)])
    return new_lines
