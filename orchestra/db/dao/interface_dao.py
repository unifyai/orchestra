from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Interface


class InterfaceDAO:
    """Data Access Object for Interface entity."""
    
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_interface(
        self,
        name: str,
        project_id: int,
        items: str = "[]",
        new_counter: int = 0,
        context: str = None,
        color: str = None,
        active_tab_id: str = None,
        is_checkpoint: bool = False,
    ) -> Interface:
        """Create a new interface."""
        interface = Interface(
            name=name,
            items=items,
            new_counter=new_counter,
            project_id=project_id,
            context=context,
            color=color,
            active_tab_id=active_tab_id,
            is_checkpoint=is_checkpoint,
        )
        self.session.add(interface)
        self.session.commit()
        self.session.refresh(interface)
        return interface

    def get(self, id: str) -> Optional[Interface]:
        """Get interface by ID."""
        query = select(Interface).where(Interface.id == id)
        return self.session.execute(query).scalars().first()

    def get_by_project_and_name(
        self, 
        project_id: int, 
        name: str,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Interface]:
        """Get interface by project ID and name."""
        query = select(Interface).where(
            Interface.project_id == project_id,
            Interface.name == name,
        )
        
        if is_checkpoint is not None:
            query = query.where(Interface.is_checkpoint == is_checkpoint)
            
        return self.session.execute(query).scalars().first()

    def get_interfaces(
        self,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
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
        interfaces = self.session.execute(query).scalars().all()
        return interfaces

    def update_interface(
        self,
        id: str,
        name: Optional[str] = None,
        items: Optional[str] = None,
        new_counter: Optional[int] = None,
        project_id: Optional[int] = None,
        context: Optional[str] = None,
        color: Optional[str] = None,
        active_tab_id: Optional[str] = None,
        is_checkpoint: Optional[bool] = None,
    ) -> Optional[Interface]:
        """Update interface by ID."""
        interface = self.get(id)
        if interface is None:
            return None
            
        if name is not None:
            interface.name = name
        if items is not None:
            interface.items = items
        if new_counter is not None:
            interface.new_counter = new_counter
        if project_id is not None:
            interface.project_id = project_id
        if context is not None:
            interface.context = context
        if color is not None:
            interface.color = color
        if active_tab_id is not None:
            interface.active_tab_id = active_tab_id
        if is_checkpoint is not None:
            interface.is_checkpoint = is_checkpoint
            
        self.session.commit()
        self.session.refresh(interface)
        return interface

    def delete_interface(self, id: str) -> bool:
        """Delete interface by ID."""
        interface = self.get(id)
        if interface is None:
            return False
            
        self.session.delete(interface)
        self.session.commit()
        return True
        
    def make_checkpoint(self, id: str) -> Optional[Interface]:
        """Mark an interface as a checkpoint (manual save)."""
        return self.update_interface(id, is_checkpoint=True)
    
    def get_latest_checkpoint(self, project_id: int, name: str) -> Optional[Interface]:
        """Get the latest manually saved checkpoint for an interface."""
        query = select(Interface).where(
            Interface.project_id == project_id,
            Interface.name == name,
            Interface.is_checkpoint == True
        ).order_by(Interface.updated_at.desc())
        
        return self.session.execute(query).scalars().first()
