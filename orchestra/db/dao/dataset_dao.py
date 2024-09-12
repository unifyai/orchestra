from datetime import datetime
import json
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Dataset,
    StoredPrompt,
    StoredPromptExtraField,
    DatasetPrompt,
)


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
        id: Optional[int] = None,  # noqa: WPS125
        user_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[Dataset]:
        query = select(Dataset)
        if id:
            query = query.where(Dataset.id == id)
        if user_id:
            query = query.where(Dataset.user_id == user_id)
        if name:
            query = query.where(Dataset.name == name)
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

    def fetch_dataset(self, user_id: str, name: str) -> list[dict]:
        try:
            dataset_id = self.filter(user_id=user_id, name=name)[0].id
        except:
            return

        query = (
            select(StoredPrompt, DatasetPrompt)
            .join(DatasetPrompt, StoredPrompt.id == DatasetPrompt.prompt_id)
            .where(DatasetPrompt.dataset_id == dataset_id)
        )

        result = self.session.execute(query).fetchall()
        dataset_prompts = []
        for stored_prompt, _ in result:
            prompt_data = {
                "id": stored_prompt.id,
                "system_msg": json.loads(stored_prompt.system_msg),
                "messages": json.loads(stored_prompt.messages),
                "prompt_kwargs": json.loads(stored_prompt.prompt_kwargs),
                "ref_answer": stored_prompt.ref_answer,
                "num_tokens": stored_prompt.num_tokens,
                "timestamp": stored_prompt.timestamp,
            }
            # Query to get extra fields for this prompt
            extra_fields_query = select(StoredPromptExtraField).where(
                StoredPromptExtraField.prompt_id == stored_prompt.id
            )
            extra_fields = self.session.execute(extra_fields_query).fetchall()

            for extra_field in extra_fields:
                prompt_data[extra_field.field] = extra_field.value

            dataset_prompts.append(prompt_data)

        return dataset_prompts

    def add_prompt_to_dataset(self, user_id, dataset_name, prompt_data):
        try:
            dataset_id = self.filter(user_id=user_id, name=dataset_name)[0].id
        except:
            return {"error": f"Dataset {dataset_name} not found"}
        prompt = prompt_data["prompt"]
        system_msg = prompt.get("system_msg")
        messages = prompt["messages"]
        prompt_kwargs = {
            k: v for k, v in prompt.items() if k not in ["system_msg", "messages"]
        }

        new_prompt = StoredPrompt(
            user_id=user_id,
            system_msg=json.dumps(system_msg),
            messages=json.dumps(messages),
            prompt_kwargs=json.dumps(prompt_kwargs),
            ref_answer=prompt.get("ref_answer"),
            num_tokens=prompt.get("num_tokens", 0),
            timestamp=prompt.get("timestamp", datetime.utcnow()),
        )
        self.session.add(new_prompt)
        self.session.flush()

        # add extra fields
        for field, value in prompt_data.items():
            if field in ["prompt", "ref_answer", "num_tokens", "timestamp"]:
                continue
            extra_field = StoredPromptExtraField(
                prompt_id=new_prompt.id,
                field=field,
                value=value,
            )
            self.session.add(extra_field)
            self.session.flush()

        dataset_prompt = DatasetPrompt(dataset_id=dataset_id, prompt_id=new_prompt.id)
        self.session.add(dataset_prompt)
        self.session.commit()

    def remove_prompt_from_dataset(self, user_id, dataset_name, prompt_id):
        try:
            dataset_id = self.filter(user_id=user_id, name=dataset_name)[0].id
        except:
            return {"error": f"Dataset {dataset_name} not found"}

        try:
            dataset_prompt = (
                self.session.query(DatasetPrompt)
                .filter_by(dataset_id=dataset_id, prompt_id=prompt_id)
                .one()
            )

            self.session.delete(dataset_prompt)
            self.session.commit()
            return {"message": "Dataset prompt deleted successfully"}
        except:
            self.session.rollback()
            return {"error": "Dataset prompt not found"}

    def delete_dataset(self, user_id, name):
        try:
            dataset = (
                self.session.query(Dataset).filter_by(user_id=user_id, name=name).one()
            )
            self.session.delete(dataset)
            self.session.commit()
            return {"info": "Dataset deleted successfully"}
        except:
            self.session.rollback()
            # TODO: This should be an exception instead of 200
            return {"message": "Unable to delete dataset"}
