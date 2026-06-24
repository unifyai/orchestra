from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AssistantUserDesktop, UserDesktop


class DesktopDAO:
    """Data access object for UserDesktop and per-user assistant links."""

    def __init__(self, session: Session):
        self.session = session

    # ── UserDesktop registry (per-user) ──────────────────────────────────

    def create(
        self,
        user_id: str,
        name: str,
        url: str,
        os: str,
    ) -> UserDesktop:
        desktop = UserDesktop(
            user_id=user_id,
            name=name,
            url=url,
            os=os,
        )
        self.session.add(desktop)
        self.session.flush()
        return desktop

    def get_by_id(self, desktop_id: int, user_id: str) -> Optional[UserDesktop]:
        stmt = select(UserDesktop).where(
            UserDesktop.id == desktop_id,
            UserDesktop.user_id == user_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_user(self, user_id: str) -> List[UserDesktop]:
        stmt = select(UserDesktop).where(UserDesktop.user_id == user_id)
        return list(self.session.execute(stmt).scalars().all())

    def update(
        self,
        desktop_id: int,
        user_id: str,
        update_data: dict,
    ) -> Optional[UserDesktop]:
        desktop = self.get_by_id(desktop_id, user_id)
        if not desktop:
            return None
        for key, value in update_data.items():
            setattr(desktop, key, value)
        self.session.flush()
        return desktop

    def delete(self, desktop_id: int, user_id: str) -> bool:
        desktop = self.get_by_id(desktop_id, user_id)
        if not desktop:
            return False
        # Linked assistant rows cascade via the FK ON DELETE CASCADE.
        self.session.delete(desktop)
        self.session.flush()
        return True

    def set_desktop_sftp_tunnel(
        self,
        desktop_id: int,
        user_id: str,
        *,
        tunnel_id: str,
    ) -> Optional[UserDesktop]:
        """Record the relay id of this device's raw-TCP SFTP tunnel.

        Per-device (not per-link): one SFTP server, one tunnel. Stored so the
        Console can deregister the tunnel from the relay when the desktop is
        deleted, instead of leaking its allocated port.
        """
        desktop = self.get_by_id(desktop_id, user_id)
        if not desktop:
            return None
        desktop.sftp_tunnel_id = tunnel_id
        self.session.flush()
        return desktop

    # ── Per-user assistant <-> desktop links ─────────────────────────────

    def list_assigned_assistant_ids(self, desktop_id: int) -> List[int]:
        """Return agent_ids of every assistant this desktop is linked to."""
        stmt = select(AssistantUserDesktop.assistant_id).where(
            AssistantUserDesktop.user_desktop_id == desktop_id,
        )
        return list(self.session.execute(stmt).scalars().all())

    def link(
        self,
        assistant_id: int,
        desktop_id: int,
        requesting_user_id: str,
        filesys_sync: bool = False,
    ) -> Optional[AssistantUserDesktop]:
        """Link the caller's own desktop to an assistant.

        Returns ``None`` when the desktop does not exist or is not owned by the
        requesting user.  Replaces any existing desktop the user has linked to
        this assistant (one machine per user per assistant).
        """
        desktop = self.get_by_id(desktop_id, requesting_user_id)
        if not desktop:
            return None

        existing = self.session.execute(
            select(AssistantUserDesktop).where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == requesting_user_id,
            ),
        ).scalar_one_or_none()
        if existing is not None:
            existing.user_desktop_id = desktop_id
            existing.filesys_sync = filesys_sync
            self._reconcile_filesync_key(existing)
            self.session.flush()
            return existing

        link = AssistantUserDesktop(
            assistant_id=assistant_id,
            user_desktop_id=desktop_id,
            owner_user_id=requesting_user_id,
            filesys_sync=filesys_sync,
        )
        self._reconcile_filesync_key(link)
        self.session.add(link)
        self.session.flush()
        return link

    @staticmethod
    def _reconcile_filesync_key(link: AssistantUserDesktop) -> None:
        """Mint a per-link key when sync is enabled; clear all SFTP state when off.

        The keypair is the CM's client identity for this user's home. Enabling
        sync mints one (if absent); disabling drops the key and the tunnel
        coordinates so a stale device can no longer be reached.
        """
        if link.filesys_sync:
            if not link.filesync_sshkey:
                from orchestra.web.api.desktop.keys import generate_filesync_keypair

                link.filesync_sshkey, _ = generate_filesync_keypair()
        else:
            link.filesync_sshkey = None
            link.sftp_tunnel_host = None
            link.sftp_tunnel_port = None

    def get_link_pubkey(self, assistant_id: int, user_id: str) -> Optional[str]:
        """Return the OpenSSH public key for the caller's link.

        Returns ``None`` when no link exists or filesystem sync is disabled.
        Generates the keypair on first read if sync is enabled but no key has
        been minted yet.
        """
        link = self.session.execute(
            select(AssistantUserDesktop).where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == user_id,
            ),
        ).scalar_one_or_none()
        if link is None or not link.filesys_sync:
            return None
        from orchestra.web.api.desktop.keys import (
            generate_filesync_keypair,
            public_from_private,
        )

        if not link.filesync_sshkey:
            link.filesync_sshkey, public = generate_filesync_keypair()
            self.session.flush()
            return public
        return public_from_private(link.filesync_sshkey)

    def set_sftp_tunnel(
        self,
        assistant_id: int,
        user_id: str,
        host: str,
        port: int,
    ) -> Optional[AssistantUserDesktop]:
        """Record the public SFTP tunnel coordinates for the caller's link."""
        link = self.session.execute(
            select(AssistantUserDesktop).where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == user_id,
            ),
        ).scalar_one_or_none()
        if link is None:
            return None
        link.sftp_tunnel_host = host
        link.sftp_tunnel_port = port
        self.session.flush()
        return link

    def unlink(self, assistant_id: int, requesting_user_id: str) -> bool:
        """Remove the caller's own desktop link from an assistant."""
        link = self.session.execute(
            select(AssistantUserDesktop).where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == requesting_user_id,
            ),
        ).scalar_one_or_none()
        if link is None:
            return False
        self.session.delete(link)
        self.session.flush()
        return True

    def get_link_for_user(
        self,
        assistant_id: int,
        user_id: str,
    ) -> Optional[Tuple[AssistantUserDesktop, UserDesktop]]:
        """Return the (link, desktop) a user has connected to an assistant."""
        stmt = (
            select(AssistantUserDesktop, UserDesktop)
            .join(UserDesktop, UserDesktop.id == AssistantUserDesktop.user_desktop_id)
            .where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == user_id,
            )
        )
        row = self.session.execute(stmt).first()
        if row is None:
            return None
        return (row[0], row[1])

    def list_links_for_assistant(
        self,
        assistant_id: int,
    ) -> List[Tuple[AssistantUserDesktop, UserDesktop]]:
        """Return every (link, desktop) connected to an assistant.

        Used to build the runtime payload that maps each user to their own
        machine for an assistant that several users share.
        """
        stmt = (
            select(AssistantUserDesktop, UserDesktop)
            .join(UserDesktop, UserDesktop.id == AssistantUserDesktop.user_desktop_id)
            .where(AssistantUserDesktop.assistant_id == assistant_id)
        )
        return [(row[0], row[1]) for row in self.session.execute(stmt).all()]
