from sqlalchemy import create_engine, MetaData
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, LargeBinary, Float
from sqlalchemy.orm import scoped_session, sessionmaker

from app.config import Config
from sqlalchemy.orm import declarative_base
import datetime

Base = declarative_base()

engine = create_engine(Config().SQLALCHEMY_DATABASE_URI, echo=True)
db_session = scoped_session(sessionmaker(autocommit=False,
                                         autoflush=False,
                                         bind=engine))


class Ranking(Base):
    __tablename__ = "ranking"
    id = Column(Integer, primary_key=True)
    type = Column(String(1), primary_key=True)
    title = Column(String(128))
    acr = Column(String(64))
    source = Column(String(64), primary_key=True)
    rank = Column(String(128))
    hindex = Column(Float, default=None)


class PublicationSource(Base):
    __tablename__ = "publication_source"
    short_name = Column(String(64), primary_key=True)
    code = Column(String(64), nullable=False)
    full_text_name = Column(String(255), nullable=False)
    category = Column(String(64), default="Uncaterogized")


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
    feed_content = Column(LargeBinary(length=(2 ** 32) - 1), default=None)
    count = Column(Integer)
    query = Column(String(2048))
    lastBuildDate = Column(DateTime, default=datetime.datetime.utcnow)
    hit = Column(Integer, default=0)


Base.metadata.create_all(bind=engine)
