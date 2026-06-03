"""orchestra package."""

from orchestra.services.bucket_service import BucketService
from orchestra_core.db.dao.log_event_dao import LogEventDAO

LogEventDAO.bucket_service_factory = BucketService
