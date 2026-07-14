import pytest

from relay.ssrf import (
    SSRFBlocked,
    SSRFPolicy,
    is_blocked_address,
    validate_resolved_addresses,
    validate_url_syntax,
)


class TestValidateUrlSyntax:
    def test_https_url_accepted(self):
        assert validate_url_syntax("https://example.com/data.json") == "example.com"

    def test_http_rejected(self):
        with pytest.raises(SSRFBlocked):
            validate_url_syntax("http://example.com/data.json")

    def test_ftp_rejected(self):
        with pytest.raises(SSRFBlocked):
            validate_url_syntax("ftp://example.com/data.json")

    def test_no_hostname_rejected(self):
        with pytest.raises(SSRFBlocked):
            validate_url_syntax("https:///data.json")

    def test_userinfo_rejected(self):
        with pytest.raises(SSRFBlocked):
            validate_url_syntax("https://user:pass@example.com/data.json")

    def test_custom_policy_allows_http(self):
        policy = SSRFPolicy(allowed_schemes=frozenset({"http", "https"}))
        assert validate_url_syntax("http://example.com", policy) == "example.com"


class TestIsBlockedAddress:
    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "127.5.5.5",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.1.1",  # link-local
            "0.0.0.0",
            "224.0.0.1",  # multicast
            "::1",  # IPv6 loopback
            "fc00::1",  # IPv6 unique local (private)
            "fe80::1",  # IPv6 link-local
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:10.0.0.5",  # IPv4-mapped private
        ],
    )
    def test_blocked_addresses(self, addr):
        assert is_blocked_address(addr) is True

    @pytest.mark.parametrize(
        "addr",
        [
            "93.184.216.34",  # example.com's public IP
            "8.8.8.8",
            "2606:2800:220:1:248:1893:25c8:1946",  # example.com's public IPv6
        ],
    )
    def test_public_addresses_allowed(self, addr):
        assert is_blocked_address(addr) is False

    def test_garbage_input_is_blocked(self):
        assert is_blocked_address("not-an-ip") is True


class TestValidateResolvedAddresses:
    def test_empty_list_rejected(self):
        with pytest.raises(SSRFBlocked):
            validate_resolved_addresses([])

    def test_any_blocked_address_rejects_the_whole_set(self):
        with pytest.raises(SSRFBlocked):
            validate_resolved_addresses(["93.184.216.34", "127.0.0.1"])

    def test_all_public_addresses_pass(self):
        validate_resolved_addresses(["93.184.216.34", "8.8.8.8"])
