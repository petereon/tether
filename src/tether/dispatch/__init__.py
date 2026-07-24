"""PC-side dispatch: background reader thread + queue, reentrant call routing.

See DESIGN.md § Call semantics. Owns:
  - the reader thread draining the transport into a queue
  - filtering "blocking" calls by request-ID while pumping other in-flight
    requests to a thread pool (reverse-call support)
  - timeout + heartbeat bookkeeping per in-flight request
"""
