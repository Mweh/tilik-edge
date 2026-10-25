"""Optional DNS-SD announcement via Avahi; tokens never enter public TXT records."""
import ipaddress
import logging
import shutil
import subprocess


def advertise(settings, port):
    host = settings['host']
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if loopback:
        return None
    program = shutil.which('avahi-publish-service')
    if not program:
        logging.getLogger(__name__).warning('Bonjour unavailable: install avahi-daemon avahi-utils. Hostname fallback still works.')
        return None
    scheme = 'https' if settings.get('tls_cert') else 'http'
    try:
        return subprocess.Popen([program, '--no-fail', 'Tilik Thermal ' + settings['device_id'][:8],
            '_tilik-edge._tcp', str(port), 'api_version=2', 'scheme=' + scheme,
            'device_id=' + settings['device_id']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        logging.getLogger(__name__).warning('Bonjour announcement unavailable: %s', error)
        return None
