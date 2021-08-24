from sqlalchemy import create_engine, MetaData
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, LargeBinary
from sqlalchemy.orm import scoped_session, sessionmaker
from app.config import Config
from sqlalchemy.orm import declarative_base
import datetime

Base = declarative_base()

engine = create_engine(Config().SQLALCHEMY_DATABASE_URI, echo=True)
db_session = scoped_session(sessionmaker(autocommit=False,
                                         autoflush=False,
                                         bind=engine))


class ScpusRequest(Base):
    __tablename__ = "history"
    id = Column(Integer, primary_key=True)
    query = Column(String(2048))
    ip = Column(String(64), default="0.0.0.0")
    count = Column(Integer, default=-1)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    fetched = Column(Boolean)


class ScpusFeed(Base):
    __tablename__ = "feed"
    id = Column(Integer, primary_key=True)
    feed_content = Column(LargeBinary(length=(2**32)-1), default=None)
    count = Column(Integer)
    query = Column(String(2048))
    lastBuildDate = Column(DateTime, default=datetime.datetime.utcnow)


Base.metadata.create_all(bind=engine)

