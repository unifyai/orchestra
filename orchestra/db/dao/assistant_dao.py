import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import available_timezones

from fastapi import HTTPException, status
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    Log,
    LogEvent,
    LogEventContext,
    LogEventLog,
    Project,
)
from orchestra.settings import settings
from orchestra.web.api.utils.assistant_infra import send_unify_message

VALID_TIMEZONES = available_timezones()


class AssistantDAO:
    """
    Data access object for Assistant operations.
    """

    def __init__(self, session: Session):
        self.session = session

    def _get_latest_assistant_log_timestamp(
        self,
        project_id: int,
        context_id: int,
    ) -> float:
        """
        Retrieves the latest 'updated_at' timestamp from logs for a specific
        project and context, filtered for assistant messages.
        """
        stmt = (
            select(func.max(LogEvent.updated_at))
            .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
            .where(
                LogEvent.project_id == project_id,
                LogEventContext.context_id == context_id,
            )
            .where(
                exists(
                    select(1)
                    .select_from(LogEventLog)
                    .join(Log, Log.id == LogEventLog.log_id)
                    .where(
                        LogEventLog.log_event_id == LogEvent.id,
                        Log.key == "medium",
                        Log.value == '"unify_message"',
                    ),
                ),
            )
            .where(
                exists(
                    select(1)
                    .select_from(LogEventLog)
                    .join(Log, Log.id == LogEventLog.log_id)
                    .where(
                        LogEventLog.log_event_id == LogEvent.id,
                        Log.key == "sender_id",
                        Log.value == "0",
                    ),
                ),
            )
        )
        latest_timestamp_dt = self.session.execute(stmt).scalar_one_or_none()

        if latest_timestamp_dt:
            return latest_timestamp_dt.timestamp()
        return 0.0

    def _get_latest_assistant_log_content(
        self,
        project_id: int,
        context_id: int,
    ) -> Optional[str]:
        """
        Retrieves the 'content' value from the most recently updated log
        for a specific project and context, filtered for assistant messages.
        """
        # Subquery to find the LogEvent.id with the latest timestamp that matches the criteria
        subq = (
            select(LogEvent.id)
            .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
            .where(
                LogEvent.project_id == project_id,
                LogEventContext.context_id == context_id,
            )
            .where(
                exists(
                    select(1)
                    .select_from(LogEventLog)
                    .join(Log, Log.id == LogEventLog.log_id)
                    .where(
                        LogEventLog.log_event_id == LogEvent.id,
                        Log.key == "medium",
                        Log.value == '"unify_message"',
                    ),
                ),
            )
            .where(
                exists(
                    select(1)
                    .select_from(LogEventLog)
                    .join(Log, Log.id == LogEventLog.log_id)
                    .where(
                        LogEventLog.log_event_id == LogEvent.id,
                        Log.key == "sender_id",
                        Log.value == "0",
                    ),
                ),
            )
            .order_by(LogEvent.updated_at.desc())
            .limit(1)
            .scalar_subquery()
        )

        # Main query to fetch the 'content' value for that LogEvent
        stmt = (
            select(Log.value)
            .join(LogEventLog, Log.id == LogEventLog.log_id)
            .where(LogEventLog.log_event_id == subq, Log.key == "content")
        )
        result = self.session.execute(stmt).scalar_one_or_none()

        # JSONB string values are stored with quotes, so we strip them.
        if isinstance(result, str):
            return result.strip('"')
        return result

    def message_assistant(
        self,
        user_id: str,
        assistant_id: int,
        contact_id: int,
        message: str,
    ) -> str:
        # 1. Get assistant details to form the context name
        assistant = self.get_assistant_by_id(user_id=user_id, agent_id=assistant_id)
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Assistant with ID {assistant_id} not found for this user.",
            )

        project = (
            self.session.query(Project)
            .filter_by(user_id=user_id, name="Assistants")
            .one_or_none()
        )
        if not project:
            # This should ideally not happen if an assistant exists, as project is created with first assistant.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistants project not found.",
            )

        context_name = f"{assistant.first_name}{assistant.surname}/Transcripts"
        context = (
            self.session.query(Context)
            .filter_by(project_id=project.id, name=context_name)
            .one_or_none()
        )
        if not context:
            initial_timestamp = 0.0
            context_id = None  # Sentinel value
        else:
            context_id = context.id
            # 2. Get the latest timestamp *before* sending the message
            initial_timestamp = self._get_latest_assistant_log_timestamp(
                project_id=project.id,
                context_id=context.id,
            )

        try:
            # 3. Send the message via the webhook
            send_unify_message(
                assistant_id=str(assistant_id),
                contact_id=contact_id,
                message=message,
                is_staging=settings.is_staging,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logging.error(
                f"Failed to send message to assistant {assistant_id} via webhook. Error: {e}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send message to assistant.",
            )

        # TODO: Update the response fetching to avoid relying on the transcripts logs
        # ---------------------------------------------------------------------------

        # 4. Poll for the response
        timeout_seconds = 60
        poll_interval_seconds = 2
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            # Re-fetch context in case it was created by the webhook now
            if context_id is None:
                context = (
                    self.session.query(Context)
                    .filter_by(project_id=project.id, name=context_name)
                    .one_or_none()
                )
                if context:
                    context_id = context.id
                else:
                    # If context is still not there, no new message has arrived yet.
                    time.sleep(poll_interval_seconds)
                    continue

            try:
                self.session.commit()
                latest_timestamp = self._get_latest_assistant_log_timestamp(
                    project_id=project.id,
                    context_id=context_id,
                )
                if latest_timestamp > initial_timestamp:
                    # 5. A new message has arrived, fetch it
                    response_content = self._get_latest_assistant_log_content(
                        project_id=project.id,
                        context_id=context_id,
                    )
                    if response_content is None:
                        # This can happen in a race condition. Let's poll again.
                        logging.warning(
                            "Detected new message timestamp but failed to retrieve content. Retrying...",
                        )
                        time.sleep(0.5)  # short sleep and retry
                        continue

                    # 6. Return the content
                    return response_content
            except Exception as e:
                # If get_logs_latest_timestamp fails, we log it and continue polling.
                logging.warning(
                    f"Polling for assistant response failed on one attempt. Error: {e}",
                )

            time.sleep(poll_interval_seconds)

        # 7. If the loop finishes, it's a timeout
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail=f"Did not receive a response from the assistant within {timeout_seconds} seconds.",
        )

        # ---------------------------------------------------------------------------

    def create_assistant(
        self,
        user_id: str,
        first_name: str,
        surname: str,
        age: int,
        region: str,
        about: str,
        weekly_limit: Decimal,
        max_parallel: int,
        profile_photo: Optional[str] = None,
        profile_video: Optional[str] = None,
        desktop_url: Optional[str] = None,
        user_local_desktop: Optional[str] = None,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        voice_id: Optional[str] = None,
        voice_provider: Optional[str] = None,
        voice_mode: Optional[str] = None,
        country: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> Assistant:
        """
        Create a new Assistant for the given user.
        """

        if timezone is not None and timezone not in VALID_TIMEZONES:
            raise ValueError(f"'{timezone}' is not a valid IANA timezone.")

        assistant = Assistant(
            user_id=user_id,
            first_name=first_name,
            surname=surname,
            age=age,
            region=region,
            profile_photo=profile_photo,
            profile_video=profile_video,
            desktop_url=desktop_url,
            user_local_desktop=user_local_desktop,
            about=about,
            weekly_limit=weekly_limit,
            max_parallel=max_parallel,
            phone=phone,
            user_phone=user_phone,
            email=email,
            user_whatsapp_number=user_whatsapp_number,
            voice_id=voice_id,
            voice_provider=voice_provider,
            voice_mode=voice_mode,
            country=country,
            timezone=timezone,
        )
        self.session.add(assistant)
        self.session.flush()
        return assistant

    def get_assistant_by_id(self, user_id: str, agent_id: int) -> Optional[Assistant]:
        """
        Retrieve an Assistant by user and agent IDs.
        """
        stmt = select(Assistant).where(
            Assistant.agent_id == agent_id,
            Assistant.user_id == user_id,
        )
        result = self.session.execute(stmt).scalar_one_or_none()
        return result

    def list_assistants_for_user(
        self,
        user_id: str,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
    ) -> List[Assistant]:
        """
        List all Assistants belonging to a specific user.
        """
        stmt = select(Assistant).where(Assistant.user_id == user_id)
        if phone is not None:
            stmt = stmt.where(Assistant.phone == phone)
        if user_phone is not None:
            stmt = stmt.where(Assistant.user_phone == user_phone)
        if email is not None:
            stmt = stmt.where(Assistant.email == email)
        if user_whatsapp_number is not None:
            stmt = stmt.where(Assistant.user_whatsapp_number == user_whatsapp_number)
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                Assistant.assistant_whatsapp_number == assistant_whatsapp_number,
            )
        result = self.session.execute(stmt).scalars().all()
        return result

    def delete_assistant(self, user_id: str, agent_id: int) -> None:
        """
        Delete an Assistant by user and agent IDs.
        """
        assistant = self.get_assistant_by_id(user_id, agent_id)
        if assistant:
            self.session.delete(assistant)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

    def update_assistant(
        self,
        user_id: str,
        agent_id: int,
        update_data: Dict[str, Any],
    ) -> Optional[Assistant]:
        """
        Update configuration for an existing Assistant.
        """
        assistant = self.get_assistant_by_id(user_id, agent_id)
        if not assistant:
            return None

        if "timezone" in update_data:
            tz = update_data["timezone"]
            if tz is not None and tz not in VALID_TIMEZONES:
                raise ValueError(f"'{tz}' is not a valid IANA timezone.")

        for key, value in update_data.items():
            setattr(assistant, key, value)

        self.session.add(assistant)
        return assistant

    def list_all_assistants(
        self,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        email: Optional[str] = None,
        agent_id: Optional[int] = None,
    ) -> List[Assistant]:
        """
        List all Assistants across all users with optional filtering.
        """
        stmt = select(Assistant)
        if phone is not None:
            stmt = stmt.where(Assistant.phone == phone)
        if user_phone is not None:
            stmt = stmt.where(Assistant.user_phone == user_phone)
        if user_whatsapp_number is not None:
            stmt = stmt.where(Assistant.user_whatsapp_number == user_whatsapp_number)
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                Assistant.assistant_whatsapp_number == assistant_whatsapp_number,
            )
        if email is not None:
            stmt = stmt.where(Assistant.email == email)
        if agent_id is not None:
            stmt = stmt.where(Assistant.agent_id == agent_id)
        result = self.session.execute(stmt).scalars().all()
        return result

    def list_all_assistant_emails(self) -> List[str]:
        """
        List all non-null email addresses from all Assistants.
        """
        stmt = select(Assistant.email).where(Assistant.email.is_not(None))
        result = self.session.execute(stmt).scalars().all()
        return result
