from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import FavoriteProject


class FavoriteProjectDAO:
    def __init__(self, session: Session):
        self.session = session

    def filter_by_user(self, user_id: str) -> List[FavoriteProject]:
        """
        Retrieve all favorite projects for a given user.
        """
        query = select(FavoriteProject).where(FavoriteProject.user_id == user_id)
        rows = self.session.execute(query)
        return rows.scalars().all()

    def delete_by_user(self, user_id: str) -> None:
        """
        Bulk delete all favorite projects for a given user.
        """
        try:
            self.session.query(FavoriteProject).filter(
                FavoriteProject.user_id == user_id,
            ).delete(synchronize_session=False)
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise ValueError(
                f"Failed to delete favorite projects for user {user_id}",
            ) from e

    def create(
        self,
        user_id: str,
        project_id: int,
        position: int,
    ) -> FavoriteProject:
        """
        Create a new favorite project entry for a user.

        Returns:
            The created FavoriteProject instance.

        Raises:
            ValueError: If a unique constraint is violated (e.g., user already favorited this project).
        """
        favorite = FavoriteProject(
            user_id=user_id,
            project_id=project_id,
            position=position,
        )
        self.session.add(favorite)
        try:
            self.session.commit()
            return favorite
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to create favorite project: {str(e)}") from e

    def get_by_id(self, user_id: str, favorite_id: int) -> FavoriteProject:
        """
        Retrieve a specific favorite project by ID for a given user.

        Args:
            user_id: The ID of the user.
            favorite_id: The ID of the favorite project entry.

        Returns:
            The favorite project if found.

        Raises:
            ValueError: If the favorite project is not found.
        """
        query = select(FavoriteProject).where(
            FavoriteProject.id == favorite_id,
            FavoriteProject.user_id == user_id,
        )
        result = self.session.execute(query).scalar_one_or_none()
        if result is None:
            raise ValueError(
                f"Favorite project with ID {favorite_id} not found for user {user_id}",
            )
        return result

    def get_by_user_and_project(self, user_id: str, project_id: int) -> FavoriteProject:
        """
        Retrieve a favorite project by user ID and project ID.

        Args:
            user_id: The ID of the user.
            project_id: The ID of the project.

        Returns:
            The favorite project if found, None otherwise.
        """
        query = select(FavoriteProject).where(
            FavoriteProject.user_id == user_id,
            FavoriteProject.project_id == project_id,
        )
        return self.session.execute(query).scalar_one_or_none()

    def update(
        self,
        user_id: str,
        favorite_id: int,
        position: int = None,
    ) -> FavoriteProject:
        """
        Update a favorite project entry.

        Args:
            user_id: The ID of the user.
            favorite_id: The ID of the favorite project entry.
            icon: The new icon (optional).
            position: The new position (optional).

        Returns:
            The updated favorite project.

        Raises:
            ValueError: If the favorite project is not found or if a constraint is violated.
        """
        favorite = self.get_by_id(user_id, favorite_id)

        if position is not None:
            favorite.position = position

        try:
            self.session.commit()
            return favorite
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to update favorite project: {str(e)}") from e

    def delete(self, user_id: str, favorite_id: int) -> None:
        """
        Delete a specific favorite project entry.

        Args:
            user_id: The ID of the user.
            favorite_id: The ID of the favorite project entry.

        Raises:
            ValueError: If the favorite project is not found.
        """
        favorite = self.get_by_id(user_id, favorite_id)

        try:
            self.session.delete(favorite)
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete favorite project: {str(e)}") from e
