import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import netmgr


def test_mac_is_locally_administered_and_derived_from_ip():
    assert netmgr.mac_for("10.200.0.2") == "02:fc:0a:c8:00:02"
    assert netmgr.mac_for("10.200.1.6") == "02:fc:0a:c8:01:06"


def test_pool_has_room_for_thousands_of_vms():
    assert len(netmgr._subnets) == 16384
    first = list(netmgr._subnets[0].hosts())
    assert [str(h) for h in first] == ["10.200.0.1", "10.200.0.2"]       # gateway, guest
