"""Transport adapters: serial, wifi, ble, mock. See DESIGN.md § Transports.

Each adapter exposes the same byte-stream-ish interface to the dispatch
layer; transport-specific concerns (raw-REPL upload for serial, BLE MTU
chunking/reassembly) are fully contained here and invisible above this layer.
"""
