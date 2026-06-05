"""orchestra package."""

from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.services.bucket_service import create_bucket_service

LogEventDAO.bucket_service_factory = create_bucket_service
