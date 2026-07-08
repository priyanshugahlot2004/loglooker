from sshtunnel import SSHTunnelForwarder
from dotenv import load_dotenv
import pymysql
import os

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
            cursor.execute("SELECT * FROM tms.test_cases limit 1")
            rows = cursor.fetchall()
            for row in rows:
                print(row)
    finally:
        conn.close()