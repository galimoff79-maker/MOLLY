"""
Molly PTO — indexing package.

Здесь находятся компоненты индексации проекта:
- scanner
- extractors
- analyzer
- indexer
- normalizer
"""

from indexing.indexer import (
    ProjectIndexer,
    index_project,
)

__all__ = [
    "ProjectIndexer",
    "index_project",
]