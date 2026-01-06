"""Async version of tile_dao for use with AsyncSession."""

import json
from typing import List, Optional, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import (
    EditorTile,
    PlotTile,
    TableTile,
    TerminalTile,
    Tile,
    ViewTile,
)


class AsyncTileDAO:
    """Async Data Access Object for Tile entity."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_tile(
        self,
        tab_id: str,
        type: str,
        name: str,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 4,
        height: float = 4,
        tile_id: Optional[str] = None,
        minW: Optional[float] = None,
        minH: Optional[float] = None,
        visible: Optional[bool] = True,
        locked: Optional[bool] = False,
        moved: Optional[bool] = False,
        static: Optional[bool] = False,
        color: Optional[str] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        column_context: Optional[str] = None,
        grouping: Optional[str] = None,
        is_checkpoint: bool = False,
        checkpoint_or_active_id: Optional[str] = None,
        # Specialized tile data parameters
        table_tile: Optional[dict] = None,
        plot_tile: Optional[dict] = None,
        view_tile: Optional[dict] = None,
        editor_tile: Optional[dict] = None,
        terminal_tile: Optional[dict] = None,
    ) -> Tile:
        """
        Create a new tile in a tab.

        Args:
            tab_id: The ID of the tab to create the tile in
            type: The type of tile (Table, Plot, View, Editor)
            name: The name of the tile
            x_position: The x position of the tile
            y_position: The y position of the tile
            width: The width of the tile
            height: The height of the tile
            minW: The minimum width of the tile
            minH: The minimum height of the tile
            visible: Whether the tile is visible
            locked: Whether the tile is locked
            moved: Whether the tile has been moved
            static: Whether the tile is static
            color: The color of the tile
            context: Optional context data for the tile
            table: Optional table data for the tile
            auto_update: Optional auto-update setting
            freeze: Optional freeze setting
            filters: Optional filters
            common_filter: Optional common filter
            metric: Optional metric data
            column_context: Optional column context data
            grouping: Optional grouping data
            is_checkpoint: Whether this is a checkpoint tile
            table_tile: Optional specialized data for Table tile
            plot_tile: Optional specialized data for Plot tile
            view_tile: Optional specialized data for View tile
            editor_tile: Optional specialized data for Editor tile
            terminal_tile: Optional specialized data for Terminal tile

        Returns:
            The created tile

        Raises:
            ValueError: If required parameters are missing or invalid
        """
        if not tab_id and not name:
            raise ValueError("tab_id and name are required")

        # Check if tile already exists
        existing = self.get_by_tab_and_name(
            tab_id=tab_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )
        if existing:
            raise ValueError(f"Tile with name {name} already exists in tab {tab_id}")
        # Validate tile type if provided
        valid_types = ["Table", "Plot", "View", "Editor", "Terminal"]
        if type and type not in valid_types:
            raise ValueError(
                f"Invalid tile type '{type}'. Must be one of {', '.join(valid_types)}",
            )

        # Create base tile
        tile = Tile(
            id=tile_id,
            tab_id=tab_id,
            type=type,
            name=name,
            x_position=x_position,
            y_position=y_position,
            width=width,
            height=height,
            minW=minW,
            minH=minH,
            visible=visible,
            locked=locked,
            moved=moved,
            static=static,
            color=color,
            context=context,
            table=table,
            auto_update=auto_update,
            freeze=freeze,
            filters=filters,
            common_filter=common_filter,
            metric=metric,
            column_context=column_context,
            grouping=grouping,
            is_checkpoint=is_checkpoint,
            checkpoint_or_active_id=checkpoint_or_active_id,
        )
        self.session.add(tile)
        # Commit to get the tile ID
        await self.session.commit()

        # Create specialized tile based on type if needed
        if type and type in valid_types:
            # First, check if a specialized tile already exists with this tile_id
            # and delete it to avoid constraint violations
            self._ensure_no_specialized_tile_exists(tile.id, type)

            # Extract the specialized data dict based on the type
            specialized_data = None
            if type == "Table" and table_tile:
                specialized_data = table_tile.copy()
                # Remove id and tile_id if they exist in specialized data
                specialized_data.pop("id", None)
                specialized_data.pop("tile_id", None)

                table_obj = TableTile(tile_id=tile.id, **(specialized_data or {}))
                self.session.add(table_obj)
                tile.table_tile = table_obj

            elif type == "Plot" and plot_tile:
                specialized_data = plot_tile.copy()
                # Remove id and tile_id if they exist in specialized data
                specialized_data.pop("id", None)
                specialized_data.pop("tile_id", None)

                plot_obj = PlotTile(tile_id=tile.id, **(specialized_data or {}))
                self.session.add(plot_obj)
                tile.plot_tile = plot_obj

            elif type == "View" and view_tile:
                specialized_data = view_tile.copy()
                # Remove id and tile_id if they exist in specialized data
                specialized_data.pop("id", None)
                specialized_data.pop("tile_id", None)

                view_obj = ViewTile(tile_id=tile.id, **(specialized_data or {}))
                self.session.add(view_obj)
                tile.view_tile = view_obj

            elif type == "Editor" and editor_tile:
                specialized_data = editor_tile.copy()
                # Remove id and tile_id if they exist in specialized data
                specialized_data.pop("id", None)
                specialized_data.pop("tile_id", None)

                editor_obj = EditorTile(tile_id=tile.id, **(specialized_data or {}))
                self.session.add(editor_obj)
                tile.editor_tile = editor_obj

            elif type == "Terminal" and terminal_tile:
                specialized_data = terminal_tile.copy()
                # Remove id and tile_id if they exist in specialized data
                specialized_data.pop("id", None)
                specialized_data.pop("tile_id", None)

                terminal_obj = TerminalTile(tile_id=tile.id, **(specialized_data or {}))
                self.session.add(terminal_obj)
                tile.terminal_tile = terminal_obj

            # If no specialized data was provided but a type was, create a default specialized tile
            elif type == "Table":
                print(f"Creating table tile")
                table_obj = TableTile(tile_id=tile.id)
                self.session.add(table_obj)
                tile.table_tile = table_obj
            elif type == "Plot":
                print(f"Creating plot tile")
                plot_obj = PlotTile(tile_id=tile.id)
                self.session.add(plot_obj)
                tile.plot_tile = plot_obj
            elif type == "View":
                print(f"Creating view tile")
                view_obj = ViewTile(tile_id=tile.id)
                self.session.add(view_obj)
                tile.view_tile = view_obj
            elif type == "Editor":
                print(f"Creating editor tile")
                editor_obj = EditorTile(tile_id=tile.id, content="")
                self.session.add(editor_obj)
                tile.editor_tile = editor_obj
            elif type == "Terminal":
                print(f"Creating terminal tile")
                terminal_obj = TerminalTile(tile_id=tile.id)
                self.session.add(terminal_obj)
                tile.terminal_tile = terminal_obj

            # Commit the specialized tile
            await self.session.commit()

        return tile

    async def _ensure_no_specialized_tile_exists(
        self,
        tile_id: str,
        tile_type: str,
    ) -> None:
        """
        Ensure no specialized tile exists for the given tile_id and type.
        This helps prevent unique constraint violations.

        Args:
            tile_id: The ID of the parent tile
            tile_type: The type of specialized tile to check for
        """
        if tile_type == "Table":
            existing = (
                self.session.query(TableTile)
                .filter(TableTile.tile_id == tile_id)
                .first()
            )
            if existing:
                print(f"Existing table tile: {existing}")
                await self.session.delete(existing)
        elif tile_type == "Plot":
            existing = (
                (
                    await self.session.execute(
                        select(PlotTile).where(PlotTile.tile_id == tile_id),
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                print(f"Existing plot tile: {existing}")
                await self.session.delete(existing)
        elif tile_type == "View":
            existing = (
                (
                    await self.session.execute(
                        select(ViewTile).where(ViewTile.tile_id == tile_id),
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                print(f"Existing view tile: {existing}")
                await self.session.delete(existing)
        elif tile_type == "Editor":
            existing = (
                self.session.query(EditorTile)
                .filter(EditorTile.tile_id == tile_id)
                .first()
            )
            if existing:
                print(f"Existing editor tile: {existing}")
                await self.session.delete(existing)
        elif tile_type == "Terminal":
            existing = (
                self.session.query(TerminalTile)
                .filter(TerminalTile.tile_id == tile_id)
                .first()
            )

    async def _handle_type_change(self, tile: Tile, new_type: str) -> None:
        """Handle logic when a tile's type is updated.

        This helper validates the new type, ensures the corresponding
        specialized tile relationship exists (creating a default one if
        necessary) and removes any previously attached specialized tile
        objects that no longer match the new type.
        """
        valid_types = ["Table", "Plot", "View", "Editor", "Terminal"]
        if new_type not in valid_types:
            raise ValueError(
                f"Invalid tile type '{new_type}'. Must be one of {', '.join(valid_types)}",
            )

        # Mapping of type -> attribute name and model class
        specialized_map = {
            "Table": ("table_tile", TableTile),
            "Plot": ("plot_tile", PlotTile),
            "View": ("view_tile", ViewTile),
            "Editor": ("editor_tile", EditorTile),
            "Terminal": ("terminal_tile", TerminalTile),
        }

        # Remove specialized tiles that do not match the new type
        for t, (attr, model_cls) in specialized_map.items():
            if t != new_type:
                existing_spec = getattr(tile, attr)
                if existing_spec is not None:
                    # Delete the orphaned specialized tile
                    await self.session.delete(existing_spec)
                    setattr(tile, attr, None)

        # Now handle the specialized object for the new type
        attr, model_cls = specialized_map[new_type]

        # Always delete any existing specialized tile of the new type to avoid conflicts
        # This allows us to completely replace a specialized tile when needed
        self._ensure_no_specialized_tile_exists(tile.id, new_type)

        # Now create a new specialized tile
        spec_obj = model_cls(tile_id=tile.id)
        self.session.add(spec_obj)
        setattr(tile, attr, spec_obj)

        # Finally, set the tile's type
        tile.type = new_type

        # Flush changes to make them visible in the current session
        await self.session.flush()

    async def _get_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tile]:
        """
        Internal method to get tile by ID or by tab_id and name.

        Args:
            id: The ID of the tile
            tab_id: The ID of the tab
            name: The name of the tile
            is_checkpoint: Whether to get a checkpoint tile

        Returns:
            The tile if found, None otherwise
        """
        if id is not None:
            query = select(Tile).where(Tile.id == str(id))
        elif tab_id is not None and name is not None:
            query = select(Tile).where(
                Tile.tab_id == str(tab_id),
                Tile.name == name,
            )
        else:
            return None

        if is_checkpoint is not None:
            query = query.where(Tile.is_checkpoint == is_checkpoint)

        return await self.session.execute(query).scalars().first()

    async def get(
        self,
        id: str,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tile]:
        """
        Get tile by ID.

        Args:
            id: The ID of the tile
            is_checkpoint: Whether to get a checkpoint tile

        Returns:
            The tile if found, None otherwise
        """
        return self._get_tile(id=id, is_checkpoint=is_checkpoint)

    async def get_by_tab_and_name(
        self,
        tab_id: str,
        name: str,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Tile]:
        """
        Get tile by tab ID and name.

        Args:
            tab_id: The ID of the tab
            name: The name of the tile
            is_checkpoint: Whether to get a checkpoint tile

        Returns:
            The tile if found, None otherwise
        """
        return self._get_tile(tab_id=tab_id, name=name, is_checkpoint=is_checkpoint)

    async def list_tiles_by_tab(
        self,
        tab_id: str,
        name: Optional[str] = None,
        type: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> List[Tile]:
        """
        List tiles for a tab with optional filtering.

        Args:
            tab_id: The ID of the tab
            name: Optional name filter
            type: Optional type filter
            is_checkpoint: Whether to list checkpoint tiles

        Returns:
            List of tiles matching the criteria
        """
        query = select(Tile).where(Tile.tab_id == tab_id)

        if name is not None:
            query = query.where(Tile.name == name)
        if type is not None:
            query = query.where(Tile.type == type)
        if is_checkpoint is not None:
            query = query.where(Tile.is_checkpoint == is_checkpoint)

        return await self.session.execute(query).scalars().all()

    async def update_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        type: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        x_position: Optional[float] = None,
        y_position: Optional[float] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
        position: Optional[dict] = None,
        minW: Optional[float] = None,
        minH: Optional[float] = None,
        visible: Optional[bool] = None,
        locked: Optional[bool] = None,
        moved: Optional[bool] = None,
        static: Optional[bool] = None,
        color: Optional[str] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        column_context: Optional[str] = None,
        grouping: Optional[str] = None,
        # Specialized payloads for initializing/updating specialized tiles when type changes
        table_tile: Optional[dict] = None,
        plot_tile: Optional[dict] = None,
        view_tile: Optional[dict] = None,
        editor_tile: Optional[dict] = None,
        terminal_tile: Optional[dict] = None,
    ) -> Optional[Tile]:
        """
        Update tile by ID or by tab_id and name.

        Args:
            id: The ID of the tile
            tab_id: The ID of the tab
            name: The name of the tile
            type: The type of the tile
            is_checkpoint: Whether to update a checkpoint tile
            x_position: New x position
            y_position: New y position
            width: New width
            height: New height
            position: New position as a dict with x, y, width, height
            minW: New minimum width
            minH: New minimum height
            visible: New visibility setting
            locked: New locked setting
            moved: New moved setting
            static: New static setting
            color: New color
            context: New context data
            table: New table data
            auto_update: New auto_update setting
            freeze: New freeze setting
            filters: New filters
            common_filter: New common filter
            metric: New metric data
            column_context: New column context data
            grouping: New grouping data
            table_tile: Specialized payload for Table tile
            plot_tile: Specialized payload for Plot tile
            view_tile: Specialized payload for View tile
            editor_tile: Specialized payload for Editor tile
            terminal_tile: Specialized payload for Terminal tile

        Returns:
            The updated tile if found, None otherwise

        Raises:
            ValueError: If neither id nor (tab_id and name) are provided
        """
        if not id and not (tab_id and name):
            raise ValueError("Either id or both tab_id and name must be provided")

        tile = self._get_tile(
            id=id,
            tab_id=tab_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if tile is None:
            return None

        # Handle position as a dict if provided
        if position:
            position_values = self._position_from_dict(position)
            if "x_position" in position_values:
                tile.x_position = position_values["x_position"]
            if "y_position" in position_values:
                tile.y_position = position_values["y_position"]
            if "width" in position_values:
                tile.width = position_values["width"]
            if "height" in position_values:
                tile.height = position_values["height"]
        else:
            # Or handle individual position parameters
            if x_position is not None:
                tile.x_position = x_position
            if y_position is not None:
                tile.y_position = y_position
            if width is not None:
                tile.width = width
            if height is not None:
                tile.height = height

        # Only update name if we identified by ID
        if name is not None and id is not None:
            tile.name = name
        if type is not None and type != tile.type:
            # Perform type switch (will create default specialized tile)
            self._handle_type_change(tile, type)
        if minW is not None:
            tile.minW = minW
        if minH is not None:
            tile.minH = minH
        if visible is not None:
            tile.visible = visible
        if locked is not None:
            tile.locked = locked
        if moved is not None:
            tile.moved = moved
        if static is not None:
            tile.static = static
        if color is not None:
            tile.color = color
        if context is not None:
            tile.context = context
        if table is not None:
            tile.table = table
        if auto_update is not None:
            tile.auto_update = auto_update
        if freeze is not None:
            tile.freeze = freeze
        if filters is not None:
            tile.filters = filters
        if common_filter is not None:
            tile.common_filter = common_filter
        if metric is not None:
            tile.metric = metric
        if column_context is not None:
            tile.column_context = column_context
        if grouping is not None:
            tile.grouping = grouping
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            tile.is_checkpoint = is_checkpoint

        # If specialized payloads passed, update them accordingly
        if tile.type == "Table" and table_tile is not None:
            if tile.table_tile is not None:
                self.update_table_tile(id=tile.table_tile.id, **table_tile)
            else:
                # If no specialized tile exists, create one first
                self._handle_type_change(tile, "Table")
                # After _handle_type_change, the table_tile should exist
                if tile.table_tile is not None:
                    self.update_table_tile(id=tile.table_tile.id, **table_tile)
        elif tile.type == "Plot" and plot_tile is not None:
            if tile.plot_tile is not None:
                self.update_plot_tile(id=tile.plot_tile.id, **plot_tile)
            else:
                # If no specialized tile exists, create one first
                self._handle_type_change(tile, "Plot")
                # After _handle_type_change, the plot_tile should exist
                if tile.plot_tile is not None:
                    self.update_plot_tile(id=tile.plot_tile.id, **plot_tile)
        elif tile.type == "View" and view_tile is not None:
            if tile.view_tile is not None:
                self.update_view_tile(id=tile.view_tile.id, **view_tile)
            else:
                # If no specialized tile exists, create one first
                self._handle_type_change(tile, "View")
                # After _handle_type_change, the view_tile should exist
                if tile.view_tile is not None:
                    self.update_view_tile(id=tile.view_tile.id, **view_tile)
        elif tile.type == "Editor" and editor_tile is not None:
            if tile.editor_tile is not None:
                self.update_editor_tile(id=tile.editor_tile.id, **editor_tile)
            else:
                # If no specialized tile exists, create one first
                self._handle_type_change(tile, "Editor")
                # After _handle_type_change, the editor_tile should exist
                if tile.editor_tile is not None:
                    self.update_editor_tile(id=tile.editor_tile.id, **editor_tile)
        elif tile.type == "Terminal" and terminal_tile is not None:
            if tile.terminal_tile is not None:
                self.update_terminal_tile(id=tile.terminal_tile.id, **terminal_tile)
            else:
                # If no specialized tile exists, create one first
                self._handle_type_change(tile, "Terminal")
                # After _handle_type_change, the terminal_tile should exist
                if tile.terminal_tile is not None:
                    self.update_terminal_tile(id=tile.terminal_tile.id, **terminal_tile)

        await self.session.commit()
        return tile

    async def delete_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> bool:
        """
        Delete tile by ID or by tab_id and name.

        Args:
            id: The ID of the tile
            tab_id: The ID of the tab
            name: The name of the tile
            is_checkpoint: Whether to delete a checkpoint tile

        Returns:
            True if deleted, False if not found

        Raises:
            ValueError: If neither id nor (tab_id and name) are provided
        """
        if not id and not (tab_id and name):
            raise ValueError("Either id or both tab_id and name must be provided")

        tile = self._get_tile(
            id=id,
            tab_id=tab_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if tile is None:
            return False

        await self.session.delete(tile)
        await self.session.commit()
        return True

    # Specialized tile types
    async def create_table_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 4,
        height: float = 4,
        minW: Optional[float] = None,
        minH: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        color: Optional[str] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        table_type: Optional[str] = None,
        column_context: Optional[str] = None,
        page_number: Optional[str] = None,
        column_order: Optional[str] = None,
        hidden_columns: Optional[str] = None,
        default_hidden_columns: bool = True,
        sorting: Optional[str] = None,
        grouping: Optional[str] = None,
        group_sorting: Optional[str] = None,
        columns_pin_left: Optional[str] = None,
        columns_pin_right: Optional[str] = None,
        selected: Optional[str] = None,
        is_checkpoint: bool = False,
    ) -> TableTile:
        """Create a new table tile."""
        # If no existing tile_id, create a base tile first
        if not tile_id:
            base_tile = self.create_tile(
                tab_id=tab_id,
                name=name,
                type="Table",
                x_position=x_position,
                y_position=y_position,
                width=width,
                height=height,
                minW=minW,
                minH=minH,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                color=color,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                column_context=column_context,
                grouping=grouping,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id

        # Create a new table tile if it doesn't exist
        if getattr(base_tile, "table_tile", None) is None:
            table_tile = TableTile(
                tile_id=tile_id,
                table_type=table_type,
                page_number=page_number,
                column_order=column_order,
                hidden_columns=hidden_columns,
                default_hidden_columns=default_hidden_columns,
                sorting=sorting,
                group_sorting=group_sorting,
                columns_pin_left=columns_pin_left,
                columns_pin_right=columns_pin_right,
                selected=selected,
            )
            self.session.add(table_tile)

        # Now update the base tile
        base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)
        if base_tile:
            base_tile.table_tile = table_tile
        else:
            print(f"Base tile not found for {tile_id}")

        await self.session.commit()
        return table_tile

    async def _get_specialized_tile(
        self,
        model_class,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Union[TableTile, PlotTile, ViewTile, EditorTile, TerminalTile]]:
        """Helper method to get a specialized tile by ID or by tab_id and name."""
        if id is not None:
            # Direct lookup by specialized tile primary key
            query = select(model_class).where(model_class.id == id)
            return await self.session.execute(query).scalars().first()

        # If identifying by tab_id and name, first get the base tile
        if tab_id is not None and name is not None:
            base_tile = self._get_tile(
                tab_id=tab_id,
                name=name,
                is_checkpoint=is_checkpoint,
            )
            if base_tile is None:
                return None

            # Then get the specialized tile using the base tile's tile_id reference
            query = select(model_class).where(model_class.tile_id == base_tile.id)
            return await self.session.execute(query).scalars().first()

        return None

    async def get_table_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[TableTile]:
        """Get table tile by ID or by tab_id and name."""
        return self._get_specialized_tile(TableTile, id, tab_id, name, is_checkpoint)

    async def update_table_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        table_type: Optional[str] = None,
        page_number: Optional[str] = None,
        column_order: Optional[str] = None,
        hidden_columns: Optional[str] = None,
        default_hidden_columns: Optional[bool] = None,
        sorting: Optional[str] = None,
        group_sorting: Optional[str] = None,
        columns_pin_left: Optional[str] = None,
        columns_pin_right: Optional[str] = None,
        selected: Optional[str] = None,
    ) -> Optional[TableTile]:
        """
        Update table tile by ID or by tab_id and name.

        Either id or (tab_id and name) must be provided to identify the tile.
        """
        table_tile = self._get_specialized_tile(
            TableTile,
            id,
            tab_id,
            name,
            is_checkpoint,
        )

        if table_tile is None:
            return None

        # Update specialized fields
        if table_type is not None:
            table_tile.table_type = table_type
        if page_number is not None:
            table_tile.page_number = page_number
        if column_order is not None:
            table_tile.column_order = column_order
        if hidden_columns is not None:
            table_tile.hidden_columns = hidden_columns
        if default_hidden_columns is not None:
            table_tile.default_hidden_columns = default_hidden_columns
        if sorting is not None:
            table_tile.sorting = sorting
        if group_sorting is not None:
            table_tile.group_sorting = group_sorting
        if columns_pin_left is not None:
            table_tile.columns_pin_left = columns_pin_left
        if columns_pin_right is not None:
            table_tile.columns_pin_right = columns_pin_right
        if selected is not None:
            table_tile.selected = selected

        await self.session.commit()
        return table_tile

    async def create_plot_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        minW: Optional[float] = None,
        minH: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        color: Optional[str] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        column_context: Optional[str] = None,
        grouping: Optional[str] = None,
        plot_type: Optional[str] = None,
        plot_scale_x: Optional[str] = None,
        plot_scale_y: Optional[str] = None,
        plot_aggregate: Optional[str] = None,
        x_axis: Optional[str] = None,
        y_axis: Optional[str] = None,
        plot_group_by: Optional[str] = None,
        plot_group_by_colors: Optional[str] = None,
        bin_count: Optional[str] = None,
        regression_line: Optional[str] = None,
        is_checkpoint: bool = False,
    ) -> PlotTile:
        """Create a new plot tile."""
        # If no existing tile_id, create a base tile first
        if not tile_id:
            base_tile = self.create_tile(
                tab_id=tab_id,
                name=name,
                type="Plot",
                x_position=x_position,
                y_position=y_position,
                width=width,
                height=height,
                minW=minW,
                minH=minH,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                color=color,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                column_context=column_context,
                grouping=grouping,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id

        # Create a new table tile if it doesn't exist
        if getattr(base_tile, "plot_tile", None) is None:
            plot_tile = PlotTile(
                tile_id=tile_id,
                plot_type=plot_type,
                plot_scale_x=plot_scale_x,
                plot_scale_y=plot_scale_y,
                plot_aggregate=plot_aggregate,
                x_axis=x_axis,
                y_axis=y_axis,
                plot_group_by=plot_group_by,
                plot_group_by_colors=plot_group_by_colors,
                bin_count=bin_count,
                regression_line=regression_line,
            )
            self.session.add(plot_tile)

        # Now update the base tile
        base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)
        if base_tile:
            base_tile.plot_tile = plot_tile
        else:
            print(f"Base tile not found for {tile_id}")

        await self.session.commit()

        return plot_tile

    async def get_plot_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[PlotTile]:
        """Get plot tile by ID or by tab_id and name."""
        return self._get_specialized_tile(PlotTile, id, tab_id, name, is_checkpoint)

    async def update_plot_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        plot_type: Optional[str] = None,
        plot_scale_x: Optional[str] = None,
        plot_scale_y: Optional[str] = None,
        plot_aggregate: Optional[str] = None,
        x_axis: Optional[str] = None,
        y_axis: Optional[str] = None,
        plot_group_by: Optional[str] = None,
        plot_group_by_colors: Optional[str] = None,
        bin_count: Optional[str] = None,
        regression_line: Optional[str] = None,
    ) -> Optional[PlotTile]:
        """
        Update plot tile by ID or by tab_id and name.

        Either id or (tab_id and name) must be provided to identify the tile.
        """
        plot_tile = self._get_specialized_tile(
            PlotTile,
            id,
            tab_id,
            name,
            is_checkpoint,
        )

        if plot_tile is None:
            return None

        # Update specialized fields
        if plot_type is not None:
            plot_tile.plot_type = plot_type
        if plot_scale_x is not None:
            plot_tile.plot_scale_x = plot_scale_x
        if plot_scale_y is not None:
            plot_tile.plot_scale_y = plot_scale_y
        if plot_aggregate is not None:
            plot_tile.plot_aggregate = plot_aggregate
        if x_axis is not None:
            plot_tile.x_axis = x_axis
        if y_axis is not None:
            plot_tile.y_axis = y_axis
        if plot_group_by is not None:
            plot_tile.plot_group_by = plot_group_by
        if plot_group_by_colors is not None:
            plot_tile.plot_group_by_colors = plot_group_by_colors
        if bin_count is not None:
            plot_tile.bin_count = bin_count
        if regression_line is not None:
            plot_tile.regression_line = regression_line

        await self.session.commit()
        return plot_tile

    async def create_editor_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        minW: Optional[float] = None,
        minH: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        color: Optional[str] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        column_context: Optional[str] = None,
        grouping: Optional[str] = None,
        content: str = "",
        file_name: Optional[str] = None,
        file_type: Optional[str] = None,
        is_checkpoint: bool = False,
    ) -> EditorTile:
        """Create a new editor tile."""
        # If no existing tile_id, create a base tile first
        if not tile_id:
            base_tile = self.create_tile(
                tab_id=tab_id,
                name=name,
                type="Editor",
                x_position=x_position,
                y_position=y_position,
                width=width,
                height=height,
                minW=minW,
                minH=minH,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                color=color,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                column_context=column_context,
                grouping=grouping,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id

        # Create a new table tile if it doesn't exist
        if getattr(base_tile, "editor_tile", None) is None:
            editor_tile = EditorTile(
                tile_id=tile_id,
                content=content,
                file_name=file_name,
                file_type=file_type,
            )
            self.session.add(editor_tile)

        # Now update the base tile
        base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)
        if base_tile:
            base_tile.editor_tile = editor_tile
        else:
            print(f"Base tile not found for {tile_id}")

        await self.session.commit()

        return editor_tile

    async def get_editor_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[EditorTile]:
        """Get editor tile by ID or by tab_id and name."""
        return self._get_specialized_tile(EditorTile, id, tab_id, name, is_checkpoint)

    async def update_editor_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        content: Optional[str] = None,
        file_name: Optional[str] = None,
        file_type: Optional[str] = None,
    ) -> Optional[EditorTile]:
        """
        Update editor tile by ID or by tab_id and name.

        Either id or (tab_id and name) must be provided to identify the tile.
        """
        editor_tile = self._get_specialized_tile(
            EditorTile,
            id,
            tab_id,
            name,
            is_checkpoint,
        )

        if editor_tile is None:
            return None

        # Update specialized fields
        if content is not None:
            editor_tile.content = content
        if file_name is not None:
            editor_tile.file_name = file_name
        if file_type is not None:
            editor_tile.file_type = file_type

        await self.session.commit()
        return editor_tile

    async def create_terminal_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        minW: Optional[float] = None,
        minH: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        color: Optional[str] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        column_context: Optional[str] = None,
        grouping: Optional[str] = None,
        shell_type: Optional[str] = None,
        is_checkpoint: bool = False,
    ) -> TerminalTile:
        """Create a new terminal tile."""

        # Ensure a base tile exists
        if not tile_id:
            base_tile = self.create_tile(
                tab_id=tab_id,
                name=name,
                type="Terminal",
                x_position=x_position,
                y_position=y_position,
                width=width,
                height=height,
                minW=minW,
                minH=minH,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                color=color,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                column_context=column_context,
                grouping=grouping,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id
        else:
            base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)

        # Create specialized record if missing
        if getattr(base_tile, "terminal_tile", None) is None:
            term_tile = TerminalTile(tile_id=tile_id, shell_type=shell_type)
            self.session.add(term_tile)
        else:
            term_tile = base_tile.terminal_tile  # type: ignore

        # Attach relationship & commit
        base_tile.terminal_tile = term_tile
        await self.session.commit()
        return term_tile

    async def get_terminal_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[TerminalTile]:
        """Retrieve a terminal tile by id or (tab_id, name)."""
        return self._get_specialized_tile(TerminalTile, id, tab_id, name, is_checkpoint)

    async def update_terminal_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        shell_type: Optional[str] = None,
    ) -> Optional[TerminalTile]:
        """Update terminal tile fields."""
        term_tile = self._get_specialized_tile(
            TerminalTile,
            id,
            tab_id,
            name,
            is_checkpoint,
        )
        if term_tile is None:
            return None
        if shell_type is not None:
            term_tile.shell_type = shell_type
        await self.session.commit()
        return term_tile

    async def create_view_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        minW: Optional[float] = None,
        minH: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        color: Optional[str] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        column_context: Optional[str] = None,
        grouping: Optional[str] = None,
        base_index: Optional[str] = None,
        is_checkpoint: bool = False,
    ) -> ViewTile:
        """Create a new view tile."""
        # If no existing tile_id, create a base tile first
        if not tile_id:
            base_tile = self.create_tile(
                tab_id=tab_id,
                name=name,
                type="View",
                x_position=x_position,
                y_position=y_position,
                width=width,
                height=height,
                minW=minW,
                minH=minH,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                color=color,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                column_context=column_context,
                grouping=grouping,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id

        # Create a new table tile if it doesn't exist
        if getattr(base_tile, "view_tile", None) is None:
            view_tile = ViewTile(
                tile_id=tile_id,
                base_index=base_index,
            )
            self.session.add(view_tile)

        # Now update the base tile
        base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)
        if base_tile:
            base_tile.view_tile = view_tile
        else:
            print(f"Base tile not found for {tile_id}")

        await self.session.commit()

        return view_tile

    async def get_view_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[ViewTile]:
        """Get view tile by ID or by tab_id and name."""
        return self._get_specialized_tile(ViewTile, id, tab_id, name, is_checkpoint)

    async def update_view_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        base_index: Optional[str] = None,
    ) -> Optional[ViewTile]:
        """
        Update view tile by ID or by tab_id and name.

        Either id or (tab_id and name) must be provided to identify the tile.
        """
        view_tile = self._get_specialized_tile(
            ViewTile,
            id,
            tab_id,
            name,
            is_checkpoint,
        )

        if view_tile is None:
            return None

        # Update specialized fields
        if base_index is not None:
            view_tile.base_index = base_index

        await self.session.commit()
        return view_tile

    async def patch_tile(
        self,
        update_data: dict,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        tile_type: Optional[str] = None,
    ) -> Optional[Tile]:
        """
        Partially update tile with only the fields that need changing.

        Args:
            update_data: Dictionary of fields to update
            id: The ID of the tile
            tab_id: The ID of the tab
            name: The name of the tile
            is_checkpoint: Whether to update a checkpoint tile
            tile_type: Type of the tile (Table, Plot, View, Editor)

        Returns:
            The updated tile if found, None otherwise

        Raises:
            ValueError: If neither id nor (tab_id and name) are provided
        """
        if not id and not (tab_id and name):
            raise ValueError("Either id or both tab_id and name must be provided")

        tile = self._get_tile(
            id=id,
            tab_id=tab_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if tile is None:
            return None

        # Extract specialized payloads now but process them later
        specialized_payloads = {
            "table_tile": update_data.pop("table_tile", None),
            "plot_tile": update_data.pop("plot_tile", None),
            "view_tile": update_data.pop("view_tile", None),
            "editor_tile": update_data.pop("editor_tile", None),
            "terminal_tile": update_data.pop("terminal_tile", None),
        }

        # Handle position updates specially
        if "position" in update_data:
            position = update_data.pop("position")
            position_fields = self._position_from_dict(position)
            for field, value in position_fields.items():
                setattr(tile, field, value)

        # Update the base tile fields - translate certain fields if needed
        allowed_fields = [
            "name",
            "type",
            "minW",
            "minH",
            "visible",
            "locked",
            "moved",
            "static",
            "color",
            "context",
            "table",
            "auto_update",
            "freeze",
            "filters",
            "common_filter",
            "metric",
            "column_context",
            "grouping",
        ]

        if "type" in update_data:
            new_type = update_data.pop("type")
            if new_type != tile.type:
                self._handle_type_change(tile, new_type)

        for field in update_data:
            if field in allowed_fields and hasattr(tile, field):
                setattr(tile, field, update_data[field])

        # ------------------------------------------------------------------
        # Finally process specialized tile data (after type / general updates)
        # ------------------------------------------------------------------
        effective_type = tile.type  # type after any change above
        print(f"Effective type: {effective_type}")
        if effective_type is not None:
            payload_key = {
                "Table": "table_tile",
                "Plot": "plot_tile",
                "View": "view_tile",
                "Editor": "editor_tile",
                "Terminal": "terminal_tile",
            }.get(effective_type)

            payload = specialized_payloads.get(payload_key)

            if payload:
                # Map type to updater method and corresponding specialized tile attribute
                updater_map = {
                    "Table": (self.update_table_tile, "table_tile"),
                    "Plot": (self.update_plot_tile, "plot_tile"),
                    "View": (self.update_view_tile, "view_tile"),
                    "Editor": (self.update_editor_tile, "editor_tile"),
                    "Terminal": (self.update_terminal_tile, "terminal_tile"),
                }

                # Convert complex structures to json strings where necessary
                cleaned_payload = {
                    k: (json.dumps(v) if isinstance(v, (list, tuple, dict)) else v)
                    for k, v in payload.items()
                }

                updater_method, attr_name = updater_map[effective_type]
                specialized_tile = getattr(tile, attr_name)

                if specialized_tile is not None:
                    # If specialized tile exists, update it using its ID
                    updater_method(id=specialized_tile.id, **cleaned_payload)
                else:
                    # If no specialized tile exists, create one first
                    self._handle_type_change(tile, effective_type)
                    specialized_tile = getattr(tile, attr_name)
                    if specialized_tile is not None:
                        updater_method(id=specialized_tile.id, **cleaned_payload)

        await self.session.commit()
        return tile

    async def patch_specialized_tile(
        self,
        id: str,
        tile_type: str,
        update_data: dict,
    ) -> Optional[Union[Tile, TableTile, PlotTile, ViewTile, EditorTile, TerminalTile]]:
        """
        Update a specialized tile with specific data for its type.

        Args:
            id: The ID of the tile to update
            tile_type: The type of tile (Table, Plot, View, Editor)
            update_data: The data to update the tile with

        Returns:
            The updated tile if found, None otherwise
        """
        # Get the base tile first
        tile = self.get(id)
        if not tile:
            return None

        # Get the specialized tile based on type
        specialized_tile = None
        if tile_type == "Table":
            specialized_tile = self.get_table_tile(id=tile.table_tile.id)
        elif tile_type == "Plot":
            specialized_tile = self.get_plot_tile(id=tile.plot_tile.id)
        elif tile_type == "View":
            specialized_tile = self.get_view_tile(id=tile.view_tile.id)
        elif tile_type == "Editor":
            specialized_tile = self.get_editor_tile(id=tile.editor_tile.id)
        elif tile_type == "Terminal":
            specialized_tile = self.get_terminal_tile(id=tile.terminal_tile.id)
        else:
            # Invalid tile type
            return None

        if not specialized_tile:
            return None

        # Extract the specialized data
        specialized_data = None
        if tile_type in update_data:
            specialized_data = update_data[tile_type]
        else:
            specialized_data = update_data

        # Update the specialized tile
        if specialized_data:
            for field, value in specialized_data.items():
                if hasattr(specialized_tile, field):
                    # JSON serialize if needed for complex types
                    if isinstance(value, (tuple, list, dict)):
                        setattr(specialized_tile, field, json.dumps(value))
                    else:
                        setattr(specialized_tile, field, value)

        await self.session.commit()
        return tile

    async def _position_from_dict(self, position_dict: Optional[dict]) -> dict:
        """Convert a position dictionary from the schema to model field values.

        Args:
            position_dict: A dictionary with x, y, width, height keys

        Returns:
            A dictionary with x_position, y_position, width, height keys
        """
        result = {}
        if position_dict:
            if "x" in position_dict:
                result["x_position"] = position_dict["x"]
            if "y" in position_dict:
                result["y_position"] = position_dict["y"]
            if "width" in position_dict:
                result["width"] = position_dict["width"]
            if "height" in position_dict:
                result["height"] = position_dict["height"]
        return result

    async def _position_to_dict(self, tile: Tile) -> dict:
        """Convert model position fields to a position dictionary for the schema.

        Args:
            tile: A Tile object

        Returns:
            A dictionary with x, y, width, height keys
        """
        return {
            "x": tile.x_position,
            "y": tile.y_position,
            "width": tile.width,
            "height": tile.height,
        }

    async def checkpoint_tile(
        self,
        tile_id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        target_tab_id: Optional[str] = None,
    ) -> Optional[Tile]:
        """
        Create or update a checkpoint of a tile, including all specialized data.

        This method handles the complete process of checkpointing a tile, including
        creating/updating the base tile and any specialized tile data.

        Args:
            tile_id: The ID of the source tile to checkpoint
            tab_id: The tab ID of the source tile
            name: The name of the source tile
            target_tab_id: Optional target tab ID if the checkpoint should be created
                           in a different tab (used for tab checkpointing)

        Returns:
            The checkpoint tile if successful, None otherwise

        Raises:
            ValueError: If neither tile_id nor (tab_id and name) are provided
        """
        if not tile_id and not (tab_id and name):
            raise ValueError("Either tile_id or both tab_id and name must be provided")

        # Get the source tile
        source_tile = self._get_tile(
            id=tile_id,
            tab_id=tab_id,
            name=name,
            is_checkpoint=False,
        )

        if not source_tile:
            return None

        # Determine the target tab_id (where to create the checkpoint)
        effective_tab_id = target_tab_id if target_tab_id else source_tile.tab_id

        # Check if a checkpoint already exists
        existing_checkpoint = (
            None
            if not source_tile.checkpoint_or_active_id
            else self._get_tile(
                id=source_tile.checkpoint_or_active_id,
                is_checkpoint=True,
            )
        )

        # Extract current position data
        position_data = self._position_from_dict(self._position_to_dict(source_tile))

        # If checkpoint exists, update it
        if existing_checkpoint:
            updated = self.update_tile(
                id=str(existing_checkpoint.id),
                name=source_tile.name,
                visible=source_tile.visible,
                locked=source_tile.locked,
                moved=source_tile.moved,
                static=source_tile.static,
                color=source_tile.color,
                context=source_tile.context,
                table=source_tile.table,
                auto_update=source_tile.auto_update,
                freeze=source_tile.freeze,
                filters=source_tile.filters,
                common_filter=source_tile.common_filter,
                metric=source_tile.metric,
                minW=source_tile.minW,
                minH=source_tile.minH,
                column_context=source_tile.column_context,
                grouping=source_tile.grouping,
                is_checkpoint=True,
                **position_data,
            )

            # Update the checkpoint_or_active_id references
            # If not already set on the source tile
            if not source_tile.checkpoint_or_active_id:
                existing_checkpoint.checkpoint_or_active_id = source_tile.id
                await self.session.commit()

                source_tile.checkpoint_or_active_id = existing_checkpoint.id
                await self.session.commit()

        # Otherwise, create a new checkpoint
        else:
            updated = self.create_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                type=source_tile.type,
                visible=source_tile.visible,
                locked=source_tile.locked,
                moved=source_tile.moved,
                static=source_tile.static,
                color=source_tile.color,
                context=source_tile.context,
                table=source_tile.table,
                auto_update=source_tile.auto_update,
                freeze=source_tile.freeze,
                filters=source_tile.filters,
                common_filter=source_tile.common_filter,
                metric=source_tile.metric,
                minW=source_tile.minW,
                minH=source_tile.minH,
                column_context=source_tile.column_context,
                grouping=source_tile.grouping,
                is_checkpoint=True,
                checkpoint_or_active_id=source_tile.id,
                **position_data,
            )

            # Commit the new tile first
            await self.session.commit()

            source_tile.checkpoint_or_active_id = updated.id
            await self.session.commit()

        # Handle specialized tile data checkpointing based on tile type
        if (
            source_tile.type == "Table"
            and hasattr(source_tile, "table_tile")
            and source_tile.table_tile
        ):
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_table_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True,
            )

            if existing_specialized:
                # Update existing specialized tile
                self.update_table_tile(
                    id=str(existing_specialized.id),
                    table_type=source_tile.table_tile.table_type,
                    page_number=source_tile.table_tile.page_number,
                    column_order=source_tile.table_tile.column_order,
                    hidden_columns=source_tile.table_tile.hidden_columns,
                    default_hidden_columns=source_tile.table_tile.default_hidden_columns,
                    sorting=source_tile.table_tile.sorting,
                    group_sorting=source_tile.table_tile.group_sorting,
                    columns_pin_left=source_tile.table_tile.columns_pin_left,
                    columns_pin_right=source_tile.table_tile.columns_pin_right,
                    selected=source_tile.table_tile.selected,
                )
            else:
                # Create new specialized tile
                self.create_table_tile(
                    tab_id=effective_tab_id,
                    name=source_tile.name,
                    tile_id=updated.id,
                    table_type=source_tile.table_tile.table_type,
                    page_number=source_tile.table_tile.page_number,
                    column_order=source_tile.table_tile.column_order,
                    hidden_columns=source_tile.table_tile.hidden_columns,
                    default_hidden_columns=source_tile.table_tile.default_hidden_columns,
                    sorting=source_tile.table_tile.sorting,
                    group_sorting=source_tile.table_tile.group_sorting,
                    columns_pin_left=source_tile.table_tile.columns_pin_left,
                    columns_pin_right=source_tile.table_tile.columns_pin_right,
                    selected=source_tile.table_tile.selected,
                    is_checkpoint=True,
                )

        elif (
            source_tile.type == "Plot"
            and hasattr(source_tile, "plot_tile")
            and source_tile.plot_tile
        ):
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_plot_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True,
            )

            if existing_specialized:
                # Update existing specialized tile
                self.update_plot_tile(
                    id=str(existing_specialized.id),
                    plot_type=source_tile.plot_tile.plot_type,
                    plot_scale_x=source_tile.plot_tile.plot_scale_x,
                    plot_scale_y=source_tile.plot_tile.plot_scale_y,
                    plot_aggregate=source_tile.plot_tile.plot_aggregate,
                    x_axis=source_tile.plot_tile.x_axis,
                    y_axis=source_tile.plot_tile.y_axis,
                    plot_group_by=source_tile.plot_tile.plot_group_by,
                    plot_group_by_colors=source_tile.plot_tile.plot_group_by_colors,
                    bin_count=source_tile.plot_tile.bin_count,
                    regression_line=source_tile.plot_tile.regression_line,
                )
            else:
                # Create new specialized tile
                self.create_plot_tile(
                    tab_id=effective_tab_id,
                    name=source_tile.name,
                    tile_id=updated.id,
                    plot_type=source_tile.plot_tile.plot_type,
                    plot_scale_x=source_tile.plot_tile.plot_scale_x,
                    plot_scale_y=source_tile.plot_tile.plot_scale_y,
                    plot_aggregate=source_tile.plot_tile.plot_aggregate,
                    x_axis=source_tile.plot_tile.x_axis,
                    y_axis=source_tile.plot_tile.y_axis,
                    plot_group_by=source_tile.plot_tile.plot_group_by,
                    plot_group_by_colors=source_tile.plot_tile.plot_group_by_colors,
                    bin_count=source_tile.plot_tile.bin_count,
                    regression_line=source_tile.plot_tile.regression_line,
                    is_checkpoint=True,
                )

        elif (
            source_tile.type == "View"
            and hasattr(source_tile, "view_tile")
            and source_tile.view_tile
        ):
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_view_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True,
            )

            if existing_specialized:
                # Update existing specialized tile
                self.update_view_tile(
                    id=str(existing_specialized.id),
                    base_index=source_tile.view_tile.base_index,
                )
            else:
                # Create new specialized tile
                self.create_view_tile(
                    tab_id=effective_tab_id,
                    name=source_tile.name,
                    tile_id=updated.id,
                    base_index=source_tile.view_tile.base_index,
                    is_checkpoint=True,
                )

        elif (
            source_tile.type == "Editor"
            and hasattr(source_tile, "editor_tile")
            and source_tile.editor_tile
        ):
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_editor_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True,
            )

            if existing_specialized:
                # Update existing specialized tile
                self.update_editor_tile(
                    id=str(existing_specialized.id),
                    content=source_tile.editor_tile.content,
                    file_name=source_tile.editor_tile.file_name,
                    file_type=source_tile.editor_tile.file_type,
                )
            else:
                # Create new specialized tile
                self.create_editor_tile(
                    tab_id=effective_tab_id,
                    name=source_tile.name,
                    tile_id=updated.id,
                    content=source_tile.editor_tile.content,
                    file_name=source_tile.editor_tile.file_name,
                    file_type=source_tile.editor_tile.file_type,
                    is_checkpoint=True,
                )
        elif (
            source_tile.type == "Terminal"
            and hasattr(source_tile, "terminal_tile")
            and source_tile.terminal_tile
        ):
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_terminal_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True,
            )

            if existing_specialized:
                # Update existing specialized tile
                self.update_terminal_tile(
                    id=str(existing_specialized.id),
                    shell_type=source_tile.terminal_tile.shell_type,
                )
            else:
                self.create_terminal_tile(
                    tab_id=effective_tab_id,
                    name=source_tile.name,
                    tile_id=updated.id,
                    shell_type=source_tile.terminal_tile.shell_type,
                    is_checkpoint=True,
                )

        # Get the full checkpoint tile with all associated data
        checkpoint_tile = self.get_by_tab_and_name(
            tab_id=effective_tab_id,
            name=source_tile.name,
            is_checkpoint=True,
        )

        return checkpoint_tile

    async def get_checkpoint(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Tile]:
        """
        Get the checkpoint version of a tile.

        This method retrieves the checkpoint version of a tile by:
        1. If the provided object is already a checkpoint, it returns it.
        2. If the provided object is an active tile, it finds its checkpoint
           using the checkpoint_or_active_id reference.

        Args:
            id: Optional ID of the tile
            tab_id: Optional tab ID to identify the tile
            name: Optional name to identify the tile

        Returns:
            The checkpoint tile if found, None otherwise
        """
        # First get the tile based on the provided parameters
        tile = self._get_tile(id=id, tab_id=tab_id, name=name)
        if not tile:
            return None

        # If the tile is already a checkpoint, return it
        if tile.is_checkpoint:
            return tile

        # If the tile has a checkpoint reference, get that checkpoint
        if tile.checkpoint_or_active_id:
            checkpoint = self._get_tile(
                id=tile.checkpoint_or_active_id,
                is_checkpoint=True,
            )
            if checkpoint and checkpoint.is_checkpoint:
                return checkpoint

        # If no direct reference, try to find by tab_id and name
        return self._get_tile(tab_id=tile.tab_id, name=tile.name, is_checkpoint=True)

    async def get_current(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Tile]:
        """
        Get the current (active) version of a tile.

        This method retrieves the current version of a tile by:
        1. If the provided object is already an active tile, it returns it.
        2. If the provided object is a checkpoint, it finds its active version
           using the checkpoint_or_active_id reference.

        Args:
            id: Optional ID of the tile
            tab_id: Optional tab ID to identify the tile
            name: Optional name to identify the tile

        Returns:
            The active tile if found, None otherwise
        """
        # First get the tile based on the provided parameters
        tile = self._get_tile(id=id, tab_id=tab_id, name=name)

        if not tile:
            return None

        # If the tile is already an active tile, return it
        if not tile.is_checkpoint:
            return tile

        # If the tile has an active reference, get that active tile
        if tile.checkpoint_or_active_id:
            active = self.get(id=tile.checkpoint_or_active_id)
            if active and not active.is_checkpoint:
                return active

        # If no direct reference, try to find by tab_id and name
        return self._get_tile(tab_id=tile.tab_id, name=tile.name, is_checkpoint=False)

    async def duplicate_tiles(self, tab_id_map: dict) -> dict:
        """
        Duplicate all tiles and specialized tile data for the given tabs.
        If a tile with the same tab_id and name already exists,
        it will be updated instead of creating a duplicate.

        Args:
            tab_id_map: Mapping of old tab IDs to new tab IDs

        Returns:
            Dictionary with mappings and counts for tiles and specialized tiles
        """
        from datetime import datetime, timezone

        import sqlalchemy

        from orchestra.db.models.orchestra_models import (
            EditorTile,
            PlotTile,
            TableTile,
            Tile,
            ViewTile,
        )

        tile_id_map = {}
        total_tile_count = 0
        table_tile_count = 0
        plot_tile_count = 0
        view_tile_count = 0
        editor_tile_count = 0
        terminal_tile_count = 0

        # Process each tab
        for old_tab_id, new_tab_id in tab_id_map.items():
            # Get tiles for the old tab
            tiles = self.list_tiles_by_tab(tab_id=old_tab_id, is_checkpoint=False)

            if tiles:
                tile_values = []
                old_tile_ids = []
                existing_tile_map = {}

                for tile in tiles:
                    # Check if a tile with the same name already exists in the target tab
                    existing_tile = self.get_by_tab_and_name(
                        tab_id=new_tab_id,
                        name=tile.name,
                        is_checkpoint=False,
                    )

                    if existing_tile:
                        # If tile already exists, store it for updating later
                        existing_tile_map[tile.id] = existing_tile
                        tile_id_map[tile.id] = existing_tile.id
                        total_tile_count += 1
                    else:
                        # If tile doesn't exist, add to list for bulk insert
                        old_tile_ids.append(tile.id)
                        tile_values.append(
                            {
                                "tab_id": new_tab_id,
                                "type": tile.type,
                                "name": tile.name,
                                "x_position": tile.x_position,
                                "y_position": tile.y_position,
                                "width": tile.width,
                                "height": tile.height,
                                "minW": tile.minW,
                                "minH": tile.minH,
                                "visible": tile.visible,
                                "locked": tile.locked,
                                "moved": tile.moved,
                                "static": tile.static,
                                "color": tile.color,
                                "context": tile.context,
                                "table": tile.table,
                                "auto_update": tile.auto_update,
                                "freeze": tile.freeze,
                                "filters": tile.filters,
                                "common_filter": tile.common_filter,
                                "metric": tile.metric,
                                "column_context": tile.column_context,
                                "grouping": tile.grouping,
                                "is_checkpoint": tile.is_checkpoint,
                                "checkpoint_or_active_id": None,  # Will be updated if needed
                                "created_at": datetime.now(timezone.utc),
                                "updated_at": datetime.now(timezone.utc),
                            },
                        )

                # Update existing tiles
                for old_id, existing_tile in existing_tile_map.items():
                    source_tile = next((t for t in tiles if t.id == old_id), None)
                    if source_tile:
                        # Update existing tile with data from source
                        self.update_tile(
                            id=existing_tile.id,
                            x_position=source_tile.x_position,
                            y_position=source_tile.y_position,
                            width=source_tile.width,
                            height=source_tile.height,
                            minW=source_tile.minW,
                            minH=source_tile.minH,
                            visible=source_tile.visible,
                            locked=source_tile.locked,
                            moved=source_tile.moved,
                            static=source_tile.static,
                            color=source_tile.color,
                            context=source_tile.context,
                            table=source_tile.table,
                            auto_update=source_tile.auto_update,
                            freeze=source_tile.freeze,
                            filters=source_tile.filters,
                            common_filter=source_tile.common_filter,
                            metric=source_tile.metric,
                            column_context=source_tile.column_context,
                            grouping=source_tile.grouping,
                            is_checkpoint=source_tile.is_checkpoint,
                        )

                # Bulk insert new tiles
                if tile_values:
                    # Bulk insert tiles and get back the new IDs
                    stmt = (
                        sqlalchemy.insert(Tile).values(tile_values).returning(Tile.id)
                    )
                    result = await self.session.execute(stmt)
                    new_tile_ids = [row[0] for row in result]

                    # Build the tile ID mapping for newly created tiles
                    for i, old_id in enumerate(old_tile_ids):
                        tile_id_map[old_id] = new_tile_ids[i]

                    total_tile_count += len(tile_values)

        # Duplicate specialized tile data
        if tile_id_map:
            # Process TableTiles
            table_tiles = (
                self.session.query(TableTile)
                .filter(TableTile.tile_id.in_(list(tile_id_map.keys())))
                .all()
            )

            if table_tiles:
                table_tile_values = []
                table_tile_updates = []

                for tt in table_tiles:
                    new_tile_id = tile_id_map[tt.tile_id]

                    # Check if a TableTile already exists for this tile
                    existing_table_tile = (
                        self.session.query(TableTile)
                        .filter(TableTile.tile_id == new_tile_id)
                        .first()
                    )

                    if existing_table_tile:
                        # Update existing TableTile
                        table_tile_updates.append(
                            {
                                "id": existing_table_tile.id,
                                "table_type": tt.table_type,
                                "page_number": tt.page_number,
                                "column_order": tt.column_order,
                                "hidden_columns": tt.hidden_columns,
                                "default_hidden_columns": tt.default_hidden_columns,
                                "sorting": tt.sorting,
                                "group_sorting": tt.group_sorting,
                                "columns_pin_left": tt.columns_pin_left,
                                "columns_pin_right": tt.columns_pin_right,
                                "selected": tt.selected,
                            },
                        )
                    else:
                        # Create new TableTile
                        table_tile_values.append(
                            {
                                "tile_id": new_tile_id,
                                "table_type": tt.table_type,
                                "page_number": tt.page_number,
                                "column_order": tt.column_order,
                                "hidden_columns": tt.hidden_columns,
                                "default_hidden_columns": tt.default_hidden_columns,
                                "sorting": tt.sorting,
                                "group_sorting": tt.group_sorting,
                                "columns_pin_left": tt.columns_pin_left,
                                "columns_pin_right": tt.columns_pin_right,
                                "selected": tt.selected,
                            },
                        )

                # Insert new TableTiles
                if table_tile_values:
                    stmt = sqlalchemy.insert(TableTile).values(table_tile_values)
                    await self.session.execute(stmt)
                    table_tile_count = len(table_tile_values)

                # Update existing TableTiles
                for update_data in table_tile_updates:
                    table_tile_id = update_data.pop("id")
                    self.session.query(TableTile).filter(
                        TableTile.id == table_tile_id,
                    ).update(update_data)
                    table_tile_count += 1

            # Process PlotTiles
            plot_tiles = (
                self.session.query(PlotTile)
                .filter(PlotTile.tile_id.in_(list(tile_id_map.keys())))
                .all()
            )

            if plot_tiles:
                plot_tile_values = []
                plot_tile_updates = []

                for pt in plot_tiles:
                    new_tile_id = tile_id_map[pt.tile_id]

                    # Check if a PlotTile already exists for this tile
                    existing_plot_tile = (
                        self.session.query(PlotTile)
                        .filter(PlotTile.tile_id == new_tile_id)
                        .first()
                    )

                    if existing_plot_tile:
                        # Update existing PlotTile
                        plot_tile_updates.append(
                            {
                                "id": existing_plot_tile.id,
                                "plot_type": pt.plot_type,
                                "plot_scale_x": pt.plot_scale_x,
                                "plot_scale_y": pt.plot_scale_y,
                                "plot_aggregate": pt.plot_aggregate,
                                "x_axis": pt.x_axis,
                                "y_axis": pt.y_axis,
                                "plot_group_by": pt.plot_group_by,
                                "plot_group_by_colors": pt.plot_group_by_colors,
                                "bin_count": pt.bin_count,
                                "regression_line": pt.regression_line,
                            },
                        )
                    else:
                        # Create new PlotTile
                        plot_tile_values.append(
                            {
                                "tile_id": new_tile_id,
                                "plot_type": pt.plot_type,
                                "plot_scale_x": pt.plot_scale_x,
                                "plot_scale_y": pt.plot_scale_y,
                                "plot_aggregate": pt.plot_aggregate,
                                "x_axis": pt.x_axis,
                                "y_axis": pt.y_axis,
                                "plot_group_by": pt.plot_group_by,
                                "plot_group_by_colors": pt.plot_group_by_colors,
                                "bin_count": pt.bin_count,
                                "regression_line": pt.regression_line,
                            },
                        )

                # Insert new PlotTiles
                if plot_tile_values:
                    stmt = sqlalchemy.insert(PlotTile).values(plot_tile_values)
                    await self.session.execute(stmt)
                    plot_tile_count = len(plot_tile_values)

                # Update existing PlotTiles
                for update_data in plot_tile_updates:
                    plot_tile_id = update_data.pop("id")
                    self.session.query(PlotTile).filter(
                        PlotTile.id == plot_tile_id,
                    ).update(update_data)
                    plot_tile_count += 1

            # Process ViewTiles
            view_tiles = (
                self.session.query(ViewTile)
                .filter(ViewTile.tile_id.in_(list(tile_id_map.keys())))
                .all()
            )

            if view_tiles:
                view_tile_values = []
                view_tile_updates = []

                for vt in view_tiles:
                    new_tile_id = tile_id_map[vt.tile_id]

                    # Check if a ViewTile already exists for this tile
                    existing_view_tile = (
                        self.session.query(ViewTile)
                        .filter(ViewTile.tile_id == new_tile_id)
                        .first()
                    )

                    if existing_view_tile:
                        # Update existing ViewTile
                        view_tile_updates.append(
                            {
                                "id": existing_view_tile.id,
                                "base_index": vt.base_index,
                            },
                        )
                    else:
                        # Create new ViewTile
                        view_tile_values.append(
                            {
                                "tile_id": new_tile_id,
                                "base_index": vt.base_index,
                            },
                        )

                # Insert new ViewTiles
                if view_tile_values:
                    stmt = sqlalchemy.insert(ViewTile).values(view_tile_values)
                    await self.session.execute(stmt)
                    view_tile_count = len(view_tile_values)

                # Update existing ViewTiles
                for update_data in view_tile_updates:
                    view_tile_id = update_data.pop("id")
                    self.session.query(ViewTile).filter(
                        ViewTile.id == view_tile_id,
                    ).update(update_data)
                    view_tile_count += 1

            # Process EditorTiles
            editor_tiles = (
                self.session.query(EditorTile)
                .filter(EditorTile.tile_id.in_(list(tile_id_map.keys())))
                .all()
            )

            if editor_tiles:
                editor_tile_values = []
                editor_tile_updates = []

                for et in editor_tiles:
                    new_tile_id = tile_id_map[et.tile_id]

                    # Check if an EditorTile already exists for this tile
                    existing_editor_tile = (
                        self.session.query(EditorTile)
                        .filter(EditorTile.tile_id == new_tile_id)
                        .first()
                    )

                    if existing_editor_tile:
                        # Update existing EditorTile
                        editor_tile_updates.append(
                            {
                                "id": existing_editor_tile.id,
                                "file_name": et.file_name,
                                "file_type": et.file_type,
                                "content": et.content,
                            },
                        )
                    else:
                        # Create new EditorTile
                        editor_tile_values.append(
                            {
                                "tile_id": new_tile_id,
                                "file_name": et.file_name,
                                "file_type": et.file_type,
                                "content": et.content,
                            },
                        )

                # Insert new EditorTiles
                if editor_tile_values:
                    stmt = sqlalchemy.insert(EditorTile).values(editor_tile_values)
                    await self.session.execute(stmt)
                    editor_tile_count = len(editor_tile_values)

                # Update existing EditorTiles
                for update_data in editor_tile_updates:
                    editor_tile_id = update_data.pop("id")
                    self.session.query(EditorTile).filter(
                        EditorTile.id == editor_tile_id,
                    ).update(update_data)
                    editor_tile_count += 1

            # Process TerminalTiles
            terminal_tiles = (
                self.session.query(TerminalTile)
                .filter(TerminalTile.tile_id.in_(list(tile_id_map.keys())))
                .all()
            )

            if terminal_tiles:
                terminal_tile_values = []
                terminal_tile_updates = []

                for tt in terminal_tiles:
                    new_tile_id = tile_id_map[tt.tile_id]

                    # Check if a TerminalTile already exists for this tile
                    existing_terminal_tile = (
                        self.session.query(TerminalTile)
                        .filter(TerminalTile.tile_id == new_tile_id)
                        .first()
                    )

                    if existing_terminal_tile:
                        # Update existing TerminalTile
                        terminal_tile_updates.append(
                            {
                                "id": existing_terminal_tile.id,
                                "shell_type": tt.shell_type,
                            },
                        )
                    else:
                        # Create new TerminalTile
                        terminal_tile_values.append(
                            {
                                "tile_id": new_tile_id,
                                "shell_type": tt.shell_type,
                            },
                        )

                # Insert new TerminalTiles
                if terminal_tile_values:
                    stmt = sqlalchemy.insert(TerminalTile).values(terminal_tile_values)
                    await self.session.execute(stmt)
                    terminal_tile_count = len(terminal_tile_values)

                # Update existing TerminalTiles
                for update_data in terminal_tile_updates:
                    terminal_tile_id = update_data.pop("id")
                    self.session.query(TerminalTile).filter(
                        TerminalTile.id == terminal_tile_id,
                    ).update(update_data)
                    terminal_tile_count += 1

        return {
            "id_map": tile_id_map,
            "tile_count": total_tile_count,
            "table_tile_count": table_tile_count,
            "plot_tile_count": plot_tile_count,
            "view_tile_count": view_tile_count,
            "editor_tile_count": editor_tile_count,
            "terminal_tile_count": terminal_tile_count,
        }
