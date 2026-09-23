from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str  # funded | cosigned | same_cex_route
    amount_sol: float = 0.0


@dataclass
class ClusterStats:
    tokens_total: int = 0
    tokens_rugged: int = 0
    tokens_10x: int = 0
    members: set[str] = field(default_factory=set)


class UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic: smaller string becomes root
            if ra < rb:
                self._parent[rb] = ra
            else:
                self._parent[ra] = rb


def build_clusters(edges: list[Edge], min_fund_sol: float = 0.05) -> dict[str, str]:
    """Map wallet → cluster_id.

    Rules (evidence-first, same as Rugprint):
      - `funded` edges count only above `min_fund_sol` (dust spam excluded)
      - `cosigned` edges always count (two keys in one tx = shared operator)
      - `same_cex_route` edges are weak: ignored here, used only as a tiebreak signal
    cluster_id = sha256 of the sorted member set, so it is stable across runs.
    """
    uf = UnionFind()
    for e in edges:
        if e.kind == "funded" and e.amount_sol < min_fund_sol:
            continue
        if e.kind == "same_cex_route":
            continue
        uf.union(e.src, e.dst)

    groups: dict[str, list[str]] = {}
    for w in {x for e in edges for x in (e.src, e.dst)}:
        groups.setdefault(uf.find(w), []).append(w)

    out: dict[str, str] = {}
    for members in groups.values():
        members.sort()
        cid = hashlib.sha256("|".join(members).encode()).hexdigest()[:16]
        for m in members:
            out[m] = cid
    return out


def score_cluster(stats: ClusterStats, prior_tokens: float = 3.0) -> float:
    """0..1 score for a deployer cluster. Bayesian-smoothed so a fresh deployer
    (no history) lands near the prior, not at 0 or 1.

    score = (clean + bonus) / (total + prior)
      clean = tokens_total - tokens_rugged
      bonus = tokens_10x * 2   (a cluster that has produced 10x runners is worth more)
      prior pulls small samples toward 0.5
    """
    clean = stats.tokens_total - stats.tokens_rugged
    bonus = stats.tokens_10x * 2.0
    num = clean + bonus + prior_tokens * 0.5
    den = stats.tokens_total + bonus + prior_tokens
    return max(0.0, min(1.0, num / den))
