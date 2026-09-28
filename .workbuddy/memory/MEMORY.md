# OpenWiFi 项目长期笔记

项目文档 `OpenWiFi-接收链路入门资料.md`（链路级：时钟域/流水线/文件清单/对齐原理）。该文档用 **①②③** 标域：① 板载射频/模拟（天线、AD9361，不在 Zynq 内）、② PL、③ PS；§2 图按此分 subgraph，§3 表格有"所在域"行，§5 清单有"所在域"列，**后续增改沿用这套编号**。**用户要求 §2 图保持干净**：节点只写模块名 + 2~4 字作用，边标只写 `LVDS`/`sample0`/`csi`/`rssi` 这类短词，**时钟频率、位宽、寄存器名、AXI 基址、行号一律不进图**，全部下沉到 §3+ 的正文/表格。openofdm_rx 逐级实现细节见 `OpenWiFi-openofdm_rx-解包链路.md`。

## 板卡与环境

- E316 = E310V2 → `BOARD_NAME=e310v2`（老 E310=`antsdr`，E200=`antsdr_e200`）。板 `192.168.10.122` / PC `192.168.10.1`，root/openwifi，AP 段 192.168.13.0/24。镜像 `users.ugent.be/~xjiao/openwifi-1.5.0-shahecheng.img.xz`，烧录已完成。
- 启动链：BootROM→FAT `BOOT.BIN`→FSBL 配 PL→U-Boot `env import uEnv.txt`（`adi_sdboot` 在此）→fatload `uImage`+`devicetree.dtb`；FAT 里 zynq-*/树莓派文件是 Kuiper 遗物。BOOT 分区按板覆盖 `BOOT.BIN`/`devicetree.dtb`/`system_top.bit.bin`。板端 `/root/openwifi`、`/root/kernel_modules32`。
- macOS 只做编辑+scp+烧 SD；挂 ext4、Vivado、内核构建须 Linux x86_64。Zynq-7000 免 Vivado 授权，`ARCH_BIT=32`。
- `adi-linux` pin 是 v4.14 假象：`prepare_kernel.sh` 强制 `reset --hard 2026_R1` → 实际 Linux 6.12（Buildroot 路线用 `40201abd`）。
- `make_all.sh $XILINX_DIR 32` 出 7 个 .ko；`side_ch.ko` 走单独 `make_driver.sh`；`ad9361_drv.ko`/`xilinx_dma.ko` 来自 ADI 内核，须与 `wgd.sh` 同目录。
- 勿手改生成文件：`pre_def.h`、`git_rev.h`、`ip_repo/*`、各宏 `.v`；`openwifi.tcl` 会 `git clean -dxf ./src/`。
- e310v2 = SMALL_FPGA + SIDE_CH_LESS_BRAM，BRAM 紧张。Viterbi 评估 license 2 小时（`sdrctl get reg rx 20` 停滞即中招）。bitstream 与 .ko 配套（验 `sdrctl get reg xpu 63`）。
- AXI 基址：tx_intf 0x83C00000 / openofdm_tx 0x83C10000 / rx_intf 0x83C20000 / openofdm_rx 0x83C30000 / xpu 0x83C40000 / side_ch 0x83C50000；tx_dma 0x80400000、rx_dma 0x80410000、cf-ad9361-lpc 0x79020000。
- 子模块：`.gitmodules` 只有 `adi-hdl`、`ip/openofdm_rx`，**本地皆空**（需 `git submodule update`）；`ip/openofdm_tx/` 有内容且不在 .gitmodules。
- `set_files.tcl` = 板级文件清单；`system.bd` 是入库成品；`side_ch_0` 只在 `ip/openwifi_ip_ultra_scale.tcl`（224-225/264/274/278/291-294）与 `post_script_common.tcl`（16-17）出现。

## sdrctl 语义

- `rf/rx_intf/tx_intf/rx/tx/xpu` = FPGA 寄存器（idx = ADDR/4）；`drv_rx/drv_tx/drv_xpu` = 驱动变量。
- `drv_rx 0`=解调门限、`drv_rx 4`=天线；FPGA `rx 0`=MULTI_RST（**写它=复位**）、`rx 2`=POWER_THRES；`xpu 8`=LBT_TH、`xpu 63`=git rev；`xpu 1 1`=解静音 Rx 基带（默认自发窗口被掐）。

## E316 射频口

- SMA 左→右：`TX/RX1`·`RX1`·`RX2`·`TX/RX2`。FDD → TX 走 TX/RX1、RX 走 RX1（默认 2 天线）。天线开关由 AD9361 CTRL_OUT 驱动（`openwifi_set_antenna()`）；TX 换口 = 另一链加满衰减。`set_ant.sh` 已不存在，用 `iw phy`。吸盘天线是 GPS，接 PPS/10M 口。AP 默认 5 GHz ch36，`hw_mode=a`。

## 接收链硬事实

- 速率链：AD9361 **40 MSPS**（`rf_init.sh`：RX BW 17.5 MHz / TX BW 37.5 MHz / LO 1 GHz / `fast_attack` / `openwifi_ad9361_fir.ftr`；11n 脚本 BW 25.215513 MHz）→ `axi_ad9361`(LVDS) → `util_wfifo` → `util_cpack2` → `adc_intf` 每 2 个 valid 取 1（`adc_valid_decimate=(adc_valid_count==0)`）→ 20 Msps（50 ns/样本）。40→20 是官方设计（`openwifi/doc/README.md` 的 Analog and digital frequency design）。
- **位宽 12→16 在 `axi_ad9361` 内完成**（符号扩展，数值不变）：LVDS 线上 12 bit（2R2T 下一帧 4 样本 = 8 时钟沿 / 4 完整周期，由 FRAME 划分）；从 `adc_data_i0/q0/i1/q1` 起全 16 bit（`util_cpack2` 4×16=64 bit）。TX 反向不对称：16 bit 只取**高 12 位**，写 DAC 要左移 4 位。来源：ADI wiki + EngineZone 112155。
- DATA_CLK = **4×fS（2R2T LVDS DDR）**=160 MHz；`util_wfifo` **不是通用异步 FIFO，只支持 1:1/1:2/1:4/1:8 固定整数比**，两侧时钟必须同源（EngineZone 586311）。
- 时钟：`l_clk`=160 MHz →BUFR÷4→`adc_clk` 40 MHz（`post_script_common.tcl:10`）→`clk_wiz_0`（`PRIM_IN_FREQ=40`、`CLKOUT1_REQUESTED_OUT_FREQ=100`，MMCM 25/10）→**100 MHz** = 全 openwifi IP（含其 s00_axi/m00_axis clk）+ 两 DMA + 3 个互连 + PS `M_AXI_GP1`。该网在 BD 里**误名 `sys_ps7_FCLK_CLK2`**，进 `openwifi_ip` 的端口名是 `m_axi_mm2s_aclk`（**名字都骗人**）。PS 出的时钟：FCLK0=100 MHz（`sys_cpu_clk`→GP0/`axi_ad9361`/`axi_gpreg`/IIC + `xpu_0/ps_clk`）、FCLK1=200 MHz 只给 `delay_clk`、FCLK2=200 MHz 给 `gmii_to_rgmii`。`board_def.v` `SAMPLING_RATE_MHZ 20` → 5 clk/样本。**改采样率必须重综合 PL**。
- `i0/q0/i1/q1` 的 0/1 = **RX 通道号**（`rx_intf.v:237-238`：`sample0={I0,Q0}`）。2T2R ⇒ ≤2 路复基带；两 RX 共享 RX LO ⇒ 双天线只能同频（MIMO/分集）。4 个 SMA ≠ 4 路 RX。
- **`sample0` 一根线三处并联抽头**（`ip/openwifi_ip_ultra_scale.tcl:327`，网名 `rx_intf_0_sample`）：`openofdm_rx_0/sample_in`、`side_ch_0/sample0_in`、`xlslice_0|1`→`xpu_0/ddc_i|ddc_q`。三处互不转手样本，xpu 只是抽头。`sample_strobe` 共用（:329，另送 `xpu_0/ddc_iq_valid`）。`sample1` 只接 side_ch（`side_ch.v:53/319`）。
- **`rssi_half_db` 是 xpu 测出的共享标量（11 bit，0.5 dB 步进），不是门限**：`xpu.v:44` 输出，`rssi.v` 用 `ddc_i/q`+`gpio_status`(AGC gain)+`slv_reg7` 校准偏移算；同一根网（`openwifi_ip_ultra_scale.tcl:396`）送 `openofdm_rx`（与其 reg2 `POWER_THRES`=124 比 → 功率门）和 `side_ch`（与 reg9 比 → RSSI 过阈触发，并逐样本写进 IQ 记录）；`rssi_half_db_lock_by_sig_valid`（:397；在 `pkt_header_valid_strobe` 拍锁存，名字里的 sig_valid 是遗留叫法，`rssi.v:126`）送 `rx_intf` 填 16 字节头 → mac80211 signal；软件读 xpu reg57（`rssi_openwifi_show.sh`），xpu CCA 用 reg8 比。**`xpu.channel`=`slv_reg4[15:0]` 只接 `tx_intf`**（发送侧信道切换），不接 openofdm_rx。
- **`sample_strobe` 语义**：20 Msps 样本有效**单周期脉冲**，= `adc_intf` 的 `data_to_bb_valid`（40 MSPS 每 2 个取 1，`adc_valid_decimate=(adc_valid_count==0)`），经 `rx_iq_intf` 的 `rf_iq_valid` 输出（`rx_iq_intf.v:55/152`，`slv_reg3[4]=1` 时切成 FIFO 读出节奏 `rden`）。名字叫 strobe 不叫 valid 是 openwifi 惯例：单周期使能，数据总线可保持旧值。官方 `doc/` 里搜不到这个词。
- **DMA 交叉映射**：`rx_dma`(0x80410000=`axi_dma_1`) S2MM←`rx_intf_0/m00_axis`、MM2S→`side_ch_0/s00_axis`；`tx_dma`(0x80400000=`axi_dma_0`) MM2S→`tx_intf_0/s00_axis`、S2MM←`side_ch_0/m00_axis`。`axi_interconnect_1` = PS `M_AXI_GP1` → 7 个 IP 的 `s00_axi`。**驱动实际只认领 3 条通道**（`dma_request_chan` 按 dts 的 `dma-names` 取）：`sdr.ko` 取 `rx_dma_s2mm`+`tx_dma_mm2s`（`sdr.c:1661/1671`），`side_ch.ko` 只取 `tx_dma_s2mm`（`side_ch.c:603`）；`rx_dma_mm2s`（→`side_ch_0/s00_axis`）在 `side_ch.c:596-602` 整段被注释，**硬件有线但驱动未启用**。DMA 通道与缓冲区一一对应、互不复用，PS 侧不存在"分发给哪个 ko"的判断。
- mac80211（`driver/sdr.c`）：`ieee80211_alloc_hw(&openwifi_ops)`(:2256)→`register_hw`(:2619)；ops(:2170) 含 tx/start/stop/add_interface/config/set_antenna/*_tsf/bss_info_changed/conf_tx/configure_filter/ampdu_action，`.wake_tx_queue`、`.add|remove|change_chanctx=ieee80211_emulate_*`。上行 `ieee80211_rx_irqsafe`(:617)，先填 `rx_status.mactime`+`RX_FLAG_MACTIME_START`(:586)；下行 `openwifi_tx`(:991) 收到的已是成形 MPDU，完成回 `ieee80211_tx_status_irqsafe`(:844)。**控制反转**：驱动是框架的硬件后端。radiotap 由 mac80211 rx 路径用 rx_status 组装，TSFT=`mactime`（不是驱动加的）。

## 数据获取三条路（解码帧 / CSI / IQ）

- ①解码帧：`rx_dma`→`sdr.ko`（16 B 头：字 0 = 64 bit TSF，字 1 = phase_offset/HT-agg/SGI/rate/len/AGC/RSSI；帧尾字节 bit7 = fcs_ok）→mac80211→radiotap；②CSI：`insmod side_ch.ko`（记录 2+56+num_eq×52 B，num_eq=8→3792 B）；③IQ：`insmod side_ch.ko iq_len_init=N`（N>0；7Z020 上 ≤4095≈204.75 µs）。
- **CSI 与 IQ 互斥只在 side_ch 搬运层**：`side_ch_control.v:313` 2:1 mux → 单 FIFO/单 DMA；CSI 组装 FSM 被 `if(iq_capture==0)` 门控。数据源本来就是同一份 `sample0`。
- IQ 记录格式：第 0 个 64 bit = 触发 TSF；每样本 4×int16：w0=I、w1=Q、w2(bit7 AGC lock / bit6:0 gain / bit15 ch_idle)、w3(bit10:0 RSSI half-dB / bit15 demod / bit14 tx_rf / bit13 fcs_ok)。**无逐样本计数器**；触发点下标 = `pre_trigger_len`，窗口起点 = TSF − `pre_trigger_len`/20 µs。
- **IQ 记录的 TSF = 触发时刻，不一定等于任何包的 TSF**（选 FCS/前导触发才与包直接对应）；官方给了用 `wlan_radio.timestamp` 区间过滤 wireshark 的方法（openwifi Discussion #344）。
- IQ 触发 `iq_trigger_select`(reg8) 32 选 1（0=FCS 时刻默认、8=long preamble、10/11=RSSI 过阈、12/13=AGC、25=addr 匹配、28~31=对侧天线 IQ 超阈）；CSI 走 6 态 FSM 按 FC→addr1→addr2 匹配（reg1[14:12] + reg5/6/7，命令 `wh1h4001`/`wh5h`/`wh6h`/`wh7h`）后等 `last_ofdm_symbol_flag`。`iq_source_select`=reg5[2:1]（0=RX 基带、1=openofdm_tx、2/3=tx_intf）。
- **快照 ≠ 解码器看到的信号**：解码器吃的是经时域 `rotate` + 频域 `rot_after_fft` 纠相后的样本，快照是未纠正原样（`rx_intf.v` 不做 CFO/DC 纠正，`phase_offset_taken` 只进帧头）；解码器有状态机与离散判决，换切窗点就解不出来 → 不能离线用快照重建解码结果。
- **e310v2 上没有第二条 IQ 出口（ADI IIO 数据路是断的）**：BD 里 `util_ad9361_adc_pack/packed_fifo_wr_data|_en` **唯一去向**是 `openwifi_ip/adc_data|adc_valid`；BD **无** `axi_ad9361_adc_dma` 实例；dts `cf-ad9361-lpc@79020000` 的 `dmas`/`dma-names` **被注释掉**（`devicetree.dts:1032-1033`）。⇒ libiio/`cf_axi_adc` 只能读 AD9361 寄存器、**无 IIO buffer 流能力**。原始 IQ 的唯一出口就是 side_ch 的 BRAM 快照。
- **xpu = openwifi 的低 MAC + 测量 + 时基单元**（顶层 `xpu.v`，16 个例化）。四组职责：①时基 `tsf_timer`（reg2/3 写、58/59 读）；②测量与信道接入 `rssi`（内含 `iq_abs_avg`/`iq_rssi_to_db`/`fifo_sample_delay`）/`cca`/`csma_ca`/`cw_exp`/`time_slice_gen`（reg5/6/7/8/19-22）；③发送时序 `tx_control`/`tx_on_detection`/`spi_module`（reg1/9/10/11/13/16/17/18/26）；④收包过滤 `phy_rx_parse`/`pkt_filter_ctl`（reg12/27-31）；外加 `xpu_s_axi` 从口与 `edge_to_flip`（例化 4 次）。寄存器逐条见 `openwifi/doc/README.md` 的 xpu 表。
- **RSSI 必须在 xpu 算**：`rssi.v:103` = `reg7[26:16]校准偏移 + iq_rssi_half_db(样本幅值，0.5 dB/步) − gpio_status[6:0]AGC增益×2`。没有 AGC 增益档位只能得"数字幅度"、得不到 dBm；该字走慢速控制面，靠 `fifo_sample_delay` 按样本数延迟对齐（延迟量 reg7[6:0]）。头号消费者是信道接入（cca/csma_ca），放 xpu 才能闭环；openofdm_rx 只提供 `POWER_THRES`（reg2）、不负责测量。
- **`spi.v` 里装的是 `spi_module`，不是通用 SPI 主控**：PS SPI0 三线是它的输入，仅在 CPU 未选中片选时抢占总线，发一条 24 bit 命令开关 TX LO（`SPI_HIGH`/`SPI_LOW` 由 `boards/ip_repo_gen.tcl:56-66` 生成，默认 `grounded_rf_port=0`→控 LO；=1 改成切 RF 端口 A/B），reg13[0] 可整体禁用。默认在自发窗口掐掉 Rx 基带。

## 工具与做法经验

- 本机 git 代理 502，拉上游用 `curl -L codeload.github.com/<org>/<repo>/tar.gz/refs/heads/master`。
- BSD grep 的 `\|` 交替静默失败，用 `grep -E`。同一文件并行发多个 Edit 会丢改动，必须串行改+复查。
- 查 `system.bd` 网络成员要用 `<inst>/<port>` 形式（下划线形式 grep 不到）；接口级连接在 `interface_nets` 键下。**`openwifi_ip` 在 BD 里是黑盒，其内部网络要看 `ip/openwifi_ip_ultra_scale.tcl` 的 `connect_bd_net` 行。**
- 用户明确要求：**先查官方文档 / wiki / 论坛**（ADI wiki、EngineZone、`openwifi/doc/`、GitHub discussions），不要为答一个问题去翻 Verilog；源码只在文档没有、或与文档冲突时用。讲概念不主动画图、不塞示例代码，只答他问的机制。
