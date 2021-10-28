import os

DEFAULT_DB = "sqlite:////tmp/memory"


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

