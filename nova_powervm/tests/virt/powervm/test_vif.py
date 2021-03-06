# Copyright 2016 IBM Corp.
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import mock

from nova import exception
from nova.network import model
from nova.network.neutronv2 import api as netapi
from nova import test
from oslo_config import cfg
from pypowervm import exceptions as pvm_ex
from pypowervm.tests import test_fixtures as pvm_fx
from pypowervm.wrappers import logical_partition as pvm_lpar
from pypowervm.wrappers import managed_system as pvm_ms
from pypowervm.wrappers import network as pvm_net

from nova_powervm.virt.powervm import vif

CONF = cfg.CONF


def cna(mac):
    """Builds a mock Client Network Adapter for unit tests."""
    nic = mock.MagicMock()
    nic.mac = mac
    nic.vswitch_uri = 'fake_href'
    return nic


class FakeNetworkAPI(object):
    def __init__(self, physnet):
        self.physical_network = physnet

    def get(self, context, netid):
        physnet = mock.MagicMock()
        physnet.physical_network = self.physical_network
        return physnet


class TestVifFunctions(test.TestCase):

    def setUp(self):
        super(TestVifFunctions, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt
        self.slot_mgr = mock.Mock()

    @mock.patch('oslo_serialization.jsonutils.dumps')
    @mock.patch('pypowervm.wrappers.event.Event')
    def test_push_vif_event(self, mock_event, mock_dumps):
        mock_vif = mock.Mock(mac='MAC', href='HREF')
        vif._push_vif_event(self.adpt, 'action', mock_vif, mock.Mock(),
                            'pvm_sea')
        mock_dumps.assert_called_once_with(
            {'provider': 'NOVA_PVM_VIF', 'action': 'action', 'mac': 'MAC',
             'type': 'pvm_sea'})
        mock_event.bld.assert_called_once_with(self.adpt, 'HREF',
                                               mock_dumps.return_value)
        mock_event.bld.return_value.create.assert_called_once_with()

        mock_dumps.reset_mock()
        mock_event.bld.reset_mock()
        mock_event.bld.return_value.create.reset_mock()

        # Exception reraises
        mock_event.bld.return_value.create.side_effect = IndexError
        self.assertRaises(IndexError, vif._push_vif_event, self.adpt, 'action',
                          mock_vif, mock.Mock(), 'pvm_sea')
        mock_dumps.assert_called_once_with(
            {'provider': 'NOVA_PVM_VIF', 'action': 'action', 'mac': 'MAC',
             'type': 'pvm_sea'})
        mock_event.bld.assert_called_once_with(self.adpt, 'HREF',
                                               mock_dumps.return_value)
        mock_event.bld.return_value.create.assert_called_once_with()

    @mock.patch('nova_powervm.virt.powervm.vif._build_vif_driver')
    @mock.patch('nova_powervm.virt.powervm.vif._push_vif_event')
    def test_plug(self, mock_event, mock_bld_drv):
        """Test the top-level plug method."""
        mock_vif = {'address': 'MAC', 'type': 'pvm_sea'}
        slot_mgr = mock.Mock()

        # 1) With slot registration
        slot_mgr.build_map.get_vnet_slot.return_value = None
        vnet = vif.plug(self.adpt, 'host_uuid', 'instance', mock_vif, slot_mgr)

        mock_bld_drv.assert_called_once_with(self.adpt, 'host_uuid',
                                             'instance', mock_vif)
        slot_mgr.build_map.get_vnet_slot.assert_called_once_with('MAC')
        mock_bld_drv.return_value.plug.assert_called_once_with(mock_vif, None,
                                                               new_vif=True)
        slot_mgr.register_vnet.assert_called_once_with(
            mock_bld_drv.return_value.plug.return_value)
        mock_event.assert_called_once_with(self.adpt, 'plug', vnet, mock.ANY,
                                           'pvm_sea')
        self.assertEqual(mock_bld_drv.return_value.plug.return_value, vnet)

        # Clean up
        mock_bld_drv.reset_mock()
        slot_mgr.build_map.get_vnet_slot.reset_mock()
        mock_bld_drv.return_value.plug.reset_mock()
        slot_mgr.register_vnet.reset_mock()
        mock_event.reset_mock()

        # 2) Without slot registration; and plug returns None (which it should
        # IRL whenever new_vif=False).
        slot_mgr.build_map.get_vnet_slot.return_value = 123
        mock_bld_drv.return_value.plug.return_value = None
        vnet = vif.plug(self.adpt, 'host_uuid', 'instance', mock_vif, slot_mgr,
                        new_vif=False)

        mock_bld_drv.assert_called_once_with(self.adpt, 'host_uuid',
                                             'instance', mock_vif)
        slot_mgr.build_map.get_vnet_slot.assert_called_once_with('MAC')
        mock_bld_drv.return_value.plug.assert_called_once_with(mock_vif, 123,
                                                               new_vif=False)
        slot_mgr.register_vnet.assert_not_called()
        mock_event.assert_not_called()
        self.assertIsNone(vnet)

    @mock.patch('nova_powervm.virt.powervm.vif._build_vif_driver')
    @mock.patch('nova_powervm.virt.powervm.vif._push_vif_event')
    def test_unplug(self, mock_event, mock_bld_drv):
        """Test the top-level unplug method."""
        mock_vif = {'address': 'MAC', 'type': 'pvm_sea'}
        slot_mgr = mock.Mock()

        # 1) With slot deregistration, default cna_w_list
        mock_bld_drv.return_value.unplug.return_value = 'vnet_w'
        vif.unplug(self.adpt, 'host_uuid', 'instance', mock_vif, slot_mgr)
        mock_bld_drv.assert_called_once_with(self.adpt, 'host_uuid',
                                             'instance', mock_vif)
        mock_bld_drv.return_value.unplug.assert_called_once_with(
            mock_vif, cna_w_list=None)
        slot_mgr.drop_vnet.assert_called_once_with('vnet_w')
        mock_event.assert_called_once_with(self.adpt, 'unplug', 'vnet_w',
                                           mock.ANY, 'pvm_sea')

        # Clean up
        mock_bld_drv.reset_mock()
        mock_bld_drv.return_value.unplug.reset_mock()
        slot_mgr.drop_vnet.reset_mock()
        mock_event.reset_mock()

        # 2) Without slot deregistration, specified cna_w_list
        mock_bld_drv.return_value.unplug.return_value = None
        vif.unplug(self.adpt, 'host_uuid', 'instance', mock_vif, slot_mgr,
                   cna_w_list='cnalist')
        mock_bld_drv.assert_called_once_with(self.adpt, 'host_uuid',
                                             'instance', mock_vif)
        mock_bld_drv.return_value.unplug.assert_called_once_with(
            mock_vif, cna_w_list='cnalist')
        slot_mgr.drop_vnet.assert_not_called()
        # When unplug doesn't find a vif, we don't push an event
        mock_event.assert_not_called()

    @mock.patch('nova_powervm.virt.powervm.vif._build_vif_driver')
    def test_plug_raises(self, mock_vif_drv):
        """HttpError is converted to VirtualInterfacePlugException."""
        vif_drv = mock.Mock(plug=mock.Mock(side_effect=pvm_ex.HttpError(
            resp=mock.Mock(status='status', reqmethod='method', reqpath='path',
                           reason='reason'))))
        mock_vif_drv.return_value = vif_drv
        mock_slot_mgr = mock.Mock()
        mock_vif = {'address': 'vifaddr'}
        self.assertRaises(exception.VirtualInterfacePlugException,
                          vif.plug, 'adap', 'huuid', 'inst', mock_vif,
                          mock_slot_mgr, new_vif='new_vif')
        mock_vif_drv.assert_called_once_with('adap', 'huuid', 'inst', mock_vif)
        vif_drv.plug.assert_called_once_with(
            mock_vif, mock_slot_mgr.build_map.get_vnet_slot.return_value,
            new_vif='new_vif')
        mock_slot_mgr.build_map.get_vnet_slot.assert_called_once_with(
            'vifaddr')

    @mock.patch('pypowervm.wrappers.network.VSwitch.search')
    def test_get_secure_rmc_vswitch(self, mock_search):
        # Test no data coming back gets none
        mock_search.return_value = []
        resp = vif.get_secure_rmc_vswitch(self.adpt, 'host_uuid')
        self.assertIsNone(resp)

        # Mock that a couple vswitches get returned, but only the correct
        # MGMT Switch gets returned
        mock_vs = mock.MagicMock()
        mock_vs.name = 'MGMTSWITCH'
        mock_search.return_value = [mock_vs]
        self.assertEqual(mock_vs,
                         vif.get_secure_rmc_vswitch(self.adpt, 'host_uuid'))
        mock_search.assert_called_with(
            self.adpt, parent_type=pvm_ms.System.schema_type,
            parent_uuid='host_uuid', name=vif.SECURE_RMC_VSWITCH)

    @mock.patch('pypowervm.tasks.cna.crt_cna')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug_secure_rmc_vif(self, mock_pvm_uuid, mock_crt):
        # Mock up the data
        mock_pvm_uuid.return_value = 'lpar_uuid'
        mock_crt.return_value = mock.Mock()
        self.slot_mgr.build_map.get_mgmt_vea_slot = mock.Mock(
            return_value=(None, None))

        # Run the method
        vif.plug_secure_rmc_vif(self.adpt, 'instance', 'host_uuid',
                                self.slot_mgr)

        # Validate responses
        mock_crt.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', 4094, vswitch='MGMTSWITCH',
            crt_vswitch=True, slot_num=None, mac_addr=None)
        self.slot_mgr.register_cna.assert_called_once_with(
            mock_crt.return_value)

    @mock.patch('pypowervm.tasks.cna.crt_cna')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug_secure_rmc_vif_with_slot(self, mock_pvm_uuid, mock_crt):
        # Mock up the data
        mock_pvm_uuid.return_value = 'lpar_uuid'
        mock_crt.return_value = mock.Mock()
        self.slot_mgr.build_map.get_mgmt_vea_slot = mock.Mock(
            return_value=('mac_addr', 5))

        # Run the method
        vif.plug_secure_rmc_vif(self.adpt, 'instance', 'host_uuid',
                                self.slot_mgr)

        # Validate responses
        mock_crt.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', 4094, vswitch='MGMTSWITCH',
            crt_vswitch=True, slot_num=5, mac_addr='mac_addr')
        self.assertFalse(self.slot_mgr.called)

    def test_build_vif_driver(self):
        # Test the Shared Ethernet Adapter type VIF
        mock_inst = mock.MagicMock()
        mock_inst.name = 'instance'
        self.assertIsInstance(
            vif._build_vif_driver(self.adpt, 'host_uuid', mock_inst,
                                  {'type': 'pvm_sea'}),
            vif.PvmSeaVifDriver)

        self.assertIsInstance(
            vif._build_vif_driver(self.adpt, 'host_uuid', mock_inst,
                                  {'type': 'pvm_sriov'}),
            vif.PvmVnicSriovVifDriver)

        # Test raises exception for no type
        self.assertRaises(exception.VirtualInterfacePlugException,
                          vif._build_vif_driver, self.adpt, 'host_uuid',
                          mock_inst, {})

        # Test an invalid vif type
        self.assertRaises(exception.VirtualInterfacePlugException,
                          vif._build_vif_driver, self.adpt, 'host_uuid',
                          mock_inst, {'type': 'bad'})

    @mock.patch('nova_powervm.virt.powervm.vif._build_vif_driver')
    def test_pre_live_migrate_at_source(self, mock_build_vif_drv):
        mock_drv = mock.MagicMock()
        mock_build_vif_drv.return_value = mock_drv
        mock_vif = mock.MagicMock()

        vif.pre_live_migrate_at_source(self.adpt, 'host_uuid', mock.Mock(),
                                       mock_vif)

        mock_drv.pre_live_migrate_at_source.assert_called_once_with(mock_vif)

    @mock.patch('nova_powervm.virt.powervm.vif._build_vif_driver')
    def test_rollback_live_migration_at_destination(self, mock_build_vif_drv):
        mock_build_vif_drv.return_value = mock_drv = mock.MagicMock()
        mock_vif, mappings = mock.MagicMock(), {}

        vif.rollback_live_migration_at_destination(
            self.adpt, 'host_uuid', mock.Mock(), mock_vif,
            mappings)

        rb = mock_drv.rollback_live_migration_at_destination
        rb.assert_called_once_with(mock_vif, mappings)

    @mock.patch('nova_powervm.virt.powervm.vif._build_vif_driver')
    def test_pre_live_migrate_at_destination(self, mock_build_vif_drv):
        mock_drv = mock.MagicMock()
        mock_build_vif_drv.return_value = mock_drv
        mock_vif = mock.MagicMock()

        vif.pre_live_migrate_at_destination(self.adpt, 'host_uuid',
                                            mock.Mock(), mock_vif, {})

        mock_drv.pre_live_migrate_at_destination.assert_called_once_with(
            mock_vif, {})

    @mock.patch('nova_powervm.virt.powervm.vif._build_vif_driver')
    def test_post_live_migrate_at_source(self, mock_build_vif_drv):
        mock_drv = mock.MagicMock()
        mock_build_vif_drv.return_value = mock_drv
        mock_vif = mock.MagicMock()

        vif.post_live_migrate_at_source(self.adpt, 'host_uuid', mock.Mock(),
                                        mock_vif)
        mock_drv.post_live_migrate_at_source.assert_called_once_with(mock_vif)


class TestVifSriovDriver(test.TestCase):

    def setUp(self):
        super(TestVifSriovDriver, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx()).adpt
        self.inst = mock.MagicMock()
        self.drv = vif.PvmVnicSriovVifDriver(self.adpt, 'host_uuid', self.inst)

    @mock.patch('pypowervm.wrappers.managed_system.System.get')
    def test_plug_no_pports(self, mock_sysget):
        """Raise when plug is called with a network with no physical ports."""
        sriov_adaps = [
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='loc1', label='foo'),
                mock.Mock(loc_code='loc2', label='')]),
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='loc3', label='bar'),
                mock.Mock(loc_code='loc4', label='foo')])]
        sys = mock.Mock(asio_config=mock.Mock(sriov_adapters=sriov_adaps))
        mock_sysget.return_value = [sys]
        self.assertRaises(exception.VirtualInterfacePlugException,
                          self.drv.plug, FakeDirectVif('net2', pports=[]), 1)

    @mock.patch('pypowervm.wrappers.iocard.VNIC.bld')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.tasks.sriov.set_vnic_back_devs')
    @mock.patch('pypowervm.wrappers.managed_system.System.get')
    def test_plug_no_physnet(self, mock_sysget, mock_back_devs, mock_pvm_uuid,
                             mock_vnic_bld):
        slot = 10
        pports = ['port1', 'port2']
        sriov_adaps = [
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port11', label='default'),
                mock.Mock(loc_code='port3', label='data1')]),
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port4', label='data2'),
                mock.Mock(loc_code='port22', label='default')])]
        sys = mock.Mock(asio_config=mock.Mock(sriov_adapters=sriov_adaps))
        mock_sysget.return_value = [sys]
        netapi.API = mock.Mock(return_value=FakeNetworkAPI('default'))
        self.drv.plug(FakeDirectVif('', pports=pports), slot)
        # Ensure back devs are created with pports from sriov_adaps and
        # not with what pports passed into plug method
        mock_back_devs.assert_called_once_with(
            mock_vnic_bld.return_value, ['port11', 'port22'], redundancy=3,
            capacity=None, check_port_status=True,
            sys_w=sys)

    @mock.patch('pypowervm.wrappers.iocard.VNIC.bld')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.tasks.sriov.set_vnic_back_devs')
    @mock.patch('pypowervm.wrappers.managed_system.System.get')
    def test_plug_no_matching_pports(self, mock_sysget, mock_back_devs,
                                     mock_pvm_uuid, mock_vnic_bld):
        slot = 10
        pports = ['port1', 'port2']
        sriov_adaps = [
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port1', label='data1'),
                mock.Mock(loc_code='port3', label='data1')]),
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port4', label='data2'),
                mock.Mock(loc_code='port2', label='data2')])]
        sys = mock.Mock(asio_config=mock.Mock(sriov_adapters=sriov_adaps))
        mock_sysget.return_value = [sys]
        netapi.API = mock.Mock(return_value=FakeNetworkAPI('default'))
        # Ensure Plug exception is raised when there are no matching pports
        # for physical network of corresponding neutron network
        self.assertRaises(exception.VirtualInterfacePlugException,
                          self.drv.plug,
                          FakeDirectVif('default', pports=pports), slot)

    @mock.patch('pypowervm.wrappers.iocard.VNIC.bld')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.tasks.sriov.set_vnic_back_devs')
    @mock.patch('pypowervm.wrappers.managed_system.System.get')
    def test_plug_bad_pports(self, mock_sysget, mock_back_devs, mock_pvm_uuid,
                             mock_vnic_bld):
        slot = 10
        pports = ['bad1', 'bad2']
        sriov_adaps = [
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port1', label='default'),
                mock.Mock(loc_code='port3', label='data1')]),
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port4', label='data2'),
                mock.Mock(loc_code='port2', label='default')])]
        sys = mock.Mock(asio_config=mock.Mock(sriov_adapters=sriov_adaps))
        mock_sysget.return_value = [sys]
        netapi.API = mock.Mock(return_value=FakeNetworkAPI('default'))
        self.drv.plug(FakeDirectVif('', pports=pports), slot)
        # Ensure back devs are created with correct pports belonging to same
        # physical network corresonding to neutron network
        mock_back_devs.assert_called_once_with(
            mock_vnic_bld.return_value, ['port1', 'port2'], redundancy=3,
            capacity=None, check_port_status=True,
            sys_w=sys)

    @mock.patch('pypowervm.wrappers.managed_system.System.get')
    @mock.patch('pypowervm.util.sanitize_mac_for_api')
    @mock.patch('pypowervm.wrappers.iocard.VNIC.bld')
    @mock.patch('pypowervm.tasks.sriov.set_vnic_back_devs')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug(self, mock_pvm_uuid, mock_back_devs, mock_vnic_bld,
                  mock_san_mac, mock_sysget):
        slot = 10
        pports = ['port1', 'port2']
        sriov_adaps = [
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port1', label='default'),
                mock.Mock(loc_code='port3', label='data1')]),
            mock.Mock(phys_ports=[
                mock.Mock(loc_code='port4', label='data2'),
                mock.Mock(loc_code='port2', label='default')])]
        sys = mock.Mock(asio_config=mock.Mock(sriov_adapters=sriov_adaps))
        mock_sysget.return_value = [sys]
        self.drv.plug(FakeDirectVif('default', pports=pports),
                      slot)
        mock_san_mac.assert_called_once_with('ab:ab:ab:ab:ab:ab')
        mock_vnic_bld.assert_called_once_with(
            self.drv.adapter, 79, slot_num=slot,
            mac_addr=mock_san_mac.return_value, allowed_vlans='NONE',
            allowed_macs='NONE')
        mock_back_devs.assert_called_once_with(
            mock_vnic_bld.return_value, ['port1', 'port2'], redundancy=3,
            capacity=None, check_port_status=True,
            sys_w=sys)
        mock_pvm_uuid.assert_called_once_with(self.drv.instance)
        mock_vnic_bld.return_value.create.assert_called_once_with(
            parent_type=pvm_lpar.LPAR, parent_uuid=mock_pvm_uuid.return_value)

        # Now with redundancy/capacity values from binding:profile
        mock_san_mac.reset_mock()
        mock_vnic_bld.reset_mock()
        mock_back_devs.reset_mock()
        mock_pvm_uuid.reset_mock()
        self.drv.plug(FakeDirectVif('default', pports=pports, cap=0.08),
                      slot)
        mock_san_mac.assert_called_once_with('ab:ab:ab:ab:ab:ab')
        mock_vnic_bld.assert_called_once_with(
            self.drv.adapter, 79, slot_num=slot,
            mac_addr=mock_san_mac.return_value, allowed_vlans='NONE',
            allowed_macs='NONE')
        mock_back_devs.assert_called_once_with(
            mock_vnic_bld.return_value, ['port1', 'port2'],
            redundancy=3, capacity=0.08, check_port_status=True,
            sys_w=sys)
        mock_pvm_uuid.assert_called_once_with(self.drv.instance)
        mock_vnic_bld.return_value.create.assert_called_once_with(
            parent_type=pvm_lpar.LPAR, parent_uuid=mock_pvm_uuid.return_value)

        # No-op with new_vif=False
        mock_san_mac.reset_mock()
        mock_vnic_bld.reset_mock()
        mock_back_devs.reset_mock()
        mock_pvm_uuid.reset_mock()
        self.assertIsNone(self.drv.plug(
            FakeDirectVif('default', pports=pports), slot, new_vif=False))
        mock_san_mac.assert_not_called()
        mock_vnic_bld.assert_not_called()
        mock_back_devs.assert_not_called()
        mock_pvm_uuid.assert_not_called()

    @mock.patch('pypowervm.wrappers.iocard.VNIC.search')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.util.sanitize_mac_for_api')
    def test_unplug(self, mock_san_mac, mock_pvm_uuid, mock_find):
        fvif = FakeDirectVif('default')
        self.assertEqual(mock_find.return_value, self.drv.unplug(fvif))
        mock_find.assert_called_once_with(
            self.drv.adapter, parent_type=pvm_lpar.LPAR,
            parent_uuid=mock_pvm_uuid.return_value,
            mac=mock_san_mac.return_value, one_result=True)
        mock_pvm_uuid.assert_called_once_with(self.inst)
        mock_san_mac.assert_called_once_with(fvif['address'])
        mock_find.return_value.delete.assert_called_once_with()

        # Not found
        mock_find.reset_mock()
        mock_pvm_uuid.reset_mock()
        mock_san_mac.reset_mock()
        mock_find.return_value = None
        self.assertIsNone(self.drv.unplug(fvif))
        mock_find.assert_called_once_with(
            self.drv.adapter, parent_type=pvm_lpar.LPAR,
            parent_uuid=mock_pvm_uuid.return_value,
            mac=mock_san_mac.return_value, one_result=True)
        mock_pvm_uuid.assert_called_once_with(self.inst)
        mock_san_mac.assert_called_once_with(fvif['address'])


class FakeDirectVif(dict):

    def __init__(self, physnet, pports=None, cap=None):
        self._physnet = physnet
        super(FakeDirectVif, self).__init__(
            network={'id': 'net_id'},
            address='ab:ab:ab:ab:ab:ab',
            details={
                'vlan': '79',
                'physical_ports': [],
                'redundancy': 3,
                'capacity': cap},
            profile={})
        if pports is not None:
            self['details']['physical_ports'] = pports

    def get_physical_network(self):
        return self._physnet


class TestVifSeaDriver(test.TestCase):

    def setUp(self):
        super(TestVifSeaDriver, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt
        self.inst = mock.MagicMock()
        self.drv = vif.PvmSeaVifDriver(self.adpt, 'host_uuid', self.inst)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.tasks.cna.crt_cna')
    def test_plug(self, mock_crt_cna, mock_pvm_uuid):
        """Tests that a VIF can be created."""

        # Set up the mocks
        fake_vif = {'network': {'meta': {'vlan': 5}},
                    'address': 'aabbccddeeff'}
        fake_slot_num = 5

        def validate_crt(adpt, host_uuid, lpar_uuid, vlan, mac_addr=None,
                         slot_num=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual(5, vlan)
            self.assertEqual('aabbccddeeff', mac_addr)
            self.assertEqual(5, slot_num)
            return pvm_net.CNA.bld(self.adpt, 5, host_uuid, slot_num=slot_num,
                                   mac_addr=mac_addr)
        mock_crt_cna.side_effect = validate_crt

        # Invoke
        resp = self.drv.plug(fake_vif, fake_slot_num)

        # Validate (along with validate method above)
        self.assertEqual(1, mock_crt_cna.call_count)
        self.assertIsNotNone(resp)
        self.assertIsInstance(resp, pvm_net.CNA)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.tasks.cna.crt_cna')
    def test_plug_from_neutron(self, mock_crt_cna, mock_pvm_uuid):
        """Tests that a VIF can be created.  Mocks Neutron net"""

        # Set up the mocks.  Look like Neutron
        fake_vif = {'details': {'vlan': 5}, 'network': {'meta': {}},
                    'address': 'aabbccddeeff'}
        fake_slot_num = 5

        def validate_crt(adpt, host_uuid, lpar_uuid, vlan, mac_addr=None,
                         slot_num=None):
            self.assertEqual('host_uuid', host_uuid)
            self.assertEqual(5, vlan)
            self.assertEqual('aabbccddeeff', mac_addr)
            self.assertEqual(5, slot_num)
            return pvm_net.CNA.bld(self.adpt, 5, host_uuid, slot_num=slot_num,
                                   mac_addr=mac_addr)
        mock_crt_cna.side_effect = validate_crt

        # Invoke
        resp = self.drv.plug(fake_vif, fake_slot_num)

        # Validate (along with validate method above)
        self.assertEqual(1, mock_crt_cna.call_count)
        self.assertIsNotNone(resp)
        self.assertIsInstance(resp, pvm_net.CNA)

    def test_plug_existing_vif(self):
        """Tests that a VIF need not be created."""

        # Set up the mocks
        fake_vif = {'network': {'meta': {'vlan': 5}},
                    'address': 'aabbccddeeff'}
        fake_slot_num = 5

        # Invoke
        resp = self.drv.plug(fake_vif, fake_slot_num, new_vif=False)

        self.assertIsNone(resp)

    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_unplug_vifs(self, mock_vm_get):
        """Tests that a delete of the vif can be done."""
        # Mock up the CNA response.  Two should already exist, the other
        # should not.
        cnas = [cna('AABBCCDDEEFF'), cna('AABBCCDDEE11'), cna('AABBCCDDEE22')]
        mock_vm_get.return_value = cnas

        # Run method.  The AABBCCDDEE11 wont' be unplugged (wasn't invoked
        # below) and the last unplug will also just no-op because its not on
        # the VM.
        self.drv.unplug({'address': 'aa:bb:cc:dd:ee:ff'})
        self.drv.unplug({'address': 'aa:bb:cc:dd:ee:22'})
        self.drv.unplug({'address': 'aa:bb:cc:dd:ee:33'})

        # The delete should have only been called once.  The second CNA didn't
        # have a matching mac...so it should be skipped.
        self.assertEqual(1, cnas[0].delete.call_count)
        self.assertEqual(0, cnas[1].delete.call_count)
        self.assertEqual(1, cnas[2].delete.call_count)


class TestVifLBDriver(test.TestCase):

    def setUp(self):
        super(TestVifLBDriver, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt
        self.inst = mock.MagicMock(uuid='inst_uuid')
        self.drv = vif.PvmLBVifDriver(self.adpt, 'host_uuid', self.inst)

    @mock.patch('nova.network.linux_net.LinuxBridgeInterfaceDriver.'
                'ensure_bridge')
    @mock.patch('nova.utils.execute')
    @mock.patch('nova.network.linux_net.create_ovs_vif_port')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                'get_trunk_dev_name')
    @mock.patch('pypowervm.tasks.cna.crt_p2p_cna')
    @mock.patch('pypowervm.tasks.partition.get_this_partition')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug(
            self, mock_pvm_uuid, mock_mgmt_lpar, mock_p2p_cna,
            mock_trunk_dev_name, mock_crt_ovs_vif_port, mock_exec,
            mock_ensure_bridge):
        # Mock the data
        mock_pvm_uuid.return_value = 'lpar_uuid'
        mock_mgmt_lpar.return_value = mock.Mock(uuid='mgmt_uuid')
        mock_trunk_dev_name.return_value = 'device'

        cna_w, trunk_wraps = mock.MagicMock(), [mock.MagicMock()]
        mock_p2p_cna.return_value = cna_w, trunk_wraps

        # Run the plug
        vif = {'network': {'bridge': 'br0'}, 'address': 'aa:bb:cc:dd:ee:ff',
               'id': 'vif_id', 'devname': 'tap_dev'}
        self.drv.plug(vif, 6)

        # Validate the calls
        mock_p2p_cna.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', ['mgmt_uuid'],
            'NovaLinkVEABridge', crt_vswitch=True,
            mac_addr='aa:bb:cc:dd:ee:ff', dev_name='tap_dev', slot_num=6)
        mock_exec.assert_called_once_with('ip', 'link', 'set', 'tap_dev', 'up',
                                          run_as_root=True)
        mock_ensure_bridge.assert_called_once_with('br0', 'tap_dev')

    @mock.patch('nova.network.linux_net.LinuxBridgeInterfaceDriver.'
                'ensure_bridge')
    def test_plug_existing_vif(
            self, mock_ensure_bridge):

        # Run the plug
        vif = {'network': {'bridge': 'br0'}, 'address': 'aa:bb:cc:dd:ee:ff',
               'id': 'vif_id', 'devname': 'tap_dev'}
        resp = self.drv.plug(vif, 6, new_vif=False)

        mock_ensure_bridge.assert_called_once_with('br0', 'tap_dev')
        self.assertIsNone(resp)

    @mock.patch('nova.utils.execute')
    @mock.patch('pypowervm.tasks.cna.find_trunks')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmLBVifDriver.'
                'get_trunk_dev_name')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmLBVifDriver.'
                '_find_cna_for_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_unplug(self, mock_get_cnas, mock_find_cna, mock_trunk_dev_name,
                    mock_find_trunks, mock_exec):
        # Set up the mocks
        mock_cna = mock.Mock()
        mock_get_cnas.return_value = [mock_cna, mock.Mock()]
        mock_find_cna.return_value = mock_cna

        t1 = mock.MagicMock()
        mock_find_trunks.return_value = [t1]

        mock_trunk_dev_name.return_value = 'fake_dev'

        # Call the unplug
        vif = {'address': 'aa:bb:cc:dd:ee:ff', 'network': {'bridge': 'br0'}}
        self.drv.unplug(vif)

        # The trunks and the cna should have been deleted
        self.assertTrue(t1.delete.called)
        self.assertTrue(mock_cna.delete.called)

        # Validate the execute
        call_ip = mock.call('ip', 'link', 'set', 'fake_dev', 'down',
                            run_as_root=True)
        call_delif = mock.call('brctl', 'delif', 'br0', 'fake_dev',
                               run_as_root=True)
        mock_exec.assert_has_calls([call_ip, call_delif])

        # Test unplug for the case where tap device
        # was not configured with the bridge.
        mock_exec.reset_mock()
        t1.reset_mock()
        mock_cna.reset_mock()
        mock_exec.side_effect = [None,
                                 exception.NovaException('Command error')]

        # Call the unplug
        self.drv.unplug(vif)

        # The trunks and the cna should have been deleted
        self.assertTrue(t1.delete.called)
        self.assertTrue(mock_cna.delete.called)

        # Validate the execute
        mock_exec.assert_has_calls([call_ip, call_delif])


class TestVifOvsDriver(test.TestCase):

    def setUp(self):
        super(TestVifOvsDriver, self).setUp()

        self.adpt = self.useFixture(pvm_fx.AdapterFx(
            traits=pvm_fx.LocalPVMTraits)).adpt
        self.inst = mock.MagicMock(uuid='inst_uuid')
        self.drv = vif.PvmOvsVifDriver(self.adpt, 'host_uuid', self.inst)

    @mock.patch('nova.utils.execute')
    @mock.patch('nova.network.linux_net.create_ovs_vif_port')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                'get_trunk_dev_name')
    @mock.patch('pypowervm.tasks.cna.crt_p2p_cna')
    @mock.patch('pypowervm.tasks.partition.get_this_partition')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_plug(self, mock_pvm_uuid, mock_mgmt_lpar, mock_p2p_cna,
                  mock_trunk_dev_name, mock_crt_ovs_vif_port, mock_exec):
        # Mock the data
        mock_pvm_uuid.return_value = 'lpar_uuid'
        mock_mgmt_lpar.return_value = mock.Mock(uuid='mgmt_uuid')
        mock_trunk_dev_name.return_value = 'device'

        cna_w, trunk_wraps = mock.MagicMock(), [mock.MagicMock()]
        mock_p2p_cna.return_value = cna_w, trunk_wraps

        # Run the plug
        network_model = model.Model({'bridge': 'br0', 'meta': {'mtu': 1450}})
        mock_vif = model.VIF(address='aa:bb:cc:dd:ee:ff', id='vif_id',
                             network=network_model)
        slot_num = 5
        self.drv.plug(mock_vif, slot_num)

        # Validate the calls
        mock_crt_ovs_vif_port.assert_called_once_with(
            'br0', 'device', 'vif_id', 'aa:bb:cc:dd:ee:ff',
            'inst_uuid', mtu=1450)
        mock_p2p_cna.assert_called_once_with(
            self.adpt, 'host_uuid', 'lpar_uuid', ['mgmt_uuid'],
            'NovaLinkVEABridge', crt_vswitch=True,
            mac_addr='aa:bb:cc:dd:ee:ff', slot_num=slot_num, dev_name='device')
        mock_exec.assert_called_with('ip', 'link', 'set', 'device', 'up',
                                     run_as_root=True)

    @mock.patch('nova.utils.execute')
    @mock.patch('nova.network.linux_net.create_ovs_vif_port')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                'get_trunk_dev_name')
    def test_plug_existing_vif(self, mock_trunk_dev_name,
                               mock_crt_ovs_vif_port, mock_exec):
        # Mock the data
        mock_trunk_dev_name.return_value = 'device'
        # Run the plug
        network_model = model.Model({'bridge': 'br0', 'meta': {'mtu': 1500}})
        mock_vif = model.VIF(address='aa:bb:cc:dd:ee:ff', id='vif_id',
                             network=network_model)
        slot_num = 5
        resp = self.drv.plug(mock_vif, slot_num, new_vif=False)

        # Validate the calls
        mock_crt_ovs_vif_port.assert_called_once_with(
            'br0', 'device', 'vif_id', 'aa:bb:cc:dd:ee:ff',
            'inst_uuid', mtu=1500)
        mock_exec.assert_called_with('ip', 'link', 'set', 'device', 'up',
                                     run_as_root=True)
        self.assertIsNone(resp)

    def test_get_trunk_dev_name(self):
        mock_vif = {'devname': 'tap_test', 'id': '1234567890123456'}

        # Test when the dev name is available
        self.assertEqual('tap_test', self.drv.get_trunk_dev_name(mock_vif))

        # And when it isn't.  Should also cut off a few characters from the id
        del mock_vif['devname']
        self.assertEqual('nic12345678901',
                         self.drv.get_trunk_dev_name(mock_vif))

    @mock.patch('pypowervm.tasks.cna.find_trunks')
    @mock.patch('nova.network.linux_net.delete_ovs_vif_port')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                'get_trunk_dev_name')
    @mock.patch('nova_powervm.virt.powervm.vif.PvmOvsVifDriver.'
                '_find_cna_for_vif')
    @mock.patch('nova_powervm.virt.powervm.vm.get_cnas')
    def test_unplug(self, mock_get_cnas, mock_find_cna, mock_trunk_dev_name,
                    mock_del_ovs_port, mock_find_trunks):
        # Set up the mocks
        mock_cna = mock.Mock()
        mock_get_cnas.return_value = [mock_cna, mock.Mock()]
        mock_find_cna.return_value = mock_cna

        t1, t2 = mock.MagicMock(), mock.MagicMock()
        mock_find_trunks.return_value = [t1, t2]

        mock_trunk_dev_name.return_value = 'fake_dev'

        # Call the unplug
        mock_vif = {'address': 'aa:bb:cc:dd:ee:ff',
                    'network': {'bridge': 'br-int'}}
        self.drv.unplug(mock_vif)

        # The trunks and the cna should have been deleted
        self.assertTrue(t1.delete.called)
        self.assertTrue(t2.delete.called)
        self.assertTrue(mock_cna.delete.called)

        # Validate the OVS port delete call was made
        mock_del_ovs_port.assert_called_with('br-int', 'fake_dev')

    @mock.patch('pypowervm.tasks.cna.find_trunks')
    @mock.patch('pypowervm.wrappers.network.CNA.search')
    @mock.patch('pypowervm.util.sanitize_mac_for_api')
    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    def test_pre_live_migrate_at_source(self, mock_pvm_uuid, mock_sanitize,
                                        mock_cna_search, mock_trunk_find):
        # Set up the mocks
        vif = {'address': 'aa:bb:cc:dd:ee:ff'}
        mock_sanitize.return_value = 'AABBCCDDEEFF'
        mock_trunk_find.return_value = 'trunk'
        mock_pvm_uuid.return_value = 'pvm_uuid'

        resp = self.drv.pre_live_migrate_at_source(vif)
        self.assertEqual(resp, 'trunk')

        # Make sure the APIs were called correctly
        mock_sanitize.assert_called_once_with(vif['address'])
        mock_cna_search.assert_called_once_with(
            self.adpt, parent_type=pvm_lpar.LPAR.schema_type,
            parent_uuid='pvm_uuid', one_result=True, mac='AABBCCDDEEFF')
        mock_trunk_find.assert_called_once_with(self.adpt, mock.ANY)

    @mock.patch('nova.network.linux_net.create_ovs_vif_port')
    @mock.patch('nova.utils.execute')
    @mock.patch('pypowervm.tasks.cna.crt_trunk_with_free_vlan')
    @mock.patch('pypowervm.tasks.partition.get_this_partition')
    def test_pre_live_migrate_at_destination(
            self, mock_part_get, mock_trunk_crt, mock_execute,
            mock_crt_ovs_port):
        # Mock the vif
        vif = {'devname': 'tap-dev', 'address': 'aa:bb:cc:dd:ee:ff',
               'network': {'bridge': 'br-int'}, 'id': 'vif_id'}

        # Mock out the management partition
        mock_mgmt_wrap = mock.MagicMock()
        mock_mgmt_wrap.uuid = 'mgmt_uuid'
        mock_part_get.return_value = mock_mgmt_wrap

        mock_trunk_crt.return_value = [mock.Mock(pvid=2)]

        # Invoke and test the basic response
        vea_vlan_mappings = {}
        self.drv.pre_live_migrate_at_destination(vif, vea_vlan_mappings)
        self.assertEqual(vea_vlan_mappings['aa:bb:cc:dd:ee:ff'], 2)

        # Now validate it called the things it needed to
        mock_execute.assert_called_once_with('ip', 'link', 'set', 'tap-dev',
                                             'up', run_as_root=True)
        mock_trunk_crt.assert_called_once_with(
            self.adpt, 'host_uuid', ['mgmt_uuid'],
            CONF.powervm.pvm_vswitch_for_novalink_io, dev_name='tap-dev')
        mock_crt_ovs_port.assert_called_once_with(
            'br-int', 'tap-dev', 'vif_id', 'aa:bb:cc:dd:ee:ff', self.inst.uuid)

    @mock.patch('nova.network.linux_net.delete_ovs_vif_port')
    @mock.patch('pypowervm.wrappers.network.CNA.get')
    @mock.patch('pypowervm.tasks.partition.get_this_partition')
    @mock.patch('pypowervm.wrappers.network.VSwitch.search')
    def test_rollback_live_migration_at_destination(
            self, mock_vs_search, mock_get_part, mock_cna_get,
            mock_delete_ovs_port):
        # All the fun mocking
        mock_vs_search.return_value = mock.MagicMock(switch_id=5)

        # Since this gets passed through conductor, the VLAN's switch to string
        # format.
        vea_vlan_mappings = {'aa:bb:cc:dd:ee:ff': '3',
                             'aa:bb:cc:dd:ee:ee': '4'}
        vif = {'devname': 'tap-dev', 'address': 'aa:bb:cc:dd:ee:ee',
               'network': {'bridge': 'br-int'}, 'id': 'vif_id'}

        mock_vio = mock.MagicMock(schema_type='VIO', uuid='uuid')
        mock_get_part.return_value = mock_vio

        trunk1 = mock.Mock(pvid=2, vswitch_id=3, trunk_pri=1)
        trunk2 = mock.Mock(pvid=3, vswitch_id=5, trunk_pri=1)
        trunk3 = mock.Mock(pvid=4, vswitch_id=5, trunk_pri=None)
        trunk4 = mock.Mock(pvid=4, vswitch_id=5, trunk_pri=1)
        mock_cna_get.return_value = [trunk1, trunk2, trunk3, trunk4]

        # Invoke
        self.drv.rollback_live_migration_at_destination(vif, vea_vlan_mappings)

        # Make sure the OVS port was deleted
        mock_delete_ovs_port.assert_called_once_with('br-int', 'tap-dev')

        # Make sure the trunk was deleted
        trunk4.delete.assert_called_once()

        # Now make sure the calls were done correctly to actually produce a
        # trunk adapter
        mock_vs_search.assert_called_once_with(
            self.drv.adapter, parent_type=pvm_ms.System, one_result=True,
            name=CONF.powervm.pvm_vswitch_for_novalink_io)
        mock_get_part.assert_called_once_with(self.drv.adapter)
        mock_cna_get.assert_called_once_with(
            self.drv.adapter, parent=mock_vio)

    @mock.patch('nova.network.linux_net.delete_ovs_vif_port')
    def test_post_live_migrate_at_source(self, mock_delete_ovs_port):
        # Mock the vif
        vif = {'devname': 'tap-dev', 'address': 'aa:bb:cc:dd:ee:ff',
               'network': {'bridge': 'br-int'}, 'id': 'vif_id'}
        # Invoke and test
        self.drv.post_live_migrate_at_source(vif)
        # Now validate it called the things it needed to
        mock_delete_ovs_port.assert_called_once_with('br-int', 'tap-dev')
