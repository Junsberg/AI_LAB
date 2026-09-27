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


def build_clusters(
    edges: list[Edge], min_fund_sol: float = 0.05, prior: dict[str, str] | None = None
) -> dict[str, str]:
    """Map wallet → cluster_id.

    Rules (evidence-first, same as Rugprint):
      - `funded` edges count only above `min_fund_sol` (dust spam excluded)
      - `cosigned` edges always count (two keys in one tx = shared operator)
      - `same_cex_route` edges are weak: ignored here, used only as a tiebreak signal
    cluster_id: a group keeps the id most of its members had in `prior` (the previous
    run), so a cluster that gains or loses a wallet is still the same cluster across
    days. A merge takes the id of the largest merged part; a split keeps the id on the
    biggest piece. Groups with no usable prior id get sha256 of their smallest member.
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

    return assign_ids([sorted(m) for m in groups.values()], prior or {})


def _fresh_id(members: list[str]) -> str:
    return hashlib.sha256(members[0].encode()).hexdigest()[:16]


def assign_ids(groups: list[list[str]], prior: dict[str, str]) -> dict[str, str]:
    """Deterministic id inheritance. Each (group, prior id) pair is scored by how many
    members carried that id; pairs are granted greedily, largest overlap first, and
    each prior id and each group is used at most once."""
    claims = []
    for gi, members in enumerate(groups):
        counts: dict[str, int] = {}
        for m in members:
            if m in prior:
                counts[prior[m]] = counts.get(prior[m], 0) + 1
        claims += [(-n, cid, gi) for cid, n in counts.items()]
    claims.sort()
    chosen: dict[int, str] = {}
    used: set[str] = set()
    for _, cid, gi in claims:
        if gi not in chosen and cid not in used:
            chosen[gi] = cid
            used.add(cid)
    out: dict[str, str] = {}
    for gi, members in enumerate(groups):
        cid = chosen.get(gi)
        if cid is None:
            cid = _fresh_id(members)
            if cid in used:  # a surviving prior id already names another group
                cid = hashlib.sha256("|".join(members).encode()).hexdigest()[:16]
            used.add(cid)
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
