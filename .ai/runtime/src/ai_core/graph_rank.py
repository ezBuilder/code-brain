"""Deterministic, bounded personalized ranking for code graphs.

The module is deliberately standalone: it accepts an in-memory edge stream,
builds only a bounded ego graph around the seeds, and performs a small
personalized PageRank-style walk without network or optional dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

_MAX_NODE_CHARS = 1_024
_MAX_RELATION_CHARS = 128
_MAX_INPUT_SEEDS = 100_000
_MAX_INPUT_EDGES = 100_000
_MAX_LIMIT = 10_000
_MAX_HOPS = 32
_MAX_NODES = 10_000
_MAX_EDGES = 50_000
_MAX_ITERATIONS = 100
_MAX_EXCLUDED_RELATIONS = 1_024


@dataclass(frozen=True, slots=True)
class GraphRankEdge:
    """A weighted graph edge used by :func:`bounded_personalized_rank`."""

    source: str
    target: str
    relation: str = "calls"
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class RankedNode:
    """A node and its deterministic personalized rank metadata."""

    node: str
    score: float
    distance: int


def _bounded_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_probability(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    return parsed


def _node(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    parsed = value.strip()
    if not parsed or "\x00" in parsed or len(parsed) > _MAX_NODE_CHARS:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return parsed


def _relation(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    parsed = value.strip()
    if not parsed or "\x00" in parsed or len(parsed) > _MAX_RELATION_CHARS:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return parsed


def _normalize_seeds(seeds: Iterable[str], *, max_nodes: int) -> list[str]:
    unique: set[str] = set()
    try:
        iterator = iter(seeds)
    except TypeError:
        raise ValueError("seeds must be iterable") from None
    for count, raw_seed in enumerate(iterator, start=1):
        if count > _MAX_INPUT_SEEDS:
            raise ValueError(f"seeds exceeds {_MAX_INPUT_SEEDS} items")
        unique.add(_node(raw_seed, name="seed"))
    return sorted(unique)[:max_nodes]


def _normalize_edges(
    edges: Iterable[GraphRankEdge],
    *,
    excluded_relations: frozenset[str],
) -> list[GraphRankEdge]:
    """Normalize and deduplicate edges in a canonical order.

    Keeping the maximum confidence for an identical directed relation prevents
    repeated extractor rows from changing the walk while remaining independent
    of input order.
    """

    deduplicated: dict[tuple[str, str, str], float] = {}
    try:
        iterator = iter(edges)
    except TypeError:
        raise ValueError("edges must be iterable") from None
    for count, edge in enumerate(iterator, start=1):
        if count > _MAX_INPUT_EDGES:
            raise ValueError(f"edges exceeds {_MAX_INPUT_EDGES} items")
        if not isinstance(edge, GraphRankEdge):
            raise ValueError("edges must contain GraphRankEdge values")
        source = _node(edge.source, name="edge.source")
        target = _node(edge.target, name="edge.target")
        relation = _relation(edge.relation, name="edge.relation")
        confidence = _bounded_probability(edge.confidence, name="edge.confidence")
        if relation in excluded_relations or source == target and confidence == 0.0:
            continue
        key = (source, target, relation)
        deduplicated[key] = max(confidence, deduplicated.get(key, 0.0))

    return [
        GraphRankEdge(source, target, relation, confidence)
        for (source, target, relation), confidence in sorted(deduplicated.items())
        if confidence > 0.0
    ]


def _normalize_excluded_relations(values: Iterable[str]) -> frozenset[str]:
    unique: set[str] = set()
    try:
        iterator = iter(values)
    except TypeError:
        raise ValueError("excluded_relations must be iterable") from None
    for count, value in enumerate(iterator, start=1):
        if count > _MAX_EXCLUDED_RELATIONS:
            raise ValueError(f"excluded_relations exceeds {_MAX_EXCLUDED_RELATIONS} items")
        unique.add(_relation(value, name="excluded_relation"))
    return frozenset(unique)


def _bounded_ego_graph(
    edges: list[GraphRankEdge],
    seeds: list[str],
    *,
    max_hops: int,
    max_nodes: int,
    max_edges: int,
    bidirectional: bool,
) -> tuple[set[str], dict[str, list[tuple[str, float, str]]], dict[str, int]]:
    """Select a deterministic bounded ego graph with shortest seed distances."""

    adjacency: dict[str, dict[tuple[str, str], float]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source, {})[(edge.target, edge.relation)] = max(
            edge.confidence,
            adjacency.get(edge.source, {}).get((edge.target, edge.relation), 0.0),
        )
        if bidirectional and edge.source != edge.target:
            adjacency.setdefault(edge.target, {})[(edge.source, edge.relation)] = max(
                edge.confidence,
                adjacency.get(edge.target, {}).get((edge.source, edge.relation), 0.0),
            )

    distances = {seed: 0 for seed in seeds}
    nodes = set(seeds)
    selected: dict[str, dict[tuple[str, str], float]] = {}
    frontier = list(seeds)
    traversed_edges = 0
    traversed_relations: set[tuple[str, str, str]] = set()

    for depth in range(max_hops):
        next_frontier: list[str] = []
        for source in sorted(frontier):
            candidates = sorted(
                adjacency.get(source, {}).items(),
                key=lambda item: (item[0][0], item[0][1], -item[1]),
            )
            for (target, relation), confidence in candidates:
                relation_key = (
                    (min(source, target), max(source, target), relation)
                    if bidirectional
                    else (source, target, relation)
                )
                if relation_key in traversed_relations:
                    continue
                if traversed_edges >= max_edges:
                    break
                traversed_relations.add(relation_key)
                traversed_edges += 1
                if target not in nodes and len(nodes) >= max_nodes:
                    continue
                nodes.add(target)
                distances.setdefault(target, depth + 1)
                selected.setdefault(source, {})[(target, relation)] = max(
                    confidence,
                    selected.get(source, {}).get((target, relation), 0.0),
                )
                if bidirectional and source != target:
                    selected.setdefault(target, {})[(source, relation)] = max(
                        confidence,
                        selected.get(target, {}).get((source, relation), 0.0),
                    )
                if target not in frontier and distances[target] == depth + 1:
                    next_frontier.append(target)
            if traversed_edges >= max_edges:
                break
        frontier = sorted(set(next_frontier))
        if not frontier or traversed_edges >= max_edges:
            break

    ranked_adjacency = {
        source: [
            (target, confidence, relation)
            for (target, relation), confidence in sorted(
                outgoing.items(), key=lambda item: (item[0][0], item[0][1], -item[1])
            )
            if target in nodes
        ]
        for source, outgoing in selected.items()
        if source in nodes
    }
    return nodes, ranked_adjacency, distances


def bounded_personalized_rank(
    edges: Iterable[GraphRankEdge],
    seeds: Iterable[str],
    *,
    limit: int = 20,
    max_hops: int = 2,
    max_nodes: int = 10_000,
    max_edges: int = 50_000,
    restart: float = 0.25,
    iterations: int = 25,
    bidirectional: bool = True,
    excluded_relations: Iterable[str] = ("contains",),
) -> list[RankedNode]:
    """Return a deterministic personalized rank over a bounded seed ego graph.

    Invalid bounds and non-finite weights raise ``ValueError``. Empty seeds or
    edges fail soft with an empty list. Duplicate edges are collapsed using
    their maximum confidence; relation confidence controls transition mass.
    """

    bounded_limit = _bounded_int(limit, name="limit", minimum=0, maximum=_MAX_LIMIT)
    bounded_hops = _bounded_int(max_hops, name="max_hops", minimum=0, maximum=_MAX_HOPS)
    bounded_nodes = _bounded_int(max_nodes, name="max_nodes", minimum=0, maximum=_MAX_NODES)
    bounded_edges = _bounded_int(max_edges, name="max_edges", minimum=0, maximum=_MAX_EDGES)
    bounded_iterations = _bounded_int(
        iterations, name="iterations", minimum=0, maximum=_MAX_ITERATIONS
    )
    bounded_restart = _bounded_probability(restart, name="restart")
    if not isinstance(bidirectional, bool):
        raise ValueError("bidirectional must be a boolean")

    normalized_excluded = _normalize_excluded_relations(excluded_relations)
    normalized_seeds = _normalize_seeds(seeds, max_nodes=bounded_nodes)
    if not normalized_seeds or bounded_limit == 0 or bounded_nodes == 0 or bounded_edges == 0:
        return []

    normalized_edges = _normalize_edges(edges, excluded_relations=normalized_excluded)
    if not normalized_edges:
        return []

    nodes, adjacency, distances = _bounded_ego_graph(
        normalized_edges,
        normalized_seeds,
        max_hops=bounded_hops,
        max_nodes=bounded_nodes,
        max_edges=bounded_edges,
        bidirectional=bidirectional,
    )
    if not nodes:
        return []

    active_seeds = sorted(node for node in normalized_seeds if node in nodes)
    if not active_seeds:
        return []
    teleport = 1.0 / len(active_seeds)
    scores = {node: (teleport if node in active_seeds else 0.0) for node in nodes}

    for _ in range(bounded_iterations):
        next_scores = {node: 0.0 for node in nodes}
        dangling = 0.0
        for source in sorted(nodes):
            outgoing = adjacency.get(source, ())
            if not outgoing:
                dangling += scores[source]
                continue
            total_weight = sum(confidence for _, confidence, _ in outgoing)
            if total_weight <= 0.0 or not math.isfinite(total_weight):
                dangling += scores[source]
                continue
            transfer = (1.0 - bounded_restart) * scores[source] / total_weight
            for target, confidence, _ in outgoing:
                next_scores[target] += transfer * confidence

        seed_share = (bounded_restart + (1.0 - bounded_restart) * dangling) * teleport
        for seed in active_seeds:
            next_scores[seed] += seed_share
        scores = next_scores

    ranked = [
        RankedNode(node=node, score=score, distance=distances[node])
        for node, score in scores.items()
        if math.isfinite(score)
    ]
    ranked.sort(key=lambda item: (-item.score, item.distance, item.node))
    return ranked[: min(bounded_limit, len(ranked))]


__all__ = ["GraphRankEdge", "RankedNode", "bounded_personalized_rank"]
