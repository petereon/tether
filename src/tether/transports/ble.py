"""BLE transport via `bleak` (install via `tether[ble]`).

Custom GATT service: one write characteristic (PC->MCU), one notify
characteristic (MCU->PC). Frame chunking/reassembly across the small BLE MTU
is handled entirely in this module — invisible above the dispatch layer.
See DESIGN.md § Transports.
"""

from __future__ import annotations
