import os

DEFAULT_DB = "sqlite:////srv/scpushack/database.sqlite"


def _ensure_sqlite_dir(uri: str) -> None:
    if not uri or not uri.startswith("sqlite:"):
        return
    if uri.startswith("sqlite:////"):
        path = "/" + uri[len("sqlite:////"):]
    elif uri.startswith("sqlite:///"):
        path = uri[len("sqlite:///"):]
    else:
        return
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


class Config(object):
    SQLALCHEMY_DATABASE_URI = os.environ.get("SQLALCHEMY_DATABASE_URI", DEFAULT_DB)
    if SQLALCHEMY_DATABASE_URI == DEFAULT_DB:
        IN_MEMORY = True
    else:
        IN_MEMORY = False
    DEVELOPMENT = True
    DEBUG = True  # some Flask specific configs
    SECRET_KEY = 'ScphusHack2021!'
    SQLALCHEMY_ECHO = False
    SQLALCHEMY_TRACK_MODIFICATIONS = False


_ensure_sqlite_dir(Config.SQLALCHEMY_DATABASE_URI)
