import hashlib
import ipaddress
import secrets
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


SSDP_ADDRESS = ("239.255.255.250", 1900)
SSDP_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "\r\n"
).encode("ascii")
SSDP_TIMEOUT_SECONDS = 3
UPNP_SERVICE_PREFIXES = (
    "urn:schemas-upnp-org:service:WANIPConnection:",
    "urn:schemas-upnp-org:service:WANPPPConnection:",
)


def certificate_fingerprint(certificate_der: bytes) -> str:
    return hashlib.sha256(certificate_der).hexdigest().upper()


def create_host_certificate(storage_root: Path) -> tuple[Path, Path, str]:
    certificate_path = storage_root / "host-certificate.pem"
    private_key_path = storage_root / "host-private-key.pem"
    if certificate_path.is_file() and private_key_path.is_file():
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        der_certificate = certificate.public_bytes(serialization.Encoding.DER)
        return (
            certificate_path,
            private_key_path,
            certificate_fingerprint(der_certificate),
        )

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "SaveSync local host")]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_path.write_bytes(private_key_pem)
    certificate_path.write_bytes(certificate_pem)
    try:
        private_key_path.chmod(0o600)
    except OSError:
        pass
    return (
        certificate_path,
        private_key_path,
        certificate_fingerprint(certificate.public_bytes(serialization.Encoding.DER)),
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        if _local_name(element.tag) == name and element.text:
            return element.text.strip()
    return None


def _device_service(location: str, expected_source: str) -> tuple[str, str]:
    parsed_location = urllib.parse.urlsplit(location)
    if (
        parsed_location.scheme != "http"
        or parsed_location.hostname != expected_source
        or parsed_location.username is not None
        or parsed_location.password is not None
    ):
        raise RuntimeError("Маршрутизатор вернул недопустимый UPnP-адрес")
    request = urllib.request.Request(location)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            description = ET.fromstring(response.read(1024 * 1024))
    except (OSError, ET.ParseError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Не удалось прочитать описание UPnP-маршрутизатора: {exc}") from exc

    base_url = _find_text(description, "URLBase") or location
    for service in description.iter():
        if _local_name(service.tag) != "service":
            continue
        service_type = _find_text(service, "serviceType")
        control_url = _find_text(service, "controlURL")
        if (
            service_type
            and control_url
            and service_type.startswith(UPNP_SERVICE_PREFIXES)
        ):
            control = urllib.parse.urljoin(base_url, control_url)
            parsed_control = urllib.parse.urlsplit(control)
            if (
                parsed_control.scheme != "http"
                or parsed_control.hostname != expected_source
                or parsed_control.username is not None
                or parsed_control.password is not None
            ):
                raise RuntimeError("Маршрутизатор вернул недопустимый UPnP control URL")
            return service_type, control
    raise RuntimeError("UPnP-маршрутизатор не объявил WANIPConnection/WANPPPConnection")


def _soap_call(
    control_url: str,
    service_type: str,
    action: str,
    arguments: dict[str, str],
) -> ET.Element:
    envelope = ET.Element(
        "{http://schemas.xmlsoap.org/soap/envelope/}Envelope",
        {"{http://schemas.xmlsoap.org/soap/envelope/}encodingStyle":
         "http://schemas.xmlsoap.org/soap/encoding/"},
    )
    body = ET.SubElement(envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Body")
    operation = ET.SubElement(body, f"{{{service_type}}}{action}")
    for name, value in arguments.items():
        ET.SubElement(operation, name).text = value
    payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    request = urllib.request.Request(
        control_url,
        data=payload,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            result = ET.fromstring(response.read(1024 * 1024))
            fault = _find_text(result, "faultstring")
            description = _find_text(result, "errorDescription")
            if fault or description:
                raise RuntimeError(
                    f"UPnP {action}: {description or fault}"
                )
            return result
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        try:
            description = _find_text(ET.fromstring(detail), "errorDescription")
        except ET.ParseError:
            description = None
        raise RuntimeError(
            f"UPnP {action}: {description or detail or f'HTTP {exc.code}'}"
        ) from exc
    except (OSError, ET.ParseError, urllib.error.URLError) as exc:
        raise RuntimeError(f"UPnP {action} завершился ошибкой: {exc}") from exc


@dataclass
class PortMapping:
    external_address: str
    external_port: int
    internal_address: str
    service_type: str
    control_url: str

    def remove(self) -> None:
        _soap_call(
            self.control_url,
            self.service_type,
            "DeletePortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": str(self.external_port),
                "NewProtocol": "TCP",
            },
        )


def add_upnp_port_mapping(port: int, internal_address: str) -> PortMapping:
    if not 1 <= port <= 65535:
        raise ValueError("Порт должен быть от 1 до 65535")
    local_ip = ipaddress.IPv4Address(internal_address)
    locations: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as discovery:
        discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        discovery.settimeout(SSDP_TIMEOUT_SECONDS)
        discovery.sendto(SSDP_SEARCH, SSDP_ADDRESS)
        deadline = time.monotonic() + SSDP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                response, peer = discovery.recvfrom(8192)
            except socket.timeout:
                break
            for line in response.decode("iso-8859-1").splitlines():
                if line.lower().startswith("location:"):
                    locations[line.split(":", 1)[1].strip()] = peer[0]
                    break

    if not locations:
        raise RuntimeError(
            "UPnP-маршрутизатор не найден. Включите UPnP на роутере; "
            "проверьте, что у провайдера есть публичный IPv4 (не CGNAT)."
        )

    errors = []
    for location, source_ip in locations.items():
        service_type = ""
        control_url = ""
        mapping_added = False
        try:
            service_type, control_url = _device_service(location, source_ip)
            _soap_call(
                control_url,
                service_type,
                "AddPortMapping",
                {
                    "NewRemoteHost": "",
                    "NewExternalPort": str(port),
                    "NewProtocol": "TCP",
                    "NewInternalPort": str(port),
                    "NewInternalClient": str(local_ip),
                    "NewEnabled": "1",
                    "NewPortMappingDescription": "SaveSync HTTPS",
                    "NewLeaseDuration": "0",
                },
            )
            mapping_added = True
            result = _soap_call(
                control_url,
                service_type,
                "GetExternalIPAddress",
                {},
            )
            external_ip = _find_text(result, "NewExternalIPAddress")
            if external_ip is None:
                raise RuntimeError("Маршрутизатор не вернул внешний IPv4-адрес")
            external_address = ipaddress.IPv4Address(external_ip)
            if not external_address.is_global:
                raise RuntimeError(
                    "Роутер не имеет публичного IPv4-адреса; вероятно, подключение "
                    "находится за CGNAT"
                )
            return PortMapping(
                str(external_address),
                port,
                str(local_ip),
                service_type,
                control_url,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            if mapping_added:
                try:
                    _soap_call(
                        control_url,
                        service_type,
                        "DeletePortMapping",
                        {
                            "NewRemoteHost": "",
                            "NewExternalPort": str(port),
                            "NewProtocol": "TCP",
                        },
                    )
                except RuntimeError as cleanup_error:
                    errors.append(
                        f"{exc}; не удалось удалить неиспользуемый проброс: "
                        f"{cleanup_error}"
                    )
                    continue
            errors.append(str(exc))
    raise RuntimeError("Не удалось настроить UPnP: " + "; ".join(errors))


def make_pinned_ssl_context(fingerprint: str) -> ssl.SSLContext:
    normalized = fingerprint.replace(":", "").replace(" ", "").upper()
    if len(normalized) != 64 or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise ValueError("Отпечаток сертификата должен быть SHA-256 (64 символа)")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def verify_peer_fingerprint(connection: ssl.SSLSocket, fingerprint: str) -> None:
    normalized = fingerprint.replace(":", "").replace(" ", "").upper()
    actual = certificate_fingerprint(connection.getpeercert(binary_form=True))
    if not secrets.compare_digest(actual, normalized):
        raise ssl.SSLError(
            "Отпечаток HTTPS-сертификата не совпадает. Проверьте адрес хоста "
            "и отпечаток, полученный от владельца."
        )
