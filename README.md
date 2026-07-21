# loglooker

A small command-line tool (`getlogs`) for tailing live application logs from
LambdaTest **KaneAI** app- and web-agent sessions.

Given a session URL (or a raw SQL query), loglooker figures out which VM or
device is running the session, opens the necessary SSH tunnels/hops to reach it,
and streams the relevant `app.log` file to your terminal in real time.

## How it works

Depending on the URL you pass, loglooker follows one of two paths:

### Web agents (`kaneaivm-*` URLs)

1. Parse the target VM IP out of the URL's `fqdn`
   (e.g. `kaneaivm-india.lambdatest.com/10-0-240-170` → `10.0.240.170`).
2. SSH into the KaneAI ingress VM, then open a direct tunnel to the target VM.
3. Connect to the target VM and stream
   `/usr/src/app/scripts/logger/app.log`.

### Device sessions (`device` URLs)

1. Extract the `session_id` from the URL and look the session up in the
   `kane_vms.sessions` table (over an SSH tunnel to the production RDS bastion)
   to obtain the device **UDID**.
2. Look up the device's host IP and OS in `lambda_lmds.device_host`.
3. SSH through the staging bastion to the device host and stream the
   appropriate log file:
   - **Android:** `Documents/kaneai/logs/app_<udid>.log`
   - **iOS:** `Documents/kaneai/app_agent_<udid>/app.log`

In both cases the log is followed with `tail -f`, so output streams
continuously. Press `Ctrl+C` to stop.

## Installation

Requires Python 3.8+.

### macOS

macOS does not ship a bare `pip`, and installing globally can fail with an
`externally-managed-environment` error, so the recommended setup is a virtual
environment.

1. **Check Python 3 is installed** (macOS uses `python3`, not `python`):

   ```bash
   python3 --version
   ```

   If it's missing, install it with [Homebrew](https://brew.sh):

   ```bash
   brew install python
   ```

2. **Create and activate a virtual environment** from the repo root:

   ```bash
   cd loglooker
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   Your prompt should now be prefixed with `(.venv)`.

3. **Install the tool** (inside the venv `pip` now exists and works):

   ```bash
   pip install -e .
   ```

4. **Run it** — while the venv is active, `getlogs` is on your `PATH`:

   ```bash
   getlogs "https://.../?fqdn=kaneaivm-india.lambdatest.com%2F10-0-240-170&session_id=..."
   ```

> **Note:** Each new terminal session needs the venv activated again with
> `source .venv/bin/activate` before `getlogs` will be found. If you'd rather not
> use a venv, you can substitute `python3 -m pip install -e .` and run the tool
> directly with `python3 main.py "..."`.

Before running, make sure a `.env` file with your credentials exists in the
project root (see [Configuration](#configuration)).

### Other platforms

```bash
pip install .
```

This installs the dependencies (`sshtunnel`, `pymysql`, `paramiko`,
`python-dotenv`) and exposes the `getlogs` command. You can also run it directly
with `python main.py`.

## Configuration

Credentials are read from environment variables, which can be placed in a
`.env` file in the project root (it is loaded automatically and is git-ignored):

```dotenv
# SSH bastion / staging access
SSH_USER=
SSH_PASS=

# tms database (session lookup)
DB_USER=
DB_PASS=

# production database (device host lookup)
DB_USER_prod=
DB_PASS_prod=

# web-agent hops
INGRESS_PASS=
AZURE_PASS=

# device host access
LTADMIN_PASS=
```

## Usage

```bash
# Web-agent session
getlogs "https://.../?fqdn=kaneaivm-india.lambdatest.com%2F10-0-240-170&session_id=..."

# Device session (by URL)
getlogs "https://device.lambdatest.com/...?session_id=..."

# Device session (by raw SQL query)
getlogs "SELECT * FROM kane_vms.sessions WHERE id = '<session_id>'"
```

loglooker decides which path to take based on the argument:

- Contains `kaneaivm` → web-agent flow.
- Contains `device` → device flow.
- Anything else → the argument is rejected.

## Requirements

- Network access to the LambdaTest bastions and internal databases.
- Valid credentials for each hop (see [Configuration](#configuration)).
