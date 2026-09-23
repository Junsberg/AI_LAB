"""Deployer lineage: who funded the deployer, which cluster they belong to,
and how that cluster's past tokens performed.

Pattern ported from Rugprint (creator/funder/co-signer walk → deterministic cluster id).
Pure functions live in `cluster.py`; chain I/O lives in `tracer.py`.
"""
