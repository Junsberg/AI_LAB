from memebot.lineage.cluster import ClusterStats, Edge, build_clusters, score_cluster


def test_funded_edges_merge_and_dust_ignored():
    edges = [
        Edge("cex1", "A", "funded", 5.0),
        Edge("A", "B", "funded", 1.0),
        Edge("A", "C", "funded", 0.001),  # dust → separate
        Edge("X", "Y", "cosigned"),
    ]
    m = build_clusters(edges)
    assert m["A"] == m["B"] == m["cex1"]
    assert m["C"] != m["A"]
    assert m["X"] == m["Y"]
    assert m["X"] != m["A"]


def test_cluster_id_deterministic():
    e = [Edge("A", "B", "funded", 1.0)]
    assert build_clusters(e) == build_clusters(list(reversed(e)))


def test_score_fresh_deployer_is_neutral():
    assert abs(score_cluster(ClusterStats()) - 0.5) < 1e-9


def test_score_serial_rugger_low_and_builder_high():
    rugger = ClusterStats(tokens_total=12, tokens_rugged=11, tokens_10x=0)
    builder = ClusterStats(tokens_total=5, tokens_rugged=0, tokens_10x=3)
    assert score_cluster(rugger) < 0.2
    assert score_cluster(builder) > 0.8


def test_sql_score_formula_matches_python():
    """refresh_cluster_scores() reimplements score_cluster() in SQL; keep them equal."""
    def sql_formula(evaluated, rugged, tenx, dead):
        return round(min(1.0, max(0.0, ((evaluated - rugged - dead) + 2.0 * tenx + 1.5)
                                  / (evaluated + 2.0 * tenx + 3.0))), 4)

    for ev, rg, tx, dd in [(0, 0, 0, 0), (12, 11, 0, 0), (5, 0, 3, 1), (30, 10, 1, 5), (1, 1, 0, 0), (40, 0, 0, 38)]:
        assert sql_formula(ev, rg, tx, dd) == round(score_cluster(ClusterStats(ev, rg, tx, tokens_dead=dd)), 4)


def test_dead_tokens_are_not_clean():
    # the 09-27 replay cluster: 40 evaluated, 0 rugs, but they had all stopped trading
    assert score_cluster(ClusterStats(40, 0, 0)) > 0.9
    assert score_cluster(ClusterStats(40, 0, 0, tokens_dead=38)) < 0.2


# --- stable ids across rebuilds (limitation ④) ---------------------------------------
import hashlib  # noqa: E402

from memebot.lineage.cluster import assign_ids  # noqa: E402


def test_id_survives_new_member():
    e = [Edge("B", "C", "funded", 1.0)]
    first = build_clusters(e)
    # "A" sorts before every member: a content/min-address id would change here
    second = build_clusters(e + [Edge("A", "B", "funded", 1.0)], prior=first)
    assert second["A"] == second["B"] == first["B"]


def test_merge_keeps_larger_part_id_and_split_keeps_bigger_piece():
    prior = {"a": "big", "b": "big", "c": "big", "x": "small"}
    merged = assign_ids([["a", "b", "c", "x"]], prior)
    assert set(merged.values()) == {"big"}
    split = assign_ids([["a", "b"], ["c"]], prior)
    assert split["a"] == "big" and split["c"] != "big"


def test_fresh_group_id_matches_singleton_formula():
    # SQL gives singleton deployers left(sha256(address),16): a later group containing
    # that wallet as its smallest member, or inheriting it, keeps the same id
    out = assign_ids([["D1", "D2"]], {})
    assert out["D1"] == hashlib.sha256(b"D1").hexdigest()[:16]
    assert assign_ids([["D0", "D1"]], {"D1": out["D1"]})["D0"] == out["D1"]


def test_assign_ids_deterministic_and_unique():
    prior = {"a": "p", "b": "p", "c": "p", "d": "p"}
    g = [["a", "b"], ["c", "d"]]  # tie: exactly one group may keep "p"
    out = assign_ids(g, prior)
    assert out == assign_ids(g, prior)
    assert len({out["a"], out["c"]}) == 2 and "p" in {out["a"], out["c"]}
