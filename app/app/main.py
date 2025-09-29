from flask import Flask
import os
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from app.config import Config


print("creating flask application")
app = Flask(__name__)
print("creating flask application done")

# Configure app and extensions
with app.app_context():
    API_KEY = os.environ.get("API_KEY", "")
    ROOT_URL = os.environ.get("ROOT_URL", "https://scholar.miage.nextnet.top")
    SHLINK_API_KEY = os.environ.get("SHLINK_API_KEY", "")
    SCPUS_BACKEND = f'https://api.elsevier.com/content/search/scopus?start=%d&count=%d&query=%s&apiKey={API_KEY}'
    SCPUS_ABTRACT_BACKEND = f'https://api.elsevier.com/content/abstract/doi/%s?apiKey={API_KEY}'
    app.config.from_object(Config())

    db = SQLAlchemy(app)

    if Config().IN_MEMORY:
        print("### IN MEMORY DB")
        db.create_all()
        db.session.commit()

# SocketIO (initialized outside app_context as recommended)
socketio = SocketIO(app, cors_allowed_origins="*")

# Register routes and Socket.IO events
from app import rest as _rest  # noqa: F401
from app import websocket as _websocket  # noqa: F401


if __name__ == "__main__":
    # Development entrypoint (not used in production containers)
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True, debug=debug)
