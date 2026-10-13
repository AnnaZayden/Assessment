import uvicorn

from .config import Settings
from .main import create_app
from .observability import configure_logging

configure_logging()
settings = Settings.from_env()
uvicorn.run(
    create_app(settings),
    host=settings.host,
    port=settings.port,
    access_log=False,  # uvicorn's access log would record full source URLs; ours records only the route
)
