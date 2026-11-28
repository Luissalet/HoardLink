import errno
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hoard_link.hub import loopback_ports as ports


def test_filters_reserved_ports_before_binding_and_holds_distinct_sockets(monkeypatch):
    monkeypatch.setattr(ports, "_windows_tcp_exclusions", lambda: (range(59023, 59026),))
    monkeypatch.setattr(ports, "_windows_tcp_in_use", lambda: {59022})
    attempted = []
    held = set()
    busy = 59019

    def socket_factory():
        def bind(address):
            host, port = address
            assert host == "127.0.0.1"
            assert port != 0 and port not in ports.RESERVED_PORTS
            attempted.append(port)
            if port == busy:
                raise OSError(errno.EADDRINUSE, "occupied")
            assert port not in held
            held.add(port)
            probe.close.side_effect = lambda: held.remove(port)
        probe = Mock(bind=Mock(side_effect=bind))
        return probe

    monkeypatch.setattr(ports.random, "randrange", lambda _size: 59018 - ports.TEMPORARY_PORTS.start)
    monkeypatch.setattr(ports.socket, "socket", socket_factory)
    assert ports.free_loopback_ports(3, exclude=(59020,)) == (59021, 59026, 59027)
    assert attempted == [59019, 59021, 59026, 59027]
    assert not held


def test_reports_a_permission_error_instead_of_trying_another_port(monkeypatch):
    monkeypatch.setattr(ports, "_windows_tcp_exclusions", lambda: ())
    monkeypatch.setattr(ports, "_windows_tcp_in_use", lambda: ())
    monkeypatch.setattr(ports.random, "randrange", lambda _size: 0)
    probe = Mock()
    probe.bind.side_effect = PermissionError(errno.EACCES, "blocked")
    factory = Mock(return_value=probe)
    monkeypatch.setattr(ports.socket, "socket", factory)
    with pytest.raises(PermissionError, match=r"127\.0\.0\.1:49152"):
        ports.free_loopback_ports(1)
    factory.assert_called_once()
    probe.close.assert_called_once()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows TCP range exclusions")
def test_reads_inclusive_tcp_exclusion_ranges_without_modifying_them(monkeypatch):
    query = Mock(return_value=SimpleNamespace(stdout="Port exclusion ranges\n  50000  50059 *\n  59016  59022\n"))
    monkeypatch.setattr(ports, "run_hidden", query)

    assert ports._windows_tcp_exclusions() == (range(50000, 50060), range(59016, 59023))
    args, = query.call_args.args
    assert args[1:] == ["interface", "ipv4", "show", "excludedportrange", "protocol=tcp"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows TCP endpoints")
def test_reads_only_local_tcp_ports_including_non_listening_bindings(monkeypatch):
    query = Mock(return_value=SimpleNamespace(stdout=(
        "  TCP    0.0.0.0:50100   0.0.0.0:0       BOUND       123\n"
        "  TCP    127.0.0.1:50101 127.0.0.1:60100 ESTABLISHED 456\n"
        "  TCP    [::]:50102      [::]:0         LISTENING   789\n"
    )))
    monkeypatch.setattr(ports, "run_hidden", query)
    assert ports._windows_tcp_in_use() == {50100, 50101, 50102}
    args, = query.call_args.args
    assert args[1:] == ["-anoq", "-p", "tcp"]
