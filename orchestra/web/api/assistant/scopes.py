"""Scope catalog mapping feature names to provider-specific OAuth scopes.

Duplicated from Communication's ``common/scopes.py`` — these are static
data structures and an HTTP round-trip to fetch them isn't warranted.
"""

from __future__ import annotations

GOOGLE_SCOPE_BUNDLES: dict[str, list[str]] = {
    "email": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    "calendar": [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    ],
    "drive": [
        "https://www.googleapis.com/auth/drive",
    ],
    "contacts": [
        "https://www.googleapis.com/auth/contacts.readonly",
    ],
    "tasks": [
        "https://www.googleapis.com/auth/tasks",
    ],
    # Google Meet Workspace Events subscriptions (transcript.v2.fileGenerated
    # and friends) accept either meetings.space.readonly or
    # meetings.space.created. Read-only metadata is the least privilege that
    # still supports user-level transcript subscriptions where the user owns
    # the meeting space; we never create/modify spaces from here, and the
    # transcript file content in Drive is already covered by the ``drive``
    # bundle. See GOOGLE_MEET_EVENT_SCOPES for the accepted-scope gate.
    "meet": [
        "https://www.googleapis.com/auth/meetings.space.readonly",
    ],
    # Google Chat Workspace Events — least-privilege readonly scopes covering
    # the independently subscribable Chat families (messages/reactions,
    # memberships, spaces, read state, availability). Reactions accept
    # chat.messages.readonly, so a separate reactions scope is not required.
    # See GOOGLE_CHAT_EVENT_SCOPES for the facade gate.
    "chat": [
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.memberships.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/chat.users.readstate.readonly",
        "https://www.googleapis.com/auth/chat.users.availability.readonly",
    ],
}

GOOGLE_BASE_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
]

# Scopes Google accepts to create a Workspace Events subscription for Meet
# events. Either one authorizes the subscription; the facade treats a
# connection as Meet-capable when at least one of these is granted.
GOOGLE_MEET_EVENT_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/meetings.space.created",
    },
)

# Scopes Google accepts for Drive Workspace Events. The connect ``drive``
# bundle already grants ``drive``; the facade accepts any of these so a
# readonly-only grant still unlocks the Drive trigger app.
GOOGLE_DRIVE_EVENT_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.metadata",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    },
)

# Chat Workspace Events need one matching scope per event family. The facade
# treats the Chat app as connected only when the full requested readonly set
# is present (same set as the ``chat`` feature bundle).
GOOGLE_CHAT_EVENT_SCOPES = frozenset(GOOGLE_SCOPE_BUNDLES["chat"])

MICROSOFT_SCOPE_BUNDLES: dict[str, list[str]] = {
    "email": ["Mail.Read", "Mail.Send", "Mail.ReadWrite"],
    "calendar": ["Calendars.Read", "Calendars.ReadWrite"],
    "drive": ["Files.Read", "Files.ReadWrite"],
    "contacts": ["Contacts.Read"],
    "teams": [
        "Chat.Read",
        "Chat.ReadWrite",
        "ChatMessage.Read",
        "ChannelMessage.Send",
        "ChannelMessage.Read.All",
        "Team.ReadBasic.All",
        "Channel.ReadBasic.All",
        "Channel.Create",
        "TeamMember.Read.All",
        "OnlineMeetings.ReadWrite",
    ],
    "sharepoint": ["Sites.Read.All", "Sites.ReadWrite.All"],
    "tasks": ["Tasks.Read", "Tasks.ReadWrite"],
}

MICROSOFT_BASE_SCOPES = ["User.Read", "offline_access"]

_BUNDLES = {
    "google": GOOGLE_SCOPE_BUNDLES,
    "microsoft": MICROSOFT_SCOPE_BUNDLES,
}
_BASE = {
    "google": GOOGLE_BASE_SCOPES,
    "microsoft": MICROSOFT_BASE_SCOPES,
}

# Meet + Drive + Chat Workspace Events scopes are required together so a
# single Google re-consent unlocks the native turn-on set. The connect UX
# only ever sends a fixed feature set (email/teams defaults + these required
# features) and has no per-feature Meet/Chat toggle. Existing assistants must
# re-consent once after deploy; until then native facades stay disconnected
# and bindings must not appear healthy.
REQUIRED_FEATURES: dict[str, list[str]] = {
    "google": ["email", "drive", "meet", "chat"],
    "microsoft": ["email", "teams", "drive", "sharepoint"],
}


def available_features(provider: str) -> list[str]:
    """Return the feature names available for *provider*."""
    return list(_BUNDLES[provider])


def build_scope_string(provider: str, features: list[str]) -> str:
    """Resolve feature names to the union of provider scopes + base scopes."""
    bundles = _BUNDLES[provider]
    scopes: list[str] = list(_BASE[provider])
    for feat in features:
        scopes.extend(bundles[feat])
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for s in scopes:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    if provider == "microsoft":
        return " ".join(
            (
                f"https://graph.microsoft.com/{s}"
                if not s.startswith("http") and s != "offline_access"
                else s
            )
            for s in unique
        )
    return " ".join(unique)


def map_scopes_to_features(provider: str, granted_scopes: str) -> list[str]:
    """Map a raw scope string back to feature names whose bundles are fully covered."""
    bundles = _BUNDLES[provider]
    granted = set(granted_scopes.split())
    features: list[str] = []
    for feat, required in bundles.items():
        if provider == "microsoft":
            required_full = {
                (
                    f"https://graph.microsoft.com/{s}"
                    if not s.startswith("http") and s != "offline_access"
                    else s
                )
                for s in required
            }
        else:
            required_full = set(required)
        if required_full.issubset(granted):
            features.append(feat)
    return features
