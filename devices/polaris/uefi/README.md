# polaris UEFI

基于 Project Silicium / Mu-Silicium，固定提交见 `BASE`。设备包位于
`files/Platforms/Xiaomi/polarisPkg`，配置在 `files/Resources/Configs/polaris.toml`。

**Mu-Silicium 在这个提交上已经自带 `Platforms/Xiaomi/polarisPkg`。** 本目录不是从零写的设备包，
而是以上游那份为准、只加上 hackios 需要的东西。上游那份在结构上是完整的；本目录相对它的差别见下。

从仓库根目录运行：

```sh
devices/polaris/uefi/apply.sh
# 按 Mu-Silicium 文档安装依赖、配置 Python 环境。
cd Mu-Silicium
python3 build_uefi.py -d polaris -r DEBUG
```

`Binaries/polaris`（手机的 UEFI 驱动）来自 Mu-Silicium 的 `Binaries` 子模块
（Project-Silicium/Device-Binaries 的 `polaris/` 目录），属于私有构建输入，不提交，也不伪造。

## 本机测得的数值

来源：手机自己的 `xbl.img` 里的 `uefiplat.cfg`（`[MemoryMap]`），以及
MIUI 12.5 fastboot 包里的 `polaris.dtb`（DTB reserved-memory）。两处独立来源一致。

| 名称 | 数值 | 来源 |
| --- | --- | --- |
| FD_BASE | `0x9FC00000` | uefiplat.cfg `"UEFI FD"` |
| FD_SIZE（**镜像**）| `0x220000` | ELF `PT_LOAD` paddr `0x9FC00000` filesz；FV `FvLength`；`_FVH` 在文件 0x82000 |
| FD 保留区（**不是**镜像大小）| `0x00300000` | uefiplat.cfg；`0x9FC00000` 到 SEC Heap `0x9FF00000` |
| PAYLOAD_BASE | `0xA0000000` | raphael 用的是 `0xF0000000`，在 polaris 上落在 4 GiB 以下的 MMIO 洞里 |
| DDR 顶端 | `0xB9400000` | `MLVM` `0xB0400000` + `0x09000000`，是内存映射里最后一个区域 |
| FB_BASE | `0x9D400000` | uefiplat.cfg `"Display Reserved"`，和 `cont_splash_region@9d400000` 一致 |
| UEFI Stack | `0x9FF90000`，`0x40000` | uefiplat.cfg；`PcdCPUCoresStackBase` 用的是同一个值 |
| GICD / GICR | `0x17A00000` / `0x17A60000` | uefiplat.cfg `[RegisterMap]`；NapaliPkg 也是这两个值 |
| 定时器 | 虚拟 PPI 3 → INTID 19 | DTB `/soc/timer` `interrupts = <1 3 ...>`；与 `device.conf` 的 `gic_timer_irq=19` 一致 |

`0x220000` 和 `0x00300000` 是两件事，不要互相"修正"：镜像必须**放得进**保留区，而不是填满它。

## 与上游的差别

`mu-silicium.patch`（改已有文件，4 个，和 raphael 同一组）：

- `BootShim/BootShim.S`：`#ifdef PAYLOAD_BASE` 时把 FD 之后可选的 "XNUP" 容器复制到 `PAYLOAD_BASE`。
- `BootManagerLib/BootManager.c`：BDS 不再走启动菜单，改调 `XnuLoaderMain()`。
- `BootManagerLib/BootManagerLib.inf`：加入 `XnuLoader.c`，补上它需要的 Protocol/Guid。
- `Sec/Sec.c`：`XNU_PAYLOAD_BOOT` 下打印几行 SEC 进度（字符串是 `HACKIOS POLARIS SEC`）。

`files/`（覆盖上游同名文件）：

- `Library/MemoryMapLib/MemoryMapLib.c`：补上游缺的 4 个区域 —— `HLOS1`、`CPU Vectors`、`HLOS2`、
  以及 **`{"XNU Payload", 0xA0000000, 0x04000000, ...}`**。最后一个是必须的：BootShim 和
  `XnuLoader.c` 都直接读写这个地址，而它们的映射来自这张表。上游 `polarisPkg` 没有这一项。
- `polaris.dsc`：`USE_CUSTOM_DISPLAY_DRIVER` 由 1 改 0（Device-Binaries 的 `polaris/RawFiles`
  没有任何 panel XML，自定义显示驱动没有面板可加载）；去掉 `Components` 里的
  `polaris/AcpiTables.inf`；补 `[BuildOptions]` 的 `-DXNU_NO_DISK_LOG -DXNU_PAYLOAD_BOOT`。
- `polaris.fdf`：对应地注释掉 `ACPITABLE polaris/AcpiTables.inf`（两个 ACPI DXE 驱动保留，
  和 raphael 一样；没有表文件它们就是空转）。
- `Resources/Configs/polaris.toml`：`[uefi_fd] size` 由上游的 `0x300000` 改成 `0x220000`。
  上游那个值是保留区的大小，`build_uefi.py` 会把它当成镜像大小做
  `assert len(fd) == FD_SIZE`，所以必须改。
- `Silicon/.../BootManagerLib/XnuLoader.{c,h}`：设备自己的副本，`XNU_PAYLOAD_BASE 0xA0000000`，
  banner 是 `HACKIOS POLARIS`，日志分区 `logdump`（polaris 的 GPT 上确实存在，65536 KB）。

`polaris-firmware-patches.json`：说明 Device-Binaries 的 `polaris/` 里哪些模块是打过补丁的、
理由是什么，以及哪些没有。它不被任何脚本使用（polaris 不需要像 raphael 那样自己打补丁）。

## 还缺什么

- **`Binaries/polaris`**：来自子模块，构建必需。缺了就是构建不出来。
- **本机没有 EDK2/Mu 工具链。** 这台机器上编译不了，唯一真正的检查是 CI
  （`.github/workflows/polaris-uefi.yml`）。该工作流只做静态一致性检查，不依赖 EDK2。
- 上游 `polarisPkg` 在 `BASE` 上是完整设备包（`.dsc` / `.fdf` / `.dec` / 两个库 / 驱动清单齐全），
  `polaris.dec` 在两边都是 0 字节，这是上游本来的样子。
- `PAYLOAD_BASE` 现在是 `0xA0000000`：它是从内存映射推出来的，没有在硬件上验证过。

## 没有做过的事

这台设备上没有跑过任何东西。没有 flash、没有 `fastboot boot`、没有在真机上验证过
`XNU Payload` 这块映射。本目录里的东西都是"照测量写下来的"，不是"跑通过的"。
