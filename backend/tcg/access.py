"""Explicit host allowlist for a shared internal service."""
from urllib.parse import urlparse

LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1', 'testserver'}


def allowed_host(host_header, configured=''):
    try:
        hostname = urlparse('//' + host_header).hostname
    except ValueError:
        return False
    hosts = LOCAL_HOSTS | {host.strip().strip('[]').lower() for host in configured.split(',') if host.strip()}
    return bool(hostname and hostname.lower() in hosts)
