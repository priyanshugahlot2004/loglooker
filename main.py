from sshtunnel import SSHTunnelForwarder
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs, unquote
import pymysql
import argparse
import socket
import sys
import time
import os
import paramiko


# --- SSH timeouts -----------------------------------------------------------
# Without these paramiko blocks indefinitely: Transport.open_channel() defaults
# to timeout=None and spins on event.wait(0.1) for as long as the transport is
# alive, so an unreachable target VM hangs the CLI with no output at all.
CONNECT_TIMEOUT = 15   # TCP connect
BANNER_TIMEOUT = 20    # wait for the SSH banner
AUTH_TIMEOUT = 20      # wait for auth to complete
CHANNEL_TIMEOUT = 20   # wait for the jump host to open direct-tcpip
KEEPALIVE = 30         # detect a silently dead tunnel while streaming


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

def ssh_timeouts(timeout):
    """Timeout kwargs for ``SSHClient.connect``, scaled off the --timeout value.

    ``max`` so that raising --timeout on a slow network never leaves a lower
    sub-timeout in place to trip first.
    """
    return {
        "timeout": timeout,
        "banner_timeout": max(timeout, BANNER_TIMEOUT),
        "auth_timeout": max(timeout, AUTH_TIMEOUT),
    }

def open_tunnel_channel(client, hop_name, target_ip, timeout=CHANNEL_TIMEOUT):
    """Open a direct-tcpip channel from a connected jump host to ``target_ip:22``.

    Bounded by ``timeout``: paramiko's default is to wait forever, and a
    ``ChannelException`` is only raised when the jump host *actively* refuses the
    channel. When the target is simply unroutable the jump host's own outbound
    connect stalls and it never replies at all — that is the case this timeout
    covers. On any failure this prints a diagnosis, closes ``client`` and exits.
    """
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        print(f"✗ The SSH session to the {hop_name} is no longer active.")
        client.close()
        sys.exit(1)

    # surface a silently dead tunnel while streaming instead of idling forever
    transport.set_keepalive(KEEPALIVE)

    started = time.monotonic()
    try:
        return transport.open_channel(
            "direct-tcpip",
            (target_ip, 22),    # target
            ("127.0.0.1", 0),   # source
            timeout=timeout,
        )
    except paramiko.ChannelException:
        print(
            f"✗ Cannot reach {target_ip}:22 through the {hop_name}.\n"
            f"  The target VM may be down, recycled, or in a different network."
        )
    except (paramiko.SSHException, socket.timeout, OSError) as e:
        print(
            f"✗ Cannot open a tunnel to {target_ip}:22 through the {hop_name} "
            f"(failed after {time.monotonic() - started:.0f}s): {e}\n"
            f"  The target VM may be down, recycled, or in a different network.\n"
            f"  Use --timeout to wait longer on a slow network."
        )

    client.close()
    sys.exit(1)

def run_cli_commands_for_web(vm_ip, timeout=CONNECT_TIMEOUT):
    # hop 1: ingress VM
    ingress = paramiko.SSHClient()
    ingress.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ingress.connect(
            "20.198.17.8",
            username="kaneaiingressvm",
            password=os.environ["INGRESS_PASS"],
            **ssh_timeouts(timeout),
        )
    except paramiko.AuthenticationException:
        print("✗ Ingress VM (20.198.17.8) rejected the login — check INGRESS_PASS in .env.")
        sys.exit(1)
    except (paramiko.SSHException, socket.timeout, OSError) as e:
        print(f"✗ Cannot reach the ingress VM (20.198.17.8): {e}")
        sys.exit(1)

    # tunnel from ingress → target VM's SSH port
    channel = open_tunnel_channel(ingress, "ingress VM", vm_ip, timeout=max(timeout, CHANNEL_TIMEOUT))

    # hop 2: target VM as azureuser (bifurcation = true)
    target = paramiko.SSHClient()
    target.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        target.connect(
            vm_ip,
            username="azureuser",
            password=os.environ["AZURE_PASS"],
            sock=channel,
            **ssh_timeouts(timeout),
        )
    except paramiko.AuthenticationException:
        print(f"✗ Target VM ({vm_ip}) rejected the azureuser login — check AZURE_PASS in .env.")
        channel.close()
        ingress.close()
        sys.exit(1)
    except (paramiko.SSHException, socket.timeout, OSError) as e:
        print(f"✗ Failed to establish SSH to target VM ({vm_ip}): {e}")
        channel.close()
        ingress.close()
        sys.exit(1)

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

def run_cli_commands_for_device(host_ip, udid, _os, timeout=CONNECT_TIMEOUT):
    # 1. connect to the bastion
    bastion = paramiko.SSHClient()
    bastion.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        bastion.connect(
            "bastion-stage.lambdatest.com",
            username=os.environ["SSH_USER"],
            password=os.environ["SSH_PASS"],
            **ssh_timeouts(timeout),
        )
    except paramiko.AuthenticationException:
        print("✗ bastion-stage.lambdatest.com rejected the login — check SSH_USER/SSH_PASS in .env.")
        sys.exit(1)
    except (paramiko.SSHException, socket.timeout, OSError) as e:
        print(f"✗ Cannot reach bastion-stage.lambdatest.com: {e}")
        sys.exit(1)

    # 2. open a tunnel from the bastion to the target host's SSH port
    channel = open_tunnel_channel(bastion, "bastion", host_ip, timeout=max(timeout, CHANNEL_TIMEOUT))

    # 3. connect to the target as ltadmin through that tunnel
    #    AutoAddPolicy = auto-accept the fingerprint ("yes")
    target = paramiko.SSHClient()
    target.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        target.connect(
            host_ip,
            username="ltadmin",
            password=os.environ["LTADMIN_PASS"],
            sock=channel,
            **ssh_timeouts(timeout),
        )
    except paramiko.AuthenticationException:
        print(f"✗ Host ({host_ip}) rejected the ltadmin login — check LTADMIN_PASS in .env.")
        channel.close()
        bastion.close()
        sys.exit(1)
    except (paramiko.SSHException, socket.timeout, OSError) as e:
        print(f"✗ Failed to establish SSH to host ({host_ip}): {e}")
        channel.close()
        bastion.close()
        sys.exit(1)

    # 4. cd + tail -f on the target, streamed line by line
    cmd = ""
    if(_os == "android"):
        cmd = f"cd Documents/kaneai/logs && tail -f app_{udid}.log"
    elif(_os == "ios"):
        cmd = f"cd Documents/kaneai/app_agent_{udid} && tail -f app.log"
    else:
        print(f"Unknown OS: {_os}")
        target.close()
        bastion.close()
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
    load_dotenv()   # load .env into os.environ for every code path
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
    parser.add_argument(
        "--timeout",
        type=int,
        default=CONNECT_TIMEOUT,
        metavar="SECONDS",
        help=(
            "How long to wait for each SSH connection step before giving up "
            f"(default: {CONNECT_TIMEOUT}). Raise this on a slow network."
        ),
    )
    args = parser.parse_args()
    arg = args.query

    try:
        if("kaneaivm" in arg):
            #do web
            try:
                vm_ip = get_vm_ip(arg)
            except ValueError as e:
                print(f"✗ {e}")
                sys.exit(1)
            run_cli_commands_for_web(vm_ip, timeout=args.timeout)

        elif("device" in arg):
            if arg.startswith("http://") or arg.startswith("https://"):
                query = build_session_query(arg)
            else:
                query = arg

            udid = run_query_get_udid(query)
            host_ip, _os = run_query_get_hostIP(udid)
            run_cli_commands_for_device(host_ip, udid, _os, timeout=args.timeout)
        else:
            print("Invalid argument. Please provide a valid SQL query or a kaneai URL.")
            sys.exit(1)
    except KeyboardInterrupt:
        # Ctrl+C while connecting/opening the tunnel, i.e. before the streaming
        # loop takes over — exit quietly instead of dumping a paramiko traceback.
        print("\nCancelled.")
        sys.exit(130)

if __name__ == "__main__":
    main()
