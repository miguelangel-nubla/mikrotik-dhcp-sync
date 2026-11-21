# Mikrotik DHCP Sync

This script synchronizes DHCP reservations from a "Master" Mikrotik router to one or more "Slave" Mikrotik routers. It also supports synchronizing host names to [WatchYourLAN](https://github.com/aceberg/WatchYourLAN).

## Features

-   **Mikrotik to Mikrotik Sync**:
    -   Reads DHCP leases from a master router.
    -   Syncs them to configured slave routers.
    -   Removes leases on slaves that the master doesn't have or that differ from it, including ones that conflict by MAC or by server.
    -   Adds new leases or updates existing ones on slaves.
    -   Exits non-zero if any change is rejected by a slave (see [Exit codes](#exit-codes)).
-   **WatchYourLAN Sync**:
    -   Updates host names in WatchYourLAN based on Mikrotik DHCP comments.
    -   Automatically marks hosts as "known" in WatchYourLAN if matched by MAC address.
    -   Unmarks "known" status (sets to unknown) for hosts in WatchYourLAN that are not present in the master router.
    -   Handles multiple WatchYourLAN instances.
    -   Exits non-zero if WatchYourLAN can't be reached or rejects an update.

## Prerequisites

-   Docker
-   SSH access to Mikrotik routers (key-based auth recommended).

## Configuration

1.  Copy `example.config.yaml` to `config.yaml` (or create your own). See [example.config.yaml](example.config.yaml) for available options.

    -   **master**: usage is self-explanatory.
    -   **slaves**: List of slave routers.
    -   **watchyourlan**: List of WatchYourLAN instances (url is required).

## Usage

Run the container using the pre-built image. Mount your configuration directory to `/app/config`.

```bash
docker run --rm \
  -v $(pwd)/config.yaml:/app/config/config.yaml \
  ghcr.io/miguelangel-nubla/mikrotik-dhcp-sync
```

### Environment Variables

-   `LOG_LEVEL`: Set logging verbosity (default: `INFO`).
    ```bash
    -e LOG_LEVEL=DEBUG
    ```
-   `DRY_RUN`: Log the changes that would be made on slaves without making them.
    ```bash
    -e DRY_RUN=1
    ```

### Exit codes

`1` means a fatal error stopped the run (bad config, SSH failure, no reservations read from the master). Otherwise every step runs, and each kind of problem adds a bit, so one code can report several:

| Code | Meaning |
| --- | --- |
| `0` | Everything synced. |
| `1` | Fatal error, the run stopped. |
| `2` | A slave lacks a DHCP server the master has; its reservations were skipped there. |
| `4` | A slave rejected a change. |
| `8` | A WatchYourLAN fetch or update failed. |

For example `10` = `2 + 8`. Test a bit with `$(( code & 8 ))`.

## Development

The tests replace the routers and WatchYourLAN with fakes, so they need no network:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests
```

