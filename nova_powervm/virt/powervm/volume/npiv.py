# Copyright 2015 IBM Corp.
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

from oslo_config import cfg
from oslo_log import log as logging
from taskflow import task

from nova.compute import task_states
from nova.i18n import _LI, _LW
from pypowervm.tasks import vfc_mapper as pvm_vfcm
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm.virt import powervm
from nova_powervm.virt.powervm import mgmt
from nova_powervm.virt.powervm.volume import driver as v_driver

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

WWPN_SYSTEM_METADATA_KEY = 'npiv_adpt_wwpns'
FABRIC_STATE_METADATA_KEY = 'fabric_state'
FS_UNMAPPED = 'unmapped'
FS_MGMT_MAPPED = 'mgmt_mapped'
FS_INST_MAPPED = 'inst_mapped'
TASK_STATES_FOR_DISCONNECT = [task_states.DELETING, task_states.SPAWNING]


class NPIVVolumeAdapter(v_driver.FibreChannelVolumeAdapter):
    """The NPIV implementation of the Volume Adapter.

    NPIV stands for N_Port ID Virtualization.  It is a means of providing
    more efficient connections between virtual machines and Fibre Channel
    backed SAN fabrics.

    From a management level, the main difference is that the Virtual Machine
    will have its own WWPNs and own Virtual FC adapter.  The Virtual I/O
    Server only passes through communication directly to the VM itself.
    """

    @classmethod
    def min_xags(cls):
        """List of pypowervm XAGs needed to support this adapter."""
        # Storage are so physical FC ports are available
        # FC mapping is for the connections between VIOS and client VM
        return [pvm_vios.VIOS.xags.FC_MAPPING, pvm_vios.VIOS.xags.STORAGE]

    def _connect_volume(self):
        """Connects the volume."""
        # Run the add for each fabric.
        for fabric in self._fabric_names():
            self._add_maps_for_fabric(fabric)

    def _disconnect_volume(self):
        """Disconnect the volume."""
        # We should only delete the NPIV mappings if we are running through a
        # VM deletion.  VM deletion occurs when the task state is deleting.
        # However, it can also occur during a 'roll-back' of the spawn.
        # Disconnect of the volumes will only be called during a roll back
        # of the spawn.
        if self.instance.task_state not in TASK_STATES_FOR_DISCONNECT:
            # NPIV should only remove the VFC mapping upon a destroy of the VM
            return

        # Run the disconnect for each fabric
        for fabric in self._fabric_names():
            self._remove_maps_for_fabric(fabric)

    def pre_live_migration_on_source(self, mig_data):
        """Performs pre live migration steps for the volume on the source host.

        Certain volume connectors may need to pass data from the source host
        to the target.  This may be required to determine how volumes connect
        through the Virtual I/O Servers.

        This method gives the volume connector an opportunity to update the
        mig_data (a dictionary) with any data that is needed for the target
        host during the pre-live migration step.

        Since the source host has no native pre_live_migration step, this is
        invoked from check_can_live_migrate_source in the overall live
        migration flow.

        :param mig_data: A dictionary that the method can update to include
                         data needed by the pre_live_migration_at_destination
                         method.
        """
        fabrics = self._fabric_names()
        vios_wraps = self.stg_ftsk.feed

        for fabric in fabrics:
            npiv_port_maps = self._get_fabric_meta(fabric)
            if not npiv_port_maps:
                continue

            client_slots = []
            for port_map in npiv_port_maps:
                vfc_map = pvm_vfcm.find_vios_for_vfc_wwpns(
                    vios_wraps, port_map[1].split())[1]
                client_slots.append(vfc_map.client_adapter.slot_number)

            # Set the client slots into the fabric data to pass to the
            # destination.
            mig_data['npiv_fabric_slots_%s' % fabric] = client_slots

    def pre_live_migration_on_destination(self, src_mig_data, dest_mig_data):
        """Perform pre live migration steps for the volume on the target host.

        This method performs any pre live migration that is needed.

        Certain volume connectors may need to pass data from the source host
        to the target.  This may be required to determine how volumes connect
        through the Virtual I/O Servers.

        This method will be called after the pre_live_migration_on_source
        method.  The data from the pre_live call will be passed in via the
        mig_data.  This method should put its output into the dest_mig_data.

        :param src_mig_data: The migration data from the source server.
        :param dest_mig_data: The migration data for the destination server.
                              If the volume connector needs to provide
                              information to the live_migration command, it
                              should be added to this dictionary.
        """
        vios_wraps = self.stg_ftsk.feed
        mgmt_uuid = mgmt.get_mgmt_partition(self.adapter).uuid

        # Each mapping should attempt to remove itself from the management
        # partition.
        for fabric in self._fabric_names():
            npiv_port_maps = self._get_fabric_meta(fabric)

            # Need to first derive the port mappings that can be passed back
            # to the source system for the live migration call.  This tells
            # the source system what 'vfc mappings' to pass in on the live
            # migration command.
            slots = src_mig_data['npiv_fabric_slots_%s' % fabric]
            fabric_mapping = pvm_vfcm.build_migration_mappings_for_fabric(
                vios_wraps, self._fabric_ports(fabric), slots)
            dest_mig_data['npiv_fabric_mapping_%s' % fabric] = fabric_mapping

            # Next we need to remove the mappings off the mgmt partition.
            for npiv_port_map in npiv_port_maps:
                ls = [LOG.info, _LI("Removing mgmt NPIV mapping for instance "
                                    "%(inst)s for fabric %(fabric)s."),
                      {'inst': self.instance.name, 'fabric': fabric}]
                vios_w, vfc_map = pvm_vfcm.find_vios_for_vfc_wwpns(
                    vios_wraps, npiv_port_map[1].split())

                if vios_w is not None:
                    # Add the subtask to remove the mapping from the management
                    # partition.
                    task_wrapper = self.stg_ftsk.wrapper_tasks[vios_w.uuid]
                    task_wrapper.add_functor_subtask(
                        pvm_vfcm.remove_maps, mgmt_uuid,
                        client_adpt=vfc_map.client_adapter, logspec=ls)
                else:
                    LOG.warn(_LW("No storage connections found between the "
                                 "Virtual I/O Servers and FC Fabric "
                                 "%(fabric)s. The connection might be removed "
                                 "already."), {'fabric': fabric})

        # TODO(thorst) Find a better place for this execute.  Works for now
        # as the stg_ftsk is all local.  Also won't do anything if there
        # happen to be no fabric changes.
        self.stg_ftsk.execute()

        # Collate all of the individual fabric mappings into a single element.
        full_map = []
        for key, value in dest_mig_data.items():
            if key.startswith('npiv_fabric_mapping_'):
                full_map.extend(value)
        dest_mig_data['vfc_lpm_mappings'] = full_map

    def post_live_migration_at_destination(self, mig_vol_stor):
        """Perform post live migration steps for the volume on the target host.

        This method performs any post live migration that is needed.  Is not
        required to be implemented.

        :param mig_vol_stor: An unbounded dictionary that will be passed to
                             each volume adapter during the post live migration
                             call.  Adapters can store data in here that may
                             be used by subsequent volume adapters.
        """
        vios_wraps = self.stg_ftsk.feed

        # This method will run on the target host after the migration is
        # completed.  Right after this the instance.save is invoked from the
        # manager.  Given that, we need to update the order of the WWPNs.
        # The first WWPN is the one that is logged into the fabric and this
        # will now indicate that our WWPN is logged in.
        for fabric in self._fabric_names():
            # We check the mig_vol_stor to see if this fabric has already been
            # flipped.  If so, we can continue.
            fabric_key = '%s_flipped' % fabric
            if mig_vol_stor.get(fabric_key, False):
                continue

            # Must not be flipped, so execute the flip
            npiv_port_maps = self._get_fabric_meta(fabric)
            new_port_maps = []
            for port_map in npiv_port_maps:
                # Flip the WPWNs
                c_wwpns = port_map[1].split()
                c_wwpns.reverse()

                # Get the new physical WWPN.
                vfc_map = pvm_vfcm.find_vios_for_vfc_wwpns(vios_wraps,
                                                           c_wwpns)[1]
                p_wwpn = vfc_map.backing_port.wwpn

                # Build the new map.
                new_map = (p_wwpn, " ".join(c_wwpns))
                new_port_maps.append(new_map)
            self._set_fabric_meta(fabric, new_port_maps)

            # Store that this fabric is now flipped.
            mig_vol_stor[fabric_key] = True

    def _is_initial_wwpn(self, fc_state, fabric):
        """Determines if the invocation to wwpns is for a general method.

        A 'general' method would be a spawn (with a volume) or a volume attach
        or detach.

        :param fc_state: The state of the fabric.
        :param fabric: The name of the fabric.
        :return: True if the invocation appears to be for a spawn/volume
                 action. False otherwise.
        """
        if (fc_state == FS_UNMAPPED and
            self.instance.task_state not in [task_states.DELETING,
                                             task_states.MIGRATING]):
            LOG.info(_LI("Mapping instance %(inst)s to the mgmt partition for "
                         "fabric %(fabric)s because the VM does not yet have "
                         "a valid vFC device."),
                     {'inst': self.instance.name, 'fabric': fabric})
            return True

        return False

    def _is_migration_wwpn(self, fc_state):
        """Determines if the WWPN call is occurring during a migration.

        This determines if it is on the target host.

        :param fc_state: The fabrics state.
        :return: True if the instance appears to be migrating to this host.
                 False otherwise.
        """
        return (fc_state == FS_INST_MAPPED and
                self.instance.task_state == task_states.MIGRATING and
                self.instance.host != CONF.host)

    def _configure_wwpns_for_migration(self, fabric):
        """Configures the WWPNs for a migration.

        During a NPIV migration, the WWPNs need to be flipped and attached to
        the management VM.  This is so that the peer WWPN is brought online.

        The WWPNs will be removed from the management partition via the
        pre_live_migration_on_destination method.  The WWPNs invocation is
        done prior to the migration, when the volume connector is gathered.

        :param fabric: The fabric to configure.
        :return: An updated port mapping.
        """
        LOG.info(_LI("Mapping instance %(inst)s to the mgmt partition for "
                     "fabric %(fabric)s because the VM is migrating to "
                     "this host."),
                 {'inst': self.instance.name, 'fabric': fabric})

        mgmt_uuid = mgmt.get_mgmt_partition(self.adapter).uuid

        # When we migrate...flip the WWPNs around.  This is so the other
        # WWPN logs in on the target fabric.  But we should only flip new
        # WWPNs.  There may already be some on the overall fabric...and if
        # there are, we keep those 'as-is'
        #
        # TODO(thorst) pending API change should be able to indicate which
        # wwpn is active.
        port_maps = self._get_fabric_meta(fabric)
        existing_wwpns = []
        new_wwpns = []

        for port_map in port_maps:
            c_wwpns = port_map[1].split()

            # Only add it as a 'new' mapping if it isn't on a VIOS already.  If
            # it is, then we know that it has already been serviced, perhaps
            # by a previous volume.
            vfc_map = pvm_vfcm.has_client_wwpns(self.stg_ftsk.feed, c_wwpns)[1]
            if vfc_map is None:
                c_wwpns.reverse()
                new_wwpns.extend(c_wwpns)
            else:
                existing_wwpns.extend(c_wwpns)

        # Now derive the mapping to THESE VIOSes physical ports
        port_mappings = pvm_vfcm.derive_npiv_map(
            self.stg_ftsk.feed, self._fabric_ports(fabric),
            new_wwpns + existing_wwpns)

        # Add the port maps to the mgmt partition
        if len(new_wwpns) > 0:
            pvm_vfcm.add_npiv_port_mappings(
                self.adapter, self.host_uuid, mgmt_uuid, port_mappings)
        return port_mappings

    def wwpns(self):
        """Builds the WWPNs of the adapters that will connect the ports."""
        vios_wraps, mgmt_uuid = None, None
        resp_wwpns = []

        # If this is a new mapping altogether, the WWPNs need to be logged
        # into the fabric so that Cinder can make use of them.  This is a bit
        # of a catch-22 because the LPAR doesn't exist yet.  So a mapping will
        # be created against the mgmt partition and then upon VM creation, the
        # mapping will be moved over to the VM.
        #
        # If a mapping already exists, we can instead just pull the data off
        # of the system metadata from the nova instance.
        for fabric in self._fabric_names():
            fc_state = self._get_fabric_state(fabric)
            LOG.info(_LI("NPIV wwpns fabric state=%(st)s for "
                         "instance %(inst)s") %
                     {'st': fc_state, 'inst': self.instance.name})

            if self._is_initial_wwpn(fc_state, fabric):
                # At this point we've determined that we need to do a mapping.
                # So we go and obtain the mgmt uuid and the VIOS wrappers.
                # We only do this for the first loop through so as to ensure
                # that we do not keep invoking these expensive calls
                # unnecessarily.
                if mgmt_uuid is None:
                    mgmt_uuid = mgmt.get_mgmt_partition(self.adapter).uuid

                    # The VIOS wrappers are also not set at this point.  Seed
                    # them as well.  Will get reused on subsequent loops.
                    vios_wraps = self.stg_ftsk.feed

                # Derive the virtual to physical port mapping
                port_maps = pvm_vfcm.derive_base_npiv_map(
                    vios_wraps, self._fabric_ports(fabric),
                    self._ports_per_fabric())

                # Every loop through, we reverse the vios wrappers.  This is
                # done so that if Fabric A only has 1 port, it goes on the
                # first VIOS.  Then Fabric B would put its port on a different
                # VIOS.  As a form of multi pathing (so that your paths were
                # not restricted to a single VIOS).
                vios_wraps.reverse()

                # Check if the fabrics are unmapped then we need to map it
                # temporarily with the management partition.
                LOG.info(_LI("Adding NPIV Mapping with mgmt partition for "
                             "instance %s") % self.instance.name)
                port_maps = pvm_vfcm.add_npiv_port_mappings(
                    self.adapter, self.host_uuid, mgmt_uuid, port_maps)

                # Set the fabric meta (which indicates on the instance how
                # the fabric is mapped to the physical port) and the fabric
                # state.
                self._set_fabric_meta(fabric, port_maps)
                self._set_fabric_state(fabric, FS_MGMT_MAPPED)
            elif self._is_migration_wwpn(fc_state):
                port_maps = self._configure_wwpns_for_migration(fabric)

                # This won't actually get saved by the process.  The save will
                # only occur after the 'post migration'.  But if there are
                # multiple volumes, their WWPNs calls will subsequently see
                # the data saved temporarily here.
                self._set_fabric_meta(fabric, port_maps)
            else:
                # This specific fabric had been previously set.  Just pull
                # from the meta (as it is likely already mapped to the
                # instance)
                port_maps = self._get_fabric_meta(fabric)

            # Port map is set by either conditional, but may be set to None.
            # If not None, then add the WWPNs to the response.
            if port_maps is not None:
                for mapping in port_maps:
                    # Only add the first WWPN.  That is the one that will be
                    # logged into the fabric.
                    resp_wwpns.append(mapping[1].split()[0])

        # The return object needs to be a list for the volume connector.
        return resp_wwpns

    def _add_maps_for_fabric(self, fabric):
        """Adds the vFC storage mappings to the VM for a given fabric.

        Will check if the Fabric is mapped to the management partition.  If it
        is, then it will remove the mappings and update the fabric state. This
        is because, in order for the WWPNs to be on the fabric (for Cinder)
        before the VM is online, the WWPNs get mapped to the management
        partition.

        This method will remove from the management partition (if needed), and
        then assign it to the instance itself.

        :param fabric: The fabric to add the mappings to.
        """
        npiv_port_maps = self._get_fabric_meta(fabric)
        vios_wraps = self.stg_ftsk.feed

        # If currently mapped to the mgmt partition, remove the mappings so
        # that they can be added to the client.
        if self._get_fabric_state(fabric) == FS_MGMT_MAPPED:
            mgmt_uuid = mgmt.get_mgmt_partition(self.adapter).uuid

            # Each port mapping should be removed from the VIOS.
            for npiv_port_map in npiv_port_maps:
                vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps,
                                                         npiv_port_map)
                ls = [LOG.info, _LI("Removing NPIV mapping for mgmt partition "
                                    "for instance %(inst)s on VIOS %(vios)s."),
                      {'inst': self.instance.name, 'vios': vios_w.name}]

                # Add the subtask to remove the map from the mgmt partition
                self.stg_ftsk.wrapper_tasks[vios_w.uuid].add_functor_subtask(
                    pvm_vfcm.remove_maps, mgmt_uuid, port_map=npiv_port_map,
                    logspec=ls)

        # This loop adds the maps from the appropriate VIOS to the client VM
        for npiv_port_map in npiv_port_maps:
            vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps, npiv_port_map)
            ls = [LOG.info, _LI("Adding NPIV mapping for instance %(inst)s "
                                "for Virtual I/O Server %(vios)s."),
                  {'inst': self.instance.name, 'vios': vios_w.name}]

            # Add the subtask to add the specific map.
            self.stg_ftsk.wrapper_tasks[vios_w.uuid].add_functor_subtask(
                pvm_vfcm.add_map, self.host_uuid, self.vm_uuid, npiv_port_map,
                logspec=ls)

        # After all the mappings, make sure the fabric state is updated.
        def set_state():
            self._set_fabric_state(fabric, FS_INST_MAPPED)
        volume_id = self.connection_info['data']['volume_id']
        self.stg_ftsk.add_post_execute(task.FunctorTask(
            set_state, name='fab_%s_%s' % (fabric, volume_id)))

    def _remove_maps_for_fabric(self, fabric):
        """Removes the vFC storage mappings from the VM for a given fabric.

        :param fabric: The fabric to remove the mappings from.
        """
        npiv_port_maps = self._get_fabric_meta(fabric)
        if not npiv_port_maps:
            # If no mappings exist, exit out of the method.
            return

        vios_wraps = self.stg_ftsk.feed

        for npiv_port_map in npiv_port_maps:
            ls = [LOG.info, _LI("Removing a NPIV mapping for instance "
                                "%(inst)s for fabric %(fabric)s."),
                  {'inst': self.instance.name, 'fabric': fabric}]
            vios_w = pvm_vfcm.find_vios_for_port_map(vios_wraps, npiv_port_map)

            if vios_w is not None:
                # Add the subtask to remove the specific map
                task_wrapper = self.stg_ftsk.wrapper_tasks[vios_w.uuid]
                task_wrapper.add_functor_subtask(
                    pvm_vfcm.remove_maps, self.vm_uuid,
                    port_map=npiv_port_map, logspec=ls)
            else:
                LOG.warn(_LW("No storage connections found between the "
                             "Virtual I/O Servers and FC Fabric %(fabric)s."),
                         {'fabric': fabric})

    def host_name(self):
        """Derives the host name that should be used for the storage device.

        :return: The host name.
        """
        host = CONF.host if len(CONF.host) < 20 else CONF.host[:20]
        return host + '_' + self.instance.name

    def _set_fabric_state(self, fabric, state):
        """Sets the fabric state into the instance's system metadata.
        :param fabric: The name of the fabric
        :param state: state of the fabric which needs to be set

         Possible Valid States:
         FS_UNMAPPED: Initial state unmapped.
         FS_MGMT_MAPPED: Fabric is mapped with the management partition
         FS_INST_MAPPED: Fabric is mapped with the nova instance.
        """
        meta_key = self._sys_fabric_state_key(fabric)
        LOG.info(_LI("Setting Fabric state=%(st)s for instance=%(inst)s") %
                 {'st': state, 'inst': self.instance.name})
        self.instance.system_metadata[meta_key] = state

    def _get_fabric_state(self, fabric):
        """Gets the fabric state from the instance's system metadata.

        :param fabric: The name of the fabric
        :return: The state of the fabric which needs to be set

         Possible Valid States:
         FS_UNMAPPED: Initial state unmapped.
         FS_MGMT_MAPPED: Fabric is mapped with the management partition
         FS_INST_MAPPED: Fabric is mapped with the nova instance.
        """
        meta_key = self._sys_fabric_state_key(fabric)
        if self.instance.system_metadata.get(meta_key) is None:
            self.instance.system_metadata[meta_key] = FS_UNMAPPED

        return self.instance.system_metadata[meta_key]

    def _sys_fabric_state_key(self, fabric):
        """Returns the nova system metadata key for a given fabric."""
        return FABRIC_STATE_METADATA_KEY + '_' + fabric

    def _set_fabric_meta(self, fabric, port_map):
        """Sets the port map into the instance's system metadata.

        The system metadata will store per-fabric port maps that link the
        physical ports to the virtual ports.  This is needed for the async
        nature between the wwpns call (get_volume_connector) and the
        connect_volume (spawn).

        :param fabric: The name of the fabric.
        :param port_map: The port map (as defined via the derive_npiv_map
                         pypowervm method).
        """

        # We will store the metadata in comma-separated strings with up to 4
        # three-token pairs. Each set of three comprises the Physical Port
        # WWPN followed by the two Virtual Port WWPNs:
        # Ex:
        # npiv_wwpn_adpt_A:
        #     "p_wwpn1,v_wwpn1,v_wwpn2,p_wwpn2,v_wwpn3,v_wwpn4,..."
        # npiv_wwpn_adpt_A_2:
        #     "p_wwpn5,v_wwpn9,vwwpn_10,p_wwpn6,..."

        meta_elems = []
        for p_wwpn, v_wwpn in port_map:
            meta_elems.append(p_wwpn)
            meta_elems.extend(v_wwpn.split())

        LOG.info(_LI("Fabric %(fabric)s wwpn metadata will be set to "
                     "%(meta)s for instance %(inst)s"),
                 {'fabric': fabric, 'meta': ",".join(meta_elems),
                  'inst': self.instance.name})

        fabric_id_iter = 1
        meta_key = self._sys_meta_fabric_key(fabric)
        key_len = len(meta_key)
        num_keys = self._get_num_keys(port_map)

        for key in range(num_keys):
            start_elem = 12 * (fabric_id_iter - 1)
            meta_value = ",".join(meta_elems[start_elem:start_elem + 12])
            self.instance.system_metadata[meta_key] = meta_value
            # If this is not the first time through, replace the end else cat
            if fabric_id_iter > 1:
                fabric_id_iter += 1
                meta_key = meta_key.replace(meta_key[key_len:],
                                            "_%s" % fabric_id_iter)
            else:
                fabric_id_iter += 1
                meta_key = meta_key + "_%s" % fabric_id_iter

    def _get_fabric_meta(self, fabric):
        """Gets the port map from the instance's system metadata.

        See _set_fabric_meta.

        :param fabric: The name of the fabric.
        :return: The port map (as defined via the derive_npiv_map pypowervm
                 method.
        """
        meta_key = self._sys_meta_fabric_key(fabric)

        if self.instance.system_metadata.get(meta_key) is None:
            # If no mappings exist, log a warning.
            LOG.warn(_LW("No NPIV mappings exist for instance %(inst)s on "
                         "fabric %(fabric)s.  May not have connected to "
                         "the fabric yet or fabric configuration was recently "
                         "modified."),
                     {'inst': self.instance.name, 'fabric': fabric})
            return []

        wwpns = self.instance.system_metadata[meta_key]
        key_len = len(meta_key)
        iterator = 2
        meta_key = meta_key + "_" + str(iterator)
        while self.instance.system_metadata.get(meta_key) is not None:
            meta_value = self.instance.system_metadata[meta_key]
            wwpns += "," + meta_value
            iterator += 1
            meta_key = meta_key.replace(meta_key[key_len:],
                                        "_" + str(iterator))

        wwpns = wwpns.split(",")

        # Rebuild the WWPNs into the natural structure.
        return [(p, ' '.join([v1, v2])) for p, v1, v2
                in zip(wwpns[::3], wwpns[1::3], wwpns[2::3])]

    def _sys_meta_fabric_key(self, fabric):
        """Returns the nova system metadata key for a given fabric."""
        return WWPN_SYSTEM_METADATA_KEY + '_' + fabric

    def _fabric_names(self):
        """Returns a list of the fabric names."""
        return powervm.NPIV_FABRIC_WWPNS.keys()

    def _fabric_ports(self, fabric_name):
        """Returns a list of WWPNs for the fabric's physical ports."""
        return powervm.NPIV_FABRIC_WWPNS[fabric_name]

    def _ports_per_fabric(self):
        """Returns the number of virtual ports that should be used per fabric.
        """
        return CONF.powervm.ports_per_fabric

    def _get_num_keys(self, port_map):
        """Returns the number of keys we need to generate"""
        # Keys will have up to 4 mapping pairs so we determine based on that
        if len(port_map) % 4 > 0:
            return int(len(port_map) / 4 + 1)
        else:
            return int(len(port_map) / 4)
