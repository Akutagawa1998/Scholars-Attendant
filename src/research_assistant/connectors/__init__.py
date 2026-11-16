"""Read-only source adapters. External content never becomes a user statement."""
from .common import ConnectorError, SyncBatch

__all__ = ["ConnectorError", "SyncBatch"]
