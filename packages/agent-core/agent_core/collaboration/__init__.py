"""Persistent, workspace-local collaboration orchestration."""
from .service import CollaborationService, CollaborationError

__all__ = ["CollaborationService", "CollaborationError"]
