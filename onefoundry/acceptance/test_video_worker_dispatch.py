"""Real PostgreSQL oracle for the persistent video's default unfiltered poll.

Run explicitly on a disposable, migrated PostgreSQL database with no queued jobs.
The deployment path calls process_one() without a project argument.
"""

from pathlib import Path

from sqlalchemy import create_engine, text

from onefoundry.tool_gateway import ToolGateway
from runner.video_edit_worker import VideoEditWorker
from tests.test_video_edit_postgres import settings_for_edit


def test_default_poll_can_read_an_empty_queue(tmp_path: Path) -> None:
    settings = settings_for_edit(tmp_path)
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM video_edit_jobs WHERE status='queued'")) == 0
        gateway = ToolGateway(engine, settings.operator_actor_id, live_text_settings=settings)
        assert VideoEditWorker(gateway).process_one() is None
    finally:
        engine.dispose()
