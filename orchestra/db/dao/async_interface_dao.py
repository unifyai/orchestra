"""Async version of interface_dao for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Interface
from orchestra.db.utils import get_next_order_value


class AsyncInterfaceDAO:
    """Async Data Access Object for Interface entity."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_interface(
        self,
        name: str,
        project_id: int,
        items: str = "[]",
        new_counter: int = 0,
        context: str = None,
        color: str = None,
        icon: str = "folder",
        order: Optional[int] = None,
        active_tab_id: str = None,
        is_checkpoint: bool = False,
        checkpoint_or_active_id: str = None,
    ) -> Interface:
        """Create a new interface."""
        # Determine order value (append to end)
        where_conditions = [Interface.project_id == project_id]
        if is_checkpoint is not None:
            where_conditions.append(Interface.is_checkpoint == is_checkpoint)

        order_value = get_next_order_value(
            session=self.session,
            model_class=Interface,
            order=order,
            where_conditions=where_conditions,
        )

        interface = Interface(
            name=name,
            items=items,
            new_counter=new_counter,
            project_id=project_id,
            context=context,
            color=color,
            icon=icon,
            order=order_value,
            active_tab_id=active_tab_id,
            is_checkpoint=is_checkpoint,
            checkpoint_or_active_id=checkpoint_or_active_id,
        )
        self.session.add(interface)
        await self.session.commit()
        return interface

    async def _get_interface(
        self,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Interface]:
        """Internal method to get interface by ID or by project_id and name."""
        if id is not None:
            query = select(Interface).where(Interface.id == str(id))
        elif project_id is not None and name is not None:
            query = select(Interface).where(
                Interface.project_id == project_id,
                Interface.name == name,
            )
        else:
            return None

        if is_checkpoint is not None:
            query = query.where(Interface.is_checkpoint == is_checkpoint)

        return await self.session.execute(query).scalars().first()

    async def get(
        self,
        id: str,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Interface]:
        """Get interface by ID."""
        return self._get_interface(id=id, is_checkpoint=is_checkpoint)

    async def get_by_project_and_name(
        self,
        project_id: int,
        name: str,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Interface]:
        """Get interface by project ID and name."""
        return self._get_interface(
            project_id=project_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

    async def get_interfaces(
        self,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> List[Interface]:
        """List interfaces with optional filtering."""
        query = select(Interface)
        if project_id is not None:
            query = query.where(Interface.project_id == project_id)
        if name is not None:
            query = query.where(Interface.name == name)
        if is_checkpoint is not None:
            query = query.where(Interface.is_checkpoint == is_checkpoint)

        query = query.order_by(Interface.created_at.asc())
        interfaces = await self.session.execute(query).scalars().all()
        return interfaces

    async def get_interfaces_bulk(
        self,
        project_ids: List[int],
        is_checkpoint: Optional[bool] = False,
    ) -> List[Interface]:
        """
        Get interfaces for multiple projects in a single query to avoid N+1 problem.

        Args:
            project_ids: List of project IDs to get interfaces for
            is_checkpoint: Filter by checkpoint status

        Returns:
            List of interfaces ordered by project_id, then by order
        """
        if not project_ids:
            return []

        query = select(Interface).where(Interface.project_id.in_(project_ids))
        if is_checkpoint is not None:
            query = query.where(Interface.is_checkpoint == is_checkpoint)

        query = query.order_by(Interface.project_id.asc(), Interface.order.asc())
        return await self.session.execute(query).scalars().all()

    async def update_interface(
        self,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        items: Optional[str] = None,
        new_counter: Optional[int] = None,
        context: Optional[str] = None,
        color: Optional[str] = None,
        icon: Optional[str] = None,
        active_tab_id: Optional[str] = None,
        order: Optional[int] = None,
    ) -> Optional[Interface]:
        """
        Update interface by ID or by project_id and name.

        Either id or (project_id and name) must be provided to identify the interface.
        Other parameters are optional updates to apply.
        """
        interface = self._get_interface(
            id=id,
            project_id=project_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if interface is None:
            return None

        # These are the fields we might want to update
        if (
            name is not None and id is not None
        ):  # Only update name if we identified by ID
            interface.name = name
        if items is not None:
            interface.items = items
        if new_counter is not None:
            interface.new_counter = new_counter
        if (
            project_id is not None and id is not None
        ):  # Only update project_id if we identified by ID
            interface.project_id = project_id
        if context is not None:
            interface.context = context
        if color is not None:
            interface.color = color
        if icon is not None:
            interface.icon = icon
        if active_tab_id is not None:
            interface.active_tab_id = active_tab_id
        if order is not None:
            interface.order = order
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            # (to avoid overriding existing checkpoints)
            interface.is_checkpoint = is_checkpoint

        await self.session.commit()
        return interface

    async def delete_interface(
        self,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> bool:
        """
        Delete interface by ID or by project_id and name.

        Either id or (project_id and name) must be provided.
        """
        interface = self._get_interface(
            id=id,
            project_id=project_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if interface is None:
            return False

        await self.session.delete(interface)
        await self.session.commit()
        return True

    async def make_checkpoint(
        self,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Optional[Interface]:
        """
        Mark an interface as a checkpoint (manual save) by ID or by project_id and name.

        Either id or (project_id and name) must be provided.
        """
        return self.update_interface(
            id=id,
            project_id=project_id,
            name=name,
            is_checkpoint=True,
        )

    async def get_latest_checkpoint(
        self,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Optional[Interface]:
        """Get the latest manually saved checkpoint for an interface."""
        if id is not None:
            query = (
                select(Interface)
                .where(Interface.id == id, Interface.is_checkpoint == True)
                .order_by(Interface.updated_at.desc())
            )
        elif project_id is not None and name is not None:
            query = (
                select(Interface)
                .where(
                    Interface.project_id == project_id,
                    Interface.name == name,
                    Interface.is_checkpoint == True,
                )
                .order_by(Interface.updated_at.desc())
            )
        else:
            return None
        return await self.session.execute(query).scalars().first()

    async def get_checkpoint(
        self,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Optional[Interface]:
        """
        Get the checkpoint version of an interface.

        This method retrieves the checkpoint version of an interface by:
        1. If the provided object is already a checkpoint, it returns it.
        2. If the provided object is an active interface, it finds its checkpoint
           using the checkpoint_or_active_id reference.

        Args:
            id: Optional ID of the interface
            project_id: Optional project ID to identify the interface
            name: Optional name to identify the interface

        Returns:
            The checkpoint interface if found, None otherwise
        """
        # First get the interface based on the provided parameters
        interface = self._get_interface(id=id, project_id=project_id, name=name)

        if not interface:
            return None

        # If the interface is already a checkpoint, return it
        if interface.is_checkpoint:
            return interface

        # If the interface has a checkpoint reference, get that checkpoint
        if interface.checkpoint_or_active_id:
            checkpoint = self._get_interface(
                id=interface.checkpoint_or_active_id,
                is_checkpoint=True,
            )
            if checkpoint and checkpoint.is_checkpoint:
                return checkpoint

        # If no direct reference, try to find by project_id and name
        return self._get_interface(
            project_id=interface.project_id,
            name=interface.name,
            is_checkpoint=True,
        )

    async def get_current(
        self,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Optional[Interface]:
        """
        Get the current (active) version of an interface.

        This method retrieves the current version of an interface by:
        1. If the provided object is already an active interface, it returns it.
        2. If the provided object is a checkpoint, it finds its active version
           using the checkpoint_or_active_id reference.

        Args:
            id: Optional ID of the interface
            project_id: Optional project ID to identify the interface
            name: Optional name to identify the interface

        Returns:
            The active interface if found, None otherwise
        """
        # First get the interface based on the provided parameters
        interface = self._get_interface(id=id, project_id=project_id, name=name)

        if not interface:
            return None

        # If the interface is already an active interface, return it
        if not interface.is_checkpoint:
            return interface

        # If the interface has an active reference, get that active interface
        if interface.checkpoint_or_active_id:
            active = self.get(id=interface.checkpoint_or_active_id)
            if active and not active.is_checkpoint:
                return active

        # If no direct reference, try to find by project_id and name
        return self._get_interface(
            project_id=interface.project_id,
            name=interface.name,
            is_checkpoint=False,
        )

    async def patch_interface(
        self,
        update_data: dict,
        id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Interface]:
        """
        Partially update interface with only the fields that need changing.

        Either id or (project_id and name) must be provided to identify the interface.
        """
        interface = self._get_interface(
            id=id,
            project_id=project_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if interface is None:
            return None

        # Update only the fields specified in update_data
        for field, value in update_data.items():
            if hasattr(interface, field):
                setattr(interface, field, value)

        await self.session.commit()
        return interface

    async def checkpoint_interface(
        self,
        interface_id: Optional[str] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Optional[Interface]:
        """
        Create or update a checkpoint of an interface.

        Args:
            interface_id: ID of the interface to checkpoint
            project_id: Project ID, if identifying interface by project and name
            name: Interface name, if identifying interface by project and name

        Returns:
            The checkpoint interface, either newly created or updated
        """
        # Get the source interface first
        source_interface = self._get_interface(
            id=interface_id,
            project_id=project_id,
            name=name,
            is_checkpoint=False,  # Always get the active interface
        )

        if source_interface is None:
            return None

        # Check if a checkpoint already exists
        existing_checkpoint = (
            None
            if not source_interface.checkpoint_or_active_id
            else self.get(
                id=source_interface.checkpoint_or_active_id,
                is_checkpoint=True,
            )
        )

        checkpoint_interface = None

        # If checkpoint exists, update it with the current interface values
        if existing_checkpoint:
            checkpoint_interface = self.update_interface(
                id=str(existing_checkpoint.id),
                name=source_interface.name,
                items=source_interface.items,
                new_counter=source_interface.new_counter,
                context=source_interface.context,
                color=source_interface.color,
                active_tab_id=source_interface.active_tab_id,
                is_checkpoint=True,
            )

            # Set checkpoint references - update one at a time
            if not source_interface.checkpoint_or_active_id:
                # Update and commit in separate transactions to avoid sorting issues
                existing_checkpoint.checkpoint_or_active_id = source_interface.id
                await self.session.commit()

                source_interface.checkpoint_or_active_id = existing_checkpoint.id
                await self.session.commit()
        # Otherwise, create a new checkpoint
        else:
            checkpoint_interface = self.create_interface(
                name=source_interface.name,
                project_id=source_interface.project_id,
                items=source_interface.items,
                new_counter=source_interface.new_counter,
                context=source_interface.context,
                color=source_interface.color,
                active_tab_id=source_interface.active_tab_id,
                is_checkpoint=True,
                checkpoint_or_active_id=str(source_interface.id),
            )

            # Update and commit in separate transactions to avoid sorting issues
            await self.session.commit()  # Commit the new interface first

            source_interface.checkpoint_or_active_id = checkpoint_interface.id
            await self.session.commit()

        return checkpoint_interface

    async def duplicate_interfaces(
        self,
        source_project_id: int,
        target_project_id: int,
    ) -> dict:
        """
        Duplicate all interfaces from one project to another.
        If an interface with the same name already exists in the target project,
        it will be updated instead of creating a duplicate.

        Args:
            source_project_id: ID of the source project
            target_project_id: ID of the target project

        Returns:
            Dictionary with mapping of old interface IDs to new interface IDs and count
        """
        from datetime import datetime, timezone

        import sqlalchemy

        # Get interfaces from source project
        interfaces = self.get_interfaces(
            project_id=source_project_id,
            is_checkpoint=False,
        )

        interface_id_map = {}
        count = 0

        if interfaces:
            interface_values = []
            old_interface_ids = []
            existing_interface_map = {}

            # First, check for existing interfaces with the same name in the target project
            for interface in interfaces:
                # Check if an interface with the same name already exists in the target project
                existing_interface = self.get_by_project_and_name(
                    project_id=target_project_id,
                    name=interface.name,
                    is_checkpoint=False,
                )

                if existing_interface:
                    # If interface already exists, store it for updating later
                    existing_interface_map[interface.id] = existing_interface
                    interface_id_map[interface.id] = existing_interface.id
                    count += 1
                else:
                    # If interface doesn't exist, add to list for bulk insert
                    old_interface_ids.append(interface.id)
                    interface_values.append(
                        {
                            "project_id": target_project_id,
                            "name": interface.name,
                            "items": interface.items,
                            "new_counter": interface.new_counter,
                            "context": interface.context,
                            "color": interface.color,
                            "active_tab_id": None,  # Will be updated after tabs are created
                            "is_checkpoint": interface.is_checkpoint,
                            "checkpoint_or_active_id": None,  # Will be updated if needed
                            "created_at": datetime.now(timezone.utc),
                            "updated_at": datetime.now(timezone.utc),
                        },
                    )

            # Update existing interfaces
            for old_id, existing_interface in existing_interface_map.items():
                source_interface = next((i for i in interfaces if i.id == old_id), None)
                if source_interface:
                    # Update existing interface with data from source
                    self.update_interface(
                        id=existing_interface.id,
                        items=source_interface.items,
                        new_counter=source_interface.new_counter,
                        context=source_interface.context,
                        color=source_interface.color,
                        is_checkpoint=source_interface.is_checkpoint,
                        # Don't update active_tab_id yet - will be done after tabs are processed
                    )

            # Bulk insert new interfaces
            if interface_values:
                # Bulk insert interfaces and get back the new IDs
                stmt = (
                    sqlalchemy.insert(Interface)
                    .values(interface_values)
                    .returning(Interface.id)
                )
                result = await self.session.execute(stmt)
                new_interface_ids = [row[0] for row in result]

                # Build the interface ID mapping for newly created interfaces
                for i, old_id in enumerate(old_interface_ids):
                    interface_id_map[old_id] = new_interface_ids[i]

                count += len(interface_values)

        return {
            "id_map": interface_id_map,
            "count": count,
        }
