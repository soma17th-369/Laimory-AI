"""staging RDB(MySQL) 접속 인프라.

SQLAlchemy async engine/session 을 설정에서 조립한다. `db_enabled` 가 True 일 때만
실제 접속이 필요하며, 접속은 host/port **직결**이다. 앱은 SSH 터널을 모른다 —
prod 는 VPC 로 private subnet DB 에 바로 붙고, 로컬 검증은 SSH 터널의 로컬 포트를
`DB_HOST` 로 가리킨다(둘 다 같은 코드).

드라이버는 aiomysql(순수 Python)이라 Windows/Linux/Python 3.14 모두에서 설치된다.
나중에 asyncmy 등으로 교체할 때는 URL drivername 과 의존성만 바꾸면 된다.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """staging 테이블 ORM 모델의 공통 declarative base."""


def build_db_url() -> URL:
    """설정에서 SQLAlchemy async DB URL 을 만든다.

    `URL.create` 가 비밀번호의 특수문자를 알아서 인코딩하므로 문자열 조립보다 안전하다.
    """

    return URL.create(
        drivername="mysql+aiomysql",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        query={"charset": "utf8mb4"},
    )


@lru_cache
def get_engine() -> AsyncEngine:
    """async engine 싱글턴을 반환한다.

    `pool_pre_ping` 으로 끊긴 커넥션을 재사용하지 않도록 한다(터널/유휴 대비).
    """

    return create_async_engine(
        build_db_url(),
        echo=settings.db_echo,
        pool_pre_ping=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """async session factory 싱글턴을 반환한다."""

    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """트랜잭션 경계를 감싼 async session 컨텍스트.

    정상 종료 시 commit, 예외 시 rollback 후 예외를 다시 던진다. 저장 로직은 이
    한 블록 안에서 INSERT/UPDATE 를 모두 수행해 원자적으로 커밋한다.
    """

    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
