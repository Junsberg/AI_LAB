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
