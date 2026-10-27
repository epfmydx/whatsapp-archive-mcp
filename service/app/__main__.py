import logging
from gunicorn.app.base import BaseApplication
from . import create_app
from .observability import SafeGunicornLogger

class Server(BaseApplication):
    def load_config(self):
        for key, value in {"bind": "0.0.0.0:8000", "workers": 2, "threads": 4, "timeout": 60, "accesslog": None, "errorlog": "-", "forwarded_allow_ips": "", "secure_scheme_headers": {}, "logger_class": SafeGunicornLogger}.items():
            self.cfg.set(key, value)
    def load(self): return create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    Server().run()
