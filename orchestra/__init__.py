"""orchestra package."""

from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.services.bucket_service import BucketService

LogEventDAO.bucket_service_factory = BucketService
