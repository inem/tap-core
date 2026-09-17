"""Declared TLS passthrough for clients that pin their own certificate authority.

Some clients never accept the profile CA: Apple's system daemons (cloudd/bird for
iCloud Drive, Contacts, Calendar, Find My, the App Store, Siri, CloudKit) abort
the TLS handshake the moment the proxy answers and retry forever, which both
breaks their service while TAP is on and fills capture.log with failed
handshakes. Nothing is captured from them either way, so the only useful policy
is to let them through untouched. This module owns that policy: one declared
list per profile, applied as macOS proxy bypass domains under system routing
and as mitmproxy ``ignore_hosts`` patterns under both routings.
"""
import re

DEFAULT_PASSTHROUGH = [
    "*.icloud.com",
    "*.icloud-content.com",
    "*.apple.com",
    "*.push.apple.com",
    "*.apple-cloudkit.com",
    "*.mzstatic.com",
    "*.cdn-apple.com",
    "17.0.0.0/8",
]
MAX_ENTRIES = 64
HOST_ENTRY = re.compile(r"^(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
CIDR_ENTRY = re.compile(r"^\d{1,3}(\.\d{1,3}){0,3}/\d{1,2}$")


def configuration(value):
    """Validate a profile's passthrough list; ``None`` selects the Core default."""
    if value is None:
        return list(DEFAULT_PASSTHROUGH)
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        raise ValueError(f"Passthrough must be a list of at most {MAX_ENTRIES} host patterns")
    result = []
    for entry in value:
        if not isinstance(entry, str) or not (HOST_ENTRY.match(entry) or CIDR_ENTRY.match(entry)):
            raise ValueError(f"Passthrough entry must be a host name, *.suffix or CIDR range: {entry!r}")
        if entry not in result:
            result.append(entry)
    return result


def ignore_host_patterns(entries):
    """mitmproxy ``ignore_hosts`` regexes (matched against ``host:port``) for the host entries.

    CIDR entries only exist as system proxy bypasses: mitmproxy sees the CONNECT
    host name, not the address the client would have resolved.
    """
    patterns = []
    for entry in entries:
        if "/" in entry:
            continue
        if entry.startswith("*."):
            patterns.append(r"^(.+\.)?" + re.escape(entry[2:]) + r":\d+$")
        else:
            patterns.append("^" + re.escape(entry) + r":\d+$")
    return patterns
