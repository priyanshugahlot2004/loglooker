from sshtunnel import SSHTunnelForwarder
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs, unquote
import pymysql
import paramiko
import argparse
import sys
import re
import os


# A session_id is a UUID, e.g. 71a985cd-6d3d-4a3c-9a0e-5ac287a127b7
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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


def session_query(session_id):
    """Build the sessions lookup query for a given session_id."""
    return (
        "SELECT * FROM kane_vms.sessions "
        f"WHERE id = '{session_id}'"
    )


def build_session_query(url):
    """Build the sessions lookup query from a kaneai URL."""
    info = parse_url(url)

    # NOTE: fqdn distinguishes scenario 1 (device.lambdatest.com) from
    # scenario 2 (kaneaivm-*.lambdatest.com/<ip>). For now both look the
    # session up the same way; branch here when scenario 2 needs different
    # behaviour.
    return session_query(info["session_id"])


def device_udid(row):
    """Extract the device UDID from a sessions row.

    The UDID lives in the ``remark`` column. This is the single integration
    point for the UDID-extraction logic — adjust the parsing here if ``remark``
    holds more than the bare UDID.
    """
    remark = row.get("remark")
    if remark is None:
        return None
    return str(remark).strip()


def build_tail_command(os_name, udid):
    """Build the remote command that tails the live device log.

    The log directory depends on the device OS:
      - iOS:     Documents/kaneai/app-agent_<udid>
      - Android: Documents/kaneai/logs
    """
    if (os_name or "").strip().lower() == "ios":
        log_dir = f"Documents/kaneai/app-agent_{udid}"
    else:  # android (default)
        log_dir = "Documents/kaneai/logs"
    return f"cd {log_dir} && tail -f app_{udid}.log"


def tail_device_logs(host_ip, os_name, udid):
    """SSH into the device host and stream its live log to stdout.

    Connects directly to ``host_ip`` as HOST_SSH_USER (default "ltadmin") using
    the password from HOST_SSH_PASS, then runs the tail command and prints its
    output live until interrupted with Ctrl-C.
    """
    ssh_user = os.environ.get("HOST_SSH_USER", "ltadmin")
    ssh_pass = os.environ.get("HOST_SSH_PASS")
    if not ssh_pass:
        print(
            "HOST_SSH_PASS is not set — add it to your .env to stream logs "
            "(e.g. HOST_SSH_PASS=... , HOST_SSH_USER defaults to 'ltadmin').",
            file=sys.stderr,
        )
        return

    command = build_tail_command(os_name, udid)
    print(f"\nConnecting to {ssh_user}@{host_ip} ...")
    print(f"$ {command}\n(press Ctrl-C to stop)\n")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        # NOTE: direct connection. If host_ip is ever only reachable via the
        # bastion, open a ProxyJump channel and pass it here as `sock=`.
        client.connect(
            hostname=host_ip,
            port=22,
            username=ssh_user,
            password=ssh_pass,
            look_for_keys=False,
            allow_agent=False,
        )

        stdin, stdout, stderr = client.exec_command(command, get_pty=True)
        try:
            for line in iter(stdout.readline, ""):
                print(line, end="")
        except KeyboardInterrupt:
            print("\nStopped tailing logs.")
    finally:
        client.close()


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
                return rows
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
            "A kaneai app/web-agent URL, a bare session_id (UUID), or a raw "
            "SQL query. For a URL or session_id the sessions lookup query is "
            "run; a UUID needs no quotes, so it avoids the shell splitting a "
            "URL on '&'. Defaults to a sample query."
        ),
    )
    parser.add_argument(
        "--no-tail",
        action="store_true",
        help=(
            "Only print the sessions row; do not SSH into the host and tail "
            "its live log. Ignored for raw SQL queries."
        ),
    )
    args = parser.parse_args()

    arg = args.query.strip()
    is_lookup = False
    if arg.startswith("http://") or arg.startswith("https://"):
        query = build_session_query(arg)
        is_lookup = True
    elif UUID_RE.match(arg):
        # Bare session_id — no shell-special characters, so no quoting needed.
        query = session_query(arg)
        is_lookup = True
    else:
        query = arg

    rows = run_query(query)

    # After a session lookup, SSH into the device host and tail its live log.
    if is_lookup and not args.no_tail and rows:
        row = rows[0]
        host_ip = row.get("host_ip")
        udid = device_udid(row)
        if not host_ip or not udid:
            print(
                "Cannot tail logs: session row is missing host_ip or udid.",
                file=sys.stderr,
            )
            return
        tail_device_logs(host_ip, row.get("os"), udid)


if __name__ == "__main__":
    main()
