"""Async version of tab_dao for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Tab
from orchestra.db.utils import get_next_order_value


class AsyncTabDAO:
    """Async Data Access Object for Tab entity."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_tab(
        self,
        interface_id: str,
        name: str,
        visible: bool = True,
        active: bool = False,
        order: Optional[int] = None,
        tab_id: Optional[str] = None,
        context: Optional[str] = None,
        color: Optional[str] = None,
        icon: Optional[str] = "tab",
        is_checkpoint: bool = False,
        checkpoint_or_active_id: Optional[str] = None,
    ) -> Tab:
        """Create a new tab in an interface."""
        # Determine order position
        where_conditions = [Tab.interface_id == interface_id]
        if is_checkpoint is not None:
            where_conditions.append(Tab.is_checkpoint == is_checkpoint)

        order_value = get_next_order_value(
            session=self.session,
            model_class=Tab,
            order=order,
            where_conditions=where_conditions,
        )

        tab = Tab(
            id=tab_id,
            interface_id=interface_id,
            name=name,
            visible=visible,
            active=active,
            order=order_value,
            context=context,
            color=color,
            icon=icon,
            is_checkpoint=is_checkpoint,
            checkpoint_or_active_id=checkpoint_or_active_id,
        )
        self.session.add(tab)
        await self.session.commit()
        return tab

    async def _get_tab(
        self,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tab]:
        """Internal method to get tab by ID or by interface_id and name."""
        if id is not None:
            query = select(Tab).where(Tab.id == str(id))
        elif interface_id is not None and name is not None:
            query = select(Tab).where(
                Tab.interface_id == str(interface_id),
                Tab.name == name,
            )
        else:
            return None

        if is_checkpoint is not None:
            query = query.where(Tab.is_checkpoint == is_checkpoint)

        return (await self.session.execute(query)).scalars().first()

    async def get(
        self,
        id: str,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tab]:
        """Get tab by ID."""
        return self._get_tab(id=id, is_checkpoint=is_checkpoint)

    async def get_by_interface_and_name(
        self,
        interface_id: str,
        name: str,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tab]:
        """Get tab by interface ID and name."""
        return self._get_tab(
            interface_id=interface_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

    async def list_tabs(
        self,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> List[Tab]:
        """List tabs with optional filtering."""
        query = select(Tab)
        if interface_id is not None:
            query = query.where(Tab.interface_id == interface_id)
        if name is not None:
            query = query.where(Tab.name == name)
        if is_checkpoint is not None:
            query = query.where(Tab.is_checkpoint == is_checkpoint)

        query = query.order_by(Tab.order.asc())
        return (await self.session.execute(query)).scalars().all()

    async def list_tabs_bulk(
        self,
        interface_ids: List[str],
        is_checkpoint: Optional[bool] = False,
    ) -> List[Tab]:
        """
        Get tabs for multiple interfaces in a single query to avoid N+1 problem.

        Args:
            interface_ids: List of interface IDs to get tabs for
            is_checkpoint: Filter by checkpoint status

        Returns:
            List of tabs ordered by interface_id, then by order
        """
        if not interface_ids:
            return []

        query = select(Tab).where(Tab.interface_id.in_(interface_ids))
        if is_checkpoint is not None:
            query = query.where(Tab.is_checkpoint == is_checkpoint)

        query = query.order_by(Tab.interface_id.asc(), Tab.order.asc())
        return (await self.session.execute(query)).scalars().all()

    async def update_tab(
        self,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        visible: Optional[bool] = None,
        active: Optional[bool] = None,
        order: Optional[int] = None,
        context: Optional[str] = None,
        color: Optional[str] = None,
        icon: Optional[str] = None,
    ) -> Optional[Tab]:
        """
        Update tab by ID or by interface_id and name.

        Either id or (interface_id and name) must be provided to identify the tab.
        Other parameters are optional updates to apply.
        """
        tab = self._get_tab(
            id=id,
            interface_id=interface_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if tab is None:
            return None

        # Only update name if we identified by ID
        if name is not None and id is not None:
            tab.name = name
        if visible is not None:
            tab.visible = visible
        if active is not None:
            # If we're making the current tab active, we need to deactivate all other tabs in the interface
            if active:
                self.set_active_tab(
                    interface_id=tab.interface_id,
                    tab_id=tab.id,
                    is_checkpoint=tab.is_checkpoint,
                )
            else:
                tab.active = active
        if order is not None:
            tab.order = order
        if context is not None:
            tab.context = context
        if color is not None:
            tab.color = color
        if icon is not None:
            tab.icon = icon
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            tab.is_checkpoint = is_checkpoint

        await self.session.commit()
        return tab

    async def delete_tab(
        self,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> bool:
        """
        Delete tab by ID or by interface_id and name.

        Either id or (interface_id and name) must be provided.
        """
        tab = self._get_tab(
            id=id,
            interface_id=interface_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if tab is None:
            return False

        await self.session.delete(tab)
        await self.session.commit()
        return True

    async def set_active_tab(
        self,
        interface_id: str,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> bool:
        """
        Set a tab as active and deactivate all other tabs in the interface.
        Also updates the interface's active_tab_id.

        Either tab_id or name must be provided to identify the tab.
        """
        # Get the tab if only name was provided
        identified_tab = None
        if tab_id is None and name is not None:
            identified_tab = self._get_tab(
                interface_id=interface_id,
                name=name,
                is_checkpoint=is_checkpoint,
            )
            if identified_tab is None:
                return False
            tab_id = identified_tab.id

        # Get all tabs for the interface
        tabs = self.list_tabs(interface_id=interface_id, is_checkpoint=is_checkpoint)

        # Check if the tab exists
        tab_exists = False
        for tab in tabs:
            if tab.id == tab_id:
                tab_exists = True
                tab.active = True
            else:
                tab.active = False

        if not tab_exists:
            return False

        # Update the interface's active_tab_id
        from orchestra.db.dao.interface_dao import InterfaceDAO

        interface_dao = InterfaceDAO(self.session)
        interface = interface_dao.update_interface(
            id=interface_id,
            active_tab_id=tab_id,
        )

        await self.session.commit()
        return interface is not None

    async def patch_tab(
        self,
        update_data: dict,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tab]:
        """
        Partially update tab with only the fields that need changing.

        Either id or (interface_id and name) must be provided to identify the tab.
        """
        tab = self._get_tab(
            id=id,
            interface_id=interface_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if tab is None:
            return None

        # Update only the fields specified in update_data
        for field, value in update_data.items():
            if hasattr(tab, field):
                setattr(tab, field, value)

        await self.session.commit()
        return tab

    async def patch_tab_by_name(
        self,
        interface_id: str,
        name: str,
        update_data: dict,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tab]:
        """Partially update tab by interface ID and name."""
        # Get the tab by name
        tab = self.get_by_interface_and_name(
            interface_id=interface_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if tab is None:
            return None

        # Update only the fields specified in update_data
        for field, value in update_data.items():
            if hasattr(tab, field):
                setattr(tab, field, value)

        await self.session.commit()
        return tab

    async def checkpoint_tab(
        self,
        tab_id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        target_interface_id: Optional[str] = None,
    ) -> Optional[Tab]:
        """
        Create or update a checkpoint of a tab.

        This method handles the complete process of checkpointing a tab,
        including creating or updating the checkpoint tab and setting the
        checkpoint references.

        Args:
            tab_id: The ID of the source tab to checkpoint
            interface_id: The interface ID of the source tab
            name: The name of the source tab
            target_interface_id: Optional target interface ID if the checkpoint should
                                 be created in a different interface

        Returns:
            The checkpoint tab if successful, None otherwise

        Raises:
            ValueError: If neither tab_id nor (interface_id and name) are provided
        """
        if not tab_id and not (interface_id and name):
            raise ValueError(
                "Either tab_id or both interface_id and name must be provided",
            )

        # Get the source tab
        source_tab = self._get_tab(
            id=tab_id,
            interface_id=interface_id,
            name=name,
            is_checkpoint=False,
        )

        if not source_tab:
            return None

        # Determine the target interface_id (where to create the checkpoint)
        effective_interface_id = (
            target_interface_id if target_interface_id else source_tab.interface_id
        )

        # Check if a checkpoint already exists
        existing_checkpoint = (
            None
            if not source_tab.checkpoint_or_active_id
            else self._get_tab(
                id=source_tab.checkpoint_or_active_id,
                is_checkpoint=True,
            )
        )

        # If checkpoint exists, update it
        if existing_checkpoint:
            updated = self.update_tab(
                id=existing_checkpoint.id,
                name=source_tab.name,
                visible=source_tab.visible,
                active=source_tab.active,
                order=source_tab.order,
                context=source_tab.context,
                color=source_tab.color,
                is_checkpoint=True,
            )

            # Update the checkpoint_or_active_id references
            # If not already set on the source tab
            if not source_tab.checkpoint_or_active_id:
                existing_checkpoint.checkpoint_or_active_id = source_tab.id
                await self.session.commit()

                source_tab.checkpoint_or_active_id = existing_checkpoint.id
                await self.session.commit()

        # Otherwise, create a new checkpoint
        else:
            updated = self.create_tab(
                interface_id=effective_interface_id,
                name=source_tab.name,
                visible=source_tab.visible,
                active=source_tab.active,
                order=source_tab.order,
                context=source_tab.context,
                color=source_tab.color,
                is_checkpoint=True,
                checkpoint_or_active_id=source_tab.id,
            )

            # Commit the new tab first
            await self.session.commit()

            source_tab.checkpoint_or_active_id = updated.id
            await self.session.commit()

        # Get the full checkpoint tab
        checkpoint_tab = self.get_by_interface_and_name(
            interface_id=effective_interface_id,
            name=source_tab.name,
            is_checkpoint=True,
        )

        return checkpoint_tab

    async def get_checkpoint(
        self,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Tab]:
        """
        Get the checkpoint version of a tab.

        This method retrieves the checkpoint version of a tab by:
        1. If the provided object is already a checkpoint, it returns it.
        2. If the provided object is an active tab, it finds its checkpoint
           using the checkpoint_or_active_id reference.

        Args:
            id: Optional ID of the tab
            interface_id: Optional interface ID to identify the tab
            name: Optional name to identify the tab

        Returns:
            The checkpoint tab if found, None otherwise
        """
        # First get the tab based on the provided parameters
        tab = self._get_tab(id=id, interface_id=interface_id, name=name)

        if not tab:
            return None

        # If the tab is already a checkpoint, return it
        if tab.is_checkpoint:
            return tab

        # If the tab has a checkpoint reference, get that checkpoint
        if tab.checkpoint_or_active_id:
            checkpoint = self._get_tab(
                id=tab.checkpoint_or_active_id,
                is_checkpoint=True,
            )
            if checkpoint and checkpoint.is_checkpoint:
                return checkpoint

        # If no direct reference, try to find by interface_id and name
        return self._get_tab(
            interface_id=tab.interface_id,
            name=tab.name,
            is_checkpoint=True,
        )

    async def get_current(
        self,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Tab]:
        """
        Get the current (active) version of a tab.

        This method retrieves the current version of a tab by:
        1. If the provided object is already an active tab, it returns it.
        2. If the provided object is a checkpoint, it finds its active version
           using the checkpoint_or_active_id reference.

        Args:
            id: Optional ID of the tab
            interface_id: Optional interface ID to identify the tab
            name: Optional name to identify the tab

        Returns:
            The active tab if found, None otherwise
        """
        # First get the tab based on the provided parameters
        tab = self._get_tab(id=id, interface_id=interface_id, name=name)

        if not tab:
            return None

        # If the tab is already an active tab, return it
        if not tab.is_checkpoint:
            return tab

        # If the tab has an active reference, get that active tab
        if tab.checkpoint_or_active_id:
            active = self.get(id=tab.checkpoint_or_active_id)
            if active and not active.is_checkpoint:
                return active

        # If no direct reference, try to find by interface_id and name
        return self._get_tab(
            interface_id=tab.interface_id,
            name=tab.name,
            is_checkpoint=False,
        )

    async def duplicate_tabs(self, interface_id_map: dict) -> dict:
        """
        Duplicate all tabs for the given interfaces.
        If a tab with the same interface_id and name already exists,
        it will be updated instead of creating a duplicate.

        Args:
            interface_id_map: Mapping of old interface IDs to new interface IDs

        Returns:
            Dictionary with mapping of old tab IDs to new tab IDs and count
        """
        from datetime import datetime, timezone

        import sqlalchemy

        from orchestra.db.dao.interface_dao import InterfaceDAO
        from orchestra.db.models.orchestra_models import Tab

        interface_dao = InterfaceDAO(self.session)
        tab_id_map = {}
        total_count = 0

        # Process each interface
        for old_interface_id, new_interface_id in interface_id_map.items():
            # Get tabs for the old interface
            tabs = self.list_tabs(interface_id=old_interface_id, is_checkpoint=False)

            if tabs:
                tab_values = []
                old_tab_ids = []
                existing_tab_map = {}

                for tab in tabs:
                    # Check if a tab with the same name already exists in the target interface
                    existing_tab = self.get_by_interface_and_name(
                        interface_id=new_interface_id,
                        name=tab.name,
                        is_checkpoint=False,
                    )

                    if existing_tab:
                        # If tab already exists, store it for updating later
                        existing_tab_map[tab.id] = existing_tab
                        tab_id_map[tab.id] = existing_tab.id
                        total_count += 1
                    else:
                        # If tab doesn't exist, add to list for bulk insert
                        old_tab_ids.append(tab.id)
                        tab_values.append(
                            {
                                "interface_id": new_interface_id,
                                "name": tab.name,
                                "visible": tab.visible,
                                "active": tab.active,
                                "order": tab.order,
                                "context": tab.context,
                                "color": tab.color,
                                "is_checkpoint": tab.is_checkpoint,
                                "checkpoint_or_active_id": None,  # Will be updated if needed
                                "created_at": datetime.now(timezone.utc),
                                "updated_at": datetime.now(timezone.utc),
                            },
                        )

                # Update existing tabs
                for old_id, existing_tab in existing_tab_map.items():
                    source_tab = next((t for t in tabs if t.id == old_id), None)
                    if source_tab:
                        # Update existing tab with data from source
                        self.update_tab(
                            id=existing_tab.id,
                            visible=source_tab.visible,
                            active=source_tab.active,
                            order=source_tab.order,
                            context=source_tab.context,
                            color=source_tab.color,
                            is_checkpoint=source_tab.is_checkpoint,
                        )

                # Bulk insert new tabs
                if tab_values:
                    # Bulk insert tabs and get back the new IDs
                    stmt = sqlalchemy.insert(Tab).values(tab_values).returning(Tab.id)
                    result = await self.session.execute(stmt)
                    new_tab_ids = [row[0] for row in result]

                    # Build the tab ID mapping for newly created tabs
                    for i, old_id in enumerate(old_tab_ids):
                        tab_id_map[old_id] = new_tab_ids[i]

                    total_count += len(tab_values)

                # Update the interface's active_tab_id if needed
                source_interface = interface_dao.get(old_interface_id)
                if source_interface and source_interface.active_tab_id:
                    if source_interface.active_tab_id in tab_id_map:
                        # Update the new interface with the corresponding new active tab ID
                        interface_dao.update_interface(
                            id=new_interface_id,
                            active_tab_id=tab_id_map[source_interface.active_tab_id],
                        )

        return {
            "id_map": tab_id_map,
            "count": total_count,
        }
