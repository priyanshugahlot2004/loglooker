from sshtunnel import SSHTunnelForwarder
from dotenv import load_dotenv
import pymysql
import argparse
import sys
import os


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
        help="SQL query to execute (defaults to a sample query).",
    )
    args = parser.parse_args()
    run_query(args.query)


if __name__ == "__main__":
    main()
