networking-srv6
================

Out-of-tree ML2 type driver that lets Neutron hand out SRv6 function
IDs to provider/tenant networks instead of a VNI or VLAN tag.

SID model (``docs/implementation-plan.md`` section 1.1): each compute
node owns an operator-assigned IPv6 *locator* carved from
``locator_pool``; each network gets a cloud-global *function ID* (the
allocated ``segmentation_id``). The End.DT4/DT6 SID at which a chassis
decapsulates a network's traffic is only formed at send time,
``SRv6TypeDriver.build_sid(chassis_locator, segmentation_id)`` --
e.g. ``fc00:0:1:2::/64`` + fn ``5`` => ``fc00:0:1:2:5::/80``.

Status: phase 7.3.1 + 7.3.1b complete (control-plane only). Function-ID
allocations are DB-backed (``srv6_locator_allocations``, alembic
subproject ``networking-srv6``): restart-safe, transactional with
network create/delete, race-safe across API workers. Network MTU
accounts for the outer IPv6 header (reduced encap, ``ml2.path_mtu`` /
``global_physnet_mtu`` minus 40). No OVN mechanism-driver changes yet
-- ``End.DT4``/``End.DT6`` programming and multi-node SID exchange land
in later phases (a temporary shim in ``ovn_compat.py`` keeps the
in-tree OVN driver from vetoing srv6 networks until then).

Install into an existing Neutron (stable/2026.1) via devstack by
adding to ``local.conf``::

    enable_plugin networking-srv6 https://github.com/<you>/networking-srv6 master

    [[post-config|/$Q_PLUGIN_CONF_FILE]]
    [ml2]
    type_drivers = srv6,vxlan,geneve,vlan,flat,local
    project_network_types = srv6

    [ml2_type_srv6]
    locator_pool = fc00:0:1::/48
    locator_prefix_length = 64
    function_bits = 16

See ``devstack/plugin.sh`` for what the devstack plugin actually does,
and ``networking_srv6/plugins/ml2/drivers/srv6/type_srv6.py`` for the
type driver implementation.
