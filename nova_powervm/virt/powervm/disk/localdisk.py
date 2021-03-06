# Copyright 2013 OpenStack Foundation
# Copyright 2015, 2016 IBM Corp.
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

from oslo_concurrency import lockutils
from oslo_log import log as logging

from nova import exception as nova_exc
from nova import image

from pypowervm import const as pvm_const
from pypowervm import exceptions as pvm_exc
from pypowervm.tasks import partition as pvm_tpar
from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.tasks import storage as tsk_stg
from pypowervm.wrappers import storage as pvm_stg
from pypowervm.wrappers import virtual_io_server as pvm_vios

from nova_powervm import conf as cfg
from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm.disk import imagecache
from nova_powervm.virt.powervm import exception as npvmex
from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm import vm


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
IMAGE_API = image.API()


class LocalStorage(disk_dvr.DiskAdapter):

    capabilities = {
        'shared_storage': False,
        'has_imagecache': True,
    }

    def __init__(self, adapter, host_uuid):
        super(LocalStorage, self).__init__(adapter, host_uuid)

        # Query to get the Volume Group UUID
        if not CONF.powervm.volume_group_name:
            raise npvmex.OptRequiredIfOtherOptValue(
                if_opt='disk_driver', if_value='localdisk',
                then_opt='volume_group_name')
        self.vg_name = CONF.powervm.volume_group_name
        self._vios_uuid, self.vg_uuid = tsk_stg.find_vg(self.vg_name)
        self.image_cache_mgr = imagecache.ImageManager(self._vios_uuid,
                                                       self.vg_uuid, adapter)
        self.cache_lock = lockutils.ReaderWriterLock()
        LOG.info(_LI("Local Storage driver initialized: volume group: '%s'"),
                 self.vg_name)

    @property
    def vios_uuids(self):
        """List the UUIDs of the Virtual I/O Servers hosting the storage.

        For localdisk, there's only one.
        """
        return [self._vios_uuid]

    def disk_match_func(self, disk_type, instance):
        """Return a matching function to locate the disk for an instance.

        :param disk_type: One of the DiskType enum values.
        :param instance: The instance whose disk is to be found.
        :return: Callable suitable for the match_func parameter of the
                 pypowervm.tasks.scsi_mapper.find_maps method, with the
                 following specification:
            def match_func(storage_elem)
                param storage_elem: A backing storage element wrapper (VOpt,
                                    VDisk, PV, or LU) to be analyzed.
                return: True if the storage_elem's mapping should be included;
                        False otherwise.
        """
        disk_name = self._get_disk_name(disk_type, instance, short=True)
        return tsk_map.gen_match_func(pvm_stg.VDisk, names=[disk_name])

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes."""
        vg_wrap = self._get_vg_wrap()

        return float(vg_wrap.capacity)

    @property
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used."""
        vg_wrap = self._get_vg_wrap()

        # Subtract available from capacity
        return float(vg_wrap.capacity) - float(vg_wrap.available_size)

    def manage_image_cache(self, context, all_instances):
        """Update the image cache

        :param context: nova context
        :param all_instances: List of all instances on the node
        """
        with self.cache_lock.write_lock():
            self.image_cache_mgr.update(context, all_instances)

    def delete_disks(self, storage_elems):
        """Removes the specified disks.

        :param storage_elems: A list of the storage elements that are to be
                              deleted.  Derived from the return value from
                              disconnect_disk.
        """
        # All of local disk is done against the volume group.  So reload
        # that (to get new etag) and then update against it.
        tsk_stg.rm_vg_storage(self._get_vg_wrap(), vdisks=storage_elems)

    def disconnect_disk(self, instance, stg_ftsk=None, disk_type=None):
        """Disconnects the storage adapters from the image disk.

        :param instance: instance to disconnect the image for.
        :param stg_ftsk: (Optional) The pypowervm transaction FeedTask for the
                         I/O Operations.  If provided, the Virtual I/O Server
                         mapping updates will be added to the FeedTask.  This
                         defers the updates to some later point in time.  If
                         the FeedTask is not provided, the updates will be run
                         immediately when this method is executed.
        :param disk_type: The list of disk types to remove or None which means
                          to remove all disks from the VM.
        :return: A list of all the backing storage elements that were
                 disconnected from the I/O Server and VM.
        """
        lpar_uuid = vm.get_pvm_uuid(instance)

        # Ensure we have a transaction manager.
        if stg_ftsk is None:
            stg_ftsk = pvm_tpar.build_active_vio_feed_task(
                self.adapter, name='localdisk', xag=[pvm_const.XAG.VIO_SMAP])

        # Build the match function
        match_func = tsk_map.gen_match_func(pvm_stg.VDisk, prefixes=disk_type)

        # Make sure the remove function will run within the transaction manager
        def rm_func(vios_w):
            LOG.info(_LI("Disconnecting instance %(inst)s from storage "
                         "disks."), {'inst': instance.name})
            return tsk_map.remove_maps(vios_w, lpar_uuid,
                                       match_func=match_func)

        stg_ftsk.wrapper_tasks[self._vios_uuid].add_functor_subtask(rm_func)

        # Find the disk directly.
        vios_w = stg_ftsk.wrapper_tasks[self._vios_uuid].wrapper
        mappings = tsk_map.find_maps(vios_w.scsi_mappings,
                                     client_lpar_id=lpar_uuid,
                                     match_func=match_func)

        # Run the transaction manager if built locally.  Must be done after
        # the find to make sure the mappings were found previously.
        if stg_ftsk.name == 'localdisk':
            stg_ftsk.execute()

        return [x.backing_storage for x in mappings]

    def disconnect_disk_from_mgmt(self, vios_uuid, disk_name):
        """Disconnect a disk from the management partition.

        :param vios_uuid: The UUID of the Virtual I/O Server serving the
                          mapping.
        :param disk_name: The name of the disk to unmap.
        """
        tsk_map.remove_vdisk_mapping(self.adapter, vios_uuid, self.mp_uuid,
                                     disk_names=[disk_name])
        LOG.info(_LI(
            "Unmapped boot disk %(disk_name)s from the management partition "
            "from Virtual I/O Server %(vios_name)s."), {
                'disk_name': disk_name, 'mp_uuid': self.mp_uuid,
                'vios_name': vios_uuid})

    def _create_disk_from_image(self, context, instance, image_meta, disk_size,
                                image_type=disk_dvr.DiskType.BOOT):
        """Creates a disk and copies the specified image to it.

        Cleans up created disk if an error occurs.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param disk_size: The size of the disk to create in GB.  If smaller
                          than the image, it will be ignored (as the disk
                          must be at least as big as the image).  Must be an
                          int.
        :param image_type: the image type. See disk constants above.
        :return: The backing pypowervm storage object that was created.
        """
        LOG.info(_LI('Create disk.'), instance=instance)

        # Disk size to API is in bytes.  Input from method is in Gb
        disk_bytes = self._disk_gb_to_bytes(disk_size, floor=image_meta.size)
        vol_name = self._get_disk_name(image_type, instance, short=True)

        with self.cache_lock.read_lock():
            img_udid = self._get_or_upload_image(context, image_meta)
            # Transfer the image
            return tsk_stg.crt_copy_vdisk(
                self.adapter, self._vios_uuid, self.vg_uuid, img_udid,
                image_meta.size, vol_name, disk_bytes)

    def _get_or_upload_image(self, context, image_meta):
        """Return a cached image name

        Attempt to find a cached copy of the image. If there is no cached copy
        of the image, create one.

        :param context: nova context used to retrieve image from glance
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :return: The name of the virtual disk containing the image
        """

        # Check for cached image
        with lockutils.lock(image_meta.id):
            vg_wrap = self._get_vg_wrap()
            cache_name = self.get_name_by_uuid(disk_dvr.DiskType.IMAGE,
                                               image_meta.id, short=True)
            image = [disk for disk in vg_wrap.virtual_disks
                     if disk.name == cache_name]
            if len(image) == 1:
                return image[0].udid

            def upload(path):
                IMAGE_API.download(context, image_meta.id, dest_path=path)

            image = tsk_stg.upload_new_vdisk(
                self.adapter, self._vios_uuid, self.vg_uuid, upload,
                cache_name, image_meta.size, d_size=image_meta.size,
                upload_type=tsk_stg.UploadType.FUNC)[0]
            return image.udid

    def connect_disk(self, instance, disk_info, stg_ftsk=None):
        """Connects the disk image to the Virtual Machine.

        :param instance: nova instance to connect the disk to.
        :param disk_info: The pypowervm storage element returned from
                          create_disk_from_image.  Ex. VOptMedia, VDisk, LU,
                          or PV.
        :param stg_ftsk: (Optional) The pypowervm transaction FeedTask for the
                         I/O Operations.  If provided, the Virtual I/O Server
                         mapping updates will be added to the FeedTask.  This
                         defers the updates to some later point in time.  If
                         the FeedTask is not provided, the updates will be run
                         immediately when this method is executed.
        """
        lpar_uuid = vm.get_pvm_uuid(instance)

        # Ensure we have a transaction manager.
        if stg_ftsk is None:
            stg_ftsk = pvm_tpar.build_active_vio_feed_task(
                self.adapter, name='localdisk', xag=[pvm_const.XAG.VIO_SMAP])

        def add_func(vios_w):
            LOG.info(_LI("Adding logical volume disk connection between VM "
                         "%(vm)s and VIOS %(vios)s."),
                     {'vm': instance.name, 'vios': vios_w.name})
            mapping = tsk_map.build_vscsi_mapping(
                self.host_uuid, vios_w, lpar_uuid, disk_info)
            return tsk_map.add_map(vios_w, mapping)

        stg_ftsk.wrapper_tasks[self._vios_uuid].add_functor_subtask(add_func)

        # Run the transaction manager if built locally.
        if stg_ftsk.name == 'localdisk':
            stg_ftsk.execute()

    def extend_disk(self, instance, disk_info, size):
        """Extends the disk.

        :param instance: instance to extend the disk for.
        :param disk_info: dictionary with disk info.
        :param size: the new size in gb.
        """
        def _extend():
            # Get the volume group
            vg_wrap = self._get_vg_wrap()
            # Find the disk by name
            vdisks = vg_wrap.virtual_disks
            disk_found = None
            for vdisk in vdisks:
                if vdisk.name == vol_name:
                    disk_found = vdisk
                    break

            if not disk_found:
                LOG.error(_LE('Disk %s not found during resize.'), vol_name,
                          instance=instance)
                raise nova_exc.DiskNotFound(
                    location=self.vg_name + '/' + vol_name)

            # Set the new size
            disk_found.capacity = size

            # Post it to the VIOS
            vg_wrap.update()

        # Get the disk name based on the instance and type
        vol_name = self._get_disk_name(disk_info['type'], instance, short=True)
        LOG.info(_LI('Extending disk: %s'), vol_name)
        try:
            _extend()
        except pvm_exc.Error:
            # TODO(IBM): Handle etag mismatch and retry
            LOG.exception()
            raise

    def _get_vg_wrap(self):
        return pvm_stg.VG.get(self.adapter, uuid=self.vg_uuid,
                              parent_type=pvm_vios.VIOS,
                              parent_uuid=self._vios_uuid)
