"""Read installed backend capabilities without starting any network listener.

Run as a mitmdump addon with --no-server and a temporary confdir.
This never starts Local Capture, installs Redirector, or enumerates processes.
"""
import importlib.metadata
import json

from mitmproxy import ctx
from mitmproxy import version
from mitmproxy.connection import Client
from mitmproxy.proxy.mode_specs import ProxyMode
import mitmproxy_rs


def running():
    versions = {}
    for name in ("mitmproxy", "mitmproxy-rs", "mitmproxy-macos"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "metadata unavailable in distribution"
    specs = {}
    for spec in ("local:12345", "local:curl", "local:curl,!12345"):
        mode = ProxyMode.parse(spec)
        specs[spec] = {"type": mode.type_name, "data": mode.data}
    print(json.dumps({"mitmproxy_version": version.VERSION,
                      "client_has_pid_field": "pid" in Client.__dataclass_fields__,
                      "client_has_process_name_field": "process_name" in Client.__dataclass_fields__,
                      "versions": versions, "parsed_modes": specs,
                      "unavailable_reason": mitmproxy_rs.local.LocalRedirector.unavailable_reason(),
                      "set_intercept_api": hasattr(mitmproxy_rs.local.LocalRedirector, "set_intercept"),
                      "local_capture_started": False}, sort_keys=True))
    ctx.master.shutdown()
