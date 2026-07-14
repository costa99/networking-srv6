# Command cheatsheet — SRv6/OVN/devstack

Live values from the reference deployment (`stack@192.168.0.157`):
OVS 3.3.4, OVN 24.03.6 (packaged), kernel 6.8, OVN source clone at
`/opt/stack/ovn` (ovs submodule at `/opt/stack/ovn/ovs`).

## Neutron -> NB: what neutron asked for

```bash
ovn-nbctl show
ovn-nbctl list Logical_Switch                 # network_type in external_ids
ovn-nbctl list Logical_Switch_Port <port-id>  # our neutron:srv6-sid rides here
```

Observed for an srv6 network (`neutron:` keys written by
`ovn_client.py:_gen_network_parameters`):

```
external_ids: {"neutron:mtu"="1460", "neutron:network_name"=srv6-demo,
               "neutron:provnet-network-type"=srv6, ...}
```

Note: neutron's `segmentation_id` (our function ID) is NOT written to NB
for tunnel-type networks — see code-map.md, "the tunnel_key surprise".

## SB: what northd derived, who the chassis are, how they tunnel

```bash
ovn-sbctl show
ovn-sbctl list Chassis
ovn-sbctl list Encap                 # per-chassis tunnel endpoints
ovn-sbctl list Port_Binding
ovn-sbctl --bare --columns=tunnel_key,external_ids find Datapath_Binding \
    'external_ids:name=neutron-<net-uuid>'   # the on-wire datapath id
```

## Chassis config (Open_vSwitch external_ids -> SB Encap)

```bash
sudo ovs-vsctl get Open_vSwitch . external_ids
sudo ovs-vsctl set Open_vSwitch . external_ids:ovn-encap-type=geneve
sudo ovs-vsctl set Open_vSwitch . external_ids:ovn-srv6-locator='fc00:0:1:1::/64'
# (ovn-srv6-locator is our phase-3 convention; ignored by stock OVN)
```

## Logical -> physical tracing

```bash
ovn-trace <datapath> '<microflow>'
ovs-vsctl show
ovs-vsctl list interface <port>          # tunnel options live here
ovs-ofctl dump-flows br-int
ovs-appctl ofproto/trace br-int '<flow>'
sudo ovs-appctl dpctl/dump-flows        # actual datapath flows
```

## OVS srv6 tunnel ports (OVS >= 3.2)

Option names (from `ovs/lib/netdev-vport.c`): `remote_ip` (final
segment / SID), `local_ip`, `srv6_segs` (comma-separated segment list,
max 6 — `parse_srv6_segs`), `srv6_flowlabel` (`zero|compute|copy`).

```bash
ovs-vsctl add-port br-x t0 -- set interface t0 type=srv6 \
    options:remote_ip=fc00:0:1:2:5:: \
    options:srv6_segs='fc00:0:1:99:1::,fc00:0:1:2:5::'
```

**Datapath support (verified 2026-07-14): userspace (netdev) datapath
only.** Encap/decap is implemented in `ovs/lib/netdev-native-tnl.c`
(`netdev_srv6_{build,push,pop}_header`); the kernel has no `srv6` link
kind (`ip link add type srv6` -> "Unknown device type"), so srv6 ports
on a system-datapath bridge cannot work. Test bridges need
`datapath_type=netdev`.

```bash
ovs-vsctl add-br br-x -- set bridge br-x datapath_type=netdev
```

## Kernel SRv6 reference semantics (validation oracle, not the dataplane)

```bash
# encap: steer prefix via SID list
sudo ip -6 route add <prefix> encap seg6 mode encap segs <sid[,sid]> dev <if>
# decap into an IPv4 VRF (End.DT4) / plain End
sudo ip -6 route add <sid> encap seg6local action End.DT4 vrftable <t> dev <if>
sudo ip -6 route add <sid> encap seg6local action End dev <if>
# ingress SRH processing is off by default (verified =0 on the box):
sudo sysctl net.ipv6.conf.<if>.seg6_enabled=1
sudo sysctl net.ipv6.conf.all.seg6_enabled=1
```

## OVN build & sandbox (source clone at /opt/stack/ovn)

```bash
cd /opt/stack/ovn/ovs && ./boot.sh && ./configure && make -j4
cd /opt/stack/ovn && ./boot.sh && \
    ./configure --with-ovs-source=/opt/stack/ovn/ovs && make -j4
make sandbox                    # interactive; ovn-nbctl etc. preconfigured
make check TESTSUITEFLAGS='-k srv6'   # (once we add srv6 system tests)
```

## Capture

```bash
tcpdump -ni <underlay-if> 'ip6' -vv            # outer IPv6 (reduced encap)
tcpdump -ni <underlay-if> 'ip6 proto 43' -vv   # explicit SRH present
tcpdump -ni <underlay-if> 'udp port 6081'      # geneve — must go silent
```
