# SRv6-native Neutron — implementation plan

Successor to the 4-line phase sketch in the thesis (§7.3). Scope decisions
locked on 2026-07-14:

- **End milestone:** phase 7.3.4 must demonstrably run — tenant traffic
  crossing two "datacenters" over BGP L3VPN + SRv6, not just designed.
- **L3 scope:** virtual routers get their own SIDs (inter-subnet East-West
  steered via SRH). North-South (FIP/SNAT over SRv6) stays design-only.
- **SID model:** chassis locator + per-network function
  (`SID = <chassis-locator>:<network-fn>::`), per RFC 8986. This reworks
  what phase 7.3.1 allocates — see Phase 1.
- **Timeframe:** thesis, 3–6 months, solo. Every phase has a timebox and a
  descope gate ("what goes in the thesis if this phase dies").
- Carried over from `phase-7.3.2-study.md` (not re-litigated): OVN-native
  architecture (patch OVN, no sidecar agent); minimal-adapter approach —
  OVS ≥ 3.2 already ships `srv6` tunnel ports, the OVN patch only teaches
  SB/`ovn-controller` to configure them.

---

## 1. Target architecture

### 1.1 SID addressing plan

```
Operator pool          fc00:0:1::/48            (config: ml2_type_srv6.locator_pool)
Chassis locator        fc00:0:1:<node>::/64     (one per compute node)
Network function       fc00:0:1:<node>:<fn>::   (End.DT4/DT6 — decap into network <fn>)
Router SID             fc00:0:1:<node>:<rfn>::  (End — steer through router pipeline)
```

- The **chassis locator** is routable in the IPv6 underlay: it identifies
  *where* a packet must go. Advertised per node (static routes intra-DC,
  BGP inter-DC in phase 5).
- The **function** identifies *what to do on arrival*: decapsulate and
  deliver into tenant network N (End.DT4 for IPv4 payloads, End.DT6 for
  IPv6). The existing `srv6_locator_allocations` offset becomes the
  function ID — same allocator, new meaning, no schema change.
- Consequence: the value Neutron allocates per network is **cloud-global**
  (same `fn` on every chassis), exactly like a VNI. The full SID is only
  formed at the sender by combining the *destination chassis'* locator
  with the network's function — this is what `controller/physical.c` will
  do, and it is structurally identical to how it picks a Geneve tunnel +
  sets the VNI today. That is why the minimal-adapter bet is plausible.

### 1.2 Packet walk (multi-node East-West, same tenant network)

```
VM-A (node 1) → br-int → logical switch pipeline
  → output to remote port binding on node 2
  → OVS srv6 tunnel port: encap IPv6, DA = fc00:0:1:2:<fn>::
  → IPv6 underlay routes on chassis locator fc00:0:1:2::/64
  → node 2 OVS srv6 port: decap (End.DT4 semantics), metadata = <fn>
  → br-int logical pipeline → VM-B
```

Note: End.DT4/DT6 semantics are *realized by the OVS srv6 tunnel port +
br-int flows*, not by kernel `seg6local` routes. The kernel `ip route ...
encap seg6local action End.DT4` form is the reference semantics used for
validation, not the dataplane.

---

## 2. Phase overview

| # | Phase | Depends on | Timebox | Must-have exit criterion |
|---|-------|-----------|---------|--------------------------|
| 0 | 7.3.1 type driver | — | done | srv6 networks allocate DB-backed offsets ✓ |
| 1 | SID-model rework (7.3.1b) | 0 | 1 wk | offset = network function; chassis locator concept in config/docs |
| 2 | OVN study (7.3.2a) | — | 2 wk (existing study plan) | design note + code map + OVS srv6 port proven by hand |
| 3 | OVN srv6 adapter (7.3.2b) | 1, 2 | 4 wk | two sandbox/fake chassis exchange packets over srv6 tunnel ports |
| 4 | Multi-node devstack (7.3.3) | 3 | 3 wk | two-node devstack: VM↔VM ping over SRv6, SRH seen in tcpdump, geneve absent |
| 5 | BGP multi-DC (7.3.4) | 4 | 4 wk | two routing domains: locators via BGP, tenant prefixes via VPNv4+SRv6 SID, VM↔VM across domains |
| 6 | Router SIDs + TE demo | 4 | 3 wk | inter-subnet E-W via router End SID in the SRH; defined-path (waypoint) steering shown on a multi-path underlay |
| 7 | SFC + polish | 6 | design-only | thesis chapter mapping port chains → SID lists |

Phases 5 and 6 are independent of each other — if the schedule slips, run
only one and take the other's descope gate. Recommended order: 5 before 6
(BGP is the declared must-have; router SIDs are the larger OVN change).

**Descope ladder** (what the thesis claims at each cut line):

1. Phases 0–4 only → "SRv6 replaces Geneve for tenant overlay in
   OpenStack" — already a complete, defensible result.
2. \+ Phase 5 → "…and extends across datacenters with BGP L3VPN/SRv6."
3. \+ Phase 6 → "…with SR-native service/router steering."

---

## 3. Phase 1 — SID-model rework (7.3.1b)

The current driver hands each network a /64 *locator*. Under the chassis
model that value is reinterpreted as the network's **function ID**. Small,
mostly-renaming phase; do it before the OVN work hard-codes the old
semantics anywhere.

### Functions to modify — `networking-srv6` (this repo)

| Where | What |
|-------|------|
| `type_srv6.py` `SRv6TypeDriver.get_locator()` | Split into `get_function_id(segmentation_id)` (the offset, range-checked) and `build_sid(chassis_locator, segmentation_id)` (combines locator + fn into a full SID/128 or /80 per the chosen layout). Keep `get_locator()` as a deprecated alias until phase 3 removes callers. |
| `type_srv6.py` config opts | Reinterpret: `locator_pool` = operator block chassis locators are carved from; add `function_bits` (width of the fn field, default 16) replacing the per-network use of `locator_prefix_length`. Validate `pool prefixlen + node_bits + function_bits ≤ 128`. |
| `type_srv6.py` `get_mtu()` | Implement for real: `underlay_mtu − 40 (IPv6) − 8 (SRH base) − 16×(nsegs−1)`; with a single SID in the DA (reduced encap) overhead is 40. Source underlay MTU from a new `ml2_type_srv6.path_mtu` opt (mirror `ml2.path_mtu` / `type_tunnel.get_mtu()` logic). Needed before phase 4 or multi-node ping fragments/mysteriously drops. |
| `constants.py` | Add SID-layout constants (fn bit-width, external_ids key names — see phase 3/4 for the keys). |
| DB (`models.py`, alembic) | **No change** — the offset column already is the function ID. Only docstrings. |
| `README.rst`, tests | Update semantics; extend `test_type_srv6.py` for `build_sid()` bit-layout edge cases (fn overflow, node overflow). |

### Created — chassis locator assignment (design decision, implement in phase 3)

Each chassis' locator is configured on the node itself, OVN-style, in
`Open_vSwitch . external_ids:ovn-srv6-locator=fc00:0:1:2::/64` (parallel
to `ovn-encap-ip`). Neutron does **not** allocate chassis locators in this
plan — the operator/devstack plugin sets them. A Neutron-side chassis
locator allocator is listed as future work (§8).

**Descope gate:** none — this phase is small and mandatory.

### Practical test steps (phase 1 — all on the devstack box, control plane only)

```bash
# 1. Dynamic allocation: a tenant network gets a function ID automatically
openstack network create t1
openstack network show t1 -c provider:network_type -c provider:segmentation_id
#    PASS: network_type=srv6, segmentation_id = some offset N >= 1

# 2. Dual-stack subnets on it (VM addressing is orthogonal to the SID plan
#    — prove IPv4 payloads and IPv6 payloads both get addressed)
openstack subnet create s4 --network t1 --subnet-range 10.10.0.0/24
openstack subnet create s6 --network t1 --ip-version 6 \
    --ipv6-address-mode slaac --ipv6-ra-mode slaac \
    --subnet-range fd00:10::/64
openstack port create p1 --network t1
#    PASS: port has one IPv4 + one IPv6 fixed address

# 3. Allocation is DB-backed and released on delete
mysql neutron -e "SELECT * FROM srv6_locator_allocations WHERE allocated=1;"
openstack network delete t1
mysql neutron -e "SELECT * FROM srv6_locator_allocations WHERE allocated=1;"
#    PASS: row for offset N flips allocated 1 -> 0

# 4. Explicit (admin-chosen) function ID + collision rejection
openstack network create t2 --provider-network-type srv6 --provider-segment 5
openstack network create t3 --provider-network-type srv6 --provider-segment 5
#    PASS: t2 gets offset 5; t3 fails with "locator offset 5 already in use"

# 5. Race safety under churn: parallel create/delete keeps offsets unique
for i in (seq 1 20); openstack network create "race$i" & ; end; wait
mysql neutron -e "SELECT locator_offset, COUNT(*) c FROM \
  srv6_locator_allocations GROUP BY locator_offset HAVING c>1;"
#    PASS: empty result (no duplicate rows), 20 distinct offsets allocated

# 6. SID formation math (new build_sid) — pure-python check, no cloud needed
python3 -c "
from networking_srv6.plugins.ml2.drivers.srv6 import type_srv6 as t
d = t.SRv6TypeDriver()
print(d.build_sid('fc00:0:1:2::/64', 5))"   # expect fc00:0:1:2:5::
#    PASS: matches the §1.1 layout; overflow fn raises

# 7. Pool exhaustion: set a tiny pool (e.g. 2 offsets) in local.conf,
#    restart neutron, create 3 networks
#    PASS: third create fails with NoNetworkAvailable, not a 500

# 8. MTU accounting
openstack network create t4 && openstack network show t4 -c mtu
#    PASS: mtu = path_mtu - 40 (reduced encap), no longer 0/1500 default
```

---

## 4. Phase 2 — OVN study (7.3.2a)

Already fully specified in `docs/phase-7.3.2-study.md`; unchanged. Its
deliverables (design note, code map with exact `file:function` on the OVN
side, hand-proven OVS srv6 tunnel port) are **inputs** to phase 3 — in
particular the exact OVS interface option names for srv6 ports, which this
plan deliberately does not guess.

### Practical test steps (phase 2 — the hands-on that de-risks everything)

The srv6-tunnel-port primitive, proven with two netns and no OVN
(this is the study plan's days 6–7, made concrete):

```bash
# Two bridges joined by an srv6 tunnel over a veth "underlay"
ip link add u0 type veth peer name u1
ip addr add fc00::a/64 dev u0; ip addr add fc00::b/64 dev u1
ip link set u0 up; ip link set u1 up

ovs-vsctl add-br br0 && ovs-vsctl add-br br1
ovs-vsctl add-port br0 t0 -- set interface t0 type=srv6 \
    options:remote_ip=fc00::b   # exact option names: record what works!
ovs-vsctl add-port br1 t1 -- set interface t1 type=srv6 \
    options:remote_ip=fc00::a

# Attach a netns "VM" to each bridge, same L2 subnet, then:
ip netns exec vm0 ping -c3 192.168.50.2
tcpdump -ni u0 'ip6' -vv
#    PASS: ping works AND capture shows outer IPv6 (proto 43 SRH when
#    multiple segments; plain IPv6-in-IPv6 for reduced encap)
#    RECORD: the exact options: keys accepted, and whether the remote
#    SID can come from a flow action (set_field) instead of the port —
#    this answers open question #1 and shapes controller/encaps.c work.

# OVN warm-up (study days 1-2): sandbox runs, trivial patch observed
make sandbox   # in the ovn clone
grep 'my VLOG marker' sandbox/ovn-controller.log
#    PASS: your added log line appears
```

---

## 5. Phase 3 — OVN srv6 adapter (7.3.2b: "local dataplane")

One correction to the thesis sketch: "try communication between VMs on the
same hypervisor" proves nothing about SRv6 — same-chassis traffic is
switched inside br-int and never touches a tunnel. The real exit criterion
is **two chassis** (OVN sandbox / ovn-fake-multinode containers, no
devstack yet) exchanging packets over srv6 tunnel ports.

### 5.1 Functions to modify — OVN (C, out of OpenStack but on the critical path)

Exact names come from the phase-2 code map; the known touch points:

| Where | What |
|-------|------|
| `ovn-sb.ovsschema` — `Encap` table | Extend the `type` enum `{geneve, stt, vxlan}` with `srv6`; the encap `options` map carries the chassis locator. Bump schema version + `utilities/ovn-sbctl` docs. |
| `controller/chassis.c` (Encap-row construction from `ovn-encap-type`/`ovn-encap-ip`) | Accept `srv6` in the encap-type list; read `external_ids:ovn-srv6-locator` and publish it in the chassis' srv6 `Encap.options`. |
| `controller/encaps.c` (SB `Encap` rows → OVS tunnel ports, around `encaps_run`/`tunnel_add`) | For a remote chassis with an srv6 encap, create the OVS tunnel port with `type=srv6` and the option names proven in phase 2 (remote locator, segment list). |
| `controller/physical.c` (output-to-remote-chassis flows, around `put_encapsulation`/`consider_port_binding`) | When the selected encap is srv6, build the tunnel destination as `remote_locator:network_fn::` — i.e. carry the datapath's function ID into the tunnel destination instead of (or in addition to) `tunnel_key` metadata. This is the one place the SID is actually *formed*. |
| `northd/` | Ideally **zero changes** in this phase: keep using the datapath `tunnel_key` as the function ID if its range fits `function_bits`; otherwise northd must copy the Neutron-provided fn from `Logical_Switch` external_ids into the SB `Datapath_Binding.external_ids` (small, mechanical). Decide from the phase-2 findings. |
| `controller/binding.c` / encap selection (`chassis_get_encap`? — pin in code map) | Encap preference order so srv6 wins over geneve when both are advertised (needed for phase 4's mixed-cluster rollout). |

### 5.2 Functions to modify — Neutron in-tree (target: small patch or subclass)

| Where | What |
|-------|------|
| `neutron/plugins/ml2/drivers/ovn/mech_driver/mech_driver.py` `OVNMechanismDriver._is_network_type_supported()` and `_validate_network_segments()` | Add `srv6` to the accepted set. This **replaces and deletes** `networking_srv6/.../ovn_compat.py` (the monkeypatch shim). Strategy: subclass `OVNMechanismDriver` in this repo (`mechanism_drivers = srv6-ovn`) rather than patching in-tree Neutron, so the whole project stays out-of-tree and devstack-installable. |
| `neutron/plugins/ml2/drivers/ovn/mech_driver/ovsdb/ovn_client.py` `OVNClient.create_network()` / `_gen_network_parameters()` | Where `provider:network_type`/`segmentation_id` land in `Logical_Switch` today, additionally write `external_ids["neutron:srv6-function"] = <offset>` (and the computed per-network info the OVN patch needs). In the subclass approach: override `_gen_network_parameters()` in an `SRv6OVNClient` and hook it via the subclassed mech driver's `_ovn_client`. |
| `neutron/common/ovn/constants.py` (reference only) | New key names defined in `networking_srv6/constants.py` instead — keep in-tree Neutron untouched. |
| `ovn_db_sync.py` (`OvnNbSynchronizer`) | Verify the NB sync doesn't strip the extra external_ids key on `neutron-ovn-db-sync-util` runs; patch the allow-list if it does. |

### 5.3 Created — this repo

- `networking_srv6/plugins/ml2/mech_srv6_ovn.py` — the subclassed
  mechanism driver + `SRv6OVNClient` (new entry point in `setup.cfg`).
- `devstack/plugin.sh` — install the patched OVN build on the node;
  set `ovn-encap-type=srv6,geneve` and `ovn-srv6-locator` in
  `Open_vSwitch external_ids`.

### Practical test steps (phase 3 — two fake chassis, no devstack)

```bash
# Setup: ovn-fake-multinode (or 2 sandbox chassis), patched OVN on both.
# On each chassis, instead of geneve:
ovs-vsctl set Open_vSwitch . \
    external_ids:ovn-encap-type=srv6 \
    external_ids:ovn-encap-ip=<chassis underlay v6 addr> \
    external_ids:ovn-srv6-locator=fc00:0:1:1::/64   # :2:: on chassis 2

# 1. Chassis publishes its locator into SB
ovn-sbctl list Encap
#    PASS: one Encap row per chassis with type=srv6 and the locator in
#    options; ovn-sbctl show lists both chassis

# 2. Controller turns the remote Encap into an OVS srv6 tunnel port
ovs-vsctl show && ovs-vsctl list interface ovn-<chassis2>-0
#    PASS: interface type=srv6, options carry chassis-2 locator info

# 3. Logical network spanning the two chassis
ovn-nbctl ls-add net1
ovn-nbctl lsp-add net1 vm1 && ovn-nbctl lsp-set-addresses vm1 "00:00:00:00:00:01 192.168.60.1"
ovn-nbctl lsp-add net1 vm2 && ovn-nbctl lsp-set-addresses vm2 "00:00:00:00:00:02 192.168.60.2"
# bind a netns port on each chassis: ovs-vsctl set interface <p> external_ids:iface-id=vm1

# 4. The SID is actually formed
ip netns exec vm1 ping -c3 192.168.60.2
tcpdump -ni <underlay-if> ip6 -vv
#    PASS: outer IPv6 DA == fc00:0:1:2:<fn>:: — i.e. remote chassis
#    locator + this datapath's function ID, per §1.2
ovn-trace net1 'inport=="vm1" && eth.dst==00:00:00:00:00:02 ...'
#    PASS: trace ends in output via the srv6 encap to chassis 2

# 5. Neutron plumbs the function ID (subclassed mech driver in place):
openstack network create t1        # on the devstack box against same NB
ovn-nbctl list Logical_Switch neutron-<net-uuid>
#    PASS: external_ids {"neutron:srv6-function"="<offset>"} present,
#    and it survives a neutron-ovn-db-sync-util run

# 6. Negative test: chassis WITHOUT srv6 support still interoperates
#    (one chassis advertises geneve only)
#    PASS: OVN falls back to a common encap or clearly refuses — record
#    which, it defines the phase-4 rollout order
```

**Exit demo:** `ovn-fake-multinode` (or two sandbox chassis): logical
switch with two ports on different chassis, `ovn-trace` shows output via
srv6 encap, tcpdump on the fake underlay shows IPv6 DA
`<locator>:<fn>::` (+SRH when >1 segment).

**Descope gate:** if the OVN C adapter stalls past the timebox, fall back
to a **kernel-programmed dataplane**: a small privileged agent (or
devstack script) installs `ip route ... encap seg6` /
`seg6local End.DT4 vrftable` rules per network beside OVN. Uglier,
contradicts the OVN-native decision, but keeps phases 4–5 alive and the
thesis honest ("OVN integration proposed, kernel PoC demonstrated").

---

## 6. Phase 4 — Multi-node devstack (7.3.3)

Turn the lab result into the real stack: two devstack nodes (the existing
`stack@192.168.0.157` + the parked second machine — **unblock this in
week 1**, it is the long pole), IPv6 underlay between them.

### Functions to modify

| Where | What |
|-------|------|
| `type_srv6.py` `get_mtu()` | Already implemented in phase 1 — verify Nova/Neutron propagate the reduced MTU into the VM (`network.mtu`, DHCP option). |
| `mech_srv6_ovn.py` | Whatever the fake-multinode demo papered over: agent liveness (`agent_alive` uses SB `Chassis_Private`), port binding vif details. Expect fixes, not features. |
| `remote-setup/` docs + `devstack/plugin.sh` | Two-node install: patched OVN debs/build on both nodes, underlay IPv6 addressing, locator external_ids per node, `path_mtu`. |
| SID uniqueness check | On `initialize()` or via a periodic task: assert no two SB chassis advertise overlapping locators (query SB `Encap` rows through the existing ovsdbapp connection); log/refuse. New helper `networking_srv6/ovn/sb_checks.py`. |
| Delete `ovn_compat.py` | The shim dies here at the latest. |

### Practical test steps (phase 4 — the headline demo, all thesis evidence)

```bash
# Setup: node1 = existing devstack, node2 = new compute; IPv6 underlay
# between them; ovn-srv6-locator fc00:0:1:1::/64 and :2::/64 respectively.

# 1. Real VMs on both nodes, same tenant network (dual-stack)
openstack network create t1
openstack subnet create s4 --network t1 --subnet-range 10.20.0.0/24
openstack subnet create s6 --network t1 --ip-version 6 \
    --ipv6-address-mode slaac --ipv6-ra-mode slaac --subnet-range fd00:20::/64
openstack server create vm-a --image cirros --flavor m1.tiny \
    --network t1 --availability-zone nova:node1
openstack server create vm-b --image cirros --flavor m1.tiny \
    --network t1 --availability-zone nova:node2
#    PASS: both ACTIVE, each with IPv4 + IPv6 addresses

# 2. VM<->VM over SRv6, both address families (End.DT4 and End.DT6 paths)
#    from vm-a console: ping 10.20.0.X ; ping6 fd00:20::X
#    on node1: tcpdump -ni <phys-if> ip6 and net fc00:0:1::/48 -vv
#    PASS: pings work; outer IPv6 DA = fc00:0:1:2:<fn>::
#    on node1: tcpdump -ni <phys-if> udp port 6081
#    PASS: SILENT — no geneve carries tenant traffic

# 3. Dynamic SID lifecycle under load: create/delete networks while pinging
for i in (seq 1 10); openstack network create "churn$i"; end
for i in (seq 1 10); openstack network delete "churn$i"; end
#    PASS: the vm-a<->vm-b ping never drops; offsets recycled (step-3 DB
#    check from phase 1 repeated here)

# 4. SID uniqueness enforcement: set node2's locator = node1's, restart
#    ovn-controller on node2
#    PASS: the sb_checks helper logs/refuses; fix locator, all recovers

# 5. Platform services unaffected (prove, don't assume):
#    - from vm-a: curl http://169.254.169.254/latest/meta-data  (metadata)
#    - reboot vm-a, confirm it re-acquires 10.20.0.X via DHCP
#    - openstack security group rule delete <icmp-rule> -> ping stops;
#      re-add -> ping resumes                          (SGs on br-int)
#    - ssh into vm-a, check `ip link` MTU == network mtu from phase-1 #8,
#      then pass a large transfer (iperf3/scp) to catch PMTU black holes

# 6. Live migration: SID is chassis-independent by construction — prove it
openstack server migrate vm-a --live-migration --host node2 --os-compute-api-version 2.30
#    PASS: ping continuity (a few lost packets ok); traffic between
#    vm-a/vm-b is now local to node2 (underlay tcpdump goes quiet)
```

**Descope gate:** single-node + one network-namespace "fake second
chassis" on the same box; weaker demo, same code paths.

---

## 7. Phase 5 — BGP multi-DC (7.3.4)

Control plane: **BGP L3VPN (VPNv4/VPNv6) over SRv6 with FRR**, not EVPN.
FRR's SRv6 support is mature for L3VPN (`segment-routing srv6` +
`sid vpn per-vrf export auto`); EVPN-over-SRv6 in FRR is not usable —
adopting L3VPN narrows the thesis claim from the original
"L3VPN/EVPN" wording to L3VPN, which is the honest, demonstrable subset.

Topology: each "datacenter" = one devstack node + one FRR speaker (FRR can
run on the node itself); eBGP (or iBGP + RR) between the two domains over
the routed IPv6 interconnect.

### What BGP carries

1. **Chassis locators** (`fc00:0:1:<node>::/64`) — plain IPv6 unicast AFI.
   Replaces static underlay routes from phase 4.
2. **Tenant reachability** — VPNv4 routes whose SRv6 VPN SID is the
   destination network's `<locator>:<fn>::`. Import/export via RTs per
   tenant network.

### Functions to modify / create

| Where | What |
|-------|------|
| `ovn-bgp-agent` (new driver) **or** static FRR config | Decision point at phase start. Recommended for the timebox: **static/templated FRR config** generated by a small script from Neutron API data (networks + their fn, chassis locators). An `ovn_bgp_agent/drivers/` SRv6 driver (watching SB `Port_Binding`/`Datapath_Binding` and driving FRR via `frr_reload`, patterned on `ovn_evpn_driver.py`) is the "proper" version — do it only if ≥2 weeks remain in the timebox. |
| New: `networking_srv6/bgp/frr_template.py` + CLI (`srv6-frr-sync`) | Reads networks via neutron client / SB DB, emits `frr.conf` blocks: `segment-routing srv6 locator <node>`, per-network VRF + `sid vpn export`, RT policy. |
| FRR config (not code) | `router bgp` VPNv4 AF, `segment-routing srv6`, locator pointing at the chassis locator, static SID-per-VRF matching `<fn>`. |
| Kernel/OVS boundary | The inter-DC path terminates in the same OVS srv6 ports as phase 4 — **if** FRR's advertised SID equals the OVN-formed SID. The whole phase hinges on keeping the SID identical in both planes; the uniqueness/consistency check from phase 4 extends to cover FRR's exported SIDs. |

### Practical test steps (phase 5 — two routing domains)

```bash
# Setup: DC-A = node1(+FRR, AS 65001), DC-B = node2(+FRR, AS 65002),
# routed IPv6 interconnect (a third box or a router netns — no shared L2).

# 1. Locator reachability comes from BGP, not static routes
#    remove the phase-4 static underlay routes first, then:
vtysh -c "show bgp ipv6 unicast"
#    PASS: fc00:0:1:1::/64 learned on node2 and vice versa;
#    ping fc00:0:1:2:: from node1 works via the learned route

# 2. FRR exports the locator + per-VRF SIDs
vtysh -c "show segment-routing srv6 locator"
vtysh -c "show bgp ipv4 vpn"
#    PASS: VPNv4 route for 10.20.0.0/24 carries an SRv6 SID equal to
#    fc00:0:1:2:<fn>:: — byte-identical to what OVN forms (the
#    consistency check from §7 asserts this automatically)

# 3. srv6-frr-sync idempotence: run it twice, diff frr.conf
#    PASS: second run is a no-op; create a new tenant network, re-run,
#    exactly one new VRF/SID block appears (dynamic SID -> BGP flow)

# 4. The headline: VM in DC-A pings VM in DC-B on the same tenant net
#    tcpdump -ni <interconnect-if> ip6 -vv
#    PASS: ping works end-to-end; capture shows the SRv6-encapped
#    packet transiting the interconnect; udp port 4789/6081 silent
#    (no VXLAN/Geneve anywhere on the wire)

# 5. Control-plane failure semantics
vtysh -c "conf t" -c "router bgp 65001" -c "neighbor <peer> shutdown"
#    PASS: cross-DC ping stops within holdtime; "no neighbor shutdown"
#    and it resumes. Screenshot both — this is the BGP-is-real evidence.

# 6. Tenant isolation across DCs: second tenant network with the SAME
#    IPv4 subnet range (10.20.0.0/24) on both sides
#    PASS: no cross-talk — VRF/RT separation holds, pings stay in-tenant
```

**Exit demo:** VM in DC-A pings VM in DC-B on the same tenant network;
`vtysh -c "show bgp ipv4 vpn"` shows the route with SRv6 SID; tcpdump on
the interconnect shows the SRH/IPv6-encapped packet; no VXLAN/Geneve
anywhere on the wire.

**Descope gate:** if FRR SID interop with the OVS ports fails, demo BGP
carrying locators + fn as *information* (control plane proven) while the
dataplane stays the phase-4 static-routed underlay; thesis reports the
interop gap as a finding.

---

## 8. Phase 6 — Router SIDs (inter-subnet East-West)

Give each logical router an **End SID** (`<chassis-locator>:<rfn>::` on
its gateway/hosting chassis) so inter-subnet traffic is steered *through*
the router with an explicit segment list — SR-native routing instead of
OVN's implicit pipeline hop.

This is the largest OVN-side change (it touches northd, not just the
controller adapter) — hence its position after the BGP must-have.

### Functions to modify

| Where | What |
|-------|------|
| `type_srv6.py` / new allocator | Allocate router function IDs from a reserved band of the same offset table (e.g. top 4096 offsets), `allocate_router_function(router_id)`. |
| `ovn_client.py` `create_router()` / `update_router()` (subclassed in `mech_srv6_ovn.py`) | Write `external_ids["neutron:srv6-router-fn"]` on the `Logical_Router`. |
| OVN `northd/northd.c` (logical-flow build for router datapaths) | Emit flows binding the router fn: packets arriving with DA = router SID enter the router pipeline (End behavior), then the *next* segment (destination network SID) takes over. This is genuinely new logical-flow work — the minimal-adapter guarantee does not cover it. |
| `controller/physical.c` | Two-segment SRH: `[router SID, dest network SID]` when source and dest subnets differ. First real SRH (phase 3–5 mostly uses single-SID reduced encap). |
| Neutron API extension (new): `networking_srv6/extensions/srv6.py` | Read-only fields exposing the SIDs (`srv6:network_sid` computed per chassis list, `srv6:router_sid`) so tenants/operators can see the segment plan. Standard neutron-lib API-definition + extension-descriptor pair, wired via the service plugin. |

### Practical test steps (phase 6a — router SIDs)

```bash
# 1. Router function allocation + API exposure
openstack network create ta && openstack subnet create sa --network ta \
    --subnet-range 10.30.1.0/24
openstack network create tb && openstack subnet create sb --network tb \
    --subnet-range 10.30.2.0/24
openstack router create r1
openstack router add subnet r1 sa && openstack router add subnet r1 sb
openstack router show r1 -c srv6:router_sid       # new API extension
#    PASS: a SID from the reserved fn band; also visible in
#    ovn-nbctl list Logical_Router (external_ids neutron:srv6-router-fn)

# 2. Inter-subnet E-W is steered THROUGH the router SID
#    vm-a on ta@node1, vm-b on tb@node2; ping vm-b from vm-a
tcpdump -ni <phys-if> 'ip6 proto 43' -vv
#    PASS: SRH with 2 segments — [router SID, dest network SID] — and
#    segments-left decrements at the router hop. This is the first
#    on-the-wire SRH of the project; screenshot it.

# 3. Reference-semantics cross-check (kernel as oracle):
#    replicate the same 2-segment path with `ip -6 route ... encap seg6
#    mode encap segs <rsid>,<nsid>` between two netns and diff the
#    captures — OVN-formed SRH must match kernel-formed SRH field-by-field
```

### Practical test steps (phase 6b — multi-path underlay + defined-path TE)

The "communicate over a defined path" demo: give the underlay two
distinct physical paths and prove SRv6 can pin tenant traffic to a chosen
one by inserting a waypoint SID — the classic SR traffic-engineering
result, and the thesis' strongest "why SRv6 at all" evidence.

```bash
# Setup: add a third box/netns W ("waypoint") so node1 reaches node2 both
#   directly (path P1) and via W (path P2). W runs no OVN — just kernel
#   SRv6: an End SID, e.g. fc00:0:1:99:1::
ip -6 route add fc00:0:1:99:1::/80 encap seg6local action End dev <if>  # on W

# 1. Baseline: traffic uses the direct path
#    tcpdump on W: silent while vm-a pings vm-b

# 2. Pin the path: encap with segment list [W's End SID, dest SID]
#    First via kernel on node1 (always works, proves the fabric):
ip -6 route add fc00:0:1:2::/64 encap seg6 mode encap \
    segs fc00:0:1:99:1:: dev <phys-if>
#    Then, if phase-2 findings allow, natively: OVS srv6 port/flow with a
#    2-entry segment list (options:srv6_segs / set_field — per code map)
tcpdump -ni <W-if> 'ip6 proto 43'
#    PASS: same vm-a->vm-b ping now transits W; SRH shows
#    [fc00:0:1:99:1::, fc00:0:1:2:<fn>::]; direct path goes quiet;
#    remove the route -> traffic snaps back to P1. Nothing changed in
#    Neutron or the VMs — path control lived entirely in the SID list.

# 3. (stretch) Per-tenant path policy: steer only network ta via W,
#    network tb stays direct — two networks, two segment lists, one wire
#    capture showing both behaviors simultaneously
```

**Descope gate (likely needed):** implement router-SID *allocation and
API exposure* (Neutron side, cheap) + design the northd flows on paper;
demo stays phase-4/5 intra-network. The thesis then presents SFC/router
steering as validated design, not running code. The 6b TE demo survives
this gate — its kernel-encap variant needs nothing from 6a and can even
run right after phase 4.

---

## 9. Phase 7 — SFC mapping (design-only)

No implementation. One thesis section mapping `networking-sfc` port chains
(`port_pair` → SF instance SID, `port_chain` → SRv6 policy/segment list)
onto the phase-6 machinery, and noting that with router/function SIDs in
place, SFC is "compile the chain to a SID list at the classifier" — which
is the SRv6 sales pitch the thesis opened with.

---

## 10. Cross-cutting concerns (all phases)

- **Testing:** unit tests in-repo per phase (type driver ✓, SID layout,
  mech-driver subclass with a mocked NB idl). OVN C changes: OVN's own
  `make check` + a system test per touch point. End-to-end: the phase exit
  demos, scripted in `remote-setup/` so they're reproducible for the
  defense.
- **Upstream hygiene:** keep the OVN patch as a clean series
  (`docs/ovn-patches/`) rebased on a pinned OVN tag; everything
  OpenStack-side stays in this repo (subclass strategy, no forked
  Neutron).
- **IPv6 underlay** is a hard prerequisite from phase 3 on — bake it into
  devstack docs early.
- **What Neutron in-tree ultimately needs changed** (thesis takeaway
  list): `OVNMechanismDriver._is_network_type_supported` /
  `_validate_network_segments` (accept pluggable types),
  `OVNClient._gen_network_parameters` (pluggable external_ids),
  SB `Encap` schema + the three `controller/*.c` touch points, northd
  router flows (phase 6). Everything else lives out-of-tree — that
  short list *is* the upstreamable diff.

## 11. Risk register

| Risk | Phase | Mitigation |
|------|-------|-----------|
| OVS srv6 port doesn't support the needed option shape (per-flow remote SID) | 2→3 | Phase-2 hands-on proves/refutes before any OVN code; fallback = kernel seg6 agent (§5 gate) |
| Second machine never materializes | 4 | Escalate now; netns fallback (§6 gate) |
| FRR SID ≠ OVS-port SID interop | 5 | Consistency check + control-plane-only demo (§7 gate) |
| northd router flows too deep for timebox | 6 | Design-on-paper gate (§8) |
| Full plan ≈ 17 weeks of timeboxes vs 12–24 wk window | all | The descope ladder (§2) is the plan, not an afterthought: cut from the top (6 then 5), never from 1–4 |

## 12. Open questions (carry into phase-2 design note)

1. Exact OVS srv6 tunnel-port option names / whether remote SID can be set
   per-flow (from OpenFlow) or only per-port — determines whether one
   tunnel port per remote chassis suffices (fn in DA via flow action) or
   one per (chassis, network).
2. Does `tunnel_key` fit as the function ID (northd already allocates it
   uniquely per datapath) — if yes, phase 3 needs no Neutron→northd fn
   plumbing at all and `neutron:srv6-function` becomes advisory.
3. Reduced encap (single SID in DA, no SRH) vs full SRH always — affects
   MTU math and how impressive the tcpdump screenshots are.
4. Chassis locator allocation: operator-set external_ids (this plan) vs
   Neutron-allocated per-chassis rows (future work).
