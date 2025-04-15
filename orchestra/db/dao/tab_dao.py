from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Tab


class TabDAO:
    """Data Access Object for Tab entity."""
    
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_tab(
        self,
        interface_id: str,
        name: str,
        visible: bool = True,
        active: bool = False,
        order: int = 0,
        global_context: Optional[str] = None,
        color: Optional[str] = None,
        is_checkpoint: bool = False,
    ) -> Tab:
        """Create a new tab in an interface."""
        tab = Tab(
            interface_id=interface_id,
            name=name,
            visible=visible,
            active=active,
            order=order,
            global_context=global_context,
            color=color,
            is_checkpoint=is_checkpoint,
        )
        self.session.add(tab)
        self.session.commit()
        self.session.refresh(tab)
        return tab

    def get_tab(self, id: str) -> Optional[Tab]:
        """Get tab by ID."""
        query = select(Tab).where(Tab.id == id)
        return self.session.execute(query).scalars().first()

    def get_tab_by_interface_and_name(
        self, 
        interface_id: str, 
        name: str,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Tab]:
        """Get tab by interface ID and name."""
        query = select(Tab).where(
            Tab.interface_id == interface_id,
            Tab.name == name,
        )
        
        if is_checkpoint is not None:
            query = query.where(Tab.is_checkpoint == is_checkpoint)
            
        return self.session.execute(query).scalars().first()

    def list_tabs_by_interface(
        self, 
        interface_id: str,
        is_checkpoint: Optional[bool] = None,
    ) -> List[Tab]:
        """List all tabs for an interface."""
        query = select(Tab).where(Tab.interface_id == interface_id)
        
        if is_checkpoint is not None:
            query = query.where(Tab.is_checkpoint == is_checkpoint)
            
        query = query.order_by(Tab.order.asc())
        return self.session.execute(query).scalars().all()

    def update_tab(
        self,
        id: str,
        name: Optional[str] = None,
        visible: Optional[bool] = None,
        active: Optional[bool] = None,
        order: Optional[int] = None,
        global_context: Optional[str] = None,
        color: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Tab]:
        """Update tab by ID."""
        tab = self.get_tab(id)
        if tab is None:
            return None
            
        if name is not None:
            tab.name = name
        if visible is not None:
            tab.visible = visible
        if active is not None:
            tab.active = active
        if order is not None:
            tab.order = order
        if global_context is not None:
            tab.global_context = global_context
        if color is not None:
            tab.color = color
        if is_checkpoint is not None:
            tab.is_checkpoint = is_checkpoint
            
        self.session.commit()
        self.session.refresh(tab)
        return tab

    def delete_tab(self, id: str) -> bool:
        """Delete tab by ID."""
        tab = self.get_tab(id)
        if tab is None:
            return False
            
        self.session.delete(tab)
        self.session.commit()
        return True

    def set_active_tab(
        self, 
        interface_id: str, 
        tab_id: str,
        is_checkpoint: Optional[bool] = None,
    ) -> bool:
        """
        Set a tab as active and deactivate all other tabs in the interface.
        Also updates the interface's active_tab_id.
        """
        # Get all tabs for the interface
        tabs = self.list_tabs_by_interface(interface_id, is_checkpoint=is_checkpoint)
        
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
        interface = interface_dao.update_interface(id=interface_id, active_tab_id=tab_id)
        
        self.session.commit()
        return interface is not None
        
    def make_checkpoint(self, id: str) -> Optional[Tab]:
        """Mark a tab as a checkpoint (manually saved)."""
        return self.update_tab(id, is_checkpoint=True)
    
    def get_latest_checkpoint(self, interface_id: str, name: str) -> Optional[Tab]:
        """Get the latest manually saved checkpoint for a tab."""
        query = select(Tab).where(
            Tab.interface_id == interface_id,
            Tab.name == name,
            Tab.is_checkpoint == True
        ).order_by(Tab.updated_at.desc())
        
        return self.session.execute(query).scalars().first() 