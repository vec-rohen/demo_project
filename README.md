# PLC Web HMI

A browser-based operator interface for a PLC-controlled process. Open it on any
device on the plant network to:

- **Overview** – live process mimic (pump, tank, heater, valves, level switches) with key process values, sparklines and active alarms.
- **I/O** – every digital and analog input/output with state lamps, range bars and alarm-limit markers; toggle writable outputs.
- **Setpoints & Control** – switch Auto/Manual and edit setpoints/targets with range validation and a confirm step. Outputs the PLC drives in Auto are locked until you switch to Manual.
- **Trends** – multi-pen trend of any analog value (1/5/15 min), scaled to % of span, with a hover cursor.
- **Alarms & Events** – ISA-18.2 style alarm list (active / unacknowledged, priorities, deadbands, acknowledge) and a journal of every alarm, operator write and system event.

![HMI overview](reports/figures/hmi-overview.png)

## Run it

```bash
pip install -r requirements.txt
python -m src.hmi                                    # built-in process simulator
python -m src.hmi --driver modbus --plc 192.168.1.10  # a real PLC over Modbus TCP
```

Then browse to http://127.0.0.1:8000. Use `--host 0.0.0.0` to serve other
machines on the network. With the simulator, the **Sim** buttons on the I/O page
force inputs (E-stop, door switch…) so you can exercise alarms and interlocks.

## Connecting your PLC

Tags live in [`src/hmi/tags.json`](src/hmi/tags.json) (or pass `--tags my_tags.json`).
Each tag has a kind (`DI`, `DO`, `AI`, `AO`, `SP`), type (`bool`/`float`), units,
engineering range, whether operators may write it, optional alarm limits (a
number, or the name of a setpoint tag holding the limit), and a Modbus address:

```json
{"name": "AI_TankLevel", "kind": "AI", "type": "float", "unit": "%", "min": 0, "max": 100,
 "alarm": {"hi": "SP_LevelHiAlarm", "lo": 10, "deadband": 1},
 "modbus": {"table": "input", "address": 0, "scale": 10}}
```

`table` is `coil`, `discrete`, `input` or `holding`; analog values are signed
16-bit registers divided by `scale`. Other protocols (OPC UA, S7, EtherNet/IP)
can be added by implementing the small `Driver` interface in
`src/hmi/drivers/base.py`.

## Architecture

```
browser ──WebSocket /ws (4 Hz)──▶ FastAPI ── Runtime (poll, alarms, history, journal) ── Driver ── PLC / simulator
        ──REST /api/*────────────▶
```

REST: `GET /api/config`, `GET /api/state`, `GET /api/history`,
`POST /api/tags/{name}`, `POST /api/alarms/ack`, `POST /api/simulate/{name}`.

> **Security:** there is no login yet; the server listens on localhost by
> default. Put it behind authentication (reverse proxy / VPN) before exposing
> write access on a plant network.

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