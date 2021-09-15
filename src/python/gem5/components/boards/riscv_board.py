# Copyright (c) 2021 The Regents of the University of California
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import os
from typing import Optional

from ...utils.override import overrides
from .simple_board import SimpleBoard
from .abstract_board import AbstractBoard
from ..processors.abstract_processor import AbstractProcessor
from ..memory.abstract_memory_system import AbstractMemorySystem
from ..cachehierarchies.abstract_cache_hierarchy import AbstractCacheHierarchy
from ...resources.resource import AbstractResource

from ...isas import ISA
from ...utils.requires import requires

import m5

from m5.objects import (
    Bridge,
    PMAChecker,
    RiscvLinux,
    AddrRange,
    IOXBar,
    RiscvRTC,
    HiFive,
    CowDiskImage,
    RawDiskImage,
    RiscvMmioVirtIO,
    VirtIOBlock,
    Frequency,
    Port,
)

from m5.util.fdthelper import (
    Fdt,
    FdtNode,
    FdtProperty,
    FdtPropertyStrings,
    FdtPropertyWords,
    FdtState,
)


class RiscvBoard(SimpleBoard):
    """
    A board capable of full system simulation for RISC-V

    At a high-level, this is based on the HiFive Unmatched board from SiFive.

    This board assumes that you will be booting Linux.

    **Limitations**
    * Only works with classic caches
    """

    def __init__(
        self,
        clk_freq: str,
        processor: AbstractProcessor,
        memory: AbstractMemorySystem,
        cache_hierarchy: AbstractCacheHierarchy,
    ) -> None:
        super().__init__(clk_freq, processor, memory, cache_hierarchy)

        requires(isa_required=ISA.RISCV)

        if cache_hierarchy.is_ruby():
            raise EnvironmentError("RiscvBoard is not compatible with Ruby")

        self.workload = RiscvLinux()

        # Contains a CLINT, PLIC, UART, and some functions for the dtb, etc.
        self.platform = HiFive()
        # Note: This only works with single threaded cores.
        self.platform.plic.n_contexts = self.processor.get_num_cores() * 2
        self.platform.attachPlic()
        self.platform.clint.num_threads = self.processor.get_num_cores()

        # Add the RTC
        # TODO: Why 100MHz? Does something else need to change when this does?
        self.platform.rtc = RiscvRTC(frequency=Frequency("100MHz"))
        self.platform.clint.int_pin = self.platform.rtc.int_pin

        # Incoherent I/O bus
        self.iobus = IOXBar()

        # The virtio disk
        self.disk = RiscvMmioVirtIO(
            vio=VirtIOBlock(),
            interrupt_id=0x8,
            pio_size=4096,
            pio_addr=0x10008000,
        )

        # Note: This overrides the platform's code because the platform isn't
        # general enough.
        self._on_chip_devices = [self.platform.clint, self.platform.plic]
        self._off_chip_devices = [self.platform.uart, self.disk]

    def _setup_io_devices(self) -> None:
        """Connect the I/O devices to the I/O bus"""
        for device in self._off_chip_devices:
            device.pio = self.iobus.mem_side_ports
        for device in self._on_chip_devices:
            device.pio = self.get_cache_hierarchy().get_mem_side_port()

        self.bridge = Bridge(delay="10ns")
        self.bridge.mem_side_port = self.iobus.cpu_side_ports
        self.bridge.cpu_side_port = (
            self.get_cache_hierarchy().get_mem_side_port()
        )
        self.bridge.ranges = [
            AddrRange(dev.pio_addr, size=dev.pio_size)
            for dev in self._off_chip_devices
        ]

    def _setup_pma(self) -> None:
        """Set the PMA devices on each core"""

        uncacheable_range = [
            AddrRange(dev.pio_addr, size=dev.pio_size)
            for dev in self._on_chip_devices + self._off_chip_devices
        ]

        # TODO: Not sure if this should be done per-core like in the example
        for cpu in self.get_processor().get_cores():
            cpu.get_mmu().pma_checker = PMAChecker(
                uncacheable=uncacheable_range
            )

    @overrides(AbstractBoard)
    def has_io_bus(self) -> bool:
        return True

    @overrides(AbstractBoard)
    def get_io_bus(self) -> IOXBar:
        return self.iobus

    def has_coherent_io(self) -> bool:
        return True

    def get_mem_side_coherent_io_port(self) -> Port:
        return self.iobus.mem_side_ports

    @overrides(AbstractBoard)
    def setup_memory_ranges(self):
        memory = self.get_memory()
        mem_size = memory.get_size()
        self.mem_ranges = [AddrRange(start=0x80000000, size=mem_size)]
        memory.set_memory_range(self.mem_ranges)

    def set_workload(
        self, bootloader: AbstractResource, disk_image: AbstractResource,
        command: Optional[str] = None
    ) -> None:
        """Setup the full system files

        See http://resources.gem5.org/resources/riscv-fs for the currently
        tested kernels and OSes.

        The command is an optional string to execute once the OS is fully
        booted, assuming the disk image is setup to run `m5 readfile` after
        booting.

        After the workload is set up, this functino will generate the device
        tree file and output it to the output directory.

        **Limitations**
        * Only supports a Linux kernel
        * Disk must be configured correctly to use the command option
        * This board doesn't support the command option

        :param bootloader: The resource encapsulating the compiled bootloader
            with the kernel as a payload
        :param disk_image: The resource encapsulating the disk image containing
            the OS data. The first partition should be the root partition.
        :param command: The command(s) to run with bash once the OS is booted
        """

        self.workload.object_file = bootloader.get_local_path()

        image = CowDiskImage(
            child=RawDiskImage(read_only=True), read_only=False
        )
        image.child.image_file = disk_image.get_local_path()
        self.disk.vio.image = image

        self.workload.command_line = "console=ttyS0 root=/dev/vda ro"

        # Note: This must be called after set_workload because it looks for an
        # attribute named "disk" and connects
        self._setup_io_devices()

        self._setup_pma()

        # Default DTB address if bbl is built with --with-dts option
        self.workload.dtb_addr = 0x87E00000

        # We need to wait to generate the device tree until after the disk is
        # set up. Now that the disk and workload are set, we can generate the
        # device tree file.
        self.generate_device_tree(m5.options.outdir)
        self.workload.dtb_filename = os.path.join(
            m5.options.outdir, "device.dtb"
        )

    def generate_device_tree(self, outdir: str) -> None:
        """Creates the dtb and dts files.

        Creates two files in the outdir: 'device.dtb' and 'device.dts'

        :param outdir: Directory to output the files
        """

        state = FdtState(addr_cells=2, size_cells=2, cpu_cells=1)
        root = FdtNode("/")
        root.append(state.addrCellsProperty())
        root.append(state.sizeCellsProperty())
        root.appendCompatible(["riscv-virtio"])

        for mem_range in self.mem_ranges:
            node = FdtNode("memory@%x" % int(mem_range.start))
            node.append(FdtPropertyStrings("device_type", ["memory"]))
            node.append(
                FdtPropertyWords(
                    "reg",
                    state.addrCells(mem_range.start)
                    + state.sizeCells(mem_range.size()),
                )
            )
            root.append(node)

        # See Documentation/devicetree/bindings/riscv/cpus.txt for details.
        cpus_node = FdtNode("cpus")
        cpus_state = FdtState(addr_cells=1, size_cells=0)
        cpus_node.append(cpus_state.addrCellsProperty())
        cpus_node.append(cpus_state.sizeCellsProperty())
        # Used by the CLINT driver to set the timer frequency. Value taken from
        # RISC-V kernel docs (Note: freedom-u540 is actually 1MHz)
        cpus_node.append(FdtPropertyWords("timebase-frequency", [10000000]))

        for i, core in enumerate(self.get_processor().get_cores()):
            node = FdtNode(f"cpu@{i}")
            node.append(FdtPropertyStrings("device_type", "cpu"))
            node.append(FdtPropertyWords("reg", state.CPUAddrCells(i)))
            node.append(FdtPropertyStrings("mmu-type", "riscv,sv48"))
            node.append(FdtPropertyStrings("status", "okay"))
            node.append(FdtPropertyStrings("riscv,isa", "rv64imafdc"))
            # TODO: Should probably get this from the core.
            freq = self.clk_domain.clock[0].frequency
            node.append(FdtPropertyWords("clock-frequency", freq))
            node.appendCompatible(["riscv"])
            int_phandle = state.phandle(f"cpu@{i}.int_state")
            node.appendPhandle(f"cpu@{i}")

            int_node = FdtNode("interrupt-controller")
            int_state = FdtState(interrupt_cells=1)
            int_phandle = int_state.phandle(f"cpu@{i}.int_state")
            int_node.append(int_state.interruptCellsProperty())
            int_node.append(FdtProperty("interrupt-controller"))
            int_node.appendCompatible("riscv,cpu-intc")
            int_node.append(FdtPropertyWords("phandle", [int_phandle]))

            node.append(int_node)
            cpus_node.append(node)

        root.append(cpus_node)

        soc_node = FdtNode("soc")
        soc_state = FdtState(addr_cells=2, size_cells=2)
        soc_node.append(soc_state.addrCellsProperty())
        soc_node.append(soc_state.sizeCellsProperty())
        soc_node.append(FdtProperty("ranges"))
        soc_node.appendCompatible(["simple-bus"])

        # CLINT node
        clint = self.platform.clint
        clint_node = clint.generateBasicPioDeviceNode(
            soc_state, "clint", clint.pio_addr, clint.pio_size
        )
        int_extended = list()
        for i, core in enumerate(self.get_processor().get_cores()):
            phandle = soc_state.phandle(f"cpu@{i}.int_state")
            int_extended.append(phandle)
            int_extended.append(0x3)
            int_extended.append(phandle)
            int_extended.append(0x7)
        clint_node.append(
            FdtPropertyWords("interrupts-extended", int_extended)
        )
        clint_node.appendCompatible(["riscv,clint0"])
        soc_node.append(clint_node)

        # PLIC node
        plic = self.platform.plic
        plic_node = plic.generateBasicPioDeviceNode(
            soc_state, "plic", plic.pio_addr, plic.pio_size
        )

        int_state = FdtState(addr_cells=0, interrupt_cells=1)
        plic_node.append(int_state.addrCellsProperty())
        plic_node.append(int_state.interruptCellsProperty())

        phandle = int_state.phandle(plic)
        plic_node.append(FdtPropertyWords("phandle", [phandle]))
        plic_node.append(FdtPropertyWords("riscv,ndev", [plic.n_src - 1]))

        int_extended = list()
        for i, core in enumerate(self.get_processor().get_cores()):
            phandle = state.phandle(f"cpu@{i}.int_state")
            int_extended.append(phandle)
            int_extended.append(0xB)
            int_extended.append(phandle)
            int_extended.append(0x9)

        plic_node.append(FdtPropertyWords("interrupts-extended", int_extended))
        plic_node.append(FdtProperty("interrupt-controller"))
        plic_node.appendCompatible(["riscv,plic0"])

        soc_node.append(plic_node)

        # UART node
        uart = self.platform.uart
        uart_node = uart.generateBasicPioDeviceNode(
            soc_state, "uart", uart.pio_addr, uart.pio_size
        )
        uart_node.append(
            FdtPropertyWords("interrupts", [self.platform.uart_int_id])
        )
        uart_node.append(FdtPropertyWords("clock-frequency", [0x384000]))
        uart_node.append(
            FdtPropertyWords("interrupt-parent", soc_state.phandle(plic))
        )
        uart_node.appendCompatible(["ns8250"])
        soc_node.append(uart_node)

        # VirtIO MMIO disk node
        disk = self.disk
        disk_node = disk.generateBasicPioDeviceNode(
            soc_state, "virtio_mmio", disk.pio_addr, disk.pio_size
        )
        disk_node.append(FdtPropertyWords("interrupts", [disk.interrupt_id]))
        disk_node.append(
            FdtPropertyWords("interrupt-parent", soc_state.phandle(plic))
        )
        disk_node.appendCompatible(["virtio,mmio"])
        soc_node.append(disk_node)

        root.append(soc_node)

        fdt = Fdt()
        fdt.add_rootnode(root)
        fdt.writeDtsFile(os.path.join(outdir, "device.dts"))
        fdt.writeDtbFile(os.path.join(outdir, "device.dtb"))
