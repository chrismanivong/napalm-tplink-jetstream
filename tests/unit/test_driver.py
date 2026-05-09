"""Unit tests for TPLinkJetstreamDriver — no real device required."""

import pytest
from unittest.mock import MagicMock, patch

from napalm_tplink_jetstream.tplink_jetstream import TPLinkJetstreamDriver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def driver():
    """Return a driver instance with a mocked Netmiko connection."""
    with patch("napalm_tplink_jetstream.tplink_jetstream.ConnectHandler"):
        drv = TPLinkJetstreamDriver(
            hostname="192.168.0.1",
            username="admin",
            password="admin",
        )
        drv.device = MagicMock()
        yield drv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SHOW_SYSTEM_INFO = """\
System Name            : T2600G-28TS
Hardware Version       : V2.0
Firmware Version       : 2.0.7 Build 20200929
MAC Address            : 00-0A-EB-13-07-82
IP Address             : 192.168.0.1
Subnet Mask            : 255.255.255.0
Default Gateway        : 192.168.0.254
System Time            : 2024-03-01 08:10:22
Run Time               : 5 day(s) 2 hour(s) 35 min(s) 16 sec(s)
"""

SHOW_INTERFACE_STATUS = """\
Port     Status    Speed  Duplex  FlowCtrl  Active-Medium  LAG  Linkdown-Status  Description
Gi1/0/1  LinkUp    1000M  Full    Disable   Copper         N/A  N/A              uplink
Gi1/0/2  LinkDown  100M   Full    Disable   Copper         N/A  N/A
"""

SHOW_INTERFACE_CONFIG = """\
Port     State   Speed  Duplex  FlowCtrl  Description
Gi1/0/1  Enable  Auto   Auto    Disable   uplink
Gi1/0/2  Enable  Auto   Auto    Disable
"""

SHOW_JUMBO = "Jumbo frames are disabled. Jumbo frame size is 1518 bytes."

SHOW_ARP = """\
Interface          Address                 Hardware Addr           Type
VLAN8              192.168.0.100           00:50:56:a1:b2:c3       DYNAMIC
VLAN8              192.168.0.254           00:0c:29:d4:e5:f6       DYNAMIC
"""

SHOW_MAC = """\
MAC Address Table
------------------------------------------------------------
MAC                VLAN    Port     Type            Aging
---                ----    ----     ----            -----
00:50:56:a1:b2:c3  8       Gi1/0/2  dynamic         aging
ff:ff:ff:ff:ff:ff  1       CPU      static          noaging
"""

SHOW_VLAN = """\
UT: Untagged;     TG: Tagged
VLAN  Name             Status    Ports
----  ---------------  --------  ----------------
1     Default          active    UT: Gi1/0/1-2
10    Management       active    UT: Gi1/0/3
"""

SHOW_LLDP_NEIGHBOR_INFO = """\
Port      Device ID               Port ID   management address  Port Description  System Name
----      ------------            --------  ------------------  ----------------  -----------
Gi1/0/1   00-AA-BB-CC-DD-EE       Gi0/1     10.0.0.1            uplink            core-sw-01
"""




# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetFacts:
    def test_returns_required_keys(self, driver):
        driver.device.send_command.return_value = SHOW_SYSTEM_INFO
        driver._send_command = lambda cmd, **kw: SHOW_SYSTEM_INFO

        # _get_interface_list needs show interface
        driver.device.send_command.side_effect = lambda cmd, **kw: (
            SHOW_SYSTEM_INFO if "system-info" in cmd else SHOW_INTERFACE
        )

        facts = driver.get_facts()
        for key in ("vendor", "model", "hostname", "os_version", "serial_number",
                    "uptime", "interface_list", "fqdn"):
            assert key in facts

    def test_vendor(self, driver):
        driver._send_command = lambda cmd: SHOW_SYSTEM_INFO
        driver._get_interface_list = lambda: []
        facts = driver.get_facts()
        assert facts["vendor"] == "TP-Link"

    def test_uptime_parsing(self, driver):
        driver._send_command = lambda cmd: SHOW_SYSTEM_INFO
        driver._get_interface_list = lambda: []
        facts = driver.get_facts()
        expected = 5 * 86400 + 2 * 3600 + 35 * 60 + 16
        assert facts["uptime"] == float(expected)

    def test_os_version(self, driver):
        driver._send_command = lambda cmd: SHOW_SYSTEM_INFO
        driver._get_interface_list = lambda: []
        facts = driver.get_facts()
        assert "2.0.7" in facts["os_version"]


class TestGetInterfaces:
    def _mock_send(self, cmd):
        if "jumbo" in cmd:
            return SHOW_JUMBO
        if "configuration" in cmd:
            return SHOW_INTERFACE_CONFIG
        return SHOW_INTERFACE_STATUS

    def test_interface_count(self, driver):
        driver._send_command = self._mock_send
        ifaces = driver.get_interfaces()
        assert len(ifaces) == 2

    def test_is_up_and_enabled(self, driver):
        driver._send_command = self._mock_send
        ifaces = driver.get_interfaces()
        gi1 = ifaces["Gi1/0/1"]
        assert gi1["is_up"] is True
        assert gi1["is_enabled"] is True
        gi2 = ifaces["Gi1/0/2"]
        assert gi2["is_up"] is False

    def test_speed(self, driver):
        driver._send_command = self._mock_send
        ifaces = driver.get_interfaces()
        assert ifaces["Gi1/0/1"]["speed"] == 1000.0

    def test_description(self, driver):
        driver._send_command = self._mock_send
        ifaces = driver.get_interfaces()
        assert ifaces["Gi1/0/1"]["description"] == "uplink"


class TestGetArpTable:
    def test_entry_count(self, driver):
        driver._send_command = lambda cmd: SHOW_ARP
        table = driver.get_arp_table()
        assert len(table) == 2

    def test_entry_structure(self, driver):
        driver._send_command = lambda cmd: SHOW_ARP
        entry = driver.get_arp_table()[0]
        for key in ("interface", "mac", "ip", "age"):
            assert key in entry

    def test_ip_value(self, driver):
        driver._send_command = lambda cmd: SHOW_ARP
        ips = {e["ip"] for e in driver.get_arp_table()}
        assert "192.168.0.100" in ips
        assert "192.168.0.254" in ips

    def test_interface_value(self, driver):
        driver._send_command = lambda cmd: SHOW_ARP
        interfaces = {e["interface"] for e in driver.get_arp_table()}
        assert "VLAN8" in interfaces


class TestGetMacAddressTable:
    def test_entry_count(self, driver):
        driver._send_command = lambda cmd: SHOW_MAC
        table = driver.get_mac_address_table()
        assert len(table) == 2

    def test_static_flag(self, driver):
        driver._send_command = lambda cmd: SHOW_MAC
        entries = {e["mac"]: e for e in driver.get_mac_address_table()}
        assert entries["FF:FF:FF:FF:FF:FF"]["static"] is True
        assert entries["00:50:56:A1:B2:C3"]["static"] is False

    def test_vlan_value(self, driver):
        driver._send_command = lambda cmd: SHOW_MAC
        entries = {e["mac"]: e for e in driver.get_mac_address_table()}
        assert entries["00:50:56:A1:B2:C3"]["vlan"] == 8


class TestGetVlans:
    def test_vlan_count(self, driver):
        driver._send_command = lambda cmd: SHOW_VLAN
        vlans = driver.get_vlans()
        assert len(vlans) == 2

    def test_vlan_name(self, driver):
        driver._send_command = lambda cmd: SHOW_VLAN
        vlans = driver.get_vlans()
        assert vlans["1"]["name"] == "Default"
        assert vlans["10"]["name"] == "Management"

    def test_port_expansion(self, driver):
        driver._send_command = lambda cmd: SHOW_VLAN
        vlans = driver.get_vlans()
        assert "Gi1/0/1" in vlans["1"]["interfaces"]
        assert "Gi1/0/2" in vlans["1"]["interfaces"]


class TestGetLldpNeighbors:
    def test_returns_neighbor(self, driver):
        driver._send_command = lambda cmd: SHOW_LLDP_NEIGHBOR_INFO
        neighbors = driver.get_lldp_neighbors()
        assert "Gi1/0/1" in neighbors
        assert neighbors["Gi1/0/1"][0]["hostname"] == "core-sw-01"
        assert neighbors["Gi1/0/1"][0]["port"] == "Gi0/1"


class TestGetLldpNeighborsDetail:
    def test_returns_detail_keys(self, driver):
        driver._send_command = lambda cmd: SHOW_LLDP_NEIGHBOR_INFO
        details = driver.get_lldp_neighbors_detail()
        entry = details["Gi1/0/1"][0]
        for key in ("remote_chassis_id", "remote_system_name", "remote_port"):
            assert key in entry

    def test_system_name_parsed(self, driver):
        driver._send_command = lambda cmd: SHOW_LLDP_NEIGHBOR_INFO
        details = driver.get_lldp_neighbors_detail()
        assert details["Gi1/0/1"][0]["remote_system_name"] == "core-sw-01"

    def test_chassis_id_parsed(self, driver):
        driver._send_command = lambda cmd: SHOW_LLDP_NEIGHBOR_INFO
        details = driver.get_lldp_neighbors_detail()
        assert details["Gi1/0/1"][0]["remote_chassis_id"] == "00-AA-BB-CC-DD-EE"


class TestIsAlive:
    def test_returns_false_when_device_is_none(self, driver):
        driver.device = None
        assert driver.is_alive() == {"is_alive": False}

    def test_returns_true_when_transport_active(self, driver):
        driver.device.remote_conn.transport.is_active.return_value = True
        assert driver.is_alive() == {"is_alive": True}


class TestUptimeParser:
    @pytest.mark.parametrize("uptime_str,expected", [
        ("0 day(s) 0 hour(s) 0 min(s) 0 sec(s)", 0),
        ("1 day(s) 0 hour(s) 0 min(s) 0 sec(s)", 86400),
        ("5 day(s) 2 hour(s) 35 min(s) 16 sec(s)", 5 * 86400 + 2 * 3600 + 35 * 60 + 16),
    ])
    def test_parse_uptime_seconds(self, uptime_str, expected):
        result = TPLinkJetstreamDriver._parse_uptime_seconds(uptime_str)
        assert result == float(expected)


class TestParseVlanPorts:
    def test_single_port(self):
        assert TPLinkJetstreamDriver._parse_vlan_ports("Gi1/0/1") == ["Gi1/0/1"]

    def test_range(self):
        result = TPLinkJetstreamDriver._parse_vlan_ports("Gi1/0/1-4")
        assert result == ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/4"]

    def test_comma_separated(self):
        result = TPLinkJetstreamDriver._parse_vlan_ports("Gi1/0/1, Gi1/0/5")
        assert result == ["Gi1/0/1", "Gi1/0/5"]

    def test_tg_ut_prefixes(self):
        result = TPLinkJetstreamDriver._parse_vlan_ports("UT: Gi1/0/1-2 TG: Gi1/0/9")
        assert "Gi1/0/1" in result
        assert "Gi1/0/2" in result
        assert "Gi1/0/9" in result
