# napalm-tplink-jetstream

NAPALM community driver for **TP-Link Jetstream** managed switches (T1500G, T1600G, T2600G, T3700G and compatible series).

## Tested devices

| Model | Series | Tested |
|---|---|---|
| TL-SG2210P | Jetstream Smart | ✅ |

> More devices from the T1500G, T1600G, T2600G and T3700G series should work as well — contributions welcome.

## Requirements

| Dependency | Minimum version |
|---|---|
| Python | 3.8 |
| NAPALM | 4.0 |
| Netmiko | 4.0 |

## Installation

```bash
pip install napalm-tplink-jetstream
```

Or from source:

```bash
git clone https://github.com/napalm-automation-community/napalm-tplink-jetstream
cd napalm-tplink-jetstream
pip install -e .
```

## Quick start

```python
from napalm import get_network_driver

driver = get_network_driver("tplink_jetstream")
with driver("192.168.0.1", "admin", "admin") as device:
    facts = device.get_facts()
    print(facts)
```

## Implemented getters

| Getter | Status |
|---|---|
| `get_facts` | ✅ |
| `get_interfaces` | ✅ |
| `get_interfaces_ip` | ✅ |
| `get_config` | ✅ |
| `get_arp_table` | ✅ |
| `get_mac_address_table` | ✅ |
| `get_lldp_neighbors` | ✅ |
| `get_lldp_neighbors_detail` | ✅ |
| `get_vlans` | ✅ |
| `cli` | ✅ |
| `is_alive` | ✅ |
| `get_bgp_neighbors` | ❌ Not applicable |
| `load_replace_candidate` | ❌ Not applicable |
| `commit_config` | ❌ Not applicable |

## Optional arguments

| Argument | Default | Description |
|---|---|---|
| `port` | `22` | SSH port |
| `force_no_enable` | `False` | Skip `enable` after login |
| `canonical_int_fmt` | `False` | Use canonical interface names |

Any additional keyword arguments are forwarded to Netmiko.

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
