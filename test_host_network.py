import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host_network import add_upnp_port_mapping, create_host_certificate


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self.body[:size]


class FakeDiscoverySocket:
    def __init__(self, *args):
        self.responses = iter(
            [
                (
                    b"HTTP/1.1 200 OK\r\n"
                    b"LOCATION: http://192.168.1.1:5000/root.xml\r\n\r\n",
                    ("192.168.1.1", 1900),
                ),
                socket.timeout(),
            ]
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        pass

    def sendto(self, *args):
        pass

    def recvfrom(self, size):
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class HostNetworkTests(unittest.TestCase):
    def test_self_signed_certificate_is_persistent_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = create_host_certificate(root)
            second = create_host_certificate(root)

            self.assertTrue(first[0].is_file())
            self.assertTrue(first[1].is_file())
            self.assertEqual(first, second)
            self.assertEqual(len(first[2]), 64)

    def test_upnp_creates_tcp_mapping_and_can_remove_it(self):
        description = (
            b"<root><URLBase>http://192.168.1.1:5000/</URLBase><device><serviceList>"
            b"<service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1"
            b"</serviceType><controlURL>/upnp/control</controlURL></service>"
            b"</serviceList></device></root>"
        )
        empty_soap = (
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            b"<s:Body /></s:Envelope>"
        )
        external_ip_soap = (
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            b"<s:Body><NewExternalIPAddress>8.8.8.8</NewExternalIPAddress>"
            b"</s:Body></s:Envelope>"
        )
        responses = iter(
            [
                FakeResponse(description),
                FakeResponse(empty_soap),
                FakeResponse(external_ip_soap),
                FakeResponse(empty_soap),
            ]
        )
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return next(responses)

        with (
            patch("host_network.socket.socket", FakeDiscoverySocket),
            patch("host_network.urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            mapping = add_upnp_port_mapping(8765, "192.168.1.55")
            mapping.remove()

        self.assertEqual(mapping.external_address, "8.8.8.8")
        self.assertEqual(mapping.internal_address, "192.168.1.55")
        self.assertEqual(len(requests), 4)
        self.assertIn("AddPortMapping", requests[1].headers["Soapaction"])
        self.assertIn("NewInternalClient", requests[1].data.decode("utf-8"))
        self.assertIn("DeletePortMapping", requests[3].headers["Soapaction"])


if __name__ == "__main__":
    unittest.main()
