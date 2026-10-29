"""Query helper for the Intercity Freight Review: SUPABASE_DB_URL when set (GitHub Actions), else the Birbal
laptop's pooler settings (Marketplace Ads Pipeline/.env, verify-full against the Supabase CA)."""
import os

import psycopg2


def connect():
    if os.environ.get("SUPABASE_DB_URL"):
        kw = dict(sslmode=os.environ.get("SUPABASE_DB_SSLMODE", "require"))
        if os.environ.get("SUPABASE_DB_SSLROOTCERT"):
            kw["sslrootcert"] = os.environ["SUPABASE_DB_SSLROOTCERT"]
        return psycopg2.connect(os.environ["SUPABASE_DB_URL"], **kw)
    from dotenv import dotenv_values
    cfg = dotenv_values(r"D:/Python/Birbal/Marketplace Ads Pipeline/.env")
    return psycopg2.connect(host=cfg["PGHOST"], port=cfg["PGPORT"], dbname=cfg["PGDATABASE"],
                            user=cfg["PGUSER"], password=cfg["PGPASSWORD"],
                            sslmode="verify-full", sslrootcert="D:/Python/Birbal/Uniware/prod-ca-2021.crt")


def run(sql, args=None, limit=500):
    c = connect()
    cur = c.cursor()
    cur.execute("set statement_timeout='900000'")
    cur.execute(sql, args)
    try:
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit)
    except Exception:
        cols, rows = [], []
    c.commit()
    c.close()
    return cols, rows
