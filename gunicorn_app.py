import asyncio
import logging
from gunicorn.app.base import BaseApplication
from gunicorn import util
from main import start_bot, shutdown, app as flask_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class CustomGunicornApp(BaseApplication):
    def __init__(self, app, options=None):
        self.application = app
        self.options = options or {}
        super().__init__()

    def load_config(self):
        config = {key: value for key, value in self.options.items() if key in self.cfg.settings and value is not None}
        for key, value in config.items():
            self.cfg.set(key.lower(), value)

    def load(self):
        # Start the asyncio tasks
        asyncio.ensure_future(start_bot())
        return self.application

    def stop(self, *args, **kwargs):
        logger.info("Stopping Gunicorn application...")
        asyncio.run(shutdown())
        super().stop(*args, **kwargs)

def run_gunicorn():
    options = {
        "bind": "0.0.0.0:%s" % os.getenv("PORT", "8091"),
        "workers": 1,
        "worker_class": "eventlet",
        "loglevel": "info",
    }
    CustomGunicornApp(flask_app, options).run()
