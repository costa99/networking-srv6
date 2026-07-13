#!/bin/bash
# devstack plugin for networking-srv6.
#
# Enable in local.conf with:
#   enable_plugin networking-srv6 <repo-url> [branch]
#
# Handles only the "install the type driver into the neutron venv"
# part. All the ml2_conf.ini options (type_drivers, tenant_network_types,
# [ml2_type_srv6] locator_pool/locator_prefix_length) are set directly
# in local.conf's [[post-config|...]] section -- see remote-setup/local.conf.

NETWORKING_SRV6_DIR=${NETWORKING_SRV6_DIR:-$DEST/networking-srv6}

function install_networking_srv6 {
    setup_develop $NETWORKING_SRV6_DIR
}

if is_service_enabled q-svc neutron-api; then
    if [[ "$1" == "stack" && "$2" == "install" ]]; then
        echo_summary "Installing networking-srv6"
        install_networking_srv6
    fi
fi
