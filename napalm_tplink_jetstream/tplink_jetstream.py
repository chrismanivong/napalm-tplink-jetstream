# -*- coding: utf-8 -*-
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""NAPALM driver for TP-Link Jetstream managed switches.

Tested against: SG2210P, T1500G, T1600G, T2600G, T3700G series.
Netmiko device_type: ``tplink_jetstream``
"""

import re
import socket
from typing import Dict, List, Optional, Union, Any

import netaddr
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

from napalm_device_types import SwitchDriver
from napalm.base import helpers as napalm_helpers
from napalm.base.exceptions import (
    ConnectionException,
    ConnectionClosedException,
    CommandErrorException,
    MergeConfigException,
    ReplaceConfigException,
)
from napalm.base.netmiko_helpers import netmiko_args
import napalm.base.constants as C


class TPLinkJetstreamDriver(SwitchDriver):
    """NAPALM driver for TP-Link Jetstream managed switches."""

    VENDOR = "TP-Link"
    # Netmiko device type for TP-Link Jetstream
    NETMIKO_DEVICE_TYPE = "tplink_jetstream"

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str,
        timeout: int = 60,
        optional_args: Optional[Dict] = None,
    ) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.device: Optional[ConnectHandler] = None

        if optional_args is None:
            optional_args = {}

        self.force_no_enable = optional_args.get("force_no_enable", False)
        self.port = optional_args.get("port", 22)
        self.use_canonical_interface = optional_args.get("canonical_int_fmt", False)
        self.netmiko_optional_args = netmiko_args(optional_args)

        # Config management state
        self._candidate_config: Optional[str] = None
        self._candidate_mode: Optional[str] = None   # 'merge' or 'replace'
        self._backup_config: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open an SSH connection to the device."""
        try:
            self.device = ConnectHandler(
                device_type=self.NETMIKO_DEVICE_TYPE,
                host=self.hostname,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                **self.netmiko_optional_args,
            )
            if not self.force_no_enable:
                self.device.enable()
        except NetmikoTimeoutException as exc:
            raise ConnectionException(
                f"Cannot connect to {self.hostname}: {exc}"
            ) from exc
        except NetmikoAuthenticationException as exc:
            raise ConnectionException(
                f"Authentication failed for {self.hostname}: {exc}"
            ) from exc

    def close(self) -> None:
        """Close the SSH connection."""
        if self.device:
            self.device.disconnect()
            self.device = None

    def is_alive(self) -> Dict[str, bool]:
        """Return connection liveness.

        Only checks the transport-level state – does NOT write anything to
        the channel, which would pollute the read buffer for subsequent
        ``send_command`` calls.
        """
        if self.device is None:
            return {"is_alive": False}
        try:
            return {"is_alive": self.device.remote_conn.transport.is_active()}
        except (socket.error, EOFError, AttributeError):
            return {"is_alive": False}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_command(self, command: Union[str, List[str]]) -> str:
        """Send a command (or list of candidate commands) to the device.

        When a list is supplied, commands are tried in order and the first
        one that does not return an error indicator is returned.

        Uses an explicit ``expect_string`` anchored to the device prompt so
        that Netmiko never mistakes a data line inside the output for the
        prompt (which would break subsequent commands in the same session).
        """
        prompt_pattern = rf"{re.escape(self.device.base_prompt)}[>#]"

        def _do_send(cmd: str) -> str:
            return self.device.send_command(
                cmd,
                expect_string=prompt_pattern,
                read_timeout=self.timeout,
            ).strip()

        try:
            if isinstance(command, list):
                output = ""
                for cmd in command:
                    output = _do_send(cmd)
                    if "% Invalid" not in output and "Error" not in output:
                        return output
                return output
            return _do_send(command)
        except (socket.error, EOFError) as exc:
            raise ConnectionClosedException(str(exc)) from exc

    # ------------------------------------------------------------------
    # Configuration-mode helpers
    # ------------------------------------------------------------------

    def _exec_prompt(self) -> str:
        return rf"{re.escape(self.device.base_prompt)}[>#]"

    def _any_prompt(self) -> str:
        """Matches exec prompt AND any config sub-mode prompt."""
        return rf"{re.escape(self.device.base_prompt)}(?:\([^)]*\))?[>#]"

    def _conf_prompt(self) -> str:
        return rf"{re.escape(self.device.base_prompt)}\(config[^)]*\)[>#]"

    def _enter_config_mode(self) -> None:
        self.device.send_command(
            "configure",
            expect_string=self._conf_prompt(),
            read_timeout=self.timeout,
        )

    def _exit_config_mode(self) -> None:
        self.device.send_command(
            "end",
            expect_string=self._exec_prompt(),
            read_timeout=self.timeout,
        )

    def _save_config(self) -> None:
        self.device.send_command(
            "copy running-config startup-config",
            expect_string=self._exec_prompt(),
            read_timeout=self.timeout,
        )

    def _apply_config_lines(self, config_text: str) -> List[str]:
        """Send config lines to the device while in config mode.

        Returns a list of error messages for any line that was rejected.
        Lines starting with ``!`` or ``#`` and blank lines are skipped.
        """
        ep_any = self._any_prompt()
        errors: List[str] = []
        for line in config_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("!", "#")):
                continue
            out = self.device.send_command(
                stripped,
                expect_string=ep_any,
                read_timeout=self.timeout,
            ).strip()
            if out and ("Error" in out or "% Invalid" in out or "% Unknown" in out):
                errors.append(f"  {stripped!r}: {out}")
        return errors

    @staticmethod
    def _parse_key_value(output: str, key: str, separator: str = "auto") -> str:
        """Extract the value for *key* from ``key : value`` or ``key - value`` style output.

        When *separator* is ``"auto"`` (default) both ``:`` and ``-`` are tried.
        """
        separators = [separator] if separator != "auto" else [":", "-"]
        for line in output.splitlines():
            if key.lower() in line.lower():
                for sep in separators:
                    if sep in line:
                        parts = line.split(sep, 1)
                        if len(parts) == 2:
                            return parts[1].strip()
        return ""

    @staticmethod
    def _parse_uptime_seconds(uptime_str: str) -> float:
        """Convert a TP-Link uptime string to seconds.

        Expected formats:
          ``5 day(s) 2 hour(s) 35 min(s) 16 sec(s)``
          ``0 day(s) 0 hour(s) 5 min(s) 3 sec(s)``
        """
        days = hours = minutes = seconds = 0
        match = re.search(r"(\d+)\s+day", uptime_str, re.I)
        if match:
            days = int(match.group(1))
        match = re.search(r"(\d+)\s+hour", uptime_str, re.I)
        if match:
            hours = int(match.group(1))
        match = re.search(r"(\d+)\s+min", uptime_str, re.I)
        if match:
            minutes = int(match.group(1))
        match = re.search(r"(\d+)\s+sec", uptime_str, re.I)
        if match:
            seconds = int(match.group(1))
        return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)

    # ------------------------------------------------------------------
    # NAPALM getters
    # ------------------------------------------------------------------

    def get_facts(self) -> Dict:
        """Return a dictionary of general device facts.

        Runs ``show system-info`` and ``show interface`` to collect:
        vendor, model, hostname, os_version, serial_number, uptime,
        interface_list.

        Example ``show system-info`` output::

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
        sys_info = self._send_command("show system-info")

        hostname = self._parse_key_value(sys_info, "System Name")
        # Model is in "Hardware Version" on older firmware (e.g. "T2600G V2.0")
        # and as a prefix of "System Description" on Omada firmware
        hw_version = self._parse_key_value(sys_info, "Hardware Version")
        model = hw_version.split()[0] if hw_version else ""

        # "Software Version" (Omada) or "Firmware Version" (classic)
        os_version = (
            self._parse_key_value(sys_info, "Software Version")
            or self._parse_key_value(sys_info, "Firmware Version")
        )
        # "Running Time" (Omada) or "Run Time" (classic)
        uptime_str = (
            self._parse_key_value(sys_info, "Running Time")
            or self._parse_key_value(sys_info, "Run Time")
        )
        uptime = self._parse_uptime_seconds(uptime_str)

        serial_number = self._parse_key_value(sys_info, "Serial Number") or ""

        interface_list = self._get_interface_list()

        return {
            "vendor": self.VENDOR,
            "model": model,
            "hostname": hostname,
            "fqdn": hostname,
            "os_version": os_version,
            "serial_number": serial_number,
            "uptime": uptime,
            "interface_list": interface_list,
        }

    def _get_interface_list(self) -> List[str]:
        """Return a sorted list of interface names from ``show interface status``."""
        output = self._send_command("show interface status")
        interfaces = []
        for line in output.splitlines():
            match = re.match(
                r"^\s*(Gi|Te|Fa|Lag|Vlan)(\S+)",
                line,
                re.I,
            )
            if match:
                interfaces.append(match.group(1) + match.group(2))
        return sorted(set(interfaces), key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)])

    def get_interfaces(self) -> Dict[str, Dict]:
        """Return a dictionary of interface details.

        Each interface entry contains:
        ``is_up``, ``is_enabled``, ``description``, ``last_flapped``,
        ``speed``, ``mtu``, ``mac_address``.

        Combines ``show interface status`` and ``show interface configuration``.

        ``show interface status`` columns (tabular)::

            Port     Status    Speed  Duplex  FlowCtrl  Active-Medium  LAG  Linkdown-Status  Description
            Gi1/0/1  LinkUp    1000M  Full    Disable   Copper         N/A  N/A              uplink

        ``show interface configuration`` columns (tabular)::

            Port     State   Speed  Duplex  FlowCtrl  Description
            Gi1/0/1  Enable  Auto   Auto    Disable   uplink
        """
        jumbo_out = self._send_command("show jumbo")
        global_mtu = 1518
        m = re.search(r"(\d+)", jumbo_out)
        if m:
            global_mtu = int(m.group(1))

        status_out = self._send_command("show interface status")
        config_out = self._send_command("show interface configuration")
        return self._parse_interfaces(status_out, config_out, global_mtu)

    def _parse_interfaces(self, status_output: str, config_output: str = "", mtu: int = 1518) -> Dict[str, Dict]:
        """Parse tabular output of 'show interface status' and 'show interface configuration'."""
        interfaces: Dict[str, Dict] = {}

        # --- parse 'show interface status' ---
        # Columns: Port Status Speed Duplex FlowCtrl Active-Medium LAG Linkdown-Status Description
        for line in status_output.splitlines():
            match = re.match(
                r"^\s*((?:Gi|Te|Fa|Lag|Vlan)\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)",
                line,
                re.I,
            )
            if not match:
                continue
            port, status, speed, duplex, _, _, _, _, description = (
                match.group(1), match.group(2), match.group(3), match.group(4),
                match.group(5), match.group(6), match.group(7), match.group(8),
                match.group(9).strip(),
            )
            speed_val = 0.0
            speed_match = re.search(r"(\d+)", speed)
            if speed_match:
                speed_val = float(speed_match.group(1))

            interfaces[port] = {
                "is_up": status.lower() == "linkup",
                "is_enabled": True,  # overwritten by config output below
                "description": description,
                "last_flapped": -1.0,
                "speed": speed_val,
                "mtu": mtu,
                "mac_address": "",
            }

        # --- parse 'show interface configuration' ---
        # Columns: Port State Speed Duplex FlowCtrl Description
        for line in config_output.splitlines():
            match = re.match(
                r"^\s*((?:Gi|Te|Fa|Lag|Vlan)\S+)\s+(\S+)\s+\S+\s+\S+\s+\S+\s*(.*)",
                line,
                re.I,
            )
            if not match:
                continue
            port, state, description = match.group(1), match.group(2), match.group(3).strip()
            if port in interfaces:
                interfaces[port]["is_enabled"] = state.lower() == "enable"
                # config description takes precedence (more reliable source)
                if description:
                    interfaces[port]["description"] = description
            else:
                interfaces[port] = {
                    "is_up": False,
                    "is_enabled": state.lower() == "enable",
                    "description": description,
                    "last_flapped": -1.0,
                    "speed": 0.0,
                    "mtu": mtu,
                    "mac_address": "",
                }

        return interfaces

    def get_interfaces_ip(self) -> Dict[str, Dict]:
        """Return all configured IP addresses grouped by interface.

        Runs ``show ip interface`` which produces a block per interface::

            VLAN8 is up, line protocol is up
             Primary IP address is 172.22.8.15/24
        """
        output = self._send_command("show ip interface")
        interfaces_ip: Dict[str, Dict] = {}
        current_iface: Optional[str] = None

        for line in output.splitlines():
            # Detect interface header: "VLAN8 is up, ..." or "VLAN8 is down, ..."
            m = re.match(r"^(\S+)\s+is\s+(?:up|down)", line, re.I)
            if m:
                current_iface = m.group(1)
                continue

            if current_iface is None:
                continue

            # "  Primary IP address is 172.22.8.15/24"
            m = re.match(r"^\s+Primary IP address is\s+(\S+)", line, re.I)
            if m:
                cidr = m.group(1)
                try:
                    ip_net = netaddr.IPNetwork(cidr)
                except (netaddr.AddrFormatError, ValueError):
                    continue
                family = f"ipv{ip_net.version}"
                if current_iface not in interfaces_ip:
                    interfaces_ip[current_iface] = {}
                interfaces_ip[current_iface].setdefault(family, {})[str(ip_net.ip)] = {
                    "prefix_length": ip_net.prefixlen
                }

        return interfaces_ip

    def get_config(
        self,
        retrieve: str = "all",
        full: bool = False,
        sanitized: bool = False,
        format: str = "text",
    ) -> Dict[str, str]:
        """Return running and/or startup configuration.

        TP-Link Jetstream does not support a candidate configuration;
        that slot is always returned as an empty string.
        """
        configs = {"running": "", "startup": "", "candidate": ""}

        if retrieve in ("all", "running"):
            configs["running"] = self._send_command("show running-config")

        if retrieve in ("all", "startup"):
            configs["startup"] = self._send_command("show startup-config")

        if sanitized:
            configs = napalm_helpers.sanitize_configs(configs, C.CISCO_SANITIZE_FILTERS)

        return configs

    def get_arp_table(self, vrf: str = "") -> List[Dict]:
        """Return the ARP table.

        Example ``show arp`` output::

            Interface          Address                 Hardware Addr           Type
            VLAN8              172.22.8.222            0c:9d:92:c2:52:e7       DYNAMIC
        """
        output = self._send_command("show arp")
        arp_table = []
        in_table = False

        for line in output.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            # Detect header by the "Address" + "Hardware" keywords
            if "Address" in line_s and "Hardware" in line_s:
                in_table = True
                continue
            if not in_table:
                continue
            # Skip summary lines
            if line_s.lower().startswith("total"):
                continue

            parts = line_s.split()
            # Columns: Interface, Address (IP), Hardware Addr (MAC), Type
            if len(parts) < 3:
                continue

            interface = parts[0]
            ip_addr = parts[1]
            mac_raw = parts[2]

            try:
                netaddr.IPAddress(ip_addr)
            except (netaddr.AddrFormatError, ValueError):
                continue

            try:
                mac_addr = napalm_helpers.mac(mac_raw)
            except Exception:
                mac_addr = mac_raw

            arp_table.append(
                {
                    "interface": interface,
                    "mac": mac_addr,
                    "ip": ip_addr,
                    "age": 0.0,
                }
            )

        return arp_table

    def get_mac_address_table(self) -> List[Dict]:
        """Return the MAC address table.

        Example ``show mac address-table`` output::

            MAC Address Table
            ------------------------------------------------------------
            MAC                VLAN    Port     Type            Aging
            ---                ----    ----     ----            -----
            00:1a:8c:81:22:94  8       Gi1/0/9  dynamic         aging
        """
        output = self._send_command("show mac address-table")
        mac_table = []
        in_table = False

        for line in output.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            if re.match(r"^-{3,}", line_s):
                in_table = True
                continue
            if not in_table:
                continue
            # Skip summary or header continuation lines
            if line_s.lower().startswith("total") or line_s.lower().startswith("mac"):
                continue

            parts = line_s.split()
            # Columns: MAC, VLAN, Port, Type, Aging
            if len(parts) < 3:
                continue

            mac_raw = parts[0]
            try:
                vlan = int(parts[1])
            except ValueError:
                continue

            interface = parts[2]
            entry_type = parts[3].lower() if len(parts) >= 4 else "dynamic"

            try:
                mac_addr = napalm_helpers.mac(mac_raw)
            except Exception:
                mac_addr = mac_raw

            mac_table.append(
                {
                    "mac": mac_addr,
                    "interface": interface,
                    "vlan": vlan,
                    "static": entry_type == "static",
                    "active": True,
                    "moves": None,
                    "last_move": None,
                }
            )

        return mac_table

    def get_lldp_neighbors(self) -> Dict[str, List[Dict]]:
        """Return a dict of LLDP neighbors keyed by local port.

        Example ``show lldp neighbor-information`` output::

            Port      Device ID               Port ID   management address  Port Description  System Name
            ----      ------------            --------  ------------------  ----------------  -----------
            Gi1/0/9   64:E8:81:E1:29:00       2         10.7.0.10           2                 swt-l0-1-10
        """
        neighbors: Dict[str, List[Dict]] = {}
        for row in self._get_lldp_table():
            neighbors.setdefault(row["local_port"], []).append(
                {"hostname": row["system_name"], "port": row["port_id"]}
            )
        return neighbors

    def _get_lldp_table(self) -> List[Dict]:
        """Parse ``show lldp neighbor-information`` into a list of row dicts."""
        output = self._send_command("show lldp neighbor-information")
        rows: List[Dict] = []
        in_table = False

        for line in output.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            if re.match(r"^-{4,}", line_s):
                in_table = True
                continue
            if not in_table:
                continue

            parts = line_s.split()
            # Columns: Port, Device ID, Port ID, Mgmt Addr, Port Description, System Name
            if len(parts) < 3:
                continue
            # First column must look like a port name
            if not re.match(r"^(?:Gi|Te|Fa|Lag)\S+", parts[0], re.I):
                continue

            # Port ID can be "gigabitEthernet X/X/X" (two tokens) when the
            # neighbor is another TP-Link switch — detect and normalise to short form.
            PORT_TYPE_MAP = {
                "gigabitethernet": "Gi",
                "fastethernet": "Fa",
                "tengigabitethernet": "Te",
            }
            idx = 2  # parts[idx] is Port ID
            if len(parts) > idx and parts[idx].lower() in PORT_TYPE_MAP and len(parts) > idx + 1:
                short = PORT_TYPE_MAP[parts[idx].lower()]
                port_id = f"{short}{parts[idx + 1]}"
                shift = 1
            else:
                port_id = parts[idx] if len(parts) > idx else ""
                shift = 0

            # Port Description (column 4+shift) may also be "gigabitEthernet X/X/X"
            pd_idx = 4 + shift
            desc_shift = 1 if (len(parts) > pd_idx and parts[pd_idx].lower() in PORT_TYPE_MAP) else 0

            rows.append(
                {
                    "local_port": parts[0],
                    "remote_chassis_id": parts[1] if len(parts) > 1 else "",
                    "port_id": port_id,
                    "mgmt_address": parts[3 + shift] if len(parts) > 3 + shift else "",
                    "port_description": parts[pd_idx] if len(parts) > pd_idx else "",
                    "system_name": parts[5 + shift + desc_shift] if len(parts) > 5 + shift + desc_shift else "",
                }
            )

        return rows

    def get_lldp_neighbors_detail(self, interface: str = "") -> Dict[str, List[Dict]]:
        """Return detailed LLDP neighbor info.

        TP-Link does not support a per-port detail command; all available
        fields are extracted from ``show lldp neighbor-information``.
        """
        details: Dict[str, List[Dict]] = {}

        for row in self._get_lldp_table():
            if interface and row["local_port"] != interface:
                continue
            details.setdefault(row["local_port"], []).append(
                {
                    "parent_interface": "",
                    "remote_port": row["port_id"],
                    "remote_port_description": row["port_description"],
                    "remote_chassis_id": row["remote_chassis_id"],
                    "remote_system_name": row["system_name"],
                    "remote_system_description": "",
                    "remote_system_capab": [],
                    "remote_system_enable_capab": [],
                }
            )

        return details

    @staticmethod
    def _parse_lldp_detail(output: str) -> Dict:
        """Parse a single ``show lldp neighbor-information interface`` block."""
        defaults = {
            "parent_interface": "",
            "remote_port": "",
            "remote_port_description": "",
            "remote_chassis_id": "",
            "remote_system_name": "",
            "remote_system_description": "",
            "remote_system_capab": [],
            "remote_system_enable_capab": [],
        }

        for line in output.splitlines():
            line_s = line.strip()
            if re.match(r"Chassis ID\s*:", line_s, re.I):
                defaults["remote_chassis_id"] = line_s.split(":", 1)[1].strip()
            elif re.match(r"Port ID\s*:", line_s, re.I):
                defaults["remote_port"] = line_s.split(":", 1)[1].strip()
            elif re.match(r"Port Description\s*:", line_s, re.I):
                defaults["remote_port_description"] = line_s.split(":", 1)[1].strip()
            elif re.match(r"System Name\s*:", line_s, re.I):
                defaults["remote_system_name"] = line_s.split(":", 1)[1].strip()
            elif re.match(r"System Description\s*:", line_s, re.I):
                defaults["remote_system_description"] = line_s.split(":", 1)[1].strip()
            elif re.match(r"System Capabilities\s*:", line_s, re.I):
                caps_str = line_s.split(":", 1)[1].strip()
                defaults["remote_system_capab"] = [
                    c.strip().lower() for c in caps_str.split(",") if c.strip()
                ]
            elif re.match(r"Enabled Capabilities\s*:", line_s, re.I):
                caps_str = line_s.split(":", 1)[1].strip()
                defaults["remote_system_enable_capab"] = [
                    c.strip().lower() for c in caps_str.split(",") if c.strip()
                ]

        return defaults

    def get_vlans(self) -> Dict[str, Dict]:
        """Return VLAN information.

        Example ``show vlan`` output::

            UT: Untagged;     TG: Tagged
            VLAN  Name                 Status    Ports
            ----- -------------------- --------- ----------------------------------------
            1     System-VLAN          active    TG: Gi1/0/9, Gi1/0/10
            8     MGMT                 active    UT: Gi1/0/2
                                                 TG: Gi1/0/1, Gi1/0/9, Gi1/0/10
        """
        output = self._send_command("show vlan")
        vlans: Dict[str, Dict] = {}
        current_id: Optional[str] = None
        in_table = False

        for line in output.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            if re.match(r"^-{4,}", line_s):
                in_table = True
                continue
            if not in_table:
                continue

            # New VLAN line starts with an integer
            m = re.match(r"^(\d+)\s+(\S+)\s+\S+\s+(.*)", line_s)
            if m:
                current_id = str(int(m.group(1)))
                vlan_name = m.group(2)
                ports_raw = m.group(3).strip()
                vlans[current_id] = {
                    "name": vlan_name,
                    "interfaces": self._parse_vlan_ports(ports_raw),
                }
            elif current_id is not None:
                # Continuation line: more ports for the current VLAN
                vlans[current_id]["interfaces"].extend(self._parse_vlan_ports(line_s))

        return vlans

    @staticmethod
    def _parse_vlan_ports(ports_raw: str) -> List[str]:
        """Parse TP-Link VLAN port string, stripping TG:/UT: prefixes and expanding ranges.

        Input examples::
            "TG: Gi1/0/9, Gi1/0/10"
            "UT: Gi1/0/1-4, Gi1/0/7"
        """
        interfaces: List[str] = []
        # Split on "TG:" or "UT:" markers to get individual segments
        for segment in re.split(r"\b(?:TG|UT)\s*:", ports_raw, flags=re.I):
            segment = segment.strip()
            if not segment:
                continue
            for port_token in segment.split(","):
                port_token = port_token.strip()
                if not port_token:
                    continue
                # Expand range notation: e.g. "Gi1/0/1-4"
                range_match = re.match(r"^([A-Za-z]+)(\d+/\d+/)(\d+)-(\d+)$", port_token)
                if range_match:
                    prefix = range_match.group(1)
                    slot = range_match.group(2)
                    start = int(range_match.group(3))
                    end = int(range_match.group(4))
                    interfaces.extend(f"{prefix}{slot}{i}" for i in range(start, end + 1))
                elif re.match(r"^(?:Gi|Te|Fa|Lag|Vlan)\S+", port_token, re.I):
                    interfaces.append(port_token)
        return interfaces

    @staticmethod
    def _expand_ports(ports_str: str) -> List[str]:
        """Expand a comma-separated port string (without TG:/UT: markers)."""
        result: List[str] = []
        for token in ports_str.split(","):
            token = token.strip()
            if not token:
                continue
            range_match = re.match(r"^([A-Za-z]+)(\d+/\d+/)(\d+)-(\d+)$", token)
            if range_match:
                prefix = range_match.group(1)
                slot = range_match.group(2)
                start = int(range_match.group(3))
                end = int(range_match.group(4))
                result.extend(f"{prefix}{slot}{i}" for i in range(start, end + 1))
            elif re.match(r"^(?:Gi|Te|Fa|Lag|Vlan)\S+", token, re.I):
                result.append(token)
        return result

    @staticmethod
    def _parse_vlan_ports_detail(ports_raw: str, default_mode: str = "UT") -> tuple:
        """Parse a VLAN port segment, returning (tagged_ports, untagged_ports).

        Recognises ``TG:`` and ``UT:`` prefixes within *ports_raw* and assigns
        each port to the correct list.  Ports listed without a prefix are
        placed according to *default_mode* (``"TG"`` or ``"UT"``), which
        allows callers to pass the last seen marker so that wrap-around
        continuation lines are classified correctly.
        """
        tagged: List[str] = []
        untagged: List[str] = []

        # re.split with a capturing group keeps the delimiters in the result list
        parts = re.split(r"\b(TG|UT)\s*:", ports_raw, flags=re.I)
        # parts[0] = text before first marker (usually empty or stray text)
        # then: parts[1]=marker, parts[2]=port-list, parts[3]=marker, parts[4]=port-list, …
        pre = parts[0].strip()
        if pre:
            ports = TPLinkJetstreamDriver._expand_ports(pre)
            if default_mode.upper() == "TG":
                tagged.extend(ports)
            else:
                untagged.extend(ports)

        i = 1
        while i < len(parts) - 1:
            marker = parts[i].upper()
            port_list = parts[i + 1]
            ports = TPLinkJetstreamDriver._expand_ports(port_list)
            if marker == "TG":
                tagged.extend(ports)
            else:
                untagged.extend(ports)
            i += 2

        return tagged, untagged

    def get_vlans_detail(self) -> Dict[str, Dict]:
        """Return VLAN information with tagged/untagged port separation.

        Returns::

            {
                "1": {"name": "System-VLAN", "tagged": ["Gi1/0/9"], "untagged": []},
                "8": {"name": "MGMT", "tagged": ["Gi1/0/1"], "untagged": ["Gi1/0/2"]},
            }
        """
        output = self._send_command("show vlan")
        vlans: Dict[str, Dict] = {}
        current_id: Optional[str] = None
        in_table = False
        last_marker = "UT"  # tracks last TG/UT seen; used for prefix-less continuation ports

        for line in output.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            if re.match(r"^-{4,}", line_s):
                in_table = True
                continue
            if not in_table:
                continue

            m = re.match(r"^(\d+)\s+(\S+)\s+\S+\s+(.*)", line_s)
            if m:
                current_id = str(int(m.group(1)))
                vlan_name = m.group(2)
                ports_raw = m.group(3).strip()
                last_marker = "UT"  # reset per VLAN
                tagged, untagged = self._parse_vlan_ports_detail(ports_raw, last_marker)
                # Update last_marker to whatever appeared last on this line
                seen = re.findall(r"\b(TG|UT)\s*:", ports_raw, re.I)
                if seen:
                    last_marker = seen[-1].upper()
                vlans[current_id] = {
                    "name": vlan_name,
                    "tagged": tagged,
                    "untagged": untagged,
                }
            elif current_id is not None:
                # Continuation line: inherit last_marker as default for prefix-less ports
                tagged, untagged = self._parse_vlan_ports_detail(line_s, last_marker)
                seen = re.findall(r"\b(TG|UT)\s*:", line_s, re.I)
                if seen:
                    last_marker = seen[-1].upper()
                vlans[current_id]["tagged"].extend(tagged)
                vlans[current_id]["untagged"].extend(untagged)

        return vlans

    # ------------------------------------------------------------------
    # NAPALM configuration management
    # ------------------------------------------------------------------

    def load_merge_candidate(
        self, filename: Optional[str] = None, config: Optional[str] = None
    ) -> None:
        """Stage a set of CLI configuration commands to be merged into the running config.

        *config* is a plain-text string of CLI commands exactly as you would
        type them in the switch's ``configure`` mode – one command per line,
        including sub-mode entry (e.g. ``interface gigabitEthernet 1/0/1``)
        and exit (``exit``).  Blank lines and lines starting with ``!`` or
        ``#`` are ignored.

        The configuration is **not** applied until :meth:`commit_config` is
        called.

        :raises MergeConfigException: on invalid input.
        """
        if filename is not None:
            try:
                with open(filename) as fh:
                    config = fh.read()
            except OSError as exc:
                raise MergeConfigException(str(exc)) from exc
        if config is None:
            raise MergeConfigException("Either 'filename' or 'config' must be provided.")
        self._candidate_config = config
        self._candidate_mode = "merge"

    def load_replace_candidate(
        self, filename: Optional[str] = None, config: Optional[str] = None
    ) -> None:
        """Stage a full running-config replacement candidate.

        The candidate is a complete ``show running-config`` style text.
        :meth:`compare_config` will show a unified diff against the current
        running config.  :meth:`commit_config` applies every non-comment line
        from the candidate in config mode (additive / merge semantics –
        TP-Link does not support atomic config-replace).  Configuration that
        exists on the device but is absent from the candidate will **not** be
        removed automatically.

        :raises ReplaceConfigException: on invalid input.
        """
        if filename is not None:
            try:
                with open(filename) as fh:
                    config = fh.read()
            except OSError as exc:
                raise ReplaceConfigException(str(exc)) from exc
        if config is None:
            raise ReplaceConfigException("Either 'filename' or 'config' must be provided.")
        self._candidate_config = config
        self._candidate_mode = "replace"

    def compare_config(self) -> str:
        """Return a human-readable diff of the pending candidate vs running config.

        For a **merge** candidate: returns the staged commands prefixed with
        ``+`` (they will all be added).

        For a **replace** candidate: returns a unified diff between the current
        ``show running-config`` output and the candidate text.

        Returns an empty string when no candidate is staged.
        """
        if self._candidate_config is None:
            return ""

        if self._candidate_mode == "merge":
            lines = []
            for line in self._candidate_config.splitlines():
                if line.strip() and not line.strip().startswith(("!", "#")):
                    lines.append(f"+{line}")
            return "\n".join(lines)

        # replace mode – unified diff
        import difflib
        running = self._send_command("show running-config")
        diff = difflib.unified_diff(
            running.splitlines(),
            self._candidate_config.splitlines(),
            fromfile="running-config",
            tofile="candidate-config",
            lineterm="",
        )
        return "\n".join(diff)

    def commit_config(self, message: str = "", revert_in: Optional[int] = None) -> None:
        """Apply the staged candidate configuration to the device and save it.

        1. Saves the current running config as rollback backup.
        2. Enters ``configure`` mode and sends the candidate lines.
        3. Returns to exec mode with ``end``.
        4. Persists the new config with ``copy running-config startup-config``.

        :raises MergeConfigException: if no candidate is staged or if lines
            were rejected by the device.
        :raises ReplaceConfigException: same, for replace candidates.
        """
        if self._candidate_config is None:
            raise MergeConfigException("No candidate configuration is staged.")

        ex_cls = ReplaceConfigException if self._candidate_mode == "replace" else MergeConfigException

        # Save backup for potential rollback
        self._backup_config = self._send_command("show running-config")

        errors: List[str] = []
        try:
            self._enter_config_mode()
            errors = self._apply_config_lines(self._candidate_config)
        finally:
            self._exit_config_mode()

        if errors:
            raise ex_cls("The following commands were rejected:\n" + "\n".join(errors))

        self._save_config()
        self._candidate_config = None
        self._candidate_mode = None

    def discard_config(self) -> None:
        """Discard the staged candidate configuration without applying it."""
        self._candidate_config = None
        self._candidate_mode = None

    def rollback(self) -> None:
        """Restore the running config to the state before the last :meth:`commit_config`.

        Computes a block-level diff between the current running config and the
        saved backup, then generates the minimal set of commands (including
        ``no <cmd>`` negations) needed to restore the previous state.

        :raises CommandErrorException: if no backup is available.
        """
        if self._backup_config is None:
            raise CommandErrorException(
                "No backup configuration available – commit_config has not been called in this session."
            )

        current = self._send_command("show running-config")
        rollback_cmds = self._diff_to_commands(self._backup_config, current)

        if rollback_cmds:
            try:
                self._enter_config_mode()
                self._apply_config_lines("\n".join(rollback_cmds))
            finally:
                self._exit_config_mode()
            self._save_config()

        self._backup_config = None

    @staticmethod
    def _parse_config_blocks(config_text: str) -> Dict[str, List[str]]:
        """Parse a running-config into a dict of context → [command lines].

        The special key ``"__global__"`` holds top-level commands.
        Sub-mode blocks (``interface …``, ``vlan …``) are stored under the
        first line that opens them (e.g. ``"interface gigabitEthernet 1/0/1"``).

        TP-Link uses bare ``#`` lines as block separators; lines starting with
        ``!`` are file-header comments.
        """
        blocks: Dict[str, List[str]] = {"__global__": []}
        ctx = "__global__"

        for line in config_text.splitlines():
            stripped = line.strip()

            # Block separator or empty → return to global context
            if not stripped or stripped == "#":
                ctx = "__global__"
                continue

            # Comment / device-model header line → skip
            if stripped.startswith("!"):
                continue

            # New sub-mode context
            if re.match(r"^(interface|vlan)\s+\S+", stripped, re.I):
                ctx = stripped
                if ctx not in blocks:
                    blocks[ctx] = []
                continue

            # exit / end → return to global context (shouldn't appear in
            # show running-config but guard anyway)
            if stripped.lower() in ("exit", "end"):
                ctx = "__global__"
                continue

            blocks.setdefault(ctx, []).append(stripped)

        return blocks

    @staticmethod
    def _negate_command(cmd: str) -> Optional[str]:
        """Return the ``no`` form of *cmd*, or *None* if not known.

        Only commands whose entire effect is removed by ``no <keyword>``
        (without repeating the value) are handled here.
        """
        # Single-keyword negation: 'no description', 'no spanning-tree', …
        for kw in (
            "description",
            "name",
            "spanning-tree",
            "lldp",
            "ip address",
            "ipv6 enable",
            "contact-info",
            "location",
            "command log",
            "telnet",
        ):
            if re.match(rf"^{re.escape(kw)}\b", cmd, re.I):
                # Use only the first word of multi-word keywords for the 'no' prefix
                return f"no {kw}"

        # 'switchport pvid N' → 'switchport pvid 1' (default PVID)
        m = re.match(r"^(switchport pvid)\s+\d+", cmd, re.I)
        if m:
            return "switchport pvid 1"

        return None

    def _diff_to_commands(self, backup: str, current: str) -> List[str]:
        """Generate the commands needed to revert *current* to *backup* state."""
        backup_blocks = self._parse_config_blocks(backup)
        current_blocks = self._parse_config_blocks(current)

        cmds: List[str] = []
        all_ctxs = set(backup_blocks.keys()) | set(current_blocks.keys())

        for ctx in sorted(all_ctxs):
            backup_lines = set(backup_blocks.get(ctx, []))
            current_lines = set(current_blocks.get(ctx, []))

            if backup_lines == current_lines:
                continue

            in_block = ctx != "__global__"
            if in_block:
                cmds.append(ctx)

            # Lines added since backup → negate them
            for line in current_lines - backup_lines:
                neg = self._negate_command(line)
                if neg:
                    cmds.append(f"  {neg}" if in_block else neg)

            # Lines removed since backup → restore them
            for line in backup_lines - current_lines:
                cmds.append(f"  {line}" if in_block else line)

            if in_block:
                cmds.append("exit")

        return cmds

    # ------------------------------------------------------------------
    # Additional NAPALM getters
    # ------------------------------------------------------------------

    def has_pending_commit(self) -> bool:
        """Return True when a candidate configuration is staged but not yet committed."""
        return self._candidate_config is not None

    def get_environment(self) -> Dict:
        """Return device environment data (CPU, memory).

        TP-Link Jetstream switches do not expose fan, temperature, or power
        rail data through the CLI, so those fields are returned with
        ``status: True`` (assumed healthy) and ``-1.0`` for numeric values.

        ``show cpu-utilization`` columns: Five-Seconds, One-Minute, Five-Minutes
        ``show memory`` column: Current Memory Utilization (percent)
        """
        cpu_out = self._send_command("show cpu-utilization")
        mem_out = self._send_command("show memory")

        # Parse CPU: first data row after the header/separator
        cpu_pct = 0.0
        for line in cpu_out.splitlines():
            m = re.search(r"^\s*1\s*\|\s*(\d+)%", line)
            if m:
                cpu_pct = float(m.group(1))
                break

        # Parse memory percentage
        mem_pct = 0.0
        for line in mem_out.splitlines():
            m = re.search(r"^\s*1\s*\|\s*(\d+)%", line)
            if m:
                mem_pct = float(m.group(1))
                break

        return {
            "fans": {},
            "temperature": {},
            "power": {},
            "cpu": {0: {"%usage": cpu_pct}},
            "memory": {
                "available_ram": int((1 - mem_pct / 100) * 100),  # relative %
                "used_ram": int(mem_pct),
            },
        }

    def get_interfaces_counters(self) -> Dict[str, Dict]:
        """Return per-interface packet and byte counters.

        Parses the block-format output of ``show interface counters``.
        Each block starts with ``Port:  <name>`` and contains key/value
        pairs separated by a tab character.
        """
        output = self._send_command("show interface counters")

        def _int(s: str) -> int:
            return int(s.replace(",", "")) if s.strip() else 0

        counters: Dict[str, Dict] = {}
        current: Optional[Dict] = None
        current_port: Optional[str] = None

        for line in output.splitlines():
            # New port block
            m = re.match(r"^Port:\s+(\S+)", line)
            if m:
                if current_port and current:
                    counters[current_port] = current
                current_port = m.group(1)
                current = {
                    "tx_errors": 0, "rx_errors": 0,
                    "tx_discards": 0, "rx_discards": 0,
                    "tx_octets": 0, "rx_octets": 0,
                    "tx_unicast_packets": 0, "rx_unicast_packets": 0,
                    "tx_multicast_packets": 0, "rx_multicast_packets": 0,
                    "tx_broadcast_packets": 0, "rx_broadcast_packets": 0,
                }
                continue

            if current is None:
                continue

            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()

            mapping = {
                "Tx Errors":    "tx_errors",
                "Rx Errors":    "rx_errors",
                "Tx Discards":  "tx_discards",
                "Rx Discards":  "rx_discards",
                "Tx Bytes":     "tx_octets",
                "Rx Bytes":     "rx_octets",
                "Tx Ucast":     "tx_unicast_packets",
                "Rx Ucast":     "rx_unicast_packets",
                "Tx Mcast":     "tx_multicast_packets",
                "Rx Mcast":     "rx_multicast_packets",
                "Tx Bcast":     "tx_broadcast_packets",
                "Rx Bcast":     "rx_broadcast_packets",
            }
            if key in mapping:
                current[mapping[key]] = _int(val)

        if current_port and current:
            counters[current_port] = current

        return counters

    def get_users(self) -> Dict[str, Dict]:
        """Return local user accounts.

        Parses ``show user account`` output::

            Index     User-Name    User-Type
            1         admin        Admin
        """
        output = self._send_command("show user account")
        users: Dict[str, Dict] = {}
        in_table = False

        for line in output.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            if re.match(r"^-{4,}", line_s):
                in_table = True
                continue
            if not in_table:
                continue

            parts = line_s.split()
            if len(parts) < 3:
                continue
            try:
                int(parts[0])   # first column is index
            except ValueError:
                continue

            username = parts[1]
            role = parts[2].lower()
            # Map TP-Link roles to NAPALM privilege levels (1-15)
            level = 15 if role == "admin" else 1

            users[username] = {"level": level, "password": "", "sshkeys": []}

        return users

    def get_snmp_information(self) -> Dict:
        """Return SNMP configuration.

        Reads contact/location from ``show system-info`` and communities
        from ``show snmp community``.  If SNMP is disabled an empty community
        dict is returned.
        """
        sys_info = self._send_command("show system-info")
        contact = self._parse_key_value(sys_info, "Contact Information")
        location = self._parse_key_value(sys_info, "System Location")
        mac = self._parse_key_value(sys_info, "Mac Address").replace("-", ":").upper()

        snmp_out = self._send_command("show snmp community")
        communities: Dict[str, Dict] = {}

        if "disabled" not in snmp_out.lower():
            in_table = False
            for line in snmp_out.splitlines():
                line_s = line.strip()
                if re.match(r"^-{4,}", line_s):
                    in_table = True
                    continue
                if not in_table or not line_s:
                    continue
                parts = line_s.split()
                if len(parts) >= 2:
                    comm_name = parts[0]
                    mode = "rw" if "write" in parts[1].lower() else "ro"
                    communities[comm_name] = {"acl": "", "mode": mode}

        return {
            "contact": contact,
            "location": location,
            "community": communities,
            "chassis_id": mac,
        }

    def get_ntp_servers(self) -> Dict[str, Dict]:
        """Return configured NTP servers.

        Extracts the server list from the running configuration line::

            system-time ntp <timezone> <server1> [<server2> ...] <interval>
        """
        running = self._send_command("show running-config")
        servers: Dict[str, Dict] = {}
        for line in running.splitlines():
            m = re.match(r"^system-time\s+ntp\s+\S+\s+(.*)", line.strip(), re.I)
            if m:
                tokens = m.group(1).split()
                # Last token is the update interval (numeric), rest are servers
                server_tokens = [t for t in tokens if not t.isdigit()]
                for srv in server_tokens:
                    servers[srv] = {}
        return servers

    def get_ntp_peers(self) -> Dict[str, Dict]:
        """Return NTP peers (same as servers on TP-Link Jetstream)."""
        return self.get_ntp_servers()

    def get_ntp_stats(self) -> List[Dict]:
        """Return NTP statistics.

        TP-Link CLI does not expose per-peer NTP sync statistics; returns
        an empty list.
        """
        return []

    def get_optics(self) -> Dict:
        """Return optical transceiver data.

        TP-Link Jetstream CLI does not provide optical transceiver diagnostics
        (DDM/DOM) through any accessible command.  Returns an empty dict; the
        SFP ports are detected as physical interfaces by :meth:`get_interfaces`.
        """
        return {}

    def get_ipv6_neighbors_table(self) -> List[Dict]:
        """Return the IPv6 neighbor table.

        TP-Link Jetstream CLI does not expose the IPv6 neighbor (ND) table.
        Returns an empty list.  IPv6 interface addresses are still available
        via :meth:`get_interfaces_ip` once IPv6 is configured.
        """
        return []

    def get_route_to(
        self,
        destination: str = "",
        protocol: str = "",
        longer: bool = False,
    ) -> Dict[str, List[Dict]]:
        """Return routing table entries.

        Parses ``show ip route`` output::

            Codes: C - connected, S - static
                   * - candidate default
            S*      0.0.0.0/0 [1/0] via 172.22.8.1, VLAN8
                    172.22.0.0/24 is subnetted, 1 subnets
            C           172.22.8.0/24 is directly connected, VLAN8
        """
        output = self._send_command("show ip route")
        routes: Dict[str, List[Dict]] = {}

        proto_map = {"c": "connected", "s": "static", "r": "rip", "o": "ospf"}

        for line in output.splitlines():
            line_s = line.strip()
            if not line_s or line_s.startswith("Codes") or line_s.startswith("*"):
                continue

            # Match: "S*  0.0.0.0/0 [1/0] via 172.22.8.1, VLAN8"
            # or    "C   172.22.8.0/24 is directly connected, VLAN8"
            m = re.match(
                r"^([A-Za-z])\*?\s+([\d./]+)"
                r"(?:\s+\[(\d+)/(\d+)\])?"
                r"(?:\s+via\s+([\d.]+))?"
                r"(?:.*?,\s*(\S+))?",
                line_s,
            )
            if not m:
                # Continuation line with subnet: "    172.22.0.0/24 is subnetted"
                continue

            code = m.group(1).lower()
            prefix = m.group(2)
            preference = int(m.group(3)) if m.group(3) else 0
            next_hop = m.group(5) or ""
            iface = m.group(6) or ""
            prot = proto_map.get(code, code)
            connected = code == "c"

            if destination and prefix != destination:
                continue
            if protocol and prot != protocol.lower():
                continue

            entry = {
                "protocol": prot,
                "current_active": True,
                "last_active": False,
                "age": -1,
                "next_hop": next_hop if not connected else "",
                "outgoing_interface": iface,
                "selected_next_hop": True,
                "preference": preference,
                "inactive_reason": "",
                "routing_table": "global",
                "protocol_attributes": {},
            }
            routes.setdefault(prefix, []).append(entry)

        return routes

    def ping(
        self,
        destination: str,
        source: str = "",
        ttl: int = 255,
        timeout: int = 2,
        size: int = 100,
        count: int = 5,
        vrf: str = "",
        source_interface: str = "",
    ) -> Dict:
        """Ping *destination* from the device.

        Uses ``ping <dst> -n <count>``.  TP-Link does not support source IP,
        TTL, or packet size selection from CLI.

        :returns: NAPALM-standard ping result dict.
        """
        cmd = f"ping {destination} -n {count}"
        output = self._send_command(cmd)

        # "Error" → destination unreachable / bad input
        if "Error" in output or "Invalid" in output:
            return {"error": output.strip()}

        # Parse stats line: "Packets: Sent = 4 , Received = 4 , Lost = 0 (0% loss)"
        sent = received = 0
        m = re.search(r"Sent\s*=\s*(\d+)\s*,\s*Received\s*=\s*(\d+)", output)
        if m:
            sent = int(m.group(1))
            received = int(m.group(2))

        # Parse RTT: "Minimum = 0ms , Maximum = 10ms , Average = 2ms"
        rtt_min = rtt_max = rtt_avg = 0.0
        m = re.search(r"Minimum\s*=\s*(\d+)ms.*?Maximum\s*=\s*(\d+)ms.*?Average\s*=\s*(\d+)ms", output)
        if m:
            rtt_min = float(m.group(1))
            rtt_max = float(m.group(2))
            rtt_avg = float(m.group(3))

        # Parse individual reply RTTs (time<16ms → 0ms threshold; treat as 1ms)
        results = []
        for reply_line in output.splitlines():
            m = re.match(r"Reply from ([\d.]+)\s*:.*?time[<=](\d+)ms", reply_line)
            if m:
                rtt_val = float(m.group(2))
                # "time<16ms" means it rounded down; at least 1ms
                results.append({"ip_address": m.group(1), "rtt": rtt_val if rtt_val > 0 else 1.0})
            elif re.search(r"Request timed out", reply_line, re.I):
                results.append({"ip_address": destination, "rtt": timeout * 1000.0})

        return {
            "success": {
                "probes_sent": sent,
                "packet_loss": sent - received,
                "rtt_min": rtt_min,
                "rtt_max": rtt_max,
                "rtt_avg": rtt_avg,
                "rtt_stddev": 0.0,
                "results": results,
            }
        }

    def traceroute(
        self,
        destination: str,
        source: str = "",
        ttl: int = 255,
        timeout: int = 2,
        vrf: str = "",
    ) -> Dict:
        """Traceroute to *destination* from the device.

        Uses ``tracert <dst>``.  TP-Link caps the hop count at 4 and does
        not support source IP selection from CLI.

        :returns: NAPALM-standard traceroute result dict.
        """
        output = self._send_command(f"tracert {destination}")

        if "Error" in output or "Invalid" in output or "Bad command" in output:
            return {"error": output.strip()}

        hops: Dict[str, Dict] = {}

        for line in output.splitlines():
            # "1         20 ms     1  ms     1  ms     172.22.8.1"
            # "3         *         *         *         Request timed out."
            m = re.match(r"^\s*(\d+)\s+(.*)", line)
            if not m:
                continue
            hop_num = m.group(1)
            rest = m.group(2).strip()

            if re.search(r"timed out|Request timed", rest, re.I):
                probes = {
                    str(i): {"rtt": timeout * 1000.0, "ip_address": "*", "host_name": ""}
                    for i in range(1, 4)
                }
            else:
                # Extract RTTs (may be "<Nms" or "Nms")
                rtts = re.findall(r"[<]?(\d+)\s*ms", rest)
                # Last token that looks like an IP
                ip_m = re.search(r"([\d.]+)\s*$", rest)
                ip_addr = ip_m.group(1) if ip_m else "*"

                probes = {}
                for i, rtt_str in enumerate(rtts, start=1):
                    probes[str(i)] = {
                        "rtt": float(rtt_str) if float(rtt_str) > 0 else 1.0,
                        "ip_address": ip_addr,
                        "host_name": "",
                    }

            if probes:
                hops[hop_num] = {"probes": probes}

        return {"success": hops}

    def cli(
        self,
        commands: List[str],
        encoding: str = "text",
    ) -> Dict[str, Union[str, Dict[str, Any]]]:
        """Execute a list of CLI commands and return their output."""
        if encoding != "text":
            raise NotImplementedError(
                f"Encoding '{encoding}' is not supported by this driver."
            )
        result: Dict[str, Union[str, Dict[str, Any]]] = {}
        for cmd in commands:
            result[cmd] = self._send_command(cmd)
        return result
