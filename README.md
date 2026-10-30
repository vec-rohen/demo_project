# PLC Web HMI – edge device

The operator interface that runs on each site gateway (e.g. a Siemens IoT2050).
It reads the PLC, runs alarms, keeps recent trends and serves the HMI on the
local network, so authorised people can use it with **no internet
connection**. A cloud portal (next step) will manage clients, sites, assets
and users centrally and send each gateway only its own configuration.

- **Overview** – live process mimic, or an automatic status overview for any asset, with key values, sparklines and active alarms.
- **I/O** – every digital and analog point with state lamps, range bars and alarm-limit markers; toggle writable outputs.
- **Setpoints & Control** – Auto/Manual and setpoint changes with range validation and a confirm step.
- **Trends** – multi-pen trends from 1 minute to 7 days (recent data in the browser, older from the device database).
- **Alarms & Events** – ISA-18.2 style alarms with acknowledge, and a journal of every alarm, operator action, configuration change, sign-in and system event.
- **Commissioning** – device & package info, PLC connection test, I/O checkout with sign-off and report, on-site I/O edits.
- **Users** – accounts and roles for this site.

![HMI overview](reports/figures/hmi-overview.png)

## Run it

```bash
pip install -r requirements.txt
python -m src.hmi                          # first start loads the demo package (simulated plant)
python -m src.hmi --package site.json      # first start from your own configuration package
python -m src.hmi --host 0.0.0.0           # serve the local plant network
```

Then browse to http://127.0.0.1:8000. On first start the server prints a
one-time password for an `admin` **integrator** account.

Everything the device needs lives in one SQLite file, `data/edge.db`
(`--db` to change; git-ignored): client/site/device identity, assets, PLC
connections, I/O, users, recent trend history (`settings.history_days`,
default 7), the event journal, I/O check results, and a log of changes made
on site waiting to sync to the cloud. A package is only loaded when the
database is empty; after that, change things in the UI or load a new package
from the Commissioning tab.

## Workflow

**Offsite (today: a package file; later: the cloud portal)** – describe the
client, site, device and each asset with its PLC connection and I/O in a
configuration package, and load it on the gateway.

**Onsite (Commissioning tab)**

1. **PLC connection** – enter or confirm the PLC's IP, port, unit ID and poll rate and press **Test connection**: it opens a separate connection and reads every point once, reporting what it read and any errors. Integrators then **Save & apply**; the asset reconnects without restarting the server.
2. **I/O check** – force or simulate each signal at the PLC, watch the live value, and mark it **Pass**, **Fail** (with a note) or **N/A**. Each mark records who and when. Filter to what's left to check, and **Export report** for a CSV commissioning record.
3. **Fix on site** – integrators can edit a point (description, range, Modbus address/scale, alarms) or add/delete points. The change applies immediately, clears that point's check so it gets re-tested, and is kept in the **Local changes** log so the cloud can pick it up later.

## Configuration packages

```bash
python -m src.hmi.package demo site.json     # write the demo package to start from
python -m src.hmi.package import site.json   # apply one to data/edge.db
python -m src.hmi.package export backup.json # current configuration incl. on-site edits
```

A package holds `client`, `site`, `device` (each with an `id` and `name`),
`settings`, a list of `assets` – each with its `plc` connection
(`{"protocol": "modbus", "host": "192.168.1.10", "port": 502, "unit": 1, "poll_ms": 500}`
or `{"protocol": "sim"}`) and its `tags` – and optionally `users`. One gateway
can run several assets; give each a unique `id`. See `src/hmi/package.py` for
the full layout. Packages are validated before anything is changed.

Each tag has a kind (`DI`, `DO`, `AI`, `AO`, `SP`), type (`bool`/`float`),
units, engineering range, whether operators may write it, the minimum role
allowed to change it, optional alarms, and a Modbus address:

```json
{"name": "AI_TankLevel", "kind": "AI", "type": "float", "unit": "%", "min": 0, "max": 100,
 "alarm": {"hi": "SP_LevelHiAlarm", "lo": 10, "deadband": 1},
 "modbus": {"table": "input", "address": 0, "scale": 10}}
```

`table` is `coil`, `discrete`, `input` or `holding`; analog values are signed
16-bit registers divided by `scale`. Other protocols (S7, OPC UA) can be added
by implementing the small `Driver` interface in `src/hmi/drivers/base.py`.

## iPhone, iPad and other tablets

The interface adapts to phone and tablet screens: touch-sized controls, no
zoom-jumps when typing, tap-and-drag on trends, and card-style I/O checks on
phones. It needs iOS/iPadOS 16.2 or newer. In Safari, **Share → Add to Home
Screen** opens it full-screen like an app (with its own sign-in).

## Sign-in and roles

| Role | Can do |
|------|--------|
| Viewer | See every screen, trend and alarm. Read-only. |
| Operator | Change setpoints and outputs, switch Auto/Manual, acknowledge alarms. |
| Engineer | Everything above, plus tags marked `"role": "engineer"` (alarm limits, tuning), simulator inputs, PLC connection tests, I/O check sign-off and managing users up to engineer. |
| Integrator | Your commissioning technicians: everything, plus PLC connection settings, editing I/O on site, loading/downloading packages and managing integrator accounts. |

- New accounts and admin password resets get a temporary password; the user must replace it at their next sign-in.
- Sessions end after 30 minutes without interaction (`HMI_IDLE_MINUTES`, `0` disables), with a warning a minute beforehand.
- Five wrong passwords lock that username for a minute.
- Accounts live in the device database with scrypt password hashes; normally they'll arrive in the package from the cloud. Local account and password changes are logged for sync.
- Locked out? Manage accounts from the gateway's command line:

  ```bash
  python -m src.hmi.users list
  python -m src.hmi.users passwd admin
  python -m src.hmi.users add alice --role operator --name "Alice Smith"
  ```

## Architecture

```
browser ──WebSocket /ws/{asset}──▶ FastAPI ── SiteManager ──┬─ Runtime (asset 1: poll, alarms, trends) ── Driver ── PLC
        ──REST /api/*────────────▶            │             └─ Runtime (asset 2) ── Driver ── PLC
                                              └── Store (SQLite: config, users, history, journal, checks, change log)
```

REST, per asset under `/api/assets/{asset}/`: `config`, `state`, `history`,
`tags/{name}` (write), `simulate/{name}`, `alarms/ack`, `plc` (get/put),
`plc/test`, `io`, `io/{name}/check`, `tags/{name}/definition` (put/delete).
Device-wide: `/api/site`, `/api/package` (get/post), `/api/changes`,
`/api/login`, `/api/logout`, `/api/me`, `/api/me/password`, `/api/users`.

> **Security:** the session cookie is HttpOnly and SameSite=Strict, but the
> server itself speaks plain HTTP. When serving beyond localhost, put it behind
> HTTPS (a reverse proxy such as Caddy or nginx) and set `HMI_SECURE_COOKIES=1`
> so passwords and session cookies never cross the network unencrypted.

Run the tests with `python -m pytest tests`.

---

# Data Project Template

<a target="_blank" href="https://datalumina.com/">
    <img src="https://img.shields.io/badge/Datalumina-Project%20Template-2856f7" alt="Datalumina Project" />
</a>

## Cookiecutter Data Science
This project template is a simplified version of the [Cookiecutter Data Science](https://cookiecutter-data-science.drivendata.org) template, created to suit the needs of Datalumina and made available as a GitHub template.

## Adjusting .gitignore

Ensure you adjust the `.gitignore` file according to your project needs. For example, since this is a template, the `/data/` folder is commented out and data will not be exlucded from source control:

```plaintext
# exclude data from source control by default
# /data/
```

Typically, you want to exclude this folder if it contains either sensitive data that you do not want to add to version control or large files.

## Duplicating the .env File
To set up your environment variables, you need to duplicate the `.env.example` file and rename it to `.env`. You can do this manually or using the following terminal command:

```bash
cp .env.example .env # Linux, macOS, Git Bash, WSL
copy .env.example .env # Windows Command Prompt
```

This command creates a copy of `.env.example` and names it `.env`, allowing you to configure your environment variables specific to your setup.


## Project Organization

```
├── LICENSE            <- Open-source license if one is chosen
├── README.md          <- The top-level README for developers using this project
├── data
│   ├── external       <- Data from third party sources
│   ├── interim        <- Intermediate data that has been transformed
│   ├── processed      <- The final, canonical data sets for modeling
│   └── raw            <- The original, immutable data dump
│
├── models             <- Trained and serialized models, model predictions, or model summaries
│
├── notebooks          <- Jupyter notebooks. Naming convention is a number (for ordering),
│                         the creator's initials, and a short `-` delimited description, e.g.
│                         `1.0-jqp-initial-data-exploration`
│
├── references         <- Data dictionaries, manuals, and all other explanatory materials
│
├── reports            <- Generated analysis as HTML, PDF, LaTeX, etc.
│   └── figures        <- Generated graphics and figures to be used in reporting
│
├── requirements.txt   <- The requirements file for reproducing the analysis environment, e.g.
│                         generated with `pip freeze > requirements.txt`
│
└── src                         <- Source code for this project
    │
    ├── __init__.py             <- Makes src a Python module
    │
    ├── config.py               <- Store useful variables and configuration
    │
    ├── dataset.py              <- Scripts to download or generate data
    │
    ├── features.py             <- Code to create features for modeling
    │
    │    
    ├── modeling                
    │   ├── __init__.py 
    │   ├── predict.py          <- Code to run model inference with trained models          
    │   └── train.py            <- Code to train models
    │
    ├── plots.py                <- Code to create visualizations 
    │
    └── services                <- Service classes to connect with external platforms, tools, or APIs
        └── __init__.py 
```

--------