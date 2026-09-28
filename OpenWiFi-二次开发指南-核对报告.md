# 《OpenWiFi-二次开发指南.md》准确性核对报告

核对方法：拉取上游 `open-sdr/openwifi` 与 `open-sdr/openwifi-hw` 的 **master** 分支快照（`codeload` tarball，非子模块），逐条比对文档中的文件名、脚本行为、寄存器索引、设备树地址、宏定义与命令行参数。设备树/内核相关结论同时对照既有实测记录。

**总体结论：主干内容准确度高。** 分层架构、模块清单、4 个内核补丁、e310v2 设备树地址表、`wgd.sh`（295 行）行为、`prepare_kernel.sh` 的 2026_R1/6.12 覆盖行为、`make_all.sh` 编译顺序、Buildroot 三块板路线、`ip_repo_gen.tcl` 生成的宏、天线逻辑等**均与上游一致**。发现 **10 处需要修正**，其中 2 处是会误导操作的实际错误（寄存器速查表的 section 归属、`create_vivado_proj.sh` 参数错位）。

---

## 一、必须修正（会直接误导操作）

### 1. §5「寄存器速查」中 `rx` / `tx` / `xpu` 三行的索引归属错了

文档原文（第 571–574 行）：

| section | 对应 | 常用 idx |
|---|---|---|
| `rx` | `openofdm_rx` | 0=解调门限、4=天线配置、7=打印配置 |
| `tx` | `openofdm_tx` | 0/1/2/3=速率、4=天线配置 |
| `xpu` | 低 MAC | 0=LBT 门限、7=git rev、8=LBT_TH |

这三行描述的其实是 **`drv_rx` / `drv_tx` / `drv_xpu`** 的索引，不是 FPGA section 的索引。证据 `driver/sdr.h`：

```c
#define DRV_TX_REG_IDX_RATE        0        // 与文档 "tx 0/1/2/3=速率" 完全对应
#define DRV_TX_REG_IDX_ANT_CFG     4
#define DRV_RX_REG_IDX_DEMOD_TH    0        // 文档写成 "rx 0=解调门限"
#define DRV_RX_REG_IDX_ANT_CFG     4
#define DRV_XPU_REG_IDX_LBT_TH     0        // 文档写成 "xpu 0=LBT 门限"
#define MAX_NUM_DRV_REG            8        // → PRINT_CFG/GIT_REV = 7
#define DRV_XPU_REG_IDX_GIT_REV    (MAX_NUM_DRV_REG-1)   // =7
```

而 FPGA section 的真实索引是（`driver/hw_def.h`）：

- `openofdm_rx`：`0`=MULTI_RST、`1`=ENABLE、`2`=POWER_THRES（解调门限）、`3`=MIN_PLATEAU、`4`=SOFT_DECODING、`5`=FFT_WIN_SHIFT、`18`=PHASE_OFFSET_ABS_TH、`20`=STATE_HISTORY
- `xpu`：`0`=MULTI_RST、`4`=BAND_CHANNEL、`5`=DIFS_ADVANCE、`8`=LBT_TH、`9`=CSMA_DEBUG、`11`=ACK_CTRL_MAX_NUM_RETRANS、`63`=FPGA_GIT_REV

后果举例：照表格执行 `sdrctl dev sdr0 set reg rx 0 100` 想改解调门限，实际写的是 **openofdm_rx 的 MULTI_RST**；`set reg xpu 0 ...` 写的是 xpu 的 MULTI_RST，不是 LBT 门限。同一张表内部也自相矛盾（`0` 和 `8` 都被标成 LBT 门限）。

**建议改法**：把三行的 section 名改成 `drv_rx` / `drv_tx` / `drv_xpu`；另起一行说明 FPGA `rx`/`tx`/`xpu` 的真实索引，或统一指向 `hw_def.h`。注意文档 §3 场景 A 的示例（`set reg drv_rx 0 100`、`set reg xpu 8 30`）本身是**对的**——只有 §5 这张表串了。

### 2. §3 场景 C 的 `create_vivado_proj.sh` 示例参数错位

文档原文（第 274 行）：

```
../create_vivado_proj.sh $XILINX_DIR xpu.tcl 100MHz  # 生成独立工程
```

脚本实际参数含义（`ip/create_vivado_proj.sh` 的 usage）：`$1`=XILINX_DIR、`$2`=TCL 文件名、`$3`=**BOARD_NAME**、`$4`=**NUM_CLK_PER_US**、`$5–$9`=用户宏。上例中 `100MHz` 会被当成 BOARD_NAME，且格式（应为 `100`）也不对。

**建议改为**：`../create_vivado_proj.sh $XILINX_DIR xpu.tcl e310v2 100`

文档 §4.3(1) 对参数含义的文字说明是**正确**的，两处需保持一致。

---

## 二、事实性偏差（不影响操作，但属错误陈述）

### 3. `driver/sdr.c` 行数

文档写「约 4100 行」。实际 `driver/sdr.c` = **2810 行**（已把 netlink 分发与 sysfs 节点拆到 `sdrctl_intf.c` 465 行、`sysfs_intf.c`）。4100 行是拆分前的旧数字，属**过时**。

### 4. `ip/openwifi_ip.tcl` 实例化的 user IP 数量

文档写「实例化 **6 个** user IP + `axi_dma:7.1` ×2 + `axi_interconnect:2.1` ×3 + `proc_sys_reset:5.0`」。

实际 `ip/openwifi_ip.tcl` 里只有 **5 个** user IP：`openofdm_rx`、`openofdm_tx`、`rx_intf`、`tx_intf`、`xpu`（`grep -c side_ch` = **0**）。`side_ch` 是在**板级** `boards/e310v2/src/system.bd` 里实例化的（`side_ch_0`，vlnv `user.org:user:side_ch:1.0`）。对照：`ip/openwifi_ip_ultra_scale.tcl` 才含 side_ch（6 个）。`axi_dma` ×2、`axi_interconnect` ×3、`proc_sys_reset` 的描述均正确。

### 5. `build_boot_bin.sh` 的职责

文档 §2.4 表格写它「用 `xsct` 造 FSBL + `bootgen` 出 `BOOT.BIN`；**同时** `bootgen -process_bitstream bin` 出 `system_top.bit.bin`」。

实际 `kernel_boot/build_boot_bin.sh` **只出 BOOT.BIN**（xsct 建 FSBL 工程 → `bootgen -arch zynq -image zynq.bif -o BOOT.BIN`）。`system_top.bit.bin` 是 **`user_space/boot_bin_gen.sh`** 在生成 `.bif` 后 `bootgen -process_bitstream bin` 得到的。§4.1(2) 的描述是对的，§2.4 这一行把两件事并到了同一个脚本。

### 6. §4.1(4) 「`wgd.sh` 到底做了什么」漏了开头三步

文档列的 7 步从 `killall hostapd / wpa_supplicant` 开始。实际 `wgd.sh` 在此之前还有：

```
insmod ad9361_drv.ko
insmod xilinx_dma.ko
modprobe mac80211
```

（Buildroot 版被 `openwifi.mk` 的 sed 改成 `modprobe ad9361 ... || true`、`modprobe xilinx_dma ... || true`）。这解释了为什么 `ad9361_drv.ko` / `xilinx_dma.ko` 必须躺在 `wgd.sh` 同目录——文档 §4.1(1) 只说它们在镜像的 `/root/kernel_modules32/`，但没说 `wgd.sh` 会从当前目录 `insmod` 它们（`setup_once.sh` 会把这几个 `.ko` 挪到 `/root/openwifi/`）。另外第 1 步实际还包括 `service dhcpcd stop` 与 `killall dhcpd`。

### 7. §4.1(5) `load_fpga_img.sh` 的解绑顺序与额外动作

文档写的顺序是 `ad9361` → `cf_axi_adc` → `cf_axi_dds`。实际脚本先是

```
ifconfig sdr0 down; rmmod sdr
rmmod openofdm_rx / openofdm_tx / rx_intf / tx_intf / xpu
```

然后才依次 unbind **`cf_axi_adc`(79020000) → `cf_axi_dds`(79024000) → `ad9361`(spi0.0)**，再停/重启 iiod。文档「故意不解绑 openwifi 的 AXI-DMA」与原因（E200 上 VDMA remove/probe 会锁 AXI 总线）**完全正确**，只是顺序和「先卸载 5 个 openwifi 模块」这两点没写。

### 8. §2.1 `ad9361_drv.ko` 行「通过 4 个补丁给它加 API」

4 个补丁中只有 **3 个**动 ad9361：`ad9361_v6_12.patch`→`drivers/iio/adc/ad9361.c`、`ad9361_private.patch`→`ad9361_private.h`、`ad9361_conv.patch`→`ad9361_conv.c`；第 4 个 `axi_hdmi_crtc.patch`→`drivers/gpu/drm/adi_axi_hdmi/axi_hdmi_crtc.c`，与 9361 无关。文档下方那张「4 个内核补丁」表本身**逐条准确**（描述与 `kernel_boot/kernel_patch_readme.md` 一致），是这一行的表述不准。

### 9. §2.2 `openofdm_rx` 子模块的仓库地址

文档写「`jhshi/openofdm` 的 `dot11zynq` 分支」。实际 `.gitmodules`：

```
[submodule "ip/openofdm_rx"]
	path = ip/openofdm_rx
	url = https://github.com/open-sdr/openofdm.git
```

即已改为 **`open-sdr/openofdm`**（jhshi 那套代码在 open-sdr 组织下的 fork），`dot11zynq` 分支确实存在于该仓库。另外 `get_ip_openofdm_rx.sh` 里 `git checkout dot11zynq` 那两行**是注释掉的**，实际版本由 openwifi-hw 的 pin 决定。建议表述为「`open-sdr/openofdm`（原 jhshi/openofdm）的 `dot11zynq` 分支」。

### 10. §2.3 目录名 `host_tools` vs `host-tools`

仓库里的实际目录名是 **`host-tools`**（`host-tools/openwifi_fw_update.py`）。文档表里第一列写成 `host_tools/openwifi_fw_update.py`、第二列写 `host-tools/`，前后不一致。

### 11. §4.3(2) `ip_repo_gen.tcl` 的打包列表

文档写「对 `openofdm_tx rx_intf tx_intf xpu side_ch` 依次走 `package_ip_complex.tcl` 打包」。实际 `ip_name_list = "openofdm_rx openofdm_tx rx_intf tx_intf xpu side_ch"`，**6 个都会过一遍**（只是 `openofdm_rx` 的 `ip_config/<ip>_pre_def.v` 用 `cat >>` 追加进它的子模块源码，其余 5 个是直接拷贝）。

---

## 三、已逐条核对、确认无误的关键内容

**架构与模块**
- 「七个独立 `.ko` + 两个外部 `.ko`」；`driver/Makefile` 的 `obj-m` 首项是 `sdr.o`，且**不含** `side_ch`（`side_ch` 走 `driver/side_ch/make_driver.sh`）
- 各 `.ko` 的 compatible 字符串：`sdr,sdr` / `sdr,tx_intf` / `sdr,rx_intf` / `sdr,openofdm_tx` / `sdr,openofdm_rx` / `sdr,xpu` / `sdr,side_ch` 全部一致
- 各 IP 的源文件清单（`xpu` / `tx_intf` / `rx_intf` / `openofdm_tx` / `side_ch`）——文档列出的文件名逐一存在（`xpu/src` 另有文档未列的 `edge_to_flip.v`、`n_sym_len14_pkt.v`，属「关键源文件」不完整，不算错）
- `openofdm_rx` 在 `ip/` 下确实是子模块（快照里连目录都没有，要 `git submodule update`）
- `xilinx_dma`：`make_xilinx_dma.sh` 用 `$XILINX_DIR/SDK/2018.3/settings64.sh`，把 `xilinx_dma.c` 覆盖进内核树（备份后覆盖），**回滚那行确实是注释掉的**

**内核侧**
- `prepare_kernel.sh`：子模块 pin `b6e3799`(v4.14) 确实被 `git fetch && git checkout 2026_R1 && git pull && git reset --hard 2026_R1` 覆盖 → 真正构建 **2026_R1 / Linux 6.12**；`cp kernel_boot/kernel_config`；`git apply` 四个补丁的顺序与清单一致（`axi_hdmi_crtc` → `ad9361_v6_12` → `ad9361_private` → `ad9361_conv`）；`make oldconfig && make prepare && make modules_prepare`；`make -j12 uImage UIMAGE_LOADADDR=0x8000` + `make modules`
- `make_all.sh`：校验 `$XILINX_DIR/Vitis`、`ARCH_BIT∈{32,64}`、写 `pre_def.h`（`USE_NEW_RX_INTERRUPT 1` + `$3..$7` 共 5 个宏）与 `git_rev.h`、`source Vitis/2022.2/settings64.sh`、编序 `openofdm_tx → openofdm_rx → tx_intf → rx_intf → xpu → side_ch(make_driver.sh) → driver(sdr.ko)` —— 文档**逐步吻合**
- 4 个补丁的作用描述与 `kernel_boot/kernel_patch_readme.md` 一字不差；`ad9361.patch` 确为老内核线、当前不 apply

**设备树与地址表（e310v2，全部实测一致）**

| 模块 | 文档 | `devicetree.dts` 实测 |
|---|---|---|
| `tx_intf` | `0x83C00000`, 64KB, SPI 34 | `0x83c00000 0x10000`, int 34 |
| `openofdm_tx` | `0x83C10000` | `0x83c10000` |
| `rx_intf` | `0x83C20000`, SPI 29/30 | `0x83c20000 0x10000`, int 29/30 |
| `openofdm_rx` | `0x83C30000` | `0x83c30000` |
| `xpu` | `0x83C40000` | `0x83c40000` |
| `side_ch` | `0x83C50000` | `0x83c50000` |
| `tx_dma` | `0x80400000`, SPI 35/36 | `0x80400000 0x10000`, int 35/36 |
| `rx_dma` | `0x80410000`, SPI 31/32 | `0x80410000 0x10000`, int 31/32 |
| `cf-ad9361-lpc` | `0x79020000` | `0x79020000` |
| `cf-ad9361-dds` | `0x79024000` | `0x79024000` |
| `sdr` 聚合节点 | 无 `reg`，中断 29/30/33/34 | 无 `reg`，`interrupts = <0 29 1 0 30 1 0 33 1 0 34 1>` |

**FPGA 侧**
- `ip_repo_gen.tcl` 生成的宏全部核对无误：`clock_speed.v`（`NUM_CLK_PER_US 100` + `fpga_size_flag==0` 时 `SMALL_FPGA 1`）、`fpga_scale.v`（`SIDE_CH_LESS_BRAM 1`）、`has_side_ch_flag.v`（`HAS_SIDE_CH 1`，改 0 则 `NO_SIDE_CH`）、`spi_command.v`（默认 `SPI_HIGH 24'h088A01` / `SPI_LOW 24'h008A01`；`grounded_rf_port=1` 时变 `24'hC22001/24'hC02001`）、`openwifi_hw_git_rev.v`
- `parse_board_name.tcl`：`e310v2` → `xc7z020clg400-1`、`fpga_size_flag = 0`（`ultra_scale_flag 0`、`board_part_string []`、`board_id_string []`）
- `boards/<board>/set_files.tcl`：e310v2 确实额外引入 `ad5640_spi.v`、`ppsloop.v`
- `openwifi.tcl`：`exec git clean -dxf ./src/`（第 47 行）、`launch_runs impl_1 -to_step write_bitstream -jobs 8`、`write_hw_platform -fixed -include_bit ... ./openwifi_$BOARD_NAME/system_top.xsa` —— 提醒「未提交的 `src/` 会被删」**属实**
- `post_script_common.tcl`：`upgrade_ip` 恰好 5 个（`system_rx_intf_0_0 system_tx_intf_0_0 system_openofdm_tx_0_0 system_xpu_0_0 system_side_ch_0_0`，不含 openofdm_rx）、`util_ad9361_divclk/clk_out` 强制 40 MHz
- `create_vivado_proj.sh` 的参数映射（3rd=BOARD_NAME、4th=NUM_CLK_PER_US、5–9=用户宏、`openofdm_rx` 的 3rd 兼作 SAMPLE_FILE）
- `create_ip_repo.sh`：写 `` `define <board> `` + `` `define <IP>_<MACRO> `` 到 `ip_config/<ip>_pre_def.v`，`source Vitis/2022.2/settings64.sh`，`vivado -source ../ip_repo_gen.tcl`
- `sdk_update.sh`：拷 `system_top.xsa` + `system_top.ltx` 到 hw-img，并把 **openwifi-hw 与 openofdm_rx 的 branch+commit** 写入 `git_info.txt`
- `prepare_adi_lib.sh $XILINX_DIR`、`prepare_adi_board_ip.sh $XILINX_DIR <board>`、`get_ip_openofdm_rx.sh` 的参数与行为一致

**用户态与部署链**
- `wgd.sh` **295 行**（一字不差）；`OPENWIFI_RELOAD_FPGA=0/1/auto` 与 Buildroot 上「RF 链已就绪则跳过重复重载」的逻辑；`rf_init_11n.sh`；`insmod` 顺序 `tx_intf rx_intf openofdm_tx openofdm_rx xpu sdr`（`sdr` 带 `test_mode`）；按 `66:55:44:33:22:*` 前缀改名为 `sdr0`；`mount debugfs` + `agc_settings.sh 1`
- `load_fpga_img.sh`：`echo 0 > /sys/class/fpga_manager/fpga0/flags` → `cp <bit.bin> /lib/firmware/` → `echo <文件名> > .../firmware` → 重新 bind；**故意不解绑 openwifi AXI-DMA**（注释原文即 E200 VDMA 锁总线）
- `boot_bin_gen.sh`：3 个参数；`e310v2` 属 Zynq-7000 分支 → `build_boot_bin.sh` + `ARCH=zynq/32`；`rm -rf output_boot_bin` 后 `mv` 到 `boards/<board>/output_boot_bin/`；生成 `fpga_bit_to_bin.bif` 并 `bootgen -process_bitstream bin`
- `drv_and_fpga_package_gen.sh`：3 个参数、板名白名单含 `e310v2`、从 `boards/<board>/sdk/system_top.xsa` 取硬件、打 `git_info.txt` + `driver.tar`
- `sdrctl` 的 section 列表就是 `rf/rx_intf/tx_intf/rx/tx/xpu/drv_rx/drv_tx/drv_xpu`；扩展命令 `rssi_th`、`tsf`、`slice_total/start/end/idx`、`addr`、`gap` 全部存在；`sdrctl_src` 目录内容（`sdrctl.c`、`cmd.c`、`sections.c`、`nl80211_testmode_def.h`）与「依赖 libnl-3/genl-3（pkg-config）」一致（Makefile 里就是这条依赖链）
- `fosdem.sh`：`ifconfig sdr0 192.168.13.1` → dhcpd → `hostapd hostapd-openwifi.conf` → `webfsd -F -p 80 -f index.html`；`hostapd-openwifi.conf` 确为 `country_code=BE` / `hw_mode=a` / `channel=36` / 速率集不含 11b 速率
- `rf_init_11n.sh`：改 `/sys/bus/iio/devices/iio:device0/in_voltage_rf_bandwidth` + 载 `openwifi_ad9361_fir_tx_0MHz_11n.ftr`；FIR 文件与 `rf_init.sh` 均在
- `setup_once.sh`：`/proc/device-tree/model` → `ANTSDR-E310V2` → `e310v2` 的映射、`/lib/modules/$(uname -r)` 软链、板端编 `sdrctl`/`side_ch_ctl`/`inject_80211`；`post_config.sh` 里确有 `apt-get install hostapd`
- `side_ch_ctl_src`：`side_ch_ctl.c` + `iq_capture*.py` / `side_info_display.py` / MATLAB 脚本；编译方式就是 `gcc -o side_ch_ctl side_ch_ctl.c`（`setup_once.sh` 里的写法）

**Buildroot 路线**
- 只支持 `antsdr_e200` / `antsdr` / `e310v2`；子命令 `build` / `rebuild-system` / `configure` / `menuconfig` / `clean`；`OPENWIFI_XSA` 或 `OPENWIFI_HW_IMG_DIR` 覆盖硬件输入
- 输出布局：`output/common/`（`.config`、`.openwifi-common-ready`、`images/rootfs.ext4`、`images/uImage`，三板共享）+ `output/<board>/`（`generated/`）+ `dl/`；每次 `build` 只 `uboot-dirclean && uboot`；`rebuild-system` 依次 `linux-dirclean` / `openwifi-dirclean` / `libad9361-iio-dirclean`
- `openwifi.mk`：`OPENWIFI_SITE` 指向仓库根 + `local`；`OPENWIFI_MODULE_SUBDIRS = driver driver/side_ch`；`POST_RSYNC_HOOKS` 生成 `pre_def.h`（`USE_NEW_RX_INTERRUPT 1`）与 `git_rev.h`（`git rev-parse --short=7`）；`BUILD_CMDS` 交叉编 `sdrctl_src`/`side_ch_ctl_src`/`inject_80211`；`INSTALL_TARGET_CMDS` 把 `user_space/` 拷到 `/root/openwifi/` 并装 `openwifi-start`、`S02openwifi-board`、`openwifi-fw-update`；`INSTALL_LOCAL_MODULE_COPIES` 把 `.ko` 放到 `/root/openwifi/` 并对 `wgd.sh`/`fosdem.sh` 做 `service`/`webfsd`/`dhcpcd`/`sudo` → BusyBox 的 sed 替换

**射频/其他事实**
- `sdr.c` 的天线逻辑：`tx_ant ∈ {1,2,3}`（`>=4 || ==0` 报 EINVAL）、`rx_ant ∈ {1,2}`；TX 侧「选口」= 给另一条链设 `AD9361_RADIO_OFF_TX_ATT`；RX 侧写 `ad9361_ctrl_outs_setup(en_mask, index=CTRL_OUT_INDEX_ANT0/1)`；两侧还各写 `TX_INTF_REG_ANT_SEL` / `RX_INTF_REG_ANT_SEL`
- `hw_def.h`：`dma_symbol_fifo_size_hw_queue[] = {4*1024 ×4}`，注释原文即「make sure align to fifo in tx_intf_s_axis.v」
- `side_ch.h`：`#define CSI_LEN 56`、`EQUALIZER_LEN (56-4)`、`HEADER_LEN 2`（timestamp + 频偏）→ 文档「CSI 56 子载波」正确
- Viterbi：README 原文「After ~2 hours, the Viterbi decoder will halt (Xilinx Evaluation License)… If output of `./sdrctl dev sdr0 reg rx 20` is always the same, it means the decoder halts」→ 文档 §5 该行（含「rx 20 = 20*4 = STATE_HISTORY」）**完全正确**
- `kernel_boot/10-network-device.rules`、`70-persistent-net.rules`、`boards/overlays/<board>.dtso` + `openwifi_32_ad9361.dtso`、`construct_device_tree.sh` 的 `openwifi_name_to_kernel_dts` 映射（`e310v2` → `zynq-antsdre310v2.dts`）均存在
- 附录 B 列的上游文档索引（`csi.md`、`iq.md`、`iq_2ant.md`、`inject_80211.md`、`csi_fuzzer.md`、`radar-self-csi.md`、`frequent_trick.md`、`ieee80211n.md`、`ad-hoc-two-sdr.md`、`ap-client-two-sdr.md`、`perf_counter.md`、`driver_stat.md`、`hls.md`、`drv_fpga_dynamic_loading.md`、`known_issue/notter.md`）**全部存在**
- `dd bs=512 count=31116288` = 15,931,539,456 字节，与既有实测的镜像总长一致

---

## 四、无法从上游仓库验证的项（非错误，仅标注）

- 各类耗时估计（内核首次编译 20–60 min、单 IP 综合 1–3 h、FPGA 改动闭环时间）——环境相关，属经验值
- 「Mac 上角色是编辑 + scp + 烧 SD」「Ubuntu 18/20/22，24 需手装 `libtinfo5`」——环境建议
- 预编译镜像 `openwifi-1.5.0` 的具体内容（`/root/kernel_modules32/`、`/root/openwifi_BOOT/`、`setup_once.sh` 的部署路径）——属既有实测结论，本次未重新核对镜像本体
- 板端 `dsi`/`rssi_openwifi_show.sh` 的实际读数、天线开关上电默认态——需实机观测

---

## 五、一句话总结

**可以用，但先改两处**：§5 寄存器速查表的 `rx`/`tx`/`xpu` 三行必须改成 `drv_rx`/`drv_tx`/`drv_xpu`（否则 `set reg rx 0` 会撞到 MULTI_RST），§3 场景 C 的 `create_vivado_proj.sh` 示例补上 `e310v2 100` 两个参数。其余 9 处属陈述精度问题（sdr.c 行数、openwifi_ip.tcl 是 5 不是 6、build_boot_bin.sh 不产 bit.bin、wgd.sh / load_fpga_img.sh 步骤不完整、补丁归属、子模块仓库名、`host-tools` 拼写、打包列表漏 openofdm_rx）。
