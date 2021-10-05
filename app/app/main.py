from flask import Flask
import os
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

from app.config import Config


app = Flask(__name__)
socketio = SocketIO(app)
socketio.init_app(app, cors_allowed_origins="*")
API_KEY = os.environ["API_KEY"]
ROOT_URL = os.environ.get("ROOT_URL", "https://scholar.miage.dev")
SHLINK_API_KEY = os.environ.get("SHLINK_API_KEY", "")
#MENDELEY_CLIENT_ID = os.environ.get("MENDELEY_CLIENT_ID", "")
#MENDELEY_SECRET = os.environ.get("MENDELEY_SECRET", "")

SCPUS_BACKEND = f'https://api.elsevier.com/content/search/scopus?start=%d&count=%d&query=%s&apiKey={API_KEY}'
app.config.from_object(Config())
db = SQLAlchemy(app)

if Config().IN_MEMORY:
    print("### IN MEMORY DB")
    db.create_all()
    db.session.commit()

print("launching FLASK")
# app.run(host="0.0.0.0")
socketio = SocketIO(app)
socketio.init_app(app, cors_allowed_origins="*")
socketio.run(app, host="0.0.0.0")
from app.rest import *
from app.websocket import *

print("launching FLASK DONE")
