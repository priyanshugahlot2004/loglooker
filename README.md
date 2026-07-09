# loglooker

`loglooker` is a small command-line tool that streams live logs from a
LambdaTest **kaneai** app/web-agent session. You give it a session URL; it looks
up the session in the backend, finds the machine running it, SSHes in, and tails
the relevant log file to your terminal.

The tool installs a single console command: **`getlogs`**.

## How it works

Given a kaneai URL, `getlogs` figures out which of two deployment scenarios it is
and acts accordingly:

- **Web agent** (`fqdn=kaneaivm-*.lambdatest.com/<ip>`)
  Extracts the target VM IP from the URL, hops through the ingress VM to the
  target VM, and tails `scripts/logger/app.log`.

- **Device** (`fqdn=device.lambdatest.com`)
  1. Extracts the `session_id` from the URL.
  2. Over an SSH tunnel to the RDS bastion, queries the `sessions` table to get
     the device **UDID**.
  3. Queries the `device_host` table to resolve the **host IP** and **OS**
     (`android` / `ios`).
  4. Hops through the stage bastion to the host and tails the matching log:
     - Android: `Documents/kaneai/logs/app_<udid>.log`
     - iOS: `Documents/kaneai/app_agent_<udid>/app.log`

Logs stream continuously (`tail -f`). Press **Ctrl+C** to stop.

## Requirements

- **Python 3.8+**
- Network access to the LambdaTest bastion / ingress / VM hosts (typically VPN)
- Credentials supplied via a `.env` file (see below)

The tool is pure Python and runs the same on **macOS**, **Linux**, and other
platforms — all dependencies (`sshtunnel`, `pymysql`, `paramiko`,
`python-dotenv`) ship prebuilt wheels, so no compiler is required.

## Installation

Clone the repo and install it (an editable install is convenient for local use):

```bash
git clone <repo-url>
cd loglooker

# optional but recommended: use a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e .
```

This puts the `getlogs` command on your `PATH`.

## Configuration

Create a `.env` file in the project root with the required credentials. This
file is git-ignored and never committed.

```dotenv
# Bastion (SSH tunnel to the databases)
SSH_USER=your_ssh_user
SSH_PASS=your_ssh_password

# tms database (session -> UDID lookup)
DB_USER=your_db_user
DB_PASS=your_db_password

# prod database (UDID -> host IP / OS lookup)
DB_USER_prod=your_prod_db_user
DB_PASS_prod=your_prod_db_password
```

`getlogs` loads this file automatically at runtime via `python-dotenv`.

## Usage

Pass a kaneai session URL as the single argument:

```bash
# Web-agent session
getlogs "https://.../?fqdn=kaneaivm-india.lambdatest.com%2F10-0-240-170&session_id=...&test_id=...&region=..."

# Device session
getlogs "https://.../?fqdn=device.lambdatest.com&session_id=..."
```

You can also pass a raw SQL query (advanced) in place of a URL for the device
path. The tool decides its behaviour from the argument:

- contains `kaneaivm` → web-agent path
- contains `device` → device path
- anything else → prints an error and exits

Example output:

```
UDID for query '...': <udid>
Host IP for UDID <udid>: 10.0.240.170
OS for UDID <udid>: android

$ cd Documents/kaneai/logs && tail -f app_<udid>.log
(streaming — press Ctrl+C to stop)

... live log lines ...
```

## Project layout

```
loglooker/
├── main.py          # the entire CLI (URL parsing, tunnels, SSH, log streaming)
├── pyproject.toml   # package metadata + the `getlogs` console script
└── README.md
```

## Notes

- Streaming runs until you interrupt it with **Ctrl+C**.
- If a required environment variable is missing, the tool raises a clear
  `KeyError` naming the missing variable — check your `.env`.
