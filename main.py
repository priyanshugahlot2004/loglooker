from sshtunnel import SSHTunnelForwarder
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs, unquote
import pymysql
import argparse
import sys
import os
import paramiko


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
        raise ValueError(f"No session_id found in URL: {url}")

    fqdn = query.get("fqdn", [None])[0]
    if fqdn is not None:
        fqdn = unquote(fqdn)

    return {
        "session_id": session_id,
        "fqdn": fqdn,
        "test_id": query.get("test_id", [None])[0],
        "region": query.get("region", [None])[0],
    }

def get_vm_ip(url):
    """Extract the target VM IP from a web-agent URL's fqdn.

    fqdn=kaneaivm-india.lambdatest.com/10-0-240-170  ->  10.0.240.170
    """
    info = parse_url(url)
    fqdn = info["fqdn"]
    if not fqdn or "/" not in fqdn:
        raise ValueError(f"No IP found in fqdn: {fqdn!r}")

    ip_part = fqdn.split("/")[-1]      # "10-0-240-170"
    return ip_part.replace("-", ".")   # "10.0.240.170"

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


def run_query_get_udid(query):
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
                remark = rows[0]["remark"]
                udid = remark.split(" ")[-1]
                print(f"UDID for query '{query}': {udid}")
                return udid
                
        finally:
            conn.close()

def run_query_get_hostIP(udid):
    load_dotenv()   # <-- this was missing; reads .env into os.environ

    # --- SSH tunnel (the "over SSH" part) ---
    ssh_host = "bastion-rds-prod.lambdatest.com"
    ssh_user = os.environ["SSH_USER"]       # brackets = clear error if absent
    ssh_password = os.environ["SSH_PASS"]

    # --- MySQL (relative to the SSH server) ---
    mysql_host = "rd-ml-db-us.prod.internal"
    mysql_port = 3306
    db_user = os.environ["DB_USER_prod"]
    db_pass = os.environ["DB_PASS_prod"]

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
                cursor.execute(f"SELECT * FROM lambda_lmds.device_host WHERE udid = '{udid}';")
                rows = cursor.fetchall()
                host_ip = rows[0]["host_ip"]
                print(f"Host IP for UDID {udid}: {host_ip}")
                _os = rows[0]["os"]
                print(f"OS for UDID {udid}: {_os}")
                return host_ip, _os
                
        finally:
            conn.close()

def run_cli_commands_for_web(vm_ip):
    # hop 1: ingress VM
    ingress = paramiko.SSHClient()
    ingress.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ingress.connect(
        "20.198.17.8",
        username="kaneaiingressvm",
        password="VqaiofN6rKbxE73BrvZ3",  
    )

    # tunnel from ingress → target VM's SSH port
    channel = ingress.get_transport().open_channel(
        "direct-tcpip",
        (vm_ip, 22),        # target
        ("127.0.0.1", 0),   # source
    )

    # hop 2: target VM as azureuser (bifurcation = true)
    target = paramiko.SSHClient()
    target.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    target.connect(
        vm_ip,
        username="azureuser",
        password="Azure123456!",
        sock=channel,
    )

    # cd + tail -f, streamed line by line
    cmd = "cd /usr/src/app/scripts/logger && tail -n 1000 -f app.log"
    print(f"\n$ {cmd}\n(streaming — press Ctrl+C to stop)\n")
    stdin, stdout, stderr = target.exec_command(cmd, get_pty=True)

    try:
        for line in iter(stdout.readline, ""):
            print(line, end="")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        target.close()
        ingress.close()

def run_cli_commands_for_device(host_ip, udid, _os):
    # 1. connect to the bastion
    bastion = paramiko.SSHClient()
    bastion.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    bastion.connect(
        "bastion-stage.lambdatest.com",
        username=os.environ["SSH_USER"],
        password=os.environ["SSH_PASS"],
    )

    # 2. open a tunnel from the bastion to the target host's SSH port
    channel = bastion.get_transport().open_channel(
        "direct-tcpip",
        (host_ip, 22),      # target
        ("127.0.0.1", 0),   # source
    )

    # 3. connect to the target as ltadmin through that tunnel
    #    AutoAddPolicy = auto-accept the fingerprint ("yes")
    target = paramiko.SSHClient()
    target.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    target.connect(
        host_ip,
        username="ltadmin",
        password="lambdatest123!",   # password for ltadmin
        sock=channel,
    )

    # 4. cd + tail -f on the target, streamed line by line
    cmd = ""
    if(_os == "android"):
        cmd = f"cd Documents/kaneai/logs && tail -f app_{udid}.log"
    elif(_os == "ios"):
        cmd = f"cd Documents/kaneai/app_agent_{udid} && tail -f app.log"
    else:
        print(f"Unknown OS: {_os}")
        return
    
    print(f"\n$ {cmd}\n(streaming — press Ctrl+C to stop)\n")
    stdin, stdout, stderr = target.exec_command(cmd, get_pty=True)

    try:
        for line in iter(stdout.readline, ""):
            print(line, end="")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        target.close()
        bastion.close()

def main():
    parser = argparse.ArgumentParser(
        prog="getlogs",
        description="Run a SQL query against the tms database over an SSH tunnel.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="",
        help=(
            "SQL query to execute, or a kaneai app/web-agent URL. When a URL "
            "is given, its session_id is extracted and the sessions lookup "
            "query is run. Defaults to a sample query."
        ),
    )
    args = parser.parse_args()
    arg = args.query
    if("kaneaivm" in arg):
        #do web
        run_cli_commands_for_web(get_vm_ip(arg))

    elif("device" in arg):
        if arg.startswith("http://") or arg.startswith("https://"):
            query = build_session_query(arg)
        else:
            query = arg

        udid = run_query_get_udid(query)
        host_ip, _os = run_query_get_hostIP(udid)
        run_cli_commands_for_device(host_ip, udid, _os)
    else:
        print("Invalid argument. Please provide a valid SQL query or a kaneai URL.")
        sys.exit(1)

if __name__ == "__main__":
    main()
