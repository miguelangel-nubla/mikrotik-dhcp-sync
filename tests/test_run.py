"""Tests for run.py, with the routers (SSH) and WatchYourLAN (HTTP) replaced by fakes."""
import importlib
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run  # noqa: E402

MASTER = (
    '/ip dhcp-server add interface=bridge name=lan\n'
    '/ip dhcp-server lease add address=10.0.0.5 comment=alpha mac-address=AA:AA:AA:AA:AA:01 server=lan\n'
    '/ip dhcp-server lease add address=10.0.0.6 comment="beta host" mac-address=AA:AA:AA:AA:AA:02\n'
)
# Has no "lan" server, so the master's lan reservations can't go there
SLAVE_NO_LAN = '/ip dhcp-server lease add address=10.0.0.6 comment="beta host" mac-address=AA:AA:AA:AA:AA:02\n'


class FakeClient:
    """A router: answers the export with `export` and records every change command."""

    def __init__(self, export, reject=False):
        self.export = export
        self.reject = reject
        self.commands = []

    def close(self):
        pass


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


class FakeWyl:
    """WatchYourLAN: `hosts` from /api/all, `edit_status` for /api/edit calls; `down` refuses everything."""

    def __init__(self, hosts=(), edit_status=200, down=False):
        self.hosts = list(hosts)
        self.edit_status = edit_status
        self.down = down
        self.urls = []

    def get(self, url, timeout=None):
        assert timeout, "every WatchYourLAN request needs a timeout"
        self.urls.append(url)
        if self.down:
            raise requests.ConnectionError("connection refused")
        if url.endswith('/api/all'):
            return FakeResponse(200, self.hosts)
        return FakeResponse(self.edit_status)


@pytest.fixture
def r(monkeypatch):
    """A fresh run module (its module-level error lists start empty) with SSH faked."""
    mod = importlib.reload(run)

    def setup(slave_export=MASTER, reject=False, wyl=None, master_export=MASTER):
        clients = {'master': FakeClient(master_export), 'slave': FakeClient(slave_export, reject)}
        config = {'master': {'host': 'master', 'username': 'u'}, 'slaves': [{'host': 'slave', 'username': 'u'}]}
        if wyl:
            config['watchyourlan'] = [{'url': 'http://wyl'}]
            monkeypatch.setattr(mod.requests, 'get', wyl.get)
        monkeypatch.setattr(mod, 'load_config', lambda f: config)
        monkeypatch.setattr(mod, 'ssh_connect', lambda host, *a, **k: clients[host])

        def ssh_command(client, command):
            if 'export' in command:
                return client.export
            client.commands.append(command)
            return 'failure: already have static lease with this MAC' if client.reject else ''

        monkeypatch.setattr(mod, 'ssh_command', ssh_command)
        return clients

    mod.setup = setup
    return mod


def exit_code(mod):
    with pytest.raises(SystemExit) as e:
        mod.main()
    return e.value.code


def test_parse_attributes_handles_quotes_and_escapes():
    attrs = run.parse_attributes('address=10.0.0.6 comment="beta \\"b\\" host" mac-address=AA:AA:AA:AA:AA:02')
    assert attrs == {'address': '10.0.0.6', 'comment': 'beta "b" host', 'mac-address': 'AA:AA:AA:AA:AA:02'}


def test_in_sync_exits_0_and_changes_nothing(r):
    clients = r.setup()
    assert exit_code(r) == 0
    assert clients['slave'].commands == []


def test_new_master_lease_is_added_and_stale_one_removed(r):
    clients = r.setup(slave_export=MASTER.replace('10.0.0.6', '10.0.0.9'))
    assert exit_code(r) == 0
    removes = [c for c in clients['slave'].commands if ' remove ' in c]
    adds = [c for c in clients['slave'].commands if ' add ' in c]
    assert len(removes) == 1 and 'address=10.0.0.9' in removes[0]
    assert len(adds) == 1 and 'address=10.0.0.6' in adds[0]
    # Removes run before adds: RouterOS refuses a second lease for the same MAC
    assert clients['slave'].commands.index(removes[0]) < clients['slave'].commands.index(adds[0])


def test_dry_run_changes_nothing(r, monkeypatch):
    monkeypatch.setattr(r, 'dry_run', True)
    clients = r.setup(slave_export='/ip dhcp-server add name=lan\n')
    assert exit_code(r) == 0
    assert clients['slave'].commands == []


def test_empty_master_is_fatal_and_slaves_untouched(r):
    clients = r.setup(master_export='/ip dhcp-server add name=lan\n')
    assert exit_code(r) == r.EXIT_FATAL
    assert clients['slave'].commands == []


def test_missing_server_on_slave_exits_2(r):
    r.setup(slave_export=SLAVE_NO_LAN)
    assert exit_code(r) == r.EXIT_MISSING_SERVERS == 2


def test_rejected_change_exits_4(r):
    r.setup(slave_export='/ip dhcp-server add name=lan\n', reject=True)
    assert exit_code(r) == r.EXIT_SLAVE_REJECTED == 4


def test_watchyourlan_down_exits_8(r):
    wyl = FakeWyl(down=True)
    r.setup(wyl=wyl)
    assert exit_code(r) == r.EXIT_WATCHYOURLAN_FAILED == 8


def test_watchyourlan_edit_error_exits_8(r):
    r.setup(wyl=FakeWyl(hosts=[{'ID': 1, 'Mac': 'aa:aa:aa:aa:aa:01', 'Name': 'old', 'Known': False}],
                        edit_status=500))
    assert exit_code(r) == 8


def test_problems_combine_and_watchyourlan_still_runs(r):
    wyl = FakeWyl(down=True)
    r.setup(slave_export=SLAVE_NO_LAN, wyl=wyl)
    assert exit_code(r) == r.EXIT_MISSING_SERVERS | r.EXIT_WATCHYOURLAN_FAILED == 10
    assert wyl.urls, "WatchYourLAN sync must run even when a slave had problems"


def test_watchyourlan_marks_known_renames_and_unmarks(r):
    wyl = FakeWyl(hosts=[
        {'ID': 1, 'Mac': 'AA:AA:AA:AA:AA:01', 'Name': 'old name', 'Known': False},  # in master: rename + mark known
        {'ID': 2, 'Mac': 'aa:aa:aa:aa:aa:02', 'Name': 'beta host', 'Known': True},  # in master, already right
        {'ID': 3, 'Mac': 'aa:aa:aa:aa:aa:99', 'Name': 'gone', 'Known': True},       # not in master: unmark
    ])
    r.setup(wyl=wyl)
    assert exit_code(r) == 0
    edits = [u for u in wyl.urls if '/api/edit/' in u]
    assert edits == [
        'http://wyl/api/edit/1/alpha',
        'http://wyl/api/edit/1/alpha/toggle',
        'http://wyl/api/edit/3/gone/toggle',
    ]
