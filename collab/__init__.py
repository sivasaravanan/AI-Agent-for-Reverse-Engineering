"""Stage D: shared / multi-user use - roles, a shared workspace (history + audit), approval workflow and signed reports.

This is a LOCAL shared-folder model (e.g. a team drive), not a network service: there is no server, no single sign-on and no
protection against someone with file-system access to the shared folder. See docs/KNOWN_ISSUES.md.
"""
from collab.workspace import TeamWorkspace, WorkspaceError
from collab.users import AuthError, PERMISSIONS, ROLES, Session, UserStore
from collab.approvals import ApprovalError, ApprovalStore
from collab.signing import SigningError, sign_file, verify_file

__all__ = ["TeamWorkspace", "WorkspaceError", "AuthError", "PERMISSIONS", "ROLES", "Session", "UserStore", "ApprovalError",
           "ApprovalStore", "SigningError", "sign_file", "verify_file"]
