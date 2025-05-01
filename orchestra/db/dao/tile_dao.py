from typing import Dict, List, Optional, Union, Any, Tuple
import json

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Tile, 
    TableTile, 
    PlotTile, 
    ViewTile, 
    EditorTile
)


class TileDAO:
    """Data Access Object for Tile entity."""
    
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_tile(
        self,
        tab_id: str,
        type: str,
        name: str,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 400,
        height: float = 400,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        is_checkpoint: bool = False,
        checkpoint_or_active_id: Optional[str] = None,
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
            min_width: The minimum width of the tile
            min_height: The minimum height of the tile
            visible: Whether the tile is visible
            locked: Whether the tile is locked
            moved: Whether the tile has been moved
            static: Whether the tile is static
            context: Optional context data for the tile
            table: Optional table data for the tile
            auto_update: Optional auto-update setting
            freeze: Optional freeze setting
            filters: Optional filters
            common_filter: Optional common filter
            metric: Optional metric data
            is_checkpoint: Whether this is a checkpoint tile
            
        Returns:
            The created tile
            
        Raises:
            ValueError: If required parameters are missing or invalid
        """
        if not tab_id or not type or not name:
            raise ValueError("tab_id, type, and name are required")
            
        # Check if tile already exists
        existing = self.get_by_tab_and_name(tab_id=tab_id, name=name, is_checkpoint=is_checkpoint)
        if existing:
            raise ValueError(f"Tile with name {name} already exists in tab {tab_id}")
            
        tile = Tile(
            tab_id=tab_id,
            type=type,
            name=name,
            x_position=x_position,
            y_position=y_position,
            width=width,
            height=height,
            min_width=min_width,
            min_height=min_height,
            visible=visible,
            locked=locked,
            moved=moved,
            static=static,
            context=context,
            table=table,
            auto_update=auto_update,
            freeze=freeze,
            filters=filters,
            common_filter=common_filter,
            metric=metric,
            is_checkpoint=is_checkpoint,
            checkpoint_or_active_id=checkpoint_or_active_id,
        )
        self.session.add(tile)
        self.session.commit()
        return tile

    def _get_tile(
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
            
        return self.session.execute(query).scalars().first()

    def get(self, id: str, is_checkpoint: Optional[bool] = False) -> Optional[Tile]:
        """
        Get tile by ID.
        
        Args:
            id: The ID of the tile
            is_checkpoint: Whether to get a checkpoint tile
            
        Returns:
            The tile if found, None otherwise
        """
        return self._get_tile(id=id, is_checkpoint=is_checkpoint)

    def get_by_tab_and_name(
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

    def list_tiles_by_tab(
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
            
        return self.session.execute(query).scalars().all()

    def update_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        x_position: Optional[float] = None,
        y_position: Optional[float] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
        position: Optional[dict] = None,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: Optional[bool] = None,
        locked: Optional[bool] = None,
        moved: Optional[bool] = None,
        static: Optional[bool] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
    ) -> Optional[Tile]:
        """
        Update tile by ID or by tab_id and name.
        
        Args:
            id: The ID of the tile
            tab_id: The ID of the tab
            name: The name of the tile
            is_checkpoint: Whether to update a checkpoint tile
            x_position: New x position
            y_position: New y position
            width: New width
            height: New height
            position: New position as a dict with x, y, width, height
            min_width: New minimum width
            min_height: New minimum height
            visible: New visibility setting
            locked: New locked setting
            moved: New moved setting
            static: New static setting
            context: New context data
            table: New table data
            auto_update: New auto_update setting
            freeze: New freeze setting
            filters: New filters
            common_filter: New common filter
            metric: New metric data
            
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
            is_checkpoint=is_checkpoint
        )

        if tile is None:
            return None
            
        # Handle position as a dict if provided
        if position:
            position_values = self._position_from_dict(position)
            if 'x_position' in position_values:
                tile.x_position = position_values['x_position']
            if 'y_position' in position_values:
                tile.y_position = position_values['y_position']
            if 'width' in position_values:
                tile.width = position_values['width']
            if 'height' in position_values:
                tile.height = position_values['height']
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
        if min_width is not None:
            tile.min_width = min_width
        if min_height is not None:
            tile.min_height = min_height
        if visible is not None:
            tile.visible = visible
        if locked is not None:
            tile.locked = locked
        if moved is not None:
            tile.moved = moved
        if static is not None:
            tile.static = static
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
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        return tile

    def delete_tile(
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
            is_checkpoint=is_checkpoint
        )
        
        if tile is None:
            return False
            
        self.session.delete(tile)
        self.session.commit()
        return True

    # Specialized tile types
    def create_table_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
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
                min_width=min_width,
                min_height=min_height,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id

        table_tile = TableTile(
            id=tile_id,
            tile_id=tile_id,
            table_type=table_type,
            column_context=column_context,
            page_number=page_number,
            column_order=column_order,
            hidden_columns=hidden_columns,
            sorting=sorting,
            grouping=grouping,
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
        
        self.session.commit()
        return table_tile
        
    def _get_specialized_tile(
        self, 
        model_class,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[Union[TableTile, PlotTile, ViewTile, EditorTile]]:
        """Helper method to get a specialized tile by ID or by tab_id and name."""
        if id is not None:
            query = select(model_class).where(model_class.id == id)
            return self.session.execute(query).scalars().first()
            
        # If identifying by tab_id and name, first get the base tile
        if tab_id is not None and name is not None:
            base_tile = self._get_tile(tab_id=tab_id, name=name, is_checkpoint=is_checkpoint)
            if base_tile is None:
                return None
                
            # Then get the specialized tile using the base tile's ID
            query = select(model_class).where(model_class.id == base_tile.id)
            return self.session.execute(query).scalars().first()
            
        return None
        
    def get_table_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[TableTile]:
        """Get table tile by ID or by tab_id and name."""
        return self._get_specialized_tile(TableTile, id, tab_id, name, is_checkpoint)
        
    def update_table_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        position: Optional[dict] = None,
        x_position: Optional[float] = None,
        y_position: Optional[float] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: Optional[bool] = None,
        locked: Optional[bool] = None,
        moved: Optional[bool] = None,
        static: Optional[bool] = None,
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
        sorting: Optional[str] = None,
        grouping: Optional[str] = None,
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
            TableTile, id, tab_id, name, is_checkpoint
        )
        
        if table_tile is None:
            return None
            
        # Process position data
        position_fields = {}
        if position:
            position_fields = self._position_from_dict(position)
        else:
            # Handle individual position fields
            if x_position is not None:
                position_fields['x_position'] = x_position
            if y_position is not None:
                position_fields['y_position'] = y_position
            if width is not None:
                position_fields['width'] = width
            if height is not None:
                position_fields['height'] = height
                
        # Apply position fields
        for field, value in position_fields.items():
            setattr(table_tile, field, value)
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            table_tile.name = name
        if min_width is not None:
            table_tile.min_width = min_width
        if min_height is not None:
            table_tile.min_height = min_height
        if visible is not None:
            table_tile.visible = visible
        if locked is not None:
            table_tile.locked = locked
        if moved is not None:
            table_tile.moved = moved
        if static is not None:
            table_tile.static = static
        if context is not None:
            table_tile.context = context
        if table is not None:
            table_tile.table = table
        if auto_update is not None:
            table_tile.auto_update = auto_update
        if freeze is not None:
            table_tile.freeze = freeze
        if filters is not None:
            table_tile.filters = filters
        if common_filter is not None:
            table_tile.common_filter = common_filter
        if metric is not None:
            table_tile.metric = metric
            
        # Update specialized fields
        if table_type is not None:
            table_tile.table_type = table_type
        if column_context is not None:
            table_tile.column_context = column_context
        if page_number is not None:
            table_tile.page_number = page_number
        if column_order is not None:
            table_tile.column_order = column_order
        if hidden_columns is not None:
            table_tile.hidden_columns = hidden_columns
        if sorting is not None:
            table_tile.sorting = sorting
        if grouping is not None:
            table_tile.grouping = grouping
        if group_sorting is not None:
            table_tile.group_sorting = group_sorting
        if columns_pin_left is not None:
            table_tile.columns_pin_left = columns_pin_left
        if columns_pin_right is not None:
            table_tile.columns_pin_right = columns_pin_right
        if selected is not None:
            table_tile.selected = selected
            
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            table_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        return table_tile
        
    def create_plot_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
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
                min_width=min_width,
                min_height=min_height,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id
            
        plot_tile = PlotTile(
            id=tile_id,
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

        # Now update the base tile
        base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)
        if base_tile:
            base_tile.plot_tile = plot_tile
        else:
            print(f"Base tile not found for {tile_id}")

        self.session.add(plot_tile)
        self.session.commit()

        return plot_tile
        
    def get_plot_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[PlotTile]:
        """Get plot tile by ID or by tab_id and name."""
        return self._get_specialized_tile(PlotTile, id, tab_id, name, is_checkpoint)
        
    def update_plot_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        position: Optional[dict] = None,
        x_position: Optional[float] = None,
        y_position: Optional[float] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: Optional[bool] = None,
        locked: Optional[bool] = None,
        moved: Optional[bool] = None,
        static: Optional[bool] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
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
            PlotTile, id, tab_id, name, is_checkpoint
        )
        
        if plot_tile is None:
            return None
            
        # Process position data
        position_fields = {}
        if position:
            position_fields = self._position_from_dict(position)
        else:
            # Handle individual position fields
            if x_position is not None:
                position_fields['x_position'] = x_position
            if y_position is not None:
                position_fields['y_position'] = y_position
            if width is not None:
                position_fields['width'] = width
            if height is not None:
                position_fields['height'] = height
                
        # Apply position fields
        for field, value in position_fields.items():
            setattr(plot_tile, field, value)
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            plot_tile.name = name
        if min_width is not None:
            plot_tile.min_width = min_width
        if min_height is not None:
            plot_tile.min_height = min_height
        if visible is not None:
            plot_tile.visible = visible
        if locked is not None:
            plot_tile.locked = locked
        if moved is not None:
            plot_tile.moved = moved
        if static is not None:
            plot_tile.static = static
        if context is not None:
            plot_tile.context = context
        if table is not None:
            plot_tile.table = table
        if auto_update is not None:
            plot_tile.auto_update = auto_update
        if freeze is not None:
            plot_tile.freeze = freeze
        if filters is not None:
            plot_tile.filters = filters
        if common_filter is not None:
            plot_tile.common_filter = common_filter
        if metric is not None:
            plot_tile.metric = metric
            
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
            
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            plot_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        return plot_tile
        
    def create_editor_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        content: str = "",
        file_path: Optional[str] = None,
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
                min_width=min_width,
                min_height=min_height,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id
            
        editor_tile = EditorTile(
            id=tile_id,
            tile_id=tile_id,
            content=content,
            file_path=file_path,
            file_type=file_type,
        )

        # Now update the base tile
        base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)
        if base_tile:
            base_tile.editor_tile = editor_tile
        else:
            print(f"Base tile not found for {tile_id}")

        self.session.add(editor_tile)
        self.session.commit()

        return editor_tile
        
    def get_editor_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[EditorTile]:
        """Get editor tile by ID or by tab_id and name."""
        return self._get_specialized_tile(EditorTile, id, tab_id, name, is_checkpoint)
        
    def update_editor_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        position: Optional[dict] = None,
        x_position: Optional[float] = None,
        y_position: Optional[float] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: Optional[bool] = None,
        locked: Optional[bool] = None,
        moved: Optional[bool] = None,
        static: Optional[bool] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        content: Optional[str] = None,
        file_path: Optional[str] = None,
        file_type: Optional[str] = None,
    ) -> Optional[EditorTile]:
        """
        Update editor tile by ID or by tab_id and name.
        
        Either id or (tab_id and name) must be provided to identify the tile.
        """
        editor_tile = self._get_specialized_tile(
            EditorTile, id, tab_id, name, is_checkpoint
        )
        
        if editor_tile is None:
            return None
            
        # Process position data
        position_fields = {}
        if position:
            position_fields = self._position_from_dict(position)
        else:
            # Handle individual position fields
            if x_position is not None:
                position_fields['x_position'] = x_position
            if y_position is not None:
                position_fields['y_position'] = y_position
            if width is not None:
                position_fields['width'] = width
            if height is not None:
                position_fields['height'] = height
                
        # Apply position fields
        for field, value in position_fields.items():
            setattr(editor_tile, field, value)
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            editor_tile.name = name
        if min_width is not None:
            editor_tile.min_width = min_width
        if min_height is not None:
            editor_tile.min_height = min_height
        if visible is not None:
            editor_tile.visible = visible
        if locked is not None:
            editor_tile.locked = locked
        if moved is not None:
            editor_tile.moved = moved
        if static is not None:
            editor_tile.static = static
        if context is not None:
            editor_tile.context = context
        if table is not None:
            editor_tile.table = table
        if auto_update is not None:
            editor_tile.auto_update = auto_update
        if freeze is not None:
            editor_tile.freeze = freeze
        if filters is not None:
            editor_tile.filters = filters
        if common_filter is not None:
            editor_tile.common_filter = common_filter
        if metric is not None:
            editor_tile.metric = metric
            
        # Update specialized fields
        if content is not None:
            editor_tile.content = content
        if file_path is not None:
            editor_tile.file_path = file_path
        if file_type is not None:
            editor_tile.file_type = file_type
            
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            editor_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        return editor_tile
        
    def create_view_tile(
        self,
        tab_id: str,
        name: str,
        tile_id: Optional[str] = None,
        x_position: float = 0,
        y_position: float = 0,
        width: float = 600,
        height: float = 400,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: bool = True,
        locked: bool = False,
        moved: bool = False,
        static: bool = False,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
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
                min_width=min_width,
                min_height=min_height,
                visible=visible,
                locked=locked,
                moved=moved,
                static=static,
                context=context,
                table=table,
                auto_update=auto_update,
                freeze=freeze,
                filters=filters,
                common_filter=common_filter,
                metric=metric,
                is_checkpoint=is_checkpoint,
            )
            tile_id = base_tile.id
            
        view_tile = ViewTile(
            id=tile_id,
            tile_id=tile_id,
            base_index=base_index,
        )

        # Now update the base tile
        base_tile = self._get_tile(id=tile_id, is_checkpoint=is_checkpoint)
        if base_tile:
            base_tile.view_tile = view_tile
        else:
            print(f"Base tile not found for {tile_id}")

        self.session.add(view_tile)
        self.session.commit()

        return view_tile
        
    def get_view_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
    ) -> Optional[ViewTile]:
        """Get view tile by ID or by tab_id and name."""
        return self._get_specialized_tile(ViewTile, id, tab_id, name, is_checkpoint)
        
    def update_view_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = False,
        position: Optional[dict] = None,
        x_position: Optional[float] = None,
        y_position: Optional[float] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
        min_width: Optional[float] = None,
        min_height: Optional[float] = None,
        visible: Optional[bool] = None,
        locked: Optional[bool] = None,
        moved: Optional[bool] = None,
        static: Optional[bool] = None,
        context: Optional[str] = None,
        table: Optional[str] = None,
        auto_update: Optional[str] = None,
        freeze: Optional[str] = None,
        filters: Optional[str] = None,
        common_filter: Optional[str] = None,
        metric: Optional[str] = None,
        base_index: Optional[str] = None,
    ) -> Optional[ViewTile]:
        """
        Update view tile by ID or by tab_id and name.
        
        Either id or (tab_id and name) must be provided to identify the tile.
        """
        view_tile = self._get_specialized_tile(
            ViewTile, id, tab_id, name, is_checkpoint
        )
        
        if view_tile is None:
            return None
            
        # Process position data
        position_fields = {}
        if position:
            position_fields = self._position_from_dict(position)
        else:
            # Handle individual position fields
            if x_position is not None:
                position_fields['x_position'] = x_position
            if y_position is not None:
                position_fields['y_position'] = y_position
            if width is not None:
                position_fields['width'] = width
            if height is not None:
                position_fields['height'] = height
                
        # Apply position fields
        for field, value in position_fields.items():
            setattr(view_tile, field, value)
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            view_tile.name = name
        if min_width is not None:
            view_tile.min_width = min_width
        if min_height is not None:
            view_tile.min_height = min_height
        if visible is not None:
            view_tile.visible = visible
        if locked is not None:
            view_tile.locked = locked
        if moved is not None:
            view_tile.moved = moved
        if static is not None:
            view_tile.static = static
        if context is not None:
            view_tile.context = context
        if table is not None:
            view_tile.table = table
        if auto_update is not None:
            view_tile.auto_update = auto_update
        if freeze is not None:
            view_tile.freeze = freeze
        if filters is not None:
            view_tile.filters = filters
        if common_filter is not None:
            view_tile.common_filter = common_filter
        if metric is not None:
            view_tile.metric = metric
            
        # Update specialized fields
        if base_index is not None:
            view_tile.base_index = base_index
            
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            view_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        return view_tile
    
    def patch_tile(
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
            is_checkpoint=is_checkpoint
        )
        
        if tile is None:
            return None
        
        # If tile_type is specified, use it; otherwise use the tile's type
        effective_type = tile_type or tile.type
        
        # Handle specialized tile data based on the effective type
        if 'table_tile' in update_data and effective_type == 'Table':
            table_tile_data = update_data.pop('table_tile')
            # Get the table tile
            table_tile = self.get_table_tile(id=tile.id)
            if table_tile:
                for field, value in table_tile_data.items():
                    if hasattr(table_tile, field):
                        if field in ('table_type', 'column_context', 'page_number', 
                                    'column_order', 'hidden_columns', 'sorting', 
                                    'grouping', 'group_sorting', 'columns_pin_left', 
                                    'columns_pin_right', 'selected') and isinstance(value, (list, dict)):
                            setattr(table_tile, field, json.dumps(value))
                        else:
                            setattr(table_tile, field, value)
        
        if 'plot_tile' in update_data and effective_type == 'Plot':
            plot_tile_data = update_data.pop('plot_tile')
            # Get the plot tile
            plot_tile = self.get_plot_tile(id=tile.id)
            if plot_tile:
                for field, value in plot_tile_data.items():
                    if hasattr(plot_tile, field):
                        if field == 'plot_data' and not isinstance(value, str):
                            setattr(plot_tile, field, json.dumps(value))
                        else:
                            setattr(plot_tile, field, value)
        
        if 'view_tile' in update_data and effective_type == 'View':
            view_tile_data = update_data.pop('view_tile')
            # Get the view tile
            view_tile = self.get_view_tile(id=tile.id)
            if view_tile:
                for field, value in view_tile_data.items():
                    if hasattr(view_tile, field):
                        if field == 'view_data' and not isinstance(value, str):
                            setattr(view_tile, field, json.dumps(value))
                        else:
                            setattr(view_tile, field, value)
        
        if 'editor_tile' in update_data and effective_type == 'Editor':
            editor_tile_data = update_data.pop('editor_tile')
            # Get the editor tile
            editor_tile = self.get_editor_tile(id=tile.id)
            if editor_tile:
                for field, value in editor_tile_data.items():
                    if hasattr(editor_tile, field):
                        setattr(editor_tile, field, value)
        
        # Handle position updates specially
        if 'position' in update_data:
            position = update_data.pop('position')
            position_fields = self._position_from_dict(position)
            for field, value in position_fields.items():
                setattr(tile, field, value)
        
        # Update the base tile fields - translate certain fields if needed
        allowed_fields = [
            'name', 'min_width', 'min_height', 'visible', 'locked',
            'moved', 'static', 'context', 'table', 'auto_update',
            'freeze', 'filters', 'common_filter', 'metric',
        ]
        
        for field in allowed_fields:
            if field in update_data:
                setattr(tile, field, update_data[field])
        
        self.session.commit()
        return tile
        
    def patch_specialized_tile(
        self,
        id: str,
        tile_type: str,
        update_data: dict,
    ) -> Optional[Union[Tile, TableTile, PlotTile, ViewTile, EditorTile]]:
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
        if tile_type == 'Table':
            specialized_key = 'table_tile'
            specialized_tile = self.get_table_tile(id=id)
        elif tile_type == 'Plot':
            specialized_key = 'plot_tile'
            specialized_tile = self.get_plot_tile(id=id)
        elif tile_type == 'View':
            specialized_key = 'view_tile'
            specialized_tile = self.get_view_tile(id=id)
        elif tile_type == 'Editor':
            specialized_key = 'editor_tile'
            specialized_tile = self.get_editor_tile(id=id)
        else:
            # Invalid tile type
            return None
            
        if not specialized_tile:
            return None
            
        # Extract the specialized data
        specialized_data = None
        if specialized_key in update_data:
            specialized_data = update_data[specialized_key]
            
        # Update the specialized tile
        if specialized_data:
            for field, value in specialized_data.items():
                if hasattr(specialized_tile, field):
                    # JSON serialize if needed for complex types
                    if tile_type == 'Table' and field in ('table_type', 'column_context', 'page_number', 
                                                         'column_order', 'hidden_columns', 'sorting', 
                                                         'grouping', 'group_sorting', 'columns_pin_left', 
                                                         'columns_pin_right', 'selected') and isinstance(value, (list, dict)):
                        setattr(specialized_tile, field, json.dumps(value))
                    elif tile_type == 'Plot' and field in ('plot_type', 'plot_scale_x', 'plot_scale_y',
                                                          'plot_aggregate', 'x_axis', 'y_axis',
                                                          'plot_group_by', 'plot_group_by_colors',
                                                          'bin_count', 'regression_line') and isinstance(value, (list, dict)):
                        setattr(specialized_tile, field, json.dumps(value))
                    elif tile_type == 'View' and field == 'base_index' and isinstance(value, (list, dict)):
                        setattr(specialized_tile, field, json.dumps(value))
                    elif tile_type == 'Editor' and field in ('file_path', 'file_type') and isinstance(value, (list, dict)):
                        setattr(specialized_tile, field, json.dumps(value))
                    else:
                        setattr(specialized_tile, field, value)
        
        self.session.commit()
        return tile

    def _position_from_dict(self, position_dict: Optional[dict]) -> dict:
        """Convert a position dictionary from the schema to model field values.
        
        Args:
            position_dict: A dictionary with x, y, width, height keys
            
        Returns:
            A dictionary with x_position, y_position, width, height keys
        """
        result = {}
        if position_dict:
            if 'x' in position_dict:
                result['x_position'] = position_dict['x']
            if 'y' in position_dict:
                result['y_position'] = position_dict['y']
            if 'width' in position_dict:
                result['width'] = position_dict['width']
            if 'height' in position_dict:
                result['height'] = position_dict['height']
        return result
        
    def _position_to_dict(self, tile: Tile) -> dict:
        """Convert model position fields to a position dictionary for the schema.
        
        Args:
            tile: A Tile object
            
        Returns:
            A dictionary with x, y, width, height keys
        """
        return {
            'x': tile.x_position,
            'y': tile.y_position,
            'width': tile.width,
            'height': tile.height
        }

    def checkpoint_tile(
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
            is_checkpoint=False
        )
        
        if not source_tile:
            return None
            
        # Determine the target tab_id (where to create the checkpoint)
        effective_tab_id = target_tab_id if target_tab_id else source_tile.tab_id

        # Check if a checkpoint already exists
        existing_checkpoint = None if not source_tile.checkpoint_or_active_id else self._get_tile(
            id=source_tile.checkpoint_or_active_id,
            is_checkpoint=True,
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
                context=source_tile.context,
                table=source_tile.table,
                auto_update=source_tile.auto_update,
                freeze=source_tile.freeze,
                filters=source_tile.filters,
                common_filter=source_tile.common_filter,
                metric=source_tile.metric,
                min_width=source_tile.min_width,
                min_height=source_tile.min_height,
                is_checkpoint=True,
                **position_data
            )
            
            # Update the checkpoint_or_active_id references
            # If not already set on the source tile
            if not source_tile.checkpoint_or_active_id:
                existing_checkpoint.checkpoint_or_active_id = source_tile.id
                self.session.commit()
                
                source_tile.checkpoint_or_active_id = existing_checkpoint.id
                self.session.commit()
        
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
                context=source_tile.context,
                table=source_tile.table,
                auto_update=source_tile.auto_update,
                freeze=source_tile.freeze,
                filters=source_tile.filters,
                common_filter=source_tile.common_filter,
                metric=source_tile.metric,
                min_width=source_tile.min_width,
                min_height=source_tile.min_height,
                is_checkpoint=True,
                checkpoint_or_active_id=source_tile.id,
                **position_data
            )
            
            # Commit the new tile first
            self.session.commit()
            
            source_tile.checkpoint_or_active_id = updated.id
            self.session.commit()
        
        # Handle specialized tile data checkpointing based on tile type
        if source_tile.type == "Table" and hasattr(source_tile, 'table_tile') and source_tile.table_tile:
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_table_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True
            )
            
            if existing_specialized:
                # Update existing specialized tile
                self.update_table_tile(
                    id=str(existing_specialized.id),
                    table_type=source_tile.table_tile.table_type,
                    column_context=source_tile.table_tile.column_context,
                    page_number=source_tile.table_tile.page_number,
                    column_order=source_tile.table_tile.column_order,
                    hidden_columns=source_tile.table_tile.hidden_columns,
                    sorting=source_tile.table_tile.sorting,
                    grouping=source_tile.table_tile.grouping,
                    group_sorting=source_tile.table_tile.group_sorting,
                    columns_pin_left=source_tile.table_tile.columns_pin_left,
                    columns_pin_right=source_tile.table_tile.columns_pin_right,
                    selected=source_tile.table_tile.selected
                )
            else:
                # Create new specialized tile
                self.create_table_tile(
                    tab_id=effective_tab_id,
                    name=source_tile.name,
                    tile_id=updated.id,
                    table_type=source_tile.table_tile.table_type,
                    column_context=source_tile.table_tile.column_context,
                    page_number=source_tile.table_tile.page_number,
                    column_order=source_tile.table_tile.column_order,
                    hidden_columns=source_tile.table_tile.hidden_columns,
                    sorting=source_tile.table_tile.sorting,
                    grouping=source_tile.table_tile.grouping,
                    group_sorting=source_tile.table_tile.group_sorting,
                    columns_pin_left=source_tile.table_tile.columns_pin_left,
                    columns_pin_right=source_tile.table_tile.columns_pin_right,
                    selected=source_tile.table_tile.selected,
                    is_checkpoint=True
                )
        
        elif source_tile.type == "Plot" and hasattr(source_tile, 'plot_tile') and source_tile.plot_tile:
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_plot_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True
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
                    regression_line=source_tile.plot_tile.regression_line
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
                    is_checkpoint=True
                )
        
        elif source_tile.type == "View" and hasattr(source_tile, 'view_tile') and source_tile.view_tile:
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_view_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True
            )
            
            if existing_specialized:
                # Update existing specialized tile
                self.update_view_tile(
                    id=str(existing_specialized.id),
                    base_index=source_tile.view_tile.base_index
                )
            else:
                # Create new specialized tile
                self.create_view_tile(
                    tab_id=effective_tab_id,
                    name=source_tile.name,
                    tile_id=updated.id,
                    base_index=source_tile.view_tile.base_index,
                    is_checkpoint=True
                )
        
        elif source_tile.type == "Editor" and hasattr(source_tile, 'editor_tile') and source_tile.editor_tile:
            # Check if checkpoint specialized tile exists
            existing_specialized = self.get_editor_tile(
                tab_id=effective_tab_id,
                name=source_tile.name,
                is_checkpoint=True
            )
            
            if existing_specialized:
                # Update existing specialized tile
                self.update_editor_tile(
                    id=str(existing_specialized.id),
                    content=source_tile.editor_tile.content,
                    file_name=source_tile.editor_tile.file_name,
                    file_type=source_tile.editor_tile.file_type
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
                    is_checkpoint=True
                )
        
        # Get the full checkpoint tile with all associated data
        checkpoint_tile = self.get_by_tab_and_name(
            tab_id=effective_tab_id,
            name=source_tile.name,
            is_checkpoint=True
        )
        
        return checkpoint_tile
        
    def get_checkpoint(self, id: Optional[str] = None, tab_id: Optional[str] = None, name: Optional[str] = None) -> Optional[Tile]:
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
            checkpoint = self._get_tile(id=tile.checkpoint_or_active_id, is_checkpoint=True)
            if checkpoint and checkpoint.is_checkpoint:
                return checkpoint
        
        # If no direct reference, try to find by tab_id and name
        return self._get_tile(
            tab_id=tile.tab_id,
            name=tile.name,
            is_checkpoint=True
        )
        
    def get_current(self, id: Optional[str] = None, tab_id: Optional[str] = None, name: Optional[str] = None) -> Optional[Tile]:
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
        return self._get_tile(
            tab_id=tile.tab_id,
            name=tile.name,
            is_checkpoint=False
        )
