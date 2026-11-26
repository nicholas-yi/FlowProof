"""Errors raised by workspace backends."""

from __future__ import annotations


class WorkspaceError(Exception):
    """A lease could not be created, inspected or released."""


class GitError(WorkspaceError):
    """A git command failed or git could not be started."""
