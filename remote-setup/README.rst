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

    sudo systemctl restart devstack@q-svc devstack@neutron-api 2>/dev/null || \
        sudo systemctl restart devstack@q-svc

After a code change (edit locally, push, then on the remote)::

    git -C /opt/stack/networking-srv6 pull
    sudo systemctl restart devstack@q-svc

Verification
------------

Driver loaded::

    sudo journalctl -u devstack@q-svc -u devstack@neutron-api --since "-5 min" | grep -i srv6
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

Known risk: devstack's default OVN mechanism driver has never seen the
``srv6`` network type. If ``network create`` fails with an OVN
mechanism-driver error (not a type-driver error), capture the traceback
from the journal -- that tells us whether phase 7.3.1 needs a minimal
mechanism-driver stub earlier than planned.
