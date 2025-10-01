"""
    :codeauthor: :email:`Shane Lee <slee@saltstack.com>`
"""
# Import Python libs
from __future__ import absolute_import, print_function, unicode_literals

# Import Salt Testing Libs
from tests.support.mixins import LoaderModuleMockMixin
from tests.support.unit import TestCase
from tests.support.mock import (
    patch,
    MagicMock,
)

# Import Salt Libs
import salt.grains.disks as disks
from tests.support.mock import MagicMock, mock_open, patch


class IscsiGrainsTestCase(TestCase, LoaderModuleMockMixin):
    '''
    Test cases for _windows_disks grains
    '''
    def setup_loader_modules(self):
        return {
            disks: {
                '__salt__': {},
            },
        }


    def test__windows_disks_dict():
        """
        Test grains._windows_disks with a single disk returned as a dict
        Should return 1 disk and no ssds
        """
        devices = {"DeviceID": 0, "MediaType": "HDD"}
        mock_powershell = MagicMock(return_value=devices)
    
        with patch.dict(disks.__salt__, {"cmd.powershell": mock_powershell}):
            result = disks._windows_disks()
            expected = {"disks": ["\\\\.\\PhysicalDrive0"], "SSDs": []}
            assert result == expected


    def test__windows_disks_list():
        """
        test grains._windows_disks with of dictsmultiple disks and types as a list
        Should return 4 disks and 1 ssd
        """
        devices = [
            {"DeviceID": 0, "MediaType": "SSD"},
            {"DeviceID": 1, "MediaType": "HDD"},
            {"DeviceID": 2, "MediaType": "HDD"},
            {"DeviceID": 3, "MediaType": "HDD"},
        ]
        mock_powershell = MagicMock(return_value=devices)

        with patch.dict(disks.__salt__, {"cmd.powershell": mock_powershell}):
            result = disks._windows_disks()
            expected = {
                "disks": [
                    "\\\\.\\PhysicalDrive0",
                    "\\\\.\\PhysicalDrive1",
                    "\\\\.\\PhysicalDrive2",
                    "\\\\.\\PhysicalDrive3",
                ],
                "SSDs": ["\\\\.\\PhysicalDrive0"],
            }
            assert result == expected


    def test__windows_disks_empty():
        """
        Test grains._windows_disks when nothing is returned
        Should return empty lists
        """
        devices = {}
        mock_powershell = MagicMock(return_value=devices)

        with patch.dict(disks.__salt__, {"cmd.powershell": mock_powershell}):
            expected = {"disks": [], "SSDs": []}
            result = disks._windows_disks()
            assert result == expected


    def test__linux_disks():
        """
        Test grains._linux_disks, normal return
        Should return a populated dictionary
        """

        files = [
            "/sys/block/asm!.asm_ctl_vbg0",
            "/sys/block/dm-0",
            "/sys/block/loop0",
            "/sys/block/ram0",
            "/sys/block/sda",
            "/sys/block/sdb",
            "/sys/block/vda",
        ]
        links = [
            "../devices/virtual/block/asm!.asm_ctl_vbg0",
            "../devices/virtual/block/dm-0",
            "../devices/virtual/block/loop0",
            "../devices/virtual/block/ram0",
            "../devices/pci0000:00/0000:00:1f.2/ata1/host0/target0:0:0/0:0:0:0/block/sda",
            "../devices/pci0000:35/0000:35:00.0/0000:36:00.0/host2/target2:1:0/2:1:0:0/block/sdb",
            "../devices/pci0000L00:0000:00:05.0/virtio2/block/vda",
        ]
        contents = [
            "1",
            "1",
            "1",
            "0",
            "1",
            "1",
            "1",
        ]

        patch_glob = patch("glob.glob", autospec=True, return_value=files)
        patch_readlink = patch("salt.utils.path.readlink", autospec=True, side_effect=links)
        patch_fopen = patch("salt.utils.files.fopen", mock_open(read_data=contents))
        with patch_glob, patch_readlink, patch_fopen:
            ret = disks._linux_disks()

        assert ret == {"disks": ["sda", "sdb", "vda"], "SSDs": []}, ret
