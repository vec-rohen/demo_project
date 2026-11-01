# Installing the edge HMI on a Siemens IoT2050

The IoT2050 runs Debian-based Linux (Industrial OS or the IoT2050 example
image). The edge HMI is a Python program that runs there as a background
service, starts on boot and restarts itself if it ever stops. Configuration
packages from the cloud portal are just data files the app loads – nothing is
installed when a package is loaded.

## Requirements

- Python **3.9 or newer** (`python3 --version`). Debian 11 based images have
  3.9, Debian 12 based images 3.11 – both work.
- `python3-venv` (the installer adds it with `apt` if it's missing and the
  device has internet).
- Internet access during install, **or** a folder of package files prepared on
  a PC (see *Offline install*).

## Install

Copy this repository to the device (USB stick, `scp`, or `git clone`), then:

```bash
cd plant-hmi                         # the copied repository
sudo ./deploy/iot2050/install.sh
```

It will:

1. create a `plant-hmi` system account (the app never runs as root),
2. copy the edge app to `/opt/plant-hmi` and set up its Python environment,
3. keep all data (configuration, users, trends, journal) in `/var/lib/plant-hmi`,
   readable only by that account,
4. install and start the `plant-hmi` systemd service on port 8000,
5. print the web address(es) and a **one-time admin password**.

Then, from a laptop on the same network:

1. Open the address it printed, e.g. `http://192.168.1.50:8000`, and sign in as
   `admin` with the one-time password; choose your own when asked.
2. The device shows **“This device isn't configured yet”**. Press **Load package…**
   and pick the package you downloaded from the cloud portal (the device's page →
   *Version history* → *Download*).
3. Sign in again with your **portal staff account** – the package replaces the
   temporary admin account with your staff (as integrators) and the client's users.
4. On **Commissioning**: test the PLC connection, then work through the I/O check.

Options: `--port 80` (serves on the standard web port), `--package site.json`
(configure on first start instead of using the browser),
`PYTHON=/usr/bin/python3.11` to pick a specific Python.

If you lose the one-time password before using it, it's in
`/var/lib/plant-hmi/initial-admin-password` (root only) until the admin
password is changed. To reset any password later:

```bash
cd /opt/plant-hmi && sudo -u plant-hmi env HMI_DB=/var/lib/plant-hmi/edge.db venv/bin/python -m src.hmi.users passwd admin
```

## Offline install

On a PC with internet, download the packages for the IoT2050's ARM processor,
matching the device's Python version (here 3.11; use `3.9` for Debian 11 images):

```bash
pip download --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 --only-binary=:all: --python-version 3.11 \
    -r deploy/iot2050/requirements-edge.txt -d wheels
```

Copy the `wheels` folder along with the repository, then on the device:

```bash
sudo ./deploy/iot2050/install.sh --wheels ./wheels
```

## Day to day

```bash
systemctl status plant-hmi           # is it running?
journalctl -u plant-hmi -f           # live log
sudo systemctl restart plant-hmi     # restart
```

**Update:** copy the new repository version to the device and re-run
`sudo ./deploy/iot2050/install.sh`. The app is replaced; the data in
`/var/lib/plant-hmi` is kept.

**Remove:** `sudo ./deploy/iot2050/install.sh --uninstall` keeps the data;
add `--purge` to delete it as well.

**Back up:** download the package from the Commissioning tab, or copy
`/var/lib/plant-hmi/edge.db` while the service is stopped.

## Security notes

- The HMI listens on the plant network over plain HTTP. Keep the IoT2050's web
  port off the internet; for remote access use the cloud link (next step) or a VPN.
- The service is sandboxed by systemd: it runs as `plant-hmi`, can only write
  `/var/lib/plant-hmi`, and its files are private to that account.
