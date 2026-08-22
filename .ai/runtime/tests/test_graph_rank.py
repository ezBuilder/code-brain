"""Regression tests for deterministic bounded personalized graph ranking."""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.graph_rank import (  # noqa: E402
    GraphRankEdge,
    RankedNode,
    _bounded_ego_graph,
    bounded_personalized_rank,
)


def _nodes(result: list[RankedNode]) -> list[str]:
    return [item.node for item in result]


def test_deterministic_and_input_order_independent():
    edges = [
        GraphRankEdge("seed", "zeta", confidence=0.7),
        GraphRankEdge("seed", "alpha", confidence=0.7),
        GraphRankEdge("alpha", "tail"),
        GraphRankEdge("zeta", "tail"),
        GraphRankEdge("seed", "alpha", confidence=0.2),
    ]

    first = bounded_personalized_rank(edges, ["seed"], iterations=30)
    second = bounded_personalized_rank(list(reversed(edges)), ["seed", "seed"], iterations=30)

    assert first == second
    assert _nodes(first) == ["seed", "alpha", "zeta", "tail"]


def test_seed_preference_and_confidence_weighting():
    result = bounded_personalized_rank(
        [
            GraphRankEdge("seed", "strong", confidence=1.0),
            GraphRankEdge("seed", "weak", confidence=0.1),
        ],
        ["seed"],
        bidirectional=False,
    )

    assert result[0].node == "seed"
    assert result[1].node == "strong"
    assert result[1].score > result[2].score
    assert result[1].distance == result[2].distance == 1


def test_contains_is_excluded_by_default():
    result = bounded_personalized_rank(
        [
            GraphRankEdge("seed", "child", relation="contains"),
            GraphRankEdge("seed", "called", relation="calls"),
        ],
        ["seed"],
        bidirectional=False,
    )

    assert _nodes(result) == ["seed", "called"]


def test_hop_node_edge_and_limit_caps():
    edges = [GraphRankEdge("seed", f"node-{index}") for index in range(10)]
    edges.extend(GraphRankEdge("node-0", f"tail-{index}") for index in range(10))

    result = bounded_personalized_rank(
        edges,
        ["seed"],
        max_hops=1,
        max_nodes=4,
        max_edges=3,
        limit=2,
        bidirectional=False,
    )

    assert len(result) == 2
    assert all(item.distance <= 1 for item in result)
    assert set(_nodes(result)) <= {"seed", "node-0", "node-1", "node-2"}


def test_cycles_and_disconnected_components_are_safe():
    result = bounded_personalized_rank(
        [
            GraphRankEdge("a", "b"),
            GraphRankEdge("b", "c"),
            GraphRankEdge("c", "a"),
            GraphRankEdge("unrelated", "far"),
        ],
        ["a"],
        max_hops=2,
        bidirectional=False,
    )

    assert _nodes(result) == ["a", "b", "c"]
    assert all(item.distance <= 2 for item in result)


def test_bidirectional_projection_includes_reverse_walk_at_hop_boundary():
    nodes, adjacency, distances = _bounded_ego_graph(
        [GraphRankEdge("seed", "child")],
        ["seed"],
        max_hops=1,
        max_nodes=10,
        max_edges=10,
        bidirectional=True,
    )

    assert nodes == {"seed", "child"}
    assert distances == {"seed": 0, "child": 1}
    assert adjacency["seed"] == [("child", 1.0, "calls")]
    assert adjacency["child"] == [("seed", 1.0, "calls")]


def test_empty_and_invalid_inputs_fail_soft_or_raise_clearly():
    assert bounded_personalized_rank([], []) == []
    assert bounded_personalized_rank([], ["seed"]) == []
    assert bounded_personalized_rank([GraphRankEdge("a", "b")], ["a"], limit=0) == []

    with pytest.raises(ValueError, match="max_nodes"):
        bounded_personalized_rank([], ["a"], max_nodes=10**9)
    with pytest.raises(ValueError, match="confidence"):
        bounded_personalized_rank([GraphRankEdge("a", "b", confidence=float("nan"))], ["a"])
    with pytest.raises(ValueError, match="restart"):
        bounded_personalized_rank([], ["a"], restart=float("inf"))
    with pytest.raises(ValueError, match="edges"):
        bounded_personalized_rank([object()], ["a"])


def test_immutable_typed_results():
    edge = GraphRankEdge("a", "b")
    result = bounded_personalized_rank([edge], ["a"])

    with pytest.raises(FrozenInstanceError):
        edge.source = "changed"  # type: ignore[misc]
    assert all(isinstance(item, RankedNode) for item in result)
