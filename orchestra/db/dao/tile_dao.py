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
        """Create a new tile in a tab."""
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
        self.session.refresh(tile)
        return tile

    def get_tile(self, id: str) -> Optional[Tile]:
        """Get tile by ID."""
        query = select(Tile).where(Tile.id == id)
        return self.session.execute(query).scalars().first()

    def get_tile_by_tab_and_name(
        self, 
        tab_id: str, 
        name: str,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Tile]:
        """Get tile by tab ID and name."""
        query = select(Tile).where(
            Tile.tab_id == tab_id,
            Tile.name == name,
        )
        
        if is_checkpoint is not None:
            query = query.where(Tile.is_checkpoint == is_checkpoint)
            
        return self.session.execute(query).scalars().first()

    def list_tiles_by_tab(
        self, 
        tab_id: str,
        is_checkpoint: Optional[bool] = None,
    ) -> List[Tile]:
        """List all tiles for a tab."""
        query = select(Tile).where(Tile.tab_id == tab_id)
        
        if is_checkpoint is not None:
            query = query.where(Tile.is_checkpoint == is_checkpoint)
            
        query = query.order_by(Tile.order.asc())
        return self.session.execute(query).scalars().all()

    def update_tile(
        self,
        id: str,
        name: Optional[str] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: Optional[int] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Tile]:
        """Update tile by ID."""
        tile = self.get_tile(id)
        if tile is None:
            return None
            
        if name is not None:
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
        if is_checkpoint is not None:
            tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        self.session.refresh(tile)
        return tile

    def delete_tile(self, id: str) -> bool:
        """Delete tile by ID."""
        tile = self.get_tile(id)
        if tile is None:
            return False
            
        self.session.delete(tile)
        self.session.commit()
        return True
        
    def make_checkpoint(self, id: str) -> Optional[Tile]:
        """Mark a tile as a checkpoint (manually saved)."""
        return self.update_tile(id, is_checkpoint=True)
    
    def get_latest_checkpoint(self, tab_id: str, name: str) -> Optional[Tile]:
        """Get the latest manually saved checkpoint for a tile."""
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
        self.session.refresh(table_tile)
        return table_tile
        
    def get_table_tile(self, id: str) -> Optional[TableTile]:
        """Get table tile by ID."""
        query = select(TableTile).where(TableTile.id == id)
        return self.session.execute(query).scalars().first()
        
    def update_table_tile(
        self,
        id: str,
        name: Optional[str] = None,
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
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[TableTile]:
        """Update table tile by ID."""
        table_tile = self.get_table_tile(id)
        if table_tile is None:
            return None
            
        if name is not None:
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
        if headers is not None:
            table_tile.headers = json.dumps(headers)
        if rows is not None:
            table_tile.rows = json.dumps(rows)
        if is_checkpoint is not None:
            table_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        self.session.refresh(table_tile)
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
        self.session.refresh(plot_tile)
        return plot_tile
        
    def get_plot_tile(self, id: str) -> Optional[PlotTile]:
        """Get plot tile by ID."""
        query = select(PlotTile).where(PlotTile.id == id)
        return self.session.execute(query).scalars().first()
        
    def update_plot_tile(
        self,
        id: str,
        name: Optional[str] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        meta: Optional[dict] = None,
        dependencies: Optional[list] = None,
        state: Optional[dict] = None,
        order: Optional[int] = None,
        plot_data: Optional[dict] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[PlotTile]:
        """Update plot tile by ID."""
        plot_tile = self.get_plot_tile(id)
        if plot_tile is None:
            return None
            
        if name is not None:
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
        if plot_data is not None:
            plot_tile.plot_data = json.dumps(plot_data)
        if is_checkpoint is not None:
            plot_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        self.session.refresh(plot_tile)
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
        self.session.refresh(editor_tile)
        return editor_tile
        
    def get_editor_tile(self, id: str) -> Optional[EditorTile]:
        """Get editor tile by ID."""
        query = select(EditorTile).where(EditorTile.id == id)
        return self.session.execute(query).scalars().first()
        
    def update_editor_tile(
        self,
        id: str,
        name: Optional[str] = None,
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
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[EditorTile]:
        """Update editor tile by ID."""
        editor_tile = self.get_editor_tile(id)
        if editor_tile is None:
            return None
            
        if name is not None:
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
        if content is not None:
            editor_tile.content = content
        if language is not None:
            editor_tile.language = language
        if is_checkpoint is not None:
            editor_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        self.session.refresh(editor_tile)
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
        self.session.refresh(view_tile)
        return view_tile
        
    def get_view_tile(self, id: str) -> Optional[ViewTile]:
        """Get view tile by ID."""
        query = select(ViewTile).where(ViewTile.id == id)
        return self.session.execute(query).scalars().first()
        
    def update_view_tile(
        self,
        id: str,
        name: Optional[str] = None,
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
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[ViewTile]:
        """Update view tile by ID."""
        view_tile = self.get_view_tile(id)
        if view_tile is None:
            return None
            
        if name is not None:
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
        if view_type is not None:
            view_tile.view_type = view_type
        if view_data is not None:
            view_tile.view_data = json.dumps(view_data)
        if is_checkpoint is not None:
            view_tile.is_checkpoint = is_checkpoint
            
        self.session.commit()
        self.session.refresh(view_tile)
        return view_tile 