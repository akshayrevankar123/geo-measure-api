from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # make sure the parent dir of a file-based sqlite DB exists
        if ":memory:" not in url:
            import os

            path = url.split("///", 1)[-1]
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
    return create_engine(url, **kwargs)


engine = make_engine(settings.db_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
