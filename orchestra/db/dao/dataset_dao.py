from typing import List, Optional, Union

from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Dataset


class DatasetDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        user_id: str,
        name: str,
    ) -> None:
        self.session.add(
            Dataset(
                user_id=user_id,
                name=name,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[Union[int, List[int]]] = None,  # noqa: WPS125
        user_id: Optional[Union[str, List[str]]] = None,
        name: Optional[Union[str, List[str]]] = None,
    ) -> List[Dataset]:
        query = select(Dataset)
        if id:
            id = id if isinstance(id, list) else [id]
            query = query.where(or_(*[Dataset.id == i for i in id]))
        if user_id:
            user_id = user_id if isinstance(user_id, list) else [user_id]
            query = query.where(or_(*[Dataset.user_id == uid for uid in user_id]))
        if name:
            name = name if isinstance(name, list) else [name]
            query = query.where(or_(*[Dataset.name == n for n in name]))
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
    ) -> None:
        query = select(Dataset)
        query = query.where(Dataset.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)  # noqa: B010

    def rename(self, user_id, name, new_name):
        try:
            dataset_id = self.filter(user_id=user_id, name=name)[0].id
        except:
            return {"error": f"No dataset with the name {name}"}

        self.update(id=dataset_id, name=new_name)

    def get_dataset_id(self, user_id: str, name: str) -> List[int]:
        # Accounts for public datasets
        try:
            datasets = self.filter(name=name)
            datasets = [d for d in datasets if d.user_id in [user_id, None]]
            return [
                datasets[0].id,
            ]
        except:
            return []

    def list_datasets(self, user_id: str):
        query = select(Dataset.name).where(
            or_(Dataset.user_id == user_id, Dataset.user_id == None),
        )
        rows = self.session.execute(query)
        return rows.fetchall()

    def get_id(self, user_id: str, name: str, include_public: bool):
        # include_public=True accounts for public datasets (with user_id=None)
        query = select(Dataset.id).where(Dataset.name == name)
        if include_public:
            query = query.where(
                or_(Dataset.user_id == user_id, Dataset.user_id == None),
            )
        else:
            query = query.where(Dataset.user_id == user_id)
        entry = self.session.execute(query).fetchone()
        return entry.id if entry else None

    def delete(self, id: int):
        try:
            dataset = self.session.query(Dataset).filter_by(id=id).one()
            self.session.delete(dataset)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
