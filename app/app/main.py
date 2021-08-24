from flask import Flask
import os
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

from app.config import Config

app = Flask(__name__)
API_KEY = os.environ["API_KEY"]
ROOT_URL = os.environ.get("ROOT_URL","https://scpushack.nextnet.top")
SCPUS_BACKEND = f'https://api.elsevier.com/content/search/scopus?start=%d&count=%d&query=%s&apiKey={API_KEY}'
db = None
app.config.from_object(Config())
SCPUS_BACKEND
db = SQLAlchemy(app)

if Config().IN_MEMORY:
    db.create_all()
    db.session.commit()

print(f"Using API_KEY={API_KEY}")


print("launching FLASK")
#app.run(host="0.0.0.0")
socketio = SocketIO(app)
socketio.init_app(app, cors_allowed_origins="*")
socketio.run(app, host="0.0.0.0")
from app.rest import *
from app.websocket import *

print("launching FLASK DONE")



