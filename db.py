import psycopg2
import psycopg2.extras

import config


def connect():
    # client_encoding UTF8 evita erro de acentos mesmo se o banco tiver sido criado como SQL_ASCII.
    conn = psycopg2.connect(config.DATABASE_URL, client_encoding="UTF8")
    conn.autocommit = False
    return conn


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_schema(conn) -> None:
    sql = (config.BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
