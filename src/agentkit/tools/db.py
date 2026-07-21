"""tools.db —— 数据库查询工具。

提供 ``DBQueryTool``,通过 ``db.query`` 注册到全局 ToolRegistry。

设计要点:
    - 连接配置从环境变量读取(DB_URL / DB_HOST / DB_PORT / DB_USER / DB_PASSWORD /
      DB_NAME / DB_DRIVER / DB_PATH),不依赖外部配置对象。
    - 数据库驱动延迟 import:sqlite3 为标准库始终可用;psycopg2 / pymysql 等
      可选依赖,按需加载,未安装时抛 ImportError 并给出可操作提示。
    - 同步 DB 调用通过 ``asyncio.to_thread`` 包装,避免阻塞事件循环。
    - SELECT(及一切返回行的语句)返回 ``{"rows": [...], "rowcount": N}``;
      非 SELECT 返回 ``{"rowcount": N}`` 并 commit。连接用完即关。
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from agentkit.tools.base import Tool, tool

if TYPE_CHECKING:
    from agentkit.core.context import Context


class DBQueryParams(BaseModel):
    """DB 查询参数。"""

    sql: str = Field(..., description="SQL 查询语句")
    database: str | None = Field(
        None, description="数据库名(可选,默认用连接配置)"
    )


@tool("db.query", role="source")
class DBQueryTool(Tool):
    """数据库查询工具。

    从环境变量读取连接配置(DB_URL / DB_HOST / DB_PORT / DB_USER / DB_PASSWORD /
    DB_NAME / DB_DRIVER / DB_PATH)。实际数据库驱动延迟 import:

        - sqlite3:标准库,始终可用(默认实现)
        - psycopg2:PostgreSQL,可选
        - pymysql:MySQL,可选

    为保证框架开箱即用且不强制依赖,默认实现:

        - 若未配置 DB_URL 且未配置 DB_DRIVER,使用 sqlite3 连接
          ``DB_PATH``(默认 ``:memory:``)执行
        - 若配置了 DB_URL,按 URL scheme 选择驱动
          (``postgresql`` -> psycopg2,``mysql`` -> pymysql,``sqlite`` -> sqlite3)
        - 若配置了 DB_DRIVER,使用对应驱动,按 DB_HOST / DB_PORT / DB_USER /
          DB_PASSWORD / DB_NAME 组装连接

    查询返回:
        - 返回行语句(SELECT / WITH / PRAGMA / SHOW / EXPLAIN 等):
          ``{"rows": [...], "rowcount": N}``,rows 为 dict 列表
          (以 cursor.description 列名为键)
        - 非返回行语句(INSERT / UPDATE / DELETE / DDL):
          ``{"rowcount": N}`` 并 commit

    连接失败 / SQL 错误抛异常,交由上层 ToolStep 的 retry 机制处理。
    """

    description = "执行 SQL 查询,返回结果行"

    @property
    def param_model(self) -> type[BaseModel]:
        return DBQueryParams

    # ------------------------------------------------------------------ #
    # 连接建立
    # ------------------------------------------------------------------ #
    def _get_connection(self, database: str | None):
        """根据环境变量建立数据库连接。

        解析优先级:

            1. ``DB_URL`` -> 按 scheme 选择驱动并解析 URL 组件
            2. ``DB_DRIVER`` 显式指定 -> 使用对应驱动,按 DB_HOST / ... 组装
            3. 默认 -> sqlite3,连接 ``DB_PATH`` 或 ``:memory:``

        Args:
            database: 可选数据库名,覆盖环境变量中的 DB_NAME。

        Returns:
            已建立的 DBAPI 连接对象。

        Raises:
            ImportError: 所需驱动未安装(附安装提示)。
            ValueError: URL scheme / DB_DRIVER 不被支持。
        """
        db_url = os.environ.get("DB_URL")
        if db_url:
            return self._connect_by_url(db_url, database)

        driver = os.environ.get("DB_DRIVER")
        if driver:
            return self._connect_by_driver(driver, database)

        # 默认 sqlite3,开箱即用
        return self._connect_sqlite(database)

    def _connect_sqlite(self, database: str | None):
        """建立 sqlite3 连接。database 优先,其次 DB_PATH,最后 :memory:。"""
        import sqlite3

        path = database or os.environ.get("DB_PATH") or ":memory:"
        return sqlite3.connect(path)

    def _connect_by_url(self, url: str, database: str | None):
        """按 DB_URL 的 scheme 选择驱动并解析组件建立连接。"""
        parsed = urlparse(url)
        scheme = (parsed.scheme or "sqlite").lower()

        if scheme in ("sqlite", "sqlite3"):
            import sqlite3

            # sqlite:///path/to.db -> path 取 parsed.path;
            # database 参数优先级最高
            path = database or (parsed.path or os.environ.get("DB_PATH") or ":memory:")
            return sqlite3.connect(path)

        if scheme in ("postgresql", "postgres", "psql"):
            try:
                import psycopg2
            except ImportError as e:  # pragma: no cover - 依赖环境相关
                raise ImportError(
                    "DB_URL 指向 PostgreSQL,但未安装 psycopg2。"
                    " 请执行 `pip install psycopg2` 后重试。"
                ) from e
            kwargs = self._collect_kwargs(parsed, database, default_port=5432)
            # psycopg2 用 dbname 而非 database
            if "database" in kwargs:
                kwargs["dbname"] = kwargs.pop("database")
            return psycopg2.connect(**kwargs)

        if scheme in ("mysql", "mariadb"):
            try:
                import pymysql
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "DB_URL 指向 MySQL,但未安装 pymysql。"
                    " 请执行 `pip install pymysql` 后重试。"
                ) from e
            kwargs = self._collect_kwargs(parsed, database, default_port=3306)
            return pymysql.connect(**kwargs)

        raise ValueError(f"不支持的 DB_URL scheme: {scheme!r}")

    def _connect_by_driver(self, driver: str, database: str | None):
        """按 DB_DRIVER 名称选择驱动,使用 DB_HOST / DB_PORT / ... 组装连接。"""
        d = driver.lower()
        if d in ("sqlite", "sqlite3"):
            return self._connect_sqlite(database)

        if d in ("postgresql", "postgres", "psql", "psycopg2"):
            try:
                import psycopg2
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "DB_DRIVER 指定 postgresql,但未安装 psycopg2。"
                    " 请执行 `pip install psycopg2` 后重试。"
                ) from e
            kwargs = {
                "host": os.environ.get("DB_HOST", "localhost"),
                "port": int(os.environ.get("DB_PORT", "5432")),
                "user": os.environ.get("DB_USER", "postgres"),
                "password": os.environ.get("DB_PASSWORD", ""),
                "dbname": database or os.environ.get("DB_NAME", "postgres"),
            }
            return psycopg2.connect(**kwargs)

        if d in ("mysql", "mariadb", "pymysql"):
            try:
                import pymysql
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "DB_DRIVER 指定 mysql,但未安装 pymysql。"
                    " 请执行 `pip install pymysql` 后重试。"
                ) from e
            kwargs = {
                "host": os.environ.get("DB_HOST", "localhost"),
                "port": int(os.environ.get("DB_PORT", "3306")),
                "user": os.environ.get("DB_USER", "root"),
                "password": os.environ.get("DB_PASSWORD", ""),
                "database": database or os.environ.get("DB_NAME", ""),
            }
            return pymysql.connect(**kwargs)

        raise ValueError(f"不支持的 DB_DRIVER: {driver!r}")

    @staticmethod
    def _collect_kwargs(parsed, database: str | None, default_port: int) -> dict:
        """从 urlparse 结果 + 环境变量组装连接 kwargs。

        URL 组件优先;缺失时回退到对应 DB_* 环境变量;None 值过滤掉。
        """
        kwargs: dict[str, Any] = {
            "host": parsed.hostname or os.environ.get("DB_HOST", "localhost"),
            "port": parsed.port or int(os.environ.get("DB_PORT", str(default_port))),
            "user": parsed.username or os.environ.get("DB_USER"),
            "password": parsed.password or os.environ.get("DB_PASSWORD"),
            "database": database
            or (parsed.path.lstrip("/") if parsed.path else None)
            or os.environ.get("DB_NAME"),
        }
        # 过滤 None 值,避免传入无意义参数
        return {k: v for k, v in kwargs.items() if v is not None}

    # ------------------------------------------------------------------ #
    # 同步执行(通过 asyncio.to_thread 调用)
    # ------------------------------------------------------------------ #
    def _execute_sync(self, sql: str, database: str | None) -> dict:
        """在同步上下文中执行 SQL 并关闭连接。

        通过 ``cursor.description`` 是否存在判断是否为返回行语句(比关键字匹配
        更稳健,DBAPI 通用)。返回行则 fetchall 转 dict 列表;否则 commit。

        Args:
            sql:      SQL 语句。
            database: 可选数据库名。

        Returns:
            dict: 返回行语句 ``{"rows": [...], "rowcount": N}``;
                  非返回行语句 ``{"rowcount": N}``。
        """
        conn = self._get_connection(database)
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                # cursor.description 非 None 表示该语句返回结果集
                if cur.description is not None:
                    columns = [d[0] for d in cur.description]
                    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
                    return {"rows": rows, "rowcount": len(rows)}
                # 非返回行语句:提交事务并返回影响行数
                conn.commit()
                return {"rowcount": cur.rowcount}
            finally:
                cur.close()
        finally:
            # 连接用完即关,避免泄漏
            conn.close()

    # ------------------------------------------------------------------ #
    # Tool 接口
    # ------------------------------------------------------------------ #
    async def call(self, params: dict, ctx: "Context") -> dict:
        """执行查询。

        用 ``asyncio.to_thread`` 包装同步 DB 调用,避免阻塞事件循环。
        连接失败 / SQL 错误抛异常,交由 ToolStep 的 retry 处理。

        Args:
            params: 包含 ``sql``(必填)与可选 ``database``。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: SELECT / 返回行语句返回 ``{"rows": [...], "rowcount": N}``;
                  非 SELECT 返回 ``{"rowcount": N}``。
        """
        sql: str = params["sql"]
        database = params.get("database")
        return await asyncio.to_thread(self._execute_sync, sql, database)
