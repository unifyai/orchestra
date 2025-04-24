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
        return tab

    def _get_tab(
        self, 
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
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
            
        return self.session.execute(query).scalars().first()

    def get_tab(self, id: str) -> Optional[Tab]:
        """Get tab by ID."""
        return self._get_tab(id=id)

    def get_tab_by_interface_and_name(
        self, 
        interface_id: str, 
        name: str,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Tab]:
        """Get tab by interface ID and name."""
        return self._get_tab(interface_id=interface_id, name=name, is_checkpoint=is_checkpoint)

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
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
        visible: Optional[bool] = None,
        active: Optional[bool] = None,
        order: Optional[int] = None,
        global_context: Optional[str] = None,
        color: Optional[str] = None,
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
            is_checkpoint=is_checkpoint
        )
        
        if tab is None:
            return None
            
        # Only update name if we identified by ID
        if name is not None and id is not None:
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
        if is_checkpoint is not None and (id is not None or not is_checkpoint):
            # Only update is_checkpoint if:
            # 1. We're identifying by ID, or
            # 2. We're identifying by name and we're setting is_checkpoint to False
            tab.is_checkpoint = is_checkpoint
            
        self.session.commit()
        return tab

    def delete_tab(
        self,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> bool:
        """
        Delete tab by ID or by interface_id and name.
        
        Either id or (interface_id and name) must be provided.
        """
        tab = self._get_tab(
            id=id, 
            interface_id=interface_id, 
            name=name,
            is_checkpoint=is_checkpoint
        )
        
        if tab is None:
            return False
            
        self.session.delete(tab)
        self.session.commit()
        return True

    def set_active_tab(
        self, 
        interface_id: str, 
        tab_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
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
                is_checkpoint=is_checkpoint
            )
            if identified_tab is None:
                return False
            tab_id = identified_tab.id
            
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
        
    def make_checkpoint(
        self,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Tab]:
        """
        Mark a tab as a checkpoint (manually saved) by ID or by interface_id and name.
        
        Either id or (interface_id and name) must be provided.
        """
        return self.update_tab(
            id=id, 
            interface_id=interface_id, 
            name=name, 
            is_checkpoint=True
        )
    
    def get_latest_checkpoint(self, interface_id: str, name: str) -> Optional[Tab]:
        """Get the latest manually saved checkpoint for a tab."""
        query = select(Tab).where(
            Tab.interface_id == interface_id,
            Tab.name == name,
            Tab.is_checkpoint == True
        ).order_by(Tab.updated_at.desc())
        
        return self.session.execute(query).scalars().first()
    
    def patch_tab(
        self,
        update_data: dict,
        id: Optional[str] = None,
        interface_id: Optional[str] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Tab]:
        """
        Partially update tab with only the fields that need changing.
        
        Either id or (interface_id and name) must be provided to identify the tab.
        """
        tab = self._get_tab(
            id=id, 
            interface_id=interface_id, 
            name=name,
            is_checkpoint=is_checkpoint
        )

        if tab is None:
            return None

        # Update only the fields specified in update_data
        for field, value in update_data.items():
            if hasattr(tab, field):
                setattr(tab, field, value)

        self.session.commit()
        return tab

    def patch_tab_by_name(
        self, 
        interface_id: str, 
        name: str,
        update_data: dict,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Tab]:
        """Partially update tab by interface ID and name."""
        # Get the tab by name
        tab = self.get_tab_by_interface_and_name(
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

        self.session.commit()
        return tab
