"""msgpack framing + the v1 type contract. See DESIGN.md § Wire protocol.

Frame: [4-byte length][msg-type][msgpack body]. PC side uses the `msgpack`
package directly; the MCU-side counterpart is the vendored umsgpack.py in
tether_runtime, uploaded with every deploy.
"""
