Remote test machine setup
=========================

Two ways to get the type driver into devstack on the remote machine.

Fresh stack
-----------

Copy ``local.conf`` from this directory into ``~/devstack/`` (merge the
password/HOST_IP lines with whatever is already there) and run
``./stack.sh``. The ``enable_plugin`` line clones this repo to
``/opt/stack/networking-srv6`` and pip-installs it; the
``[[post-config]]`` block writes the ml2 options.

Hot install into an already-stacked devstack
--------------------------------------------

No restack needed -- install the package, add the ml2 options, restart
neutron::

    cd /opt/stack
    git clone https://github.com/costa99/networking-srv6.git

    # Recent devstack installs everything into a shared venv; if
    # /opt/stack/data/venv does not exist, use plain "sudo pip3".
    /opt/stack/data/venv/bin/pip install -e /opt/stack/networking-srv6

    source /opt/stack/devstack/inc/ini-config
    CONF=/etc/neutron/plugins/ml2/ml2_conf.ini
    iniset $CONF ml2 type_drivers srv6,vxlan,geneve,vlan,flat,local
    iniset $CONF ml2 tenant_network_types srv6
    iniset $CONF ml2_type_srv6 locator_pool fc00:0:1::/48
    iniset $CONF ml2_type_srv6 locator_prefix_length 64

    sudo systemctl restart devstack@neutron-api

Apply the DB schema (once, and again whenever a new migration lands)::

    /opt/stack/data/venv/bin/neutron-db-manage \
        --subproject networking-srv6 upgrade head

After a code change (edit locally, push, then on the remote)::

    git -C /opt/stack/networking-srv6 pull
    sudo systemctl restart devstack@neutron-api

If the change touched setup.cfg (entry points) also re-run
``pip install -e`` before restarting.

Verification
------------

Driver loaded::

    sudo journalctl -u devstack@neutron-api --since "-5 min" | grep -i srv6
    # expect: SRv6TypeDriver initialized with locator_pool=fc00:0:1::/48 ...

Allocation semantics::

    source /opt/stack/devstack/openrc admin admin

    openstack network create demo-net
    openstack network show demo-net -c provider:network_type -c provider:segmentation_id
    # expect: srv6 / <integer offset>

    openstack network create --provider-network-type srv6 --provider-segment 42 prov-net
    openstack network create --provider-network-type srv6 --provider-segment 42 dup-net
    # the second one must FAIL

    openstack network delete demo-net prov-net

Note on OVN: the in-tree OVN mechanism driver hard-codes its supported
network types and rejects ``srv6`` in ``create_network_precommit``
(confirmed on this machine, 2026-07-14). ``ovn_compat.py`` works around
it by patching ``_is_network_type_supported`` when the type driver
initializes -- look for "Patched OVNMechanismDriver" in the journal.
The shim goes away when the real OVN integration lands (phase 7.3.2+).
