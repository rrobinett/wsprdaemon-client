"""Canonical spot row schema + decoder-output parsers.

Pipeline v2 (see docs/PIPELINE-V2-DESIGN.md): wd-ka9q-record will
write spots directly into `wspr.spots` via `sigmond.hamsci_ch.Writer`
once Phase 2 lands.  This package is the contract between the
decoders (wsprd / jt9 / wsprd's FST4 mode) and the writer — the
row shape lives here so it can be tested in isolation in Phase 1
without touching any running service.
"""
from .row import Row, SCHEMA_VERSION, row_to_dict

__all__ = ["Row", "SCHEMA_VERSION", "row_to_dict"]
