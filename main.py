from sshtunnel import SSHTunnelForwarder
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs, unquote
import pymysql
import argparse
import sys
import os


def parse_url(url):
    """Extract useful fields from a kaneai app/web-agent URL.

    Returns a dict with at least ``session_id``. ``fqdn`` is also returned so
    callers can branch on the deployment the URL points at:

    - Scenario 1: ``fqdn=device.lambdatest.com``
    - Scenario 2: ``fqdn=kaneaivm-india.lambdatest.com%2F10-0-240-153``
      (URL-encoded; the ``%2F`` decodes to ``/``)

    Both scenarios currently resolve the same way (look up the session by id),
    but ``fqdn`` is surfaced here so scenario-2-specific handling can be added
    later without changing the call sites.
    """
    query = parse_qs(urlparse(url).query)

    session_id = query.get("session_id", [None])[0]
    if not session_id:
        raise ValueError(
            f"No session_id found in URL: {url}\n"
            "If the URL was cut off at the first '&', wrap it in double "
            'quotes so the shell keeps it as one argument, e.g.:\n'
            '    getlogs "https://.../app-agent?...&session_id=..."'
        )

    fqdn = query.get("fqdn", [None])[0]
    if fqdn is not None:
        fqdn = unquote(fqdn)

    return {
        "session_id": session_id,
        "fqdn": fqdn,
        "test_id": query.get("test_id", [None])[0],
        "region": query.get("region", [None])[0],
    }


def build_session_query(url):
    """Build the sessions lookup query from a kaneai URL."""
    info = parse_url(url)
    session_id = info["session_id"]

    # NOTE: fqdn distinguishes scenario 1 (device.lambdatest.com) from
    # scenario 2 (kaneaivm-*.lambdatest.com/<ip>). For now both look the
    # session up the same way; branch here when scenario 2 needs different
    # behaviour.
    return (
        "SELECT * FROM kane_vms.sessions "
        f"WHERE id = '{session_id}'"
    )


def run_query(query):
    load_dotenv()   # <-- this was missing; reads .env into os.environ

    # --- SSH tunnel (the "over SSH" part) ---
    ssh_host = "bastion-rds-prod.lambdatest.com"
    ssh_user = os.environ["SSH_USER"]       # brackets = clear error if absent
    ssh_password = os.environ["SSH_PASS"]

    # --- MySQL (relative to the SSH server) ---
    mysql_host = "auth-test-management-us-east-1.prod.internal"
    mysql_port = 3306
    db_user = os.environ["DB_USER"]
    db_pass = os.environ["DB_PASS"]

    with SSHTunnelForwarder(
        (ssh_host, 22),
        ssh_username=ssh_user,
        ssh_password=ssh_password,
        remote_bind_address=(mysql_host, mysql_port),
    ) as tunnel:

        conn = pymysql.connect(
            host="127.0.0.1",                 # local end of the tunnel
            port=tunnel.local_bind_port,      # sshtunnel picks a free local port
            user=db_user,
            password=db_pass,
            database="tms",
            cursorclass=pymysql.cursors.DictCursor,
        )

        try:
            with conn.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
                for row in rows:
                    print(row)
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(
        prog="getlogs",
        description="Run a SQL query against the tms database over an SSH tunnel.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="SELECT * FROM tms.test_cases limit 1",
        help=(
            "SQL query to execute, or a kaneai app/web-agent URL. When a URL "
            "is given, its session_id is extracted and the sessions lookup "
            "query is run. Defaults to a sample query."
        ),
    )
    args = parser.parse_args()

    arg = args.query
    if arg.startswith("http://") or arg.startswith("https://"):
        query = build_session_query(arg)
    else:
        query = arg

    run_query(query)


if __name__ == "__main__":
    main()
