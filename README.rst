networking-srv6
================

Out-of-tree ML2 type driver that lets Neutron hand out IPv6 SRv6
locators to provider/tenant networks instead of a VNI or VLAN tag.

Status: phase 7.3.1 (control-plane only). No OVN mechanism-driver
changes yet -- ``End.DT4``/``End.DT6`` programming and multi-node SID
exchange land in later phases.

Install into an existing Neutron (stable/2026.1) via devstack by
adding to ``local.conf``::

    enable_plugin networking-srv6 https://github.com/<you>/networking-srv6 main

    [[post-config|/$Q_PLUGIN_CONF_FILE]]
    [ml2]
    type_drivers = srv6,vxlan,geneve,vlan,flat,local
    tenant_network_types = srv6

    [ml2_type_srv6]
    locator_pool = fc00:0:1::/48
    locator_prefix_length = 64

See ``devstack/plugin.sh`` for what the devstack plugin actually does,
and ``networking_srv6/plugins/ml2/drivers/srv6/type_srv6.py`` for the
type driver implementation.
