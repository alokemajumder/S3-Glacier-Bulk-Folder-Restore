"""Bulk restore of archived Amazon S3 objects under a prefix.

Recursively initiates restore requests for objects in the S3 Glacier Flexible
Retrieval, S3 Glacier Deep Archive, and (optionally) S3 Intelligent-Tiering
archive tiers, concurrently and resumably.
"""

from __future__ import annotations

__version__ = "2.0.1"

__all__ = ["__version__"]
