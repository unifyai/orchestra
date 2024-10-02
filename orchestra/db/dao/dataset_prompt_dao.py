from fastapi import Depends
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DatasetPrompt


class DatasetPromptDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        prompt_id: int,
        dataset_id: int,
    ) -> None:
        self.session.add(
            DatasetPrompt(
                prompt_id=prompt_id,
                dataset_id=dataset_id,
            ),
        )

    def delete(self, dataset_id):
        dataset_prompts = (
            self.session.query(DatasetPrompt).filter_by(dataset_id=dataset_id).all()
        )
        for dp in dataset_prompts:
            self.session.delete(dp)
        self.session.commit()
        return {"info": "Dataset deleted successfully"}
