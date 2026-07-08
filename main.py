from sshtunnel import SSHTunnelForwarder
import pymysql
import os

# --- SSH tunnel (the "over SSH" part) ---
ssh_host = "bastion-rds-prod.lambdatest.com"
ssh_user = os.getenv("SSH_USER")
ssh_password = os.getenv("SSH_PASS")

# --- MySQL (relative to the SSH server) ---
mysql_host = "auth-test-management-us-east-1.prod.internal"
mysql_port = 3306
db_user    = os.getenv("DB_USER")
db_pass    = os.getenv("DB_PASS")

with SSHTunnelForwarder(
    (ssh_host, 22),
    ssh_username=ssh_user,
    ssh_password=ssh_password,
    remote_bind_address=(mysql_host, mysql_port),
) as tunnel:

    conn = pymysql.connect(
        host="127.0.0.1",                 # connect to the local end of the tunnel
        port=tunnel.local_bind_port,      # sshtunnel picks a free local port
        user=db_user,
        password=db_pass,
        database="tms",         # optional; your Default Schema was blank
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        with conn.cursor() as cursor:
            cursor.execute('select * from tms.test_cases where id = "01KWN228Y2AQ8S07ZQZYYY0659";')
            rows = cursor.fetchall()
            for row in rows:
                print(row)
    finally:
        conn.close()