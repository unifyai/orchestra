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
        position_x: int = 0,
        position_y: int = 0,
        width: int = 400,
        height: int = 400,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: int = 0,
        is_checkpoint: bool = False,
    ) -> Tile:
        """
        Create a new tile in a tab.
        
        Args:
            tab_id: The ID of the tab to create the tile in
            type: The type of tile (Table, Plot, View, Editor)
            name: The name of the tile
            position_x: The x position of the tile
            position_y: The y position of the tile
            width: The width of the tile
            height: The height of the tile
            meta: Optional metadata for the tile
            dependencies: Optional list of dependencies
            state: Optional state data
            order: The order of the tile
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
            position_x=position_x,
            position_y=position_y,
            width=width,
            height=height,
            meta=json.dumps(meta or {}),
            dependencies=json.dumps(dependencies or []),
            state=json.dumps(state or {}),
            order=order,
            is_checkpoint=is_checkpoint,
        )
        self.session.add(tile)
        self.session.commit()
        return tile

    def _get_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
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

    def get(self, id: str, is_checkpoint: Optional[bool] = None) -> Optional[Tile]:
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
        is_checkpoint: Optional[bool] = None,
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
        is_checkpoint: Optional[bool] = None,
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
        is_checkpoint: Optional[bool] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: Optional[int] = None,
    ) -> Optional[Tile]:
        """
        Update tile by ID or by tab_id and name.
        
        Args:
            id: The ID of the tile
            tab_id: The ID of the tab
            name: The name of the tile
            is_checkpoint: Whether to update a checkpoint tile
            position_x: New x position
            position_y: New y position
            width: New width
            height: New height
            meta: New metadata
            dependencies: New dependencies
            state: New state
            order: New order
            
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
            
        # Only update name if we identified by ID
        if name is not None and id is not None:
            tile.name = name
        if position_x is not None:
            tile.position_x = position_x
        if position_y is not None:
            tile.position_y = position_y
        if width is not None:
            tile.width = width
        if height is not None:
            tile.height = height
        if meta is not None:
            tile.meta = json.dumps(meta)
        if dependencies is not None:
            tile.dependencies = json.dumps(dependencies)
        if state is not None:
            tile.state = json.dumps(state)
        if order is not None:
            tile.order = order
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
        is_checkpoint: Optional[bool] = None,
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
        
    def make_checkpoint(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Tile]:
        """
        Mark a tile as a checkpoint (manually saved) by ID or by tab_id and name.
        
        Args:
            id: The ID of the tile
            tab_id: The ID of the tab
            name: The name of the tile
            
        Returns:
            The checkpointed tile if found, None otherwise
            
        Raises:
            ValueError: If neither id nor (tab_id and name) are provided
        """
        if not id and not (tab_id and name):
            raise ValueError("Either id or both tab_id and name must be provided")
            
        return self.update_tile(
            id=id, 
            tab_id=tab_id, 
            name=name, 
            is_checkpoint=True
        )
    
    def get_latest_checkpoint(self, tab_id: str, name: str) -> Optional[Tile]:
        """
        Get the latest manually saved checkpoint for a tile.
        
        Args:
            tab_id: The ID of the tab
            name: The name of the tile
            
        Returns:
            The latest checkpoint tile if found, None otherwise
        """
        query = select(Tile).where(
            Tile.tab_id == tab_id,
            Tile.name == name,
            Tile.is_checkpoint == True
        ).order_by(Tile.updated_at.desc())
        
        return self.session.execute(query).scalars().first()

    # Specialized tile types
    def create_table_tile(
        self,
        tab_id: str,
        name: str,
        position_x: int = 0,
        position_y: int = 0,
        width: int = 600,
        height: int = 400,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: int = 0,
        headers: Optional[list] = None,
        rows: Optional[list] = None,
        is_checkpoint: bool = False,
    ) -> TableTile:
        """Create a new table tile."""
        table_tile = TableTile(
            tab_id=tab_id,
            name=name,
            position_x=position_x,
            position_y=position_y,
            width=width,
            height=height,
            meta=json.dumps(meta or {}),
            dependencies=json.dumps(dependencies or []),
            state=json.dumps(state or {}),
            order=order,
            headers=json.dumps(headers or []),
            rows=json.dumps(rows or []),
            is_checkpoint=is_checkpoint,
        )
        self.session.add(table_tile)
        self.session.commit()
        return table_tile
        
    def _get_specialized_tile(
        self, 
        model_class,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
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
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[TableTile]:
        """Get table tile by ID or by tab_id and name."""
        return self._get_specialized_tile(TableTile, id, tab_id, name, is_checkpoint)
        
    def update_table_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: Optional[int] = None,
        headers: Optional[list] = None,
        rows: Optional[list] = None,
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
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            table_tile.name = name
        if position_x is not None:
            table_tile.position_x = position_x
        if position_y is not None:
            table_tile.position_y = position_y
        if width is not None:
            table_tile.width = width
        if height is not None:
            table_tile.height = height
        if meta is not None:
            table_tile.meta = json.dumps(meta)
        if dependencies is not None:
            table_tile.dependencies = json.dumps(dependencies)
        if state is not None:
            table_tile.state = json.dumps(state)
        if order is not None:
            table_tile.order = order
            
        # Update specialized fields
        if headers is not None:
            table_tile.headers = json.dumps(headers)
        if rows is not None:
            table_tile.rows = json.dumps(rows)
            
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
        position_x: int = 0,
        position_y: int = 0,
        width: int = 600,
        height: int = 400,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: int = 0,
        plot_data: Optional[dict] = None,
        is_checkpoint: bool = False,
    ) -> PlotTile:
        """Create a new plot tile."""
        plot_tile = PlotTile(
            tab_id=tab_id,
            name=name,
            position_x=position_x,
            position_y=position_y,
            width=width,
            height=height,
            meta=json.dumps(meta or {}),
            dependencies=json.dumps(dependencies or []),
            state=json.dumps(state or {}),
            order=order,
            plot_data=json.dumps(plot_data or {}),
            is_checkpoint=is_checkpoint,
        )
        self.session.add(plot_tile)
        self.session.commit()
        return plot_tile
        
    def get_plot_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[PlotTile]:
        """Get plot tile by ID or by tab_id and name."""
        return self._get_specialized_tile(PlotTile, id, tab_id, name, is_checkpoint)
        
    def update_plot_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: Optional[int] = None,
        plot_data: Optional[dict] = None,
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
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            plot_tile.name = name
        if position_x is not None:
            plot_tile.position_x = position_x
        if position_y is not None:
            plot_tile.position_y = position_y
        if width is not None:
            plot_tile.width = width
        if height is not None:
            plot_tile.height = height
        if meta is not None:
            plot_tile.meta = json.dumps(meta)
        if dependencies is not None:
            plot_tile.dependencies = json.dumps(dependencies)
        if state is not None:
            plot_tile.state = json.dumps(state)
        if order is not None:
            plot_tile.order = order
            
        # Update specialized fields
        if plot_data is not None:
            plot_tile.plot_data = json.dumps(plot_data)
            
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
        position_x: int = 0,
        position_y: int = 0,
        width: int = 600,
        height: int = 400,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: int = 0,
        content: str = "",
        language: str = "python",
        is_checkpoint: bool = False,
    ) -> EditorTile:
        """Create a new editor tile."""
        editor_tile = EditorTile(
            tab_id=tab_id,
            name=name,
            position_x=position_x,
            position_y=position_y,
            width=width,
            height=height,
            meta=json.dumps(meta or {}),
            dependencies=json.dumps(dependencies or []),
            state=json.dumps(state or {}),
            order=order,
            content=content,
            language=language,
            is_checkpoint=is_checkpoint,
        )
        self.session.add(editor_tile)
        self.session.commit()
        return editor_tile
        
    def get_editor_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[EditorTile]:
        """Get editor tile by ID or by tab_id and name."""
        return self._get_specialized_tile(EditorTile, id, tab_id, name, is_checkpoint)
        
    def update_editor_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: Optional[int] = None,
        content: Optional[str] = None,
        language: Optional[str] = None,
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
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            editor_tile.name = name
        if position_x is not None:
            editor_tile.position_x = position_x
        if position_y is not None:
            editor_tile.position_y = position_y
        if width is not None:
            editor_tile.width = width
        if height is not None:
            editor_tile.height = height
        if meta is not None:
            editor_tile.meta = json.dumps(meta)
        if dependencies is not None:
            editor_tile.dependencies = json.dumps(dependencies)
        if state is not None:
            editor_tile.state = json.dumps(state)
        if order is not None:
            editor_tile.order = order
            
        # Update specialized fields
        if content is not None:
            editor_tile.content = content
        if language is not None:
            editor_tile.language = language
            
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
        position_x: int = 0,
        position_y: int = 0,
        width: int = 600,
        height: int = 400,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: int = 0,
        view_type: str = "markdown",
        view_data: Optional[dict] = None,
        is_checkpoint: bool = False,
    ) -> ViewTile:
        """Create a new view tile."""
        view_tile = ViewTile(
            tab_id=tab_id,
            name=name,
            position_x=position_x,
            position_y=position_y,
            width=width,
            height=height,
            meta=json.dumps(meta or {}),
            dependencies=json.dumps(dependencies or []),
            state=json.dumps(state or {}),
            order=order,
            view_type=view_type,
            view_data=json.dumps(view_data or {}),
            is_checkpoint=is_checkpoint,
        )
        self.session.add(view_tile)
        self.session.commit()
        return view_tile
        
    def get_view_tile(
        self, 
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[ViewTile]:
        """Get view tile by ID or by tab_id and name."""
        return self._get_specialized_tile(ViewTile, id, tab_id, name, is_checkpoint)
        
    def update_view_tile(
        self,
        id: Optional[str] = None,
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: Optional[int] = None,
        view_type: Optional[str] = None,
        view_data: Optional[dict] = None,
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
            
        # Update the base tile fields
        if name is not None and id is not None:  # Only update name if identifying by ID
            view_tile.name = name
        if position_x is not None:
            view_tile.position_x = position_x
        if position_y is not None:
            view_tile.position_y = position_y
        if width is not None:
            view_tile.width = width
        if height is not None:
            view_tile.height = height
        if meta is not None:
            view_tile.meta = json.dumps(meta)
        if dependencies is not None:
            view_tile.dependencies = json.dumps(dependencies)
        if state is not None:
            view_tile.state = json.dumps(state)
        if order is not None:
            view_tile.order = order
            
        # Update specialized fields
        if view_type is not None:
            view_tile.view_type = view_type
        if view_data is not None:
            view_tile.view_data = json.dumps(view_data)
            
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
        is_checkpoint: Optional[bool] = None,
        tile_type: Optional[str] = None,
    ) -> Optional[Tile]:
        """
        Partially update tile with only the fields that need changing.
        
        Either id or (tab_id and name) must be provided to identify the tile.
        If tile_type is provided, it will be used to determine the specialized tile type;
        otherwise it will be determined from the tile's type field.
        """
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
                        if field in ('headers', 'rows') and not isinstance(value, str):
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
            if 'x' in position:
                tile.position_x = position['x']
            if 'y' in position:
                tile.position_y = position['y']
            if 'width' in position:
                tile.width = position['width']
            if 'height' in position:
                tile.height = position['height']
        
        # Update the base tile fields
        for field, value in update_data.items():
            if hasattr(tile, field):
                if field in ('meta', 'dependencies', 'state') and not isinstance(value, str):
                    setattr(tile, field, json.dumps(value))
                else:
                    setattr(tile, field, value)
        
        self.session.commit()
        return tile
