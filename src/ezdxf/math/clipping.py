#  Copyright (c) 2021, Manfred Moitzi
#  License: MIT License
from typing import TYPE_CHECKING, Iterable, List, Tuple

from ezdxf.math import (
    Vec2, intersection_line_line_2d,
    has_clockwise_orientation, point_to_line_relation,
)

if TYPE_CHECKING:
    from ezdxf.eztypes import Vertex


def clip_polygon(clip: Iterable['Vertex'],
                 subject: Iterable['Vertex']) -> List['Vec2']:
    """ Clip the `subject` polygon by the **convex** clipping polygon `clip`.

    Implements the `Sutherland–Hodgman`_ algorithm for clipping polygons.

    Args:
        clip: the convex clipping polygon as iterable of vertices
        subject: the polygon to clip as a iterable of vertices

    Returns:
        the clipped subject as list of :class:`~ezdxf.math.Vec2`

    .. versionadded:: 0.16

    .. _Sutherland–Hodgman: https://de.wikipedia.org/wiki/Algorithmus_von_Sutherland-Hodgman

    """

    def polygon(vertices: Iterable['Vertex']) -> List[Vec2]:
        vertices = Vec2.list(vertices)
        if len(vertices) > 1:
            if vertices[0].isclose(vertices[-1]):
                vertices.pop()
        return vertices

    def is_inside(point: Vec2) -> bool:
        return point_to_line_relation(
            point, clip_start, clip_end) == -1  # left of line

    def edge_intersection() -> Vec2:
        return intersection_line_line_2d(
            (edge_start, edge_end), (clip_start, clip_end))

    clipping_polygon = polygon(clip)
    if has_clockwise_orientation(clipping_polygon):
        clipping_polygon.reverse()
    if len(clipping_polygon) > 2:
        clip_start = clipping_polygon[-1]
    else:
        raise ValueError('invalid clipping polygon')
    clipped = polygon(subject)

    for clip_end in clipping_polygon:
        # next clipping edge to test: clip_start -> clip_end
        if not clipped:  # no subject vertices left to test
            break
        vertices = list(clipped)
        clipped.clear()
        edge_start = vertices[-1]
        for edge_end in vertices:
            # next polygon edge to test: edge_start -> edge_end
            if is_inside(edge_end):
                if not is_inside(edge_start):
                    clipped.append(edge_intersection())
                clipped.append(edge_end)
            elif is_inside(edge_start):
                clipped.append(edge_intersection())
            edge_start = edge_end
        clip_start = clip_end
    return clipped
