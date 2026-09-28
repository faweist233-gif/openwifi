# openwifi 二次开发指南（AntSDR E316 / `BOARD_NAME=e310v2`）

> 配套文档：`ANTSDR-E316-openwifi-烧录指南.md`（镜像烧录与首次启动）
> 本文对应上游 `open-sdr/openwifi` master（driver/software）+ `open-sdr/openwifi-hw` master（FPGA），Vivado/Vitis **2022.2**，内核 **ADI 2026_R1 = Linux 6.12**。
> 板卡识别：E316 的设备树 model 是 `ANTSDR-E310V2`，因此所有地方的 `BOARD_NAME` 都写 `e310v2`，**不要**写 `e316`。

---

## 0. 先明确边界：哪些活能在本机干，哪些必须在 Linux x86_64 上干

你当前的工作机是 macOS。这件事直接决定二次开发的迭代路径。三条流水线里只有"用户态"那条能在 Mac 上干完。

| 工作内容 | 需要什么 | 能否在 macOS 干 | 说明 |
|---|---|---|---|
| 改 `user_space/*.sh`、`hostapd*.conf` | 无 | ✅ 板上直接改 | 迭代最快 |
| 编 `sdrctl` / `side_ch_ctl` / `inject_80211` | gcc + libnl + libpcap | ✅ **在板上编**（`user_space/sdrctl_src` 等目录已随镜像放到板上） | `*.sh` 改完 scp 上板即可 |
| 编内核驱动 `.ko` | Linux 内核源码树 + `arm-linux-gnueabihf-` 交叉工具链 + `$XILINX_DIR/Vitis/2022.2/settings64.sh` | ❌ | 必须 Linux x86_64 |
| 编内核 `uImage` / `devicetree.dtb` / `BOOT.BIN` | 同上 + `dtc` + `mkimage`(u-boot-tools) + `xsct`/`bootgen` | ❌ | 必须 Linux x86_64 |
| 综合 FPGA / 出 bitstream | Vivado **2022.2** + Vitis（不是 Vitis_HLS）+ Viterbi 评估 license | ❌ | 必须 Linux；Windows 也行但不推荐 |
| 完整 SD 镜像 | Buildroot 工具链（第一次编译很久）+ `python3` | ❌ | 必须在 Linux |

**结论**：Mac 上的角色是"编辑 + scp + 烧 SD + 差分解压镜像"。真正的构建请准备一台 Ubuntu 18/20/22（24 需要手动装 `libtinfo5`）。

---

## 1. 系统分层与数据通路

openwifi 不是"一块网卡驱动"，而是把一个完整 802.11 芯片拆成了软件和 FPGA 两半。理解这条链路是二次开发的前提：

```
┌─ 用户空间 ──────────────────────────────────────────────────────────────┐
│ hostapd / wpa_supplicant / iw / iwconfig      sdrctl / side_ch_ctl /    │
│        │                                       inject_80211 / webserver  │
│        │ netlink (cfg80211)                     │ netlink testmode        │
│        │                                        (NL80211_CMD_TESTMODE)   │
└────────┼────────────────────────────────────────┼───────────────────────┘
         ▼                                        ▼
┌─ 内核 ──────────────────────────────────────────────────────────────────┐
│ mac80211 / cfg80211  ◄──────►  sdr.ko  （mac80211 驱动主体，硬件 MAC 的软件侧）│
│                                 │ 调用 six IP 驱动 API（函数指针表）        │
│   tx_intf.ko  rx_intf.ko  openofdm_tx.ko  openofdm_rx.ko  xpu.ko  side_ch.ko │
│                                 │                                       │
│   xilinx_dma.ko（AXI-DMA 控制器）  ad9361_drv.ko（ADI IIO RF 驱动，非 openwifi）│
└────────┬────────────────────────────────┬───────────────────────────────┘
         │ AXI-Lite 32bit 寄存器读写       │ AXI-Stream + AXI-DMA (64bit)
         ▼                                ▼
┌─ FPGA (PL) ────────────── openwifi_ip 层级 ─────────────────────────────┐
│  TX:  sdr.ko → tx_dma → tx_intf → openofdm_tx → dac_intf ──┐            │
│  RX:  ◄─ rx_dma ◄─ rx_intf ◄─ openofdm_rx ◄─ adc_intf ◄────┤ AD9361     │
│       xpu（DCF/CSMA-CA、TSF、SPI 控制 AD9361）              │ 射频前端    │
│       side_ch（CSI 56 子载波 + 时间戳 + 频偏 / IQ 抓取）      │ 开关(CTRL_OUT)│
└─────────────────────────────────────────────────────────┴────────────┘
```

**一句话分工**：

| 域 | 谁负责 | 典型内容 |
|---|---|---|
| 时序敏感的高层 MAC | **FPGA `xpu`** | SIFS 10µs、DIFS、slot、CW 退避、ACK、RTS/CTS、重传、时间切片 |
| PHY 实时链路 | **FPGA `tx_intf`/`openofdm_tx`/`rx_intf`/`openofdm_rx`** | 前导、CRC、扰码、卷积编码、交织、IFFT/FFT、信道估计、Viterbi |
| 协议栈对接、缓冲管理、信道/速率/功率 | **软件 `sdr.ko`** | 描述符环、DMA 管理、中断、mac80211 ops、速率与衰减表 |
| 射频配置 | **ADI `ad9361_drv.ko` + `xpu` 的 SPI** | 本振、采样率、增益、RF 带宽、IIO 接口 |
| 旁路观测 | **`side_ch` IP + `side_ch.ko`** | CSI、等化器、IQ 采样送 PS |

---

## 2. 模块清单：每个模块干什么、文件在哪、改它动哪里

### 2.1 内核驱动层（`openwifi/driver/`）

驱动是**七个独立 `.ko` + 两个外部 `.ko`**。这是 openwifi 结构上最关键的一点：每个 FPGA IP 有一个一一对应的驱动模块，负责把该 IP 的 AXI-Lite 寄存器暴露成 C 函数指针表，供 `sdr.ko` 调用。

| `.ko` | 源文件 | 设备树 compatible | 职责 | 编译方式 |
|---|---|---|---|---|
| **`sdr.ko`** | `driver/sdr.c`（约 2800 行）、`sdr.h`、`hw_def.h`、`sdrctl_intf.c`、`sysfs_intf.c` | `sdr,sdr` | **主体**。mac80211 驱动：TX/RX 描述符环、DMA 收发、TX/RX 中断处理、信道/带宽/天线切换、TX/RX 速率与衰减管理、beacon、`ieee80211_ops`、rfkill、LED；同时是 `sdrctl` 的 netlink testmode 入口 | `driver/Makefile` 里的 `obj-m` 第一项 |
| `tx_intf.ko` | `driver/tx_intf/tx_intf.c` | `sdr,tx_intf` | PL 发送路径：S_AXIS FIFO 阈值/无空间判断、BB 增益、天线选择、AMPDU 聚合动作、任意 IQ 注入、CSI fuzzer 控制、包信息统计 | 独立子 Makefile |
| `rx_intf.ko` | `driver/rx_intf/rx_intf.c` | `sdr,rx_intf` | PL 接收路径：IQ 源选择、mixer 配置、DMA 符号数、TLAST 超时上限、S2MM 中断延时、天线选择 | 独立子 Makefile |
| `openofdm_tx.ko` | `driver/openofdm_tx/openofdm_tx.c` | `sdr,openofdm_tx` | 发送 OFDM 调制器初始化（pilot/data 状态机复位） | 独立子 Makefile |
| `openofdm_rx.ko` | `driver/openofdm_rx/openofdm_rx.c` | `sdr,openofdm_rx` | 接收 OFDM 解调器：功率阈值 `POWER_THRES`、最小 plateau、软判决开关、FFT 窗位移、相位偏移绝对值门限、状态历史读取 | 独立子 Makefile |
| `xpu.ko` | `driver/xpu/xpu.c` | `sdr,xpu` | 低 MAC 控制：TSF 加载/读取、band/channel、DIFS advance、CW 与 CSMA 配置、LBT 门限、RSSI dB 校正、ACK 最大重传、RTS/CTS 与 CTS-to-self、MAC/BSSID 过滤 flag、时间切片（slice total/start/end/idx/addr/gap）、FPGA git rev | 独立子 Makefile |
| `side_ch.ko` | `driver/side_ch/side_ch.c`、`side_ch.h` | `sdr,side_ch` | 旁路信道：CSI（56 子载波）+ 时间戳 + 频偏、等化器、IQ 采样抓取，经 DMA 送 PS；触发条件可由 FC/ADDR1/ADDR2/RSSI/增益门限配置 | **单独** `driver/side_ch/make_driver.sh`（不在 `driver/Makefile` 的 `obj-m` 里） |
| ⚠️ `xilinx_dma.ko` | `driver/xilinx_dma/xilinx_dma.c` | — | AXI-DMA 控制器驱动。**不是从 openwifi 编出来的**：`make_xilinx_dma.sh` 把该文件**覆盖**到 ADI 内核 `drivers/dma/xilinx/xilinx_dma.c` 再单独 `make`，而且用的是 `$XILINX_DIR/SDK/2018.3/settings64.sh` | `make_xilinx_dma.sh $XILINX_DIR 32` |
| ⚠️ `ad9361_drv.ko` | ADI 内核 `drivers/iio/adc/ad9361.c` + 补丁 | `adi,ad9361` | RF 收发器驱动。openwifi 通过 3 个补丁（`ad9361_*` 那三个）给它加 API 和 AGC 寄存器写 | 随内核一起编，产物在 `adi-linux/drivers/iio/adc/` |

**4 个内核补丁**（`kernel_boot/`，`prepare_kernel.sh` 自动 apply）：

| 补丁 | 作用 |
|---|---|
| `axi_hdmi_crtc.patch` | 启用 Xilinx AXI-DMA 后避免 axi_hdmi 编译报错 |
| `ad9361_v6_12.patch` | 暴露 openwifi 需要的 API + 一条 AGC 设置寄存器写（对应 Linux 6.12） |
| `ad9361_private.patch` | 补一个缺失的 AGC 设置 bool |
| `ad9361_conv.patch` | 对低端/差硬件跳过 61.44 Msps LVDS 接口自校准（自校准有时过不去） |

> `kernel_boot/ad9361.patch` 是给老内核（v4.14 那条线）的，当前 `prepare_kernel.sh` 只 apply 上面 4 个。

**文件名速查（sdr.ko 内部）**：

| 文件 | 为什么你会去改它 |
|---|---|
| `hw_def.h` | **寄存器地址与驱动 API 的唯一权威**。新增/改动任何 FPGA 寄存器，这里必须同步 |
| `sdrctl_intf.c` | `sdrctl set/get` 的 netlink 命令分发（`OPENWIFI_CMD_*`），加新命令改这里 |
| `sysfs_intf.c` | `/sys/kernel/debug/ieee80211/phyX/...` 调试节点，加调试接口改这里 |
| `sdr.h` | `struct openwifi_priv`、`MAX_NUM_*`、`DRV_*_REG_IDX_*`、TX/RX BD 数量与缓冲大小 |
| `sdr.c` | `openwifi_ops`、`openwifi_tx/rx`、`openwifi_config`、`openwifi_bss_info_changed`、`priv->rf_bw`、`test_mode` 解析 |

### 2.2 FPGA 层（`openwifi-hw/`）

#### 六个可改 IP（`ip/<name>/src/`）

| IP | 关键源文件 | 职责 | 对外寄存器文件 |
|---|---|---|---|
| **`xpu`** | `xpu.v`、`xpu_s_axi.v`、`cca.v`、`csma_ca.v`、`cw_exp.v`、`tx_control.v`、`tsf_timer.v`、`dc_rm.v`、`rssi.v`、`iq_rssi_to_db.v`、`iq_abs_avg.v`、`mv_avg.v`、`mv_avg_dual_ch.v`、`time_slice_gen.v`、`spi.v`、`phy_rx_parse.v`、`pkt_filter_ctl.v`、`tx_on_detection.v`、`fifo_sample_delay.v` | 低 MAC（DCF/CSMA-CA）、TSF 定时器、CCA/RSSI 测量、时间切片、**通过 SPI 直接配置 AD9361 寄存器** | `xpu_s_axi.v` |
| **`tx_intf`** | `tx_intf.v`、`tx_intf_s_axi.v`、`tx_intf_s_axis.v`、`tx_bit_intf.v`、`tx_iq_intf.v`、`tx_status_fifo.v`、`tx_interrupt_selection.v`、`dac_intf.v`、`csi_fuzzer.v`、`ht_sig_crc_calc.v`、`div_int.v` | 802.11 帧拼装（前导/PLCP 头/CRC）、AXI-Stream 输入缓冲、DAC 时序、CSI fuzzer | `tx_intf_s_axi.v` |
| **`rx_intf`** | `rx_intf.v`、`rx_intf_s_axi.v`、`rx_intf_m_axis.v`、`rx_intf_pl_to_m_axis.v`、`rx_iq_intf.v`、`adc_intf.v`、`byte_to_word_fcs_sn_insert.v`、`gpio_status_rf_to_bb.v`、`mv_avg.v`、`mv_avg_dual_ch.v`、`edge_to_flip.v` | ADC 采样接口、下变频/抽取、帧边界对齐、FCS/序号/时间戳插入、AXI-Stream 输出 | `rx_intf_s_axi.v` |
| **`openofdm_tx`** | `dot11_tx.v`、`openofdm_tx.v`、`modulation.v`、`convenc.v`、`crc32_tx.v`、`punc_interlv_lut.v`、`bitreverse.v`、`ifftmain.v`、`ifftstage.v`、`bimpy.v`、`hwbfly.v`、`laststage.v`、`ht_ltf_rom.v`、`ht_stf_rom.v`、`l_ltf_rom.v`、`l_stf_rom.v`、`icmem_*.mem` | OFDM 调制：扰码、卷积编码、穿孔交织、QAM 映射、IFFT | `openofdm_tx_s_axi.v` |
| **`openofdm_rx`** | **git submodule**（`open-sdr/openofdm`，即原 `jhshi/openofdm`，`dot11zynq` 分支），用 `./get_ip_openofdm_rx.sh` 拉取 | OFDM 解调：同步、FFT、信道估计与均衡、Viterbi 译码、CRC 校验 | — |
| **`side_ch`** | `side_ch.v`、`side_ch_control.v`、`side_ch_s_axi.v`、`side_ch_s_axis.v`、`side_ch_m_axis.v`、`side_ch_counter.v`、`side_ch_counter_event_cfg.v`、`dpram.v` | CSI/等化器/IQ 抓取写入 BRAM，再由 DMA 读出到 PS | `side_ch_s_axi.v` |

> `openofdm_rx` 是**子模块**，`ip/` 下没有 `src/`，`git submodule update` 才出现。改它要进子模块提交，`sdk_update.sh` 会把它的 git 信息一并写进 `git_info.txt`。

#### 顶层与板级

| 文件/目录 | 作用 |
|---|---|
| `ip/openwifi_ip.tcl` | **Zynq-7000 用的 openwifi IP 层级**：实例化 5 个 user IP（`openofdm_rx`、`openofdm_tx`、`rx_intf`、`tx_intf`、`xpu`）+ `axi_dma:7.1` ×2 + `axi_interconnect:2.1` ×3 + `proc_sys_reset:5.0`，并连线；`side_ch` 在板级 `system.bd` 里实例化。E316 用这个 |
| `ip/openwifi_ip_ultra_scale.tcl` | ZynqMP / RFSoC 版本（zcu102、rfsoc4x2） |
| `ip/parse_board_name.tcl` | **板卡参数表**：`part_string`、`board_part_string`、`board_id_string`、`fpga_size_flag`、`ultra_scale_flag`。e310v2 → `xc7z020clg400-1`、`fpga_size_flag=0` |
| `ip/create_vivado_proj.sh` | 生成单个 IP 的独立 Vivado 工程（改 IP / 跑仿真用） |
| `boards/ip_repo_gen.tcl` | **板级构建主脚本**：生成 `ip_repo/` 下所有 `*_pre_def.v` 宏文件 → 逐个 `package_ip_complex.tcl` 打包 IP → `source ../openwifi.tcl` 建顶层工程并跑综合实现 |
| `boards/<board>/set_files.tcl` | 该板要加进工程的 RTL、XDC、IP 仓库路径（e310v2 含 `ad5640_spi.v`、`ppsloop.v`） |
| `boards/<board>/src/system.bd` | **Block Design**（ADI 参考设计改造版）：AD9361 IP、DMA、interconnect、openwifi 层级 |
| `boards/<board>/src/system_top.v` | 顶层 wrapper（含 openwifi 版本号与 git rev 打印逻辑） |
| `boards/<board>/src/system.xdc` | 引脚/时序约束 |
| `boards/<board>/synth_impl_strategy.tcl` | 综合实现策略 |
| `boards/post_script_common.tcl` | 建完工程后的公共收尾：`open_bd_design`、强制 `util_ad9361_divclk` 出 40 MHz、`upgrade_ip` 五个 IP |
| `boards/pack_hdf_bit.sh`、`boards/sdk_update.sh` | 打包 `.xsa`/`.ltx` 并归档到 `openwifi-hw-img` |
| `prepare_adi_lib.sh` / `prepare_adi_board_ip.sh` | 准备 ADI HDL 库（`adi-hdl/library`）与板级 ADI IP（每板一次） |
| `get_git_rev.sh` | 生成 `OPENWIFI_HW_GIT_REV` 宏（会印到 `xpu` 寄存器 63） |

#### 板卡变体开关（`parse_board_name.tcl` → `ip_repo_gen.tcl` 生成宏）

`ip_repo_gen.tcl` 会生成这些宏文件并拷进各 IP 的 `src/`，源码里用 `` `ifdef `` 条件编译：

| 生成文件 | e310v2 的取值 | 含义 |
|---|---|---|
| `clock_speed.v` | `` `define NUM_CLK_PER_US 100 `` `` `define SMALL_FPGA 1 `` | 基带时钟 100 MHz；小 FPGA（FIFO/队列深度缩小） |
| `fpga_scale.v` | `` `define SIDE_CH_LESS_BRAM 1 `` | side_ch BRAM 缩小（E316 只有 7020，BRAM 紧张） |
| `has_side_ch_flag.v` | `` `define HAS_SIDE_CH 1 `` | 是否要 side_ch，改成 `NO_SIDE_CH` 可省资源 |
| `board_def.v` | — | 板级 define |
| `spi_command.v` | `SPI_HIGH 24'h088A01` / `SPI_LOW 24'h008A01` | xpu 通过 SPI 配 AD9361 的指令；`grounded_rf_port=1` 会切成端口接地控制 |
| `openwifi_hw_git_rev.v` | `` `define OPENWIFI_HW_GIT_REV (32'h<sha>) `` | 版本号，可被 `xpu` 寄存器读出验证 bitstream 与 .ko 是否配套 |
| `ip_config/<ip>_pre_def.v` | 首行 `` `define e310v2 `` + 用户传的宏 | IP 级条件编译 |

### 2.3 用户态工具（`user_space/`）

| 工具 | 源目录/文件 | 编译方式 | 用途 |
|---|---|---|---|
| **`sdrctl`** | `sdrctl_src/`（`sdrctl.c`、`cmd.c`、`sections.c`、`nl80211_testmode_def.h`） | **板上** `make`（依赖 libnl-3/genl-3）或用 Buildroot 交叉编 | 读写所有 IP 寄存器：`sdrctl dev sdr0 set/get reg <section> <idx> [val]`。`<section>` 取值：`rf`、`rx_intf`、`tx_intf`、`rx`、`tx`、`xpu`、`drv_rx`、`drv_tx`、`drv_xpu` |
| `sdrctl` 扩展命令 | `cmd.c` 里 `COMMAND(set, ...)` / `COMMAND(get, ...)` | 同上 | `rssi_th`、`tsf`、`slice_total/start/end/idx`、`addr`、`gap` |
| **`side_ch_ctl`** | `side_ch_ctl_src/side_ch_ctl.c` + `iq_capture*.py`、`side_info_display.py` | `gcc -o side_ch_ctl side_ch_ctl.c`（板上） | 读 side_ch 的 CSI/IQ，前导码触发，输出到文件 |
| `inject_80211` / `analyze_80211` | `inject_80211/` | 板上 `make`（需 libpcap） | 注入/解析 802.11 帧、radiotap |
| `arbitrary_iq_gen` | `arbitrary_iq_gen/` | MATLAB 脚本 + `.bin` | 生成任意 IQ 波形，配合 tx_intf 的任意 IQ 模式 |
| `fast_reg_log` | `fast_reg_log/` | 板上 gcc + MATLAB 分析 | 高速寄存器/状态记录 |
| `webserver` | `webserver/index.html` | 静态 | 板上 80 端口的演示页 |
| `host-tools/openwifi_fw_update.py` | `host-tools/` | 宿主机 python | 固件更新工具 |

**脚本类（改这些是最低成本的二次开发）**：

| 脚本 | 什么时候改 |
|---|---|
| `wgd.sh` | 板端加载入口（insmod/s 顺序、test_mode、FPGA 重载） |
| `load_fpga_img.sh` | FPGA 动态重载实现（见 §4.1） |
| `post_config.sh` / `setup_once.sh` | 新板首次装机：建 `/lib/modules/$(uname -r)` 软链、编工具、装 hostapd |
| `rf_init.sh` / `rf_init_11n.sh` | RF 带宽、FIR 滤波器（`openwifi_ad9361_fir*.ftr`） |
| `fosdem.sh` / `fosdem-11ag.sh` | 起 AP：`ifconfig sdr0 192.168.13.1` + dhcpd + hostapd + webserver |
| `hostapd-openwifi.conf` / `-11ag.conf` | SSID、信道、带宽、速率集（11b 必须抑制） |
| `agc_settings.sh`、`set_rx_gain_auto/manual.sh`、`set_tx_lo.sh`、`set_restrict_freq.sh`、`set_lbt_th.sh`、`set_dbg_ch*.sh`、`stat_enable.sh` | 各单项寄存器调节 |
| `slice_cfg.sh`、`set_rx_target_sender_mac_addr.sh` | 时间切片与目标 MAC 过滤 |
| `monitor_ch.sh`、`cw_*.sh`、`nav_disable.sh`、`csi_fuzzer*.sh`、`inject_80211/inject_80211.sh` | 各实验场景 |
| `prepare_kernel.sh`、`boot_bin_gen.sh`、`drv_and_fpga_package_gen.sh`、`transfer_*_to_board.sh`、`populate_*.sh`、`update_sdcard.sh`、`sdcard_boot_update.sh` | 宿主机/板端部署流水线（见 §4） |

### 2.4 板级与启动层（`kernel_boot/`）

| 文件 | 作用 | 改动场景 |
|---|---|---|
| `boards/<board>/devicetree.dts` / `.dtb` | 板级设备树。**openwifi IP 的 AXI 基地址、中断号、DMA 引用全在这里** | 新增 IP、改地址、改中断 → 必须改 |
| `boards/overlays/<board>.dtso` + `openwifi_32_ad9361.dtso` | 相对 ADI 默认设备树的 overlay | 用 `construct_device_tree.sh` 生成完整 dtb |
| `boards/<board>/u-boot.elf` | 板级 U-Boot | 一般不动 |
| `boards/construct_device_tree.sh <board> <32\|64>` | `dtc`+`fdtoverlay` 生成 `devicetree.dtb` | 改完 dts 后跑 |
| `build_boot_bin.sh <xsa> <u-boot.elf>` | 用 `xsct` 造 FSBL + `bootgen` 出 `BOOT.BIN`（`system_top.bit.bin` 由 `user_space/boot_bin_gen.sh` 出） | 出 BOOT.BIN |
| `kernel_config` / `kernel_config_zynqmp` | 32 位 / 64 位内核 defconfig | 加内核模块/驱动开关 |
| `10-network-device.rules`、`70-persistent-net.rules` | udev 规则（sdr0 命名相关） | 网卡名不对时 |

**e310v2 的 AXI 地址表**（来自 `devicetree.dts` 实测）：

| 模块 | 基地址 | 大小 | 中断 |
|---|---|---|---|
| `tx_intf` | `0x83C00000` | 64 KB | SPI 34 |
| `openofdm_tx` | `0x83C10000` | 64 KB | — |
| `rx_intf` | `0x83C20000` | 64 KB | SPI 29 / 30 |
| `openofdm_rx` | `0x83C30000` | 64 KB | — |
| `xpu` | `0x83C40000` | 64 KB | — |
| `side_ch` | `0x83C50000` | 64 KB | — |
| `tx_dma` (axi_dma) | `0x80400000` | 64 KB | SPI 35 / 36 |
| `rx_dma` (axi_dma) | `0x80410000` | 64 KB | SPI 31 / 32 |
| `cf-ad9361-lpc` | `0x79020000` | 24 KB | — |
| `cf-ad9361-dds-core-lpc` | `0x79024000` | — | — |
| `sdr`（聚合节点） | 无 `reg`，只有 dmas/interrupts | — | 29/30/33/34 |

---

## 3. 心法：改一个功能，到底要动几个地方？

这是 openwifi 二次开发里最容易卡住的地方。**取决于你改到哪一层**，改动点数量差别很大：

### 场景 A｜纯参数/策略级（0 编译 或 只编用户态）

改 `hostapd-openwifi.conf`、`*.sh`、`sdrctl` 命令的参数。**不碰任何 C 和 Verilog**。

```bash
# 板上
cd ~/openwifi
vi hostapd-openwifi.conf          # 改 channel / hw_mode / ssid / supported_rates
./fosdem.sh                        # 重启 AP

# 运行时微调（不重启）
./sdrctl dev sdr0 set reg xpu 8 30        # LBT 门限
./sdrctl dev sdr0 get reg rf 0            # 读 TX 衰减
./sdrctl dev sdr0 set reg drv_rx 0 100    # 解调门限
iw dev sdr0 set bitrates legacy-5 6 12 24 # 速率
```

**改动点**：1 个文件。**产物**：无。**验证**：`iw`/`iperf3`/CSI 观察。

### 场景 B｜驱动级（改 `.ko`，不重烧 SD）

例：给 `xpu` 加一个"新的 CSMA 参数"，或改 `sdr.c` 的 TX 环管理逻辑。

| 步骤 | 改什么 | 命令 |
|---|---|---|
| 1 | `driver/xpu/xpu.c`（逻辑）+ `driver/hw_def.h`（**若涉及寄存器地址**） | 编辑 |
| 2 | （若动了寄存器地址）`openwifi-hw/ip/xpu/src/xpu_s_axi.v` + `xpu.v` | → 那就升级成场景 C 了 |
| 3 | 编 `.ko` | `cd openwifi/driver && ./make_all.sh $XILINX_DIR 32` |
| 4 | 上板 | `scp $(find driver -name '*.ko') root@192.168.10.122:openwifi/` 或在 `user_space/` 里 `./transfer_driver_userspace_to_board.sh` |
| 5 | 重载（不用重启） | 板上 `cd ~/openwifi && ./wgd.sh` |

**产物**：`sdr.ko` + 六个子模块 `.ko`。**闭环时间**：分钟级（首次编内核 20–60 min，之后增量很快）。**验证**：`dmesg`、`sdrctl get reg`、`iperf3`。

> `make_all.sh` 会自动生成两个**生成文件**，不要手改、不要提交：
> - `driver/pre_def.h` ← 写入 `#define USE_NEW_RX_INTERRUPT 1` + 你从命令行第 3–7 个参数传进来的宏
> - `driver/git_rev.h` ← `#define GIT_REV 0x<short sha>`
> 加条件编译用：`./make_all.sh $XILINX_DIR 32 MY_FEATURE_ABC`（最多 5 个）。

### 场景 C｜FPGA IP 级（改 Verilog，要重综合）

这是 openwifi 真正"硬"的二次开发。改动**必须成组**，否则会出现"寄存器写了没反应"或"驱动 probe 失败"。

以"给 `xpu` 加一个 `CCA_CFG2` 寄存器"为例：

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `openwifi-hw/ip/xpu/src/xpu_s_axi.v` | 地址译码 + 新寄存器读写逻辑（**地址必须与 hw_def.h 一致**） |
| 2 | `openwifi-hw/ip/xpu/src/xpu.v` | 使用新寄存器的功能逻辑 |
| 3 | `openwifi/driver/hw_def.h` | `#define XPU_REG_CCA_CFG2_ADDR (20*4)` + `struct xpu_driver_api` 增加 `_read`/`_write` 函数指针 |
| 4 | `openwifi/driver/xpu/xpu.c` | 实现 `XPU_REG_CCA_CFG2_read/write` + 在 `hw_init` 或 probe 里注册进函数表 |
| 5 | （可选）`openwifi/driver/xpu/xpu.c` | 暴露给 `sdrctl`：在 `sdrctl_intf.c` 加 `OPENWIFI_CMD_*`，在 `user_space/sdrctl_src/cmd.c` 加 `COMMAND(...)` |
| 6 | 编译 | 见 §4 |

**闭环时间**：单 IP 打包 + 顶层综合实现 **1–3 小时**（7020 小片，`impl_1` 用 8 jobs）。所以：

> **先跑 IP 级仿真再综合。** `ip/xpu/unit_test/`、`ip/rx_intf/unit_test/`、`ip/openofdm_tx/unit_test/` 下都有 testbench 和 `test_vec/` 激励。
> ```
> cd openwifi-hw/ip/xpu
> ../create_vivado_proj.sh $XILINX_DIR xpu.tcl e310v2 100  # 生成独立工程
> # Vivado 里 Sources → Simulation Sources → *_tb，Run Behavioral Simulation
> ```
> 仿真第一次编译子 IP 很慢（只一次），之后 Relaunch 很快。

### 场景 D｜新增/更换板卡

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `openwifi-hw/boards/<new>/` | 新建：`set_files.tcl`、`synth_impl_strategy.tcl`、`src/{system.bd,system_top.v,system_wrapper.v,system.xdc}` |
| 2 | `openwifi-hw/ip/parse_board_name.tcl` | 加 `elseif {$BOARD_NAME=="<new>"}` 分支（part、board_part、fpga_size_flag） |
| 3 | `openwifi-hw/boards/ip_repo_gen.tcl` | `ip_name_list` 若新增了 IP 要加进去 |
| 4 | `openwifi-hw/boards/pack_hdf_bit.sh`、`sdk_update.sh` | 板名列表 |
| 5 | `openwifi/kernel_boot/boards/<new>/` | `devicetree.dts`（**AXI 基址必须与 BD Address Editor 完全一致**）、`u-boot.elf`、`overlays/<new>.dtso` |
| 6 | `openwifi/kernel_boot/boards/construct_device_tree.sh` | `openwifi_name_to_kernel_dts` 加映射 |
| 7 | `openwifi/user_space/boot_bin_gen.sh`、`drv_and_fpga_package_gen.sh` | 板名白名单 |
| 8 | `openwifi/user_space/setup_once.sh` | `DEVICE_TREE_MODEL_STRING` → `BOARD_NAME` 的映射 |
| 9 | `openwifi/buildroot-build.sh` + `buildroot-external/support/prepare-board.py` | 若要走 Buildroot 出完整镜像 |
| 10 | 预编译镜像的 `BOOT/openwifi/<new>/` | 三个 BOOT 文件 + `system_top.bit.bin` |

### 场景 E｜新增一个 IP（最重的改动）

在场景 C 的基础上额外要动：

1. `ip_repo_gen.tcl` → `ip_name_list` 追加
2. `ip/openwifi_ip.tcl`（和 `_ultra_scale` 版）→ `create_bd_cell` + 连线 + 时钟复位
3. `boards/<board>/src/system.bd` → 用 Vivado GUI 打开、加进 BD、在 **Address Editor** 分配基址
4. `boards/post_script_common.tcl` → `upgrade_ip [get_ips {...}]` 列表
5. `kernel_boot/boards/<board>/devicetree.dts` → 新增节点，`compatible` 必须**逐字符等于** `hw_def.h` 里的 `*_compatible_str`
6. `driver/Makefile` → `obj-m += <new>/<new>.o`（或像 side_ch 那样单独脚本）
7. `buildroot-external/package/openwifi/openwifi.mk` → `OPENWIFI_MODULE_SUBDIRS` 追加

### 三处同步原则（贴在墙上）

```
Verilog  <ip>_s_axi.v 的地址常量
        ↕  必须完全一致
hw_def.h  #define <IP>_REG_xxx_ADDR
        ↕  必须完全一致
驱动 <ip>.c  的 read/write + sdrctl 的 section/idx
```
任何一处不同步，症状都是"写入成功但行为不变"或"读到旧值"，而且**不报错**。

---

## 4. 编译与产物：五条流水线

### 4.1 快速迭代环：`.ko` + `system_top.bit.bin`（**不烧 SD**）

这是 E316 日常开发的主力路径。

**（1）编驱动**

```bash
export XILINX_DIR=/opt/Xilinx          # 必须含 Vitis 目录
cd openwifi/driver
./make_all.sh $XILINX_DIR 32           # E316 = Zynq-7000 → 32
```

`make_all.sh` 做的事，按顺序：

1. 校验 `$XILINX_DIR/Vitis` 存在、`ARCH_BIT` ∈ {32,64}
2. 生成 `pre_def.h`（`USE_NEW_RX_INTERRUPT 1` + 命令行额外宏）、`git_rev.h`
3. `source $XILINX_DIR/Vitis/2022.2/settings64.sh`
4. 32 位 → `adi-linux/`、`ARCH=arm`、`CROSS_COMPILE=arm-linux-gnueabihf-`；64 位 → `adi-linux-64/`、`arm64`
5. 依次 `make` — `openofdm_tx` → `openofdm_rx` → `tx_intf` → `rx_intf` → `xpu` → `side_ch`（走 `make_driver.sh`）→ `driver/`（出 `sdr.ko`）

产物：`driver/sdr.ko`、`driver/{tx_intf,rx_intf,openofdm_tx,openofdm_rx,xpu,side_ch}/<name>.ko`（共 7 个）。
`ad9361_drv.ko` 和 `xilinx_dma.ko` **不在这里**，它们在预编译镜像的 `/root/kernel_modules32/` 里。

**（2）出 FPGA 的 bit.bin 与 BOOT.BIN**

从 `openwifi-hw-img` 的 `.xsa` 出发（如果你没改 FPGA，就直接用上游的）：

```bash
export OPENWIFI_HW_IMG_DIR=~/git/openwifi-hw-img
cd openwifi/user_space
./boot_bin_gen.sh $XILINX_DIR e310v2 $OPENWIFI_HW_IMG_DIR/boards/e310v2/sdk/system_top.xsa
```

做的三件事：

| 动作 | 结果 |
|---|---|
| `cd ../kernel_boot && ./build_boot_bin.sh <xsa> boards/e310v2/u-boot.elf`（**需要 `xsct`+`bootgen`**） | `boards/e310v2/output_boot_bin/` → `BOOT.BIN`（FSBL + bitstream + U-Boot） |
| 临时生成 `fpga_bit_to_bin.bif` + `bootgen -image ... -process_bitstream bin` | `system_top.bit.bin` |
| 输出位置 | 当前目录，可直接 scp |

**（3）打包 + 上板**

```bash
scp ./system_top.bit.bin root@192.168.10.122:openwifi/
cd openwifi/driver && scp `find ./ -name \*.ko` root@192.168.10.122:openwifi/
# 板端
ssh root@192.168.10.122
cd ~/openwifi && ./wgd.sh
```

或者一次打包（推荐）：

```bash
cd openwifi/user_space
./drv_and_fpga_package_gen.sh $OPENWIFI_HW_IMG_DIR $XILINX_DIR e310v2
# → drv_and_fpga.tar.gz（含 bit.bin + 7 个 ko + git_info.txt + 驱动源码 tar）
scp drv_and_fpga.tar.gz root@192.168.10.122:
# 板端
./wgd.sh drv_and_fpga.tar.gz
```

**（4）板端 `wgd.sh` 到底做了什么**（295 行，读懂它等于读懂了热重载模型）

```
1. insmod ad9361_drv.ko；insmod xilinx_dma.ko（这两个从当前目录取，所以必须与 wgd.sh 同目录）；modprobe mac80211
2. killall hostapd；service dhcpcd stop（dhcp 客户端会给 sdr0 抢出第二个 IP）；killall dhcpd / wpa_supplicant；ifconfig sdr0 down
3. rmmod sdr
4. 若本目录/指定目录有 system_top.bit.bin → 调 load_fpga_img.sh 动态重载 PL
   （OPENWIFI_RELOAD_FPGA=0/1/auto 可控制跳过或强制；Buildroot 系统上若 RF 链已就绪会跳过重复重载）
5. ./rf_init_11n.sh          ← 设 RF 带宽 + 加载 FIR
6. 按序 insmod：tx_intf → rx_intf → openofdm_tx → openofdm_rx → xpu → sdr
   （sdr 这一步把 test_mode 作为模块参数传入）
7. 确认 sdr0 出现（找不到就按 MAC 前缀 66:55:44:33:22:* 改名）
8. mount debugfs；./agc_settings.sh 1
```

**（5）`load_fpga_img.sh` 的重载机制**（为什么它能不重启换 bitstream）

```
ifconfig sdr0 down；rmmod sdr
rmmod openofdm_rx / openofdm_tx / rx_intf / tx_intf / xpu     ← 先卸掉所有会访问 PL 的 openwifi 模块
停掉 iiod
unbind /sys/bus/platform/drivers/cf_axi_adc   79020000.cf-ad9361-lpc
unbind /sys/bus/platform/drivers/cf_axi_dds   79024000.cf-ad9361-dds-core-lpc
unbind /sys/bus/spi/drivers/ad9361        spi0.0
（**故意不解绑 openwifi 的 AXI-DMA**——E200 上 VDMA remove/probe 会访问 PL 寄存器而锁死 AXI 总线）
echo 0 > /sys/class/fpga_manager/fpga0/flags      ← 允许整片重配
cp <bit.bin> /lib/firmware/
echo <bit.bin 文件名> > /sys/class/fpga_manager/fpga0/firmware   ← 真正重配 PL
重新 bind ad9361 → cf_axi_dds → cf_axi_adc；恢复 iiod
```

> 前提：**设备树里必须已经有这些节点**（`fpga_manager`、spi0.0、cf-axi-*）。所以"只换 bitstream 不换设备树"的前提是 **AXI 地址表与 IP 拓扑不变**。一旦你在 BD 里动了地址或增删 IP，就必须换 `devicetree.dtb` + 重启。

### 4.2 内核 / 设备树变更

```bash
sudo apt install flex bison libssl-dev device-tree-compiler u-boot-tools -y
cd openwifi/user_space
./prepare_kernel.sh $XILINX_DIR 32          # 只做一次，20–60 min
```

`prepare_kernel.sh` 实际做的事（**这里有个必知的反直觉点**）：

| 步骤 | 内容 |
|---|---|
| 1 | `git submodule update adi-linux` → 子模块账面 pin 是 `b6e3799`（v4.14），**但下一步被覆盖** |
| 2 | `git fetch && git checkout 2026_R1 && git pull && git reset --hard 2026_R1` → **真正构建的是 ADI 2026_R1，Linux 6.12** |
| 3 | `cp kernel_boot/kernel_config .config`（32 位）或 `kernel_config_zynqmp`（64 位） |
| 4 | `git apply` 4 个补丁：`axi_hdmi_crtc` + `ad9361_v6_12` + `ad9361_private` + `ad9361_conv` |
| 5 | `make oldconfig && make prepare && make modules_prepare` |
| 6 | `make -j12 uImage UIMAGE_LOADADDR=0x8000`（32 位）或 `Image`（64 位）+ `make modules` |

产物：`adi-linux/arch/arm/boot/uImage`、`adi-linux/drivers/**/*.ko`。

上板：

```bash
cd openwifi/user_space
./transfer_kernel_image_module_to_board.sh      # 打包 + scp
./boot_bin_gen.sh $XILINX_DIR e310v2 <xsa>      # 顺带更新 bit.bin/BOOT.BIN
# 板端 /root/ 下
./populate_kernel_image_module_reboot.sh && reboot
```

> ⚠️ 换内核后**必须重编 openwifi 的 7 个 `.ko`**——`vermagic` 不匹配会直接 `insmod` 失败。且 `modules_prepare` 出来的树只能编模块，`make modules` 才能得到 `ad9361_drv.ko`。
> 若 `insmod` 报 symbol/version 错，多半是板上内核比 host 上编的旧，把 `uImage` 也换掉。

### 4.3 FPGA 改动 → 新 bitstream

**（0）一次性准备**

```bash
cd openwifi-hw
export XILINX_DIR=/opt/Xilinx
./prepare_adi_lib.sh $XILINX_DIR                 # 拉 ADI HDL 库（含 adi-hdl/library）
./prepare_adi_board_ip.sh $XILINX_DIR e310v2    # 准备该板 ADI IP（见到 "Building ..." 就能 Ctrl-C）
./get_ip_openofdm_rx.sh                          # git submodule init/update ip/openofdm_rx
```

**（1）改 IP 并单测**

```bash
cd openwifi-hw/ip/xpu
../create_vivado_proj.sh $XILINX_DIR xpu.tcl e310v2 100   # 独立工程，跑 unit_test 仿真
```

`create_vivado_proj.sh` 的额外参数会被写成 `` `define `` 宏：
- 第 3 个参数 = `BOARD_NAME`
- 第 4 个 = `NUM_CLK_PER_US`
- 第 5–9 个 = 用户宏，生成 `` `define XPU_<MACRO> ``

**（2）打包 IP + 建顶层工程 + 综合**

```bash
cd openwifi-hw/boards/e310v2
../create_ip_repo.sh $XILINX_DIR
# 加 ILA / 调试宏（可选）
../create_ip_repo.sh $XILINX_DIR xpu ENABLE_DBG tx_intf ENABLE_DBG rx_intf ENABLE_DBG
```

`create_ip_repo.sh` 会

1. 在 `boards/e310v2/ip_config/<ip>_pre_def.v` 写入 `` `define e310v2 `` + 你传的宏
2. `source $XILINX_DIR/Vitis/2022.2/settings64.sh`
3. `vivado -source ../ip_repo_gen.tcl`

而 `ip_repo_gen.tcl` 会

1. 重建 `boards/e310v2/ip_repo/`，生成本板的所有宏文件（`clock_speed.v`、`has_side_ch_flag.v`、`fpga_scale.v`、`board_def.v`、`spi_command.v`、`openwifi_hw_git_rev.v`），**逐份拷进六个 IP 的 `src/`**
2. 对 `openofdm_rx openofdm_tx rx_intf tx_intf xpu side_ch` 依次走 `package_ip_complex.tcl` 打包成 `ip_repo/<ip>/`
3. `source ../openwifi.tcl` → 建 `openwifi_e310v2` 工程、`update_ip_catalog`、`launch_runs impl_1 -to_step write_bitstream -jobs 8`、`write_hw_platform -fixed -include_bit -o ./openwifi_e310v2/system_top.xsa`

> ⚠️ `openwifi.tcl` 里有 `exec git clean -dxf ./src/` —— **放在 `boards/e310v2/src/` 下没提交的东西会被删掉**。改 `system_top.v`/`system.bd`/`system.xdc` 后务必先 commit。

**（3）归档到 openwifi-hw-img**

```bash
cd openwifi-hw/boards
./sdk_update.sh e310v2 $OPENWIFI_HW_IMG_DIR
# 拷贝 openwifi_e310v2/system_top.xsa + system_top.ltx 到 hw-img，并记录
# openwifi-hw / openofdm_rx 的 branch+commit 到 git_info.txt
```

**（4）回到 §4.1 的 (2) 出 bit.bin / BOOT.BIN**

### 4.4 完整 SD 镜像（Buildroot，E316 官方支持）

这是上游现在主推的路线，**不用 adi-linux 子模块**，用 buildroot 子模块 + ADI Linux 6.12（commit `40201abd`）。

```bash
git submodule update --init buildroot
export OPENWIFI_HW_IMG_DIR=~/git/openwifi-hw-img     # 或用 OPENWIFI_XSA 直接指 .xsa
./buildroot-build.sh e310v2            # build（默认）
```

支持的三块板：`antsdr_e200`、`antsdr`、`e310v2`。子命令：

| 命令 | 作用 |
|---|---|
| `build` | 准备板级输入 → 构建/复用共享系统 → 装配本板的镜像 |
| `rebuild-system` | 强制 `linux-dirclean` + `openwifi-dirclean` + `libad9361-iio-dirclean` 后整体重编，再装配 |
| `configure` | 重新生成板级输入并载入 `openwifi_common_defconfig` |
| `menuconfig` | 打开共享 Buildroot 配置 |
| `clean` | 只删本板输出，保留共享系统 |

**输出布局**（这是设计上的关键点）：

| 目录 | 内容 | 说明 |
|---|---|---|
| `output/common/` | `.config`、`.openwifi-common-ready`、`images/rootfs.ext4`、`images/uImage` | **三块板共享**的内核与 rootfs |
| `output/e310v2/` | `generated/`、装配出的最终镜像 | 板级独有；每次 `build` 只 `uboot-dirclean && uboot` 重编该板 U-Boot |
| `dl/` | Buildroot 下载缓存 | 首次很慢 |

**`buildroot-external/package/openwifi/openwifi.mk` 做的事**（读它就知道镜像里 openwifi 怎么放的）：

- `OPENWIFI_SITE` = 仓库根（local 包），`OPENWIFI_MODULE_SUBDIRS = driver driver/side_ch`（内核模块由 Buildroot 编）
- `POST_RSYNC_HOOKS` 生成 `driver/pre_def.h`（`USE_NEW_RX_INTERRUPT 1`）与 `driver/git_rev.h`（来自 `git rev-parse`）
- `BUILD_CMDS` 交叉编 `sdrctl_src`、`side_ch_ctl_src`、`inject_80211`
- `INSTALL_TARGET_CMDS` 把 `user_space/` 整个拷到 rootfs 的 `/root/openwifi/`，装 `openwifi-start`、`S02openwifi-board`、`openwifi-fw-update`
- `INSTALL_LOCAL_MODULE_COPIES` 把 `.ko` 放到 `/root/openwifi/`，并把 `wgd.sh`/`fosdem.sh` 里 Debian 系的命令（`service`、`webfsd`、`dhcpcd`、`sudo`）**sed 替换成 BusyBox 版**

### 4.5 烧 SD

| 场景 | 要覆盖的文件 | 备注 |
|---|---|---|
| 只换 FPGA（地址表不变） | `BOOT/system_top.bit.bin` | 也可热重载，不必重烧 |
| 换 FPGA + 内核 + dtb | `BOOT/{BOOT.BIN, uImage, devicetree.dtb, system_top.bit.bin}` | BOOT.BIN 走 `boot_bin_gen.sh` |
| 换 rootfs 内容 | Buildroot 出的 rootfs 分区镜像 | |
| 全新烧录 | `dd bs=512 count=31116288 if=openwifi-xyz.img of=/dev/rdiskX`（Mac）/ Startup Disk Creator（Ubuntu） | 见烧录指南 |

**BOOT 分区不放行的话，Zynq-7000 起不来**：BootROM 只在 FAT 根按固定名找 `BOOT.BIN`；`uEnv.txt` 里的 `adi_sdboot` 变量负责 `fatload uImage/devicetree.dtb`；`devicetree.dtb` 决定 `sdr0` 能不能 probe 出来。详见烧录指南。

---

## 5. 验证与调试：出问题时看哪里

| 现象 | 先查 | 命令 |
|---|---|---|
| `sdr0` 不出现 | 驱动 probe 日志、设备树是否匹配 | `dmesg \| grep -i openwifi`；`cat /proc/device-tree/model` |
| `insmod` 失败 | vermagic / 依赖顺序 | `modinfo <ko>`；`dmesg \| tail` |
| 关联网卡名不是 `sdr0` | MAC 前缀 | `ip link`；前缀应为 `66:55:44:33:22:*` |
| 寄存器写了没反应 | **三处同步**是否一致 | `./sdrctl dev sdr0 get reg xpu 63`（读 FPGA git rev，验证 bitstream 与 .ko 配套） |
| 收不到包 | RX 功率阈值 / 天线 | `./rssi_openwifi_show.sh`、`./rssi_ad9361_show.sh`、`./sdrctl dev sdr0 set reg drv_rx 0 <th>` |
| 大概 2 小时后突然不工作 | **Viterbi 评估 license 超时** | `./sdrctl dev sdr0 get reg rx 20` 输出恒定不变即中招 → 重载 FPGA 或断电重启 |
| 5 GHz 信号弱 | 天线口 / 频段 | E316 上 `country_code=BE`、`channel=36`（5180 MHz），天线必须支持 5 GHz |
| 想确认天线开关状态 | `openwifi_set_antenna` 日志 | `dmesg \| grep openwifi_set_antenna` |
| SPI 配置 AD9361 失败 | `xpu` 的 `spi_command.v` 宏 | 见 §2.2 板卡变体开关 |

**寄存器速查（`sdrctl` 的 section / idx）**

| section | 对应 | 常用 idx |
|---|---|---|
| `rf` | AD9361 射频 | 0=TX 衰减(dB×1000)、1=TX 频点、4=RX 增益、5=RX 频点 |
| `rx` | FPGA `openofdm_rx` | 0=MULTI_RST、2=解调门限(POWER_THRES)、3=MIN_PLATEAU、4=SOFT_DECODING、5=FFT_WIN_SHIFT、18=PHASE_OFFSET_ABS_TH、20=STATE_HISTORY |
| `tx` | FPGA `openofdm_tx` | 0=MULTI_RST、1=INIT_PILOT_STATE、2=INIT_DATA_STATE |
| `xpu` | FPGA `xpu`（低 MAC） | 0=MULTI_RST、4=BAND_CHANNEL、5=DIFS_ADVANCE、8=LBT_TH、9=CSMA_DEBUG、11=ACK_CTL_MAX_NUM_RETRANS、63=git rev |
| `rx_intf` / `tx_intf` | PL 接口 | 见 `hw_def.h` 的 `*_REG_*_ADDR` / 4 |
| `drv_rx` | 驱动内部变量 | 0=解调门限、4=天线配置、7=打印配置 |
| `drv_tx` | 驱动内部变量 | 0/1/2/3=速率(legacy/HT/VHT/HE)、4=天线配置、7=打印配置 |
| `drv_xpu` | 驱动内部变量 | 0=LBT 门限、7=git rev |

---

## 6. 我认为最有价值的几件事

### 6.1 明确"成本梯度"，选对起点

| 层级 | 最小闭环 | 需要 Vivado | 典型耗时 | 适合做的事 |
|---|---|---|---|---|
| 参数/脚本 | scp + 重启 AP | ❌ | 秒 | 调信道/带宽/速率/CCA、起 AP/STA/Ad-hoc/Monitor |
| 用户态工具 | 板上 `make` | ❌ | 分钟 | CSI 采集、IQ 抓取、帧注入/模糊测试、上层感知应用 |
| 驱动 | `make_all.sh` + `wgd.sh` | ❌ | 分钟（首次编内核除外） | 自定义调度、速率控制、新增寄存器读写封装 |
| 内核/dtb | `prepare_kernel.sh` + 重烧 BOOT | ❌ | 1 小时起 | 换内核版本、加内核模块、改地址表 |
| FPGA IP | 仿真 → 打包 → 综合 → 重载 | ✅ | **1–3 小时/次** | 改 PHY/MAC 硬件逻辑、加新硬件功能 |
| 新板 | 全套 | ✅ | 天 | 移植新硬件 |

**建议的 E316 起步顺序**：先把 §3 场景 A（换信道、抑制 11b、CSI 采集、`inject_80211`）走通，建立"寄存器 ↔ 现象"的直觉；再进场景 B；最后才碰场景 C。直接上 FPGA 改动的失败率最高，且你会分不清是 Verilog 错还是驱动没同步。

### 6.2 最适合作为二次开发切入点的三处

1. **`side_ch` + `side_ch_ctl`（CSI/IQ）**：改动小、不涉及 PHY 主链路、成果立即可视化（CSI 时频图），是感知类研究的标准入口。upper 层 Python/MATLAB 脚本在 `user_space/side_ch_ctl_src/` 里，改起来完全无痛。
2. **`xpu`（低 MAC）**：所有"非标准 MAC 行为"的实验都在这里——改 `cw_exp.v`/`csma_ca.v`/`difs`/`slot` 做新退避算法，改 `time_slice_gen.v` 做时间门控调度，改 `pkt_filter_ctl.v` 做选择性接收。寄存器接口成熟，且 `unit_test/` 有现成 tb。
3. **`tx_intf` 的 `csi_fuzzer.v`**：能在发送端造出人为信道响应，是"物理层欺骗/鲁棒性"类研究的独特能力，上游已带 app note（`doc/app_notes/csi_fuzzer.md`）。

### 6.3 几条容易吃大亏的坑

1. **生成文件不要手改**：`driver/pre_def.h`、`driver/git_rev.h`、`boards/<b>/ip_repo/*.{v}`、`boards/<b>/ip_config/*_pre_def.v`、`ip/<ip>/src/{board_def,clock_speed,fpga_scale,has_side_ch_flag,spi_command,openwifi_hw_git_rev}.v`。它们每次构建都被覆盖。你手写的东西应该进 `boards/<b>/src/` 或 IP 的 `src/`。
2. **`openwifi.tcl` 会 `git clean -dxf ./src/`**：`boards/<board>/src/` 里未提交的改动会被删。
3. **`xilinx_dma` 走的是 Xilinx SDK 2018.3**，且会**覆盖**内核树里的 `xilinx_dma.c`（脚本里回滚那行是注释掉的）。换内核后要重跑。
4. **e310v2 是 SMALL_FPGA**：`SMALL_FPGA 1` + `SIDE_CH_LESS_BRAM 1`。IPCORE 里的 FIFO/BRAM 深度是缩过的（`hw_def.h` 里 `dma_symbol_fifo_size_hw_queue[]` 注释明说"要跟 `tx_intf_s_axis.v` 对齐"）。想在 7020 上加逻辑，资源预算要非常小心。
5. **Viterbi 评估 license 只给你 2 小时**。长时间跑实验必须在脚本里加"检测 `sdrctl get reg rx 20` 是否停滞 → 重载 FPGA"的看门狗。商用 license 要去买。
6. **`prepare_kernel.sh` 会 `git reset --hard 2026_R1`**，把子模块切到 Linux 6.12，无视 `.gitmodules` 里的 v4.14 pin。别照子模块 pin 去理解驱动行为。
7. **bitstream 与 `.ko` 是配套的**。`xpu` 寄存器 63 存了 FPGA 的 git short hash，`hw_def.h` 里的寄存器偏移也依赖 RTL 版本。用新 `.ko` 配旧 bitstream（或反之）会出现"能加载但行为诡异"，比报错更难查。
8. **`ad9361_drv.ko` 不是 openwifi 编出来的**：它来自带 4 个补丁的 ADI 内核。改 RF 行为要先确认你改的是 openwifi 侧（`rf_init*.sh`、`sdr.c` 里的 `ad9361_set_*` 调用、`xpu` 的 SPI 落盘寄存器）还是 ADI 驱动侧。
9. **E316 是 FDD 双天线**：默认 `tx_ant/rx_ant` 走不同 SMA 口，`sdr.c` 里 `tx_ant` 取值范围 1/2/3、`rx_ant` 1/2。"给另一个链加满衰减"（`AD9361_RADIO_OFF_TX_ATT`）是 TX 选口的实现方式，改天线逻辑时别误以为动了物理开关（开关由 AD9361 `CTRL_OUT` 引脚驱动）。
10. **`side_ch` 的驱动不在 `driver/Makefile` 的 `obj-m` 里**，单独 `make_driver.sh`。忘了编它，`wgd.sh` 会在 `insmod side_ch` 那步失败。

### 6.4 一份最小可用的"改 FPGA 后回归清单"

```
□ ip/<name>/unit_test 仿真通过
□ <name>_s_axi.v 的地址常量 == hw_def.h 的 #define
□ <name>.c 里 read/write 已注册进 struct <name>_driver_api
□ 若新增 IP：ip_repo_gen.tcl / openwifi_ip.tcl / system.bd Address Editor / devicetree.dts 四处齐了
□ create_ip_repo.sh 无报错（有报错就去看它和 ip_repo_gen.tcl 的 include 列表）
□ sdk_update.sh 归档后，hw-img 的 git_info.txt 已更新
□ boot_bin_gen.sh 出的 system_top.bit.bin 与 .ko 同批
□ 板上 wgd.sh 后 sdrctl get reg xpu 63 == hw-img 里的 openwifi-hw commit 前 7 位
□ iperf3 双向吞吐 & RSSI 正常
```

---

## 附录 A：改动点速查表

| 我想改… | 改这些文件 | 产物 | 上板方式 |
|---|---|---|---|
| SSID / 信道 / 带宽 / 速率集 | `user_space/hostapd-openwifi*.conf` | — | 重跑 `fosdem.sh` |
| AP 启动流程 / 网络配置 | `user_space/fosdem.sh`、`dhcpd.conf` | — | scp |
| 某个运行时参数 | `sdrctl set reg` 或 `set_*.sh` | — | 直接执行 |
| RF 带宽 / FIR | `user_space/rf_init*.sh`、`openwifi_ad9361_fir*.ftr` | — | scp |
| AGC | `user_space/agc_settings.sh` | — | scp |
| CPU 侧 MAC 逻辑 | `driver/sdr.c`、`sdr.h` | `sdr.ko` | `wgd.sh` |
| 某个 IP 的寄存器读写在软件侧 | `driver/<ip>/<ip>.c`、`driver/hw_def.h` | `<ip>.ko` | `wgd.sh` |
| `sdrctl` 命令 | `driver/sdrctl_intf.c`、`user_space/sdrctl_src/cmd.c` | `sdr.ko` + `sdrctl` | `wgd.sh` |
| sysfs/debugfs 节点 | `driver/sysfs_intf.c` | `sdr.ko` | `wgd.sh` |
| PHY/MAC 硬件逻辑 | `openwifi-hw/ip/<ip>/src/*.v` | 新 `system_top.xsa` → `bit.bin` / `BOOT.BIN` | `wgd.sh <tarball>` 或烧 BOOT |
| 板上引脚/约束 | `openwifi-hw/boards/e310v2/src/system.xdc` | 新 bitstream | 同上 |
| AD9361 直配指令 | `ip_repo_gen.tcl` 的 `spi_command.v` 生成段 | 新 bitstream | 同上 |
| 硬件 IP 拓扑（增删 IP） | BD + `Address Editor` + `ip_repo_gen.tcl` + `openwifi_ip.tcl` + `devicetree.dts` + `hw_def.h` + `Makefile` | 新 bitstream + 新 dtb | **烧 BOOT** |
| 内核版本 / 内核模块 | `kernel_boot/kernel_config*`、`prepare_kernel.sh` | `uImage` | 烧 BOOT |
| 设备树任何东西 | `kernel_boot/boards/e310v2/devicetree.dts` + `construct_device_tree.sh` | `devicetree.dtb` | 烧 BOOT |
| rootfs 里的工具/服务 | `buildroot-external/package/openwifi/openwifi.mk`、`buildroot-external/board/common/*` | 新 rootfs | 烧 SD |
| 加一块新板 | 见 §3 场景 D（10 处） | 全套 | 全套 |

## 附录 B：上游文档索引（别重复造轮子）

| 想了解 | 看哪里 |
|---|---|
| 快速上手 / 更新驱动 / 更新 FPGA | `openwifi/README.md` |
| FPGA 构建、改 IP、跑仿真、条件编译宏、迁移 Vivado 版本 | `openwifi-hw/README.md` |
| 驱动与 FPGA 动态重载原理 | `openwifi/doc/app_notes/drv_fpga_dynamic_loading.md` |
| CSI 格式与用法 | `doc/app_notes/csi.md` |
| IQ 抓取（含双天线） | `doc/app_notes/iq.md`、`iq_2ant.md` |
| 帧注入与模糊测试 | `doc/app_notes/inject_80211.md` |
| CSI fuzzer | `doc/app_notes/csi_fuzzer.md` |
| CSI radar / 自感知 | `doc/app_notes/radar-self-csi.md` |
| DCF/SIFS/CCA 等"技巧"参数 | `doc/app_notes/frequent_trick.md` |
| 802.11n / 40MHz / 保护间隔 | `doc/app_notes/ieee80211n.md` |
| Ad-hoc / AP-STA 两机互连 | `doc/app_notes/ad-hoc-two-sdr.md`、`ap-client-two-sdr.md` |
| 性能计数器、驱动统计 | `doc/app_notes/perf_counter.md`、`driver_stat.md` |
| HLS / ASIC 方向 | `doc/app_notes/hls.md`、`doc/asic/` |
| 从零构建镜像（Kuiper / OpenWrt / Buildroot） | `doc/img_build_instruction/` |
| 已知问题 | `doc/known_issue/notter.md` |
| 移植新板 | `openwifi/README.md` 的 Porting guide |
