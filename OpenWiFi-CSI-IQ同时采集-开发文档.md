# OpenWiFi CSI-IQ 联合采集 开发文档（AI Agent 启动参考）

> 本文档是"CSI + 前导码原始 IQ 同时经 UDP 采集"二次开发工程的**启动参考**，供 AI agent（或新工程师）开工前一次读完。
> 所有代码行号均已对照当前工作区源码核实。配套文档：《OpenWiFi-CSI-IQ同时采集-方案汇报.md》（方案原理与开销）、《OpenWiFi-二次开发指南.md》（构建/部署全景，本文不重复其内容，只引用）。

---

## 0. 工作区事实（开工前必读）

- 工作区根：`/Users/juicy/Projects/OpenWiFi/`，含两个仓库：`openwifi/`（驱动+软件）、`openwifi-hw/`（FPGA）。**根目录本身不是 git 仓库**，改动前先确认各仓库 `git status`。
- `openwifi/AGENTS.md` 是该仓库对 AI agent 的约束文件（Conventional Commits、未经要求不得 commit/push 等），**先读并遵守**。
- 目标板：AntSDR E316 → **BOARD_NAME 一律写 `e310v2`**（设备树 model 是 ANTSDR-E310V2）。Zynq-7020、32 位、Vivado/Vitis 2022.2、内核 ADI 2026_R1 (Linux 6.12)。
- 当前工作机是 **macOS，不能做任何构建**。可做：改代码、scp。内核 `.ko` 与 FPGA 综合必须在 Linux x86_64 机器上（流程见二次开发指南 §4）。
- 板卡访问：`ssh root@192.168.10.122`（密码 openwifi），宿主机有线网卡静态 IP `192.168.10.1`。side_ch 数据 UDP 发往 **目标 IP 的 4000 端口**。
- `openwifi-hw/ip/openofdm_rx/` 是**未检出的 git 子模块**（`./get_ip_openofdm_rx.sh` 拉取）。**本任务不需要检出**：改动全部在 `side_ch`，openofdm_rx 只是信号来源。
- E316 关键宏：`SMALL_FPGA 1`、`SIDE_CH_LESS_BRAM 1`（side_ch 的 dpram/m_axis FIFO 深度从 8192 降为 **4096**）、`NUM_CLK_PER_US 100`（基带 100 MHz）。

## 1. 任务定义与验收标准

**需求**：对每一个被接收机解析（且通过 FC/addr 过滤）的 802.11 帧，通过 UDP 向目标 IP 同时输出：
1. 该帧的 **CSI**（FPGA 信道估计器输出，56 子载波，可选 num_eq 个等化器输出）；
2. 该帧**前导码（L-STF+L-LTF+L-SIG）对应的原始 IQ 采样**（接收链路 20 MHz 基带同源数据）。

**验收标准**：
- 同一 UDP datagram 内可按定长记录切分，每条记录含 IQ 块与 CSI 块，两块 TSF 相差 ≤ 20 µs（互验配对）；
- 不匹配 FC/addr 过滤条件的帧**不产生任何记录**（不能只吐 IQ 块）；
- IQ 块首采样与包起点（L-STF 第 0 点）对齐，偏差恒等于定标常数 C1（§2.6，采集与定标须同 FFT_WIN_SHIFT 配置）；
- CSI 块数值与纯 CSI 模式（`side_info_display.py`）对同一包的输出一致；
- 回归：不匹配包 / HT 包 / 极短包（IQ 流与 CSI 流时间重叠）/ 背靠背包 / FIFO 近满，仿真全绿；
- 板级：自发自收回环（`sdrctl dev sdr0 set reg xpu 1 1` 解静音 + `inject_80211`）CSI 应平坦，前导 IQ 与已知 L-STF/L-LTF 波形互相关对齐；`tcpdump` 抓包 TSF 与记录 TSF 对得上。

**路线选择**：主路线 A（FPGA 联合模式，见 §3）；过渡路线 B（零 FPGA 改动，见 §4）。建议先 B 后 A（B 验证软件管线，A 出原生数据）。

---

## 2. 现有架构事实（已核对到行号）

### 2.1 数据通路

```
AD9361 → adi-hdl → rx_intf(adc_intf 抽取到 20MHz) ──sample_in──► openofdm_rx（解调/信道估计）
                                     │                              │ ├─ csi/csi_valid、equalizer/equalizer_valid ──► side_ch
                                     └────────sample_in 同源──────►─┘ ├─ FC_DI/addr1/addr2/pkt_header/fcs 等握手 ──────► side_ch
                                                                     └─ 解码字节 → rx_intf → rx_dma → PS(skb)
side_ch 内部：CSI 路径(csi→512深小FIFO→CSI状态机) ┐
              IQ 路径(采样→4096深dpram环形缓冲→触发回读) ┴→ 64bit mux → m_axis FIFO(4096深) → AXI-DMA(S2MM) → DDR
驱动 side_ch.ko：netlink 收到 get 请求 → 算记录数 → 写 reg2 自动启动 DMA → 数据经 netlink 单播回 side_ch_ctl
side_ch_ctl：每 interval ms 轮询一次，把 netlink 载荷原样 sendto() 到 目标IP:4000
```

关键连线证据（`openwifi-hw/boards/e310v2/src/system.bd`）：`openofdm_rx_0/csi→side_ch_0/csi`（4530-4558 行）、`openofdm_rx_0/sample_in→side_ch_0/sample0_in`（4772-4789 行）。**IQ 源 = 解调器正在吃的那路采样，语义上正是"解析出的前导码对应的原始 IQ"。**

### 2.2 互斥机制（本任务要拆除的东西）

`openwifi-hw/ip/side_ch/src/side_ch_control.v`，全部由 `iq_capture`（reg3 bit0）控制：
- `:313-314`：`side_info/side_info_valid = (iq_capture==0 ? CSI路径 : IQ路径)`；
- `:704`：CSI 小 FIFO `.rst(...|iq_capture)`——IQ 模式下常置复位；
- `:707`：CSI 小 FIFO `.wr_en(...&(iq_capture==0))`——CSI 根本不写入；
- `:436`、`:479`、`:730`：三处 `if (iq_capture==0)` 门住 ofdm_symbol 计数器、capture_src_flag、CSI 状态机。

驱动侧：`driver/side_ch/side_ch.c:34`（注释 "if iq_len>0, iq capture enabled, csi disabled"）、`:373-376`（`num_dma_symbol_per_trans` 二选一）。

### 2.3 side_ch 寄存器表（`side_ch.h` + `side_ch.v` 实例化对照；`side_ch_ctl wh<idx>d/h<val>` 可写）

| idx | 名字 | 位域 |
|---|---|---|
| 0 | MULTI_RST | bit0 复位 m_axis，bit2 复位 control FSM |
| 1 | CONFIG | bit1:0 m_axis_start_mode；bit4 endless；**bit12 FC 匹配开关、bit13 addr1、bit14 addr2**（match_cfg） |
| 2 | NUM_DMA_SYMBOL | 低 16 位给 PS；**写它自动触发一次 DMA** |
| 3 | IQ_CAPTURE | **bit0 iq_capture**；bit5:4 iq_capture_cfg（bit0=0：单天线+状态字；=1：双天线 {iq1,iq0}）；**bit1 空闲——路线 A 用它做 csi_iq_combined** |
| 4 | NUM_EQ | bit3:0 num_eq（0~8）；bit4/bit2:0 被 IQ 捕获条件复用 |
| 5 | 多用途 | CSI 模式 bit15:0=FC_target；IQ 模式 bit0 free_run、bit2:1 iq_source_select(0=rx)、bit7:4 tx_state_target、bit9:8 phy_type_target |
| 6/7 | ADDR1/ADDR2_TARGET | MAC 低 32 位，字节序 `{addr[23:16],addr[31:24],addr[39:32],addr[47:40]}`（如 56:5b:01:ec:e2:8f → 01ece28f） |
| 8 | IQ_TRIGGER | 5 位触发选择（见 2.4） |
| 9/10 | RSSI_TH / GAIN_TH | 半 dB / 增益门限 |
| 11 | PRE_TRIGGER_LEN | 触发前回溯采样数（E316 有效 0~4094） |
| 12 | IQ_LEN | 每次触发捕获的采样数（E316 ≤4095） |
| 20 | M_AXIS_DATA_COUNT | 只读，FIFO 内 64bit 字数 |
| 26-31 | 事件计数器 | side_ch_counter |

已知 quirk：触发 25 的表达式（`:609`）把 match_cfg bit12 当 phy_type 门用（与 CSI FSM 的 FC 门含义不同），参考时注意。

### 2.4 触发条件表（`side_ch_control.v:583-617`，本任务相关子集）

| 值 | 含义 |
|---|---|
| 0/1/2 | FCS 结果（任意/通过/失败）；0 加 free_run=常触发 |
| 4/5/6/7 | SIGNAL 头校验（通过/失败/HT/非HT） |
| **8** | **long_preamble_detected（LTF 相关尖峰时刻；只能落在 [T1 结束, LTF 结束] 内，见 §2.6）** |
| 9 | short_preamble_detected |
| 10/11 | RSSI 上/下穿门限 |
| **25** | **addr1/addr2 匹配通过时刻（配合 reg6/7 与 reg1 bit13/14）** |

### 2.5 现有记录格式（64bit 字流，软件侧解析的契约）

**CSI 模式**（`HEADER_LEN+CSI_LEN+num_eq*EQUALIZER_LEN` 字/包）：
```
[TSF(64b)][phase_offset_taken(低32b有效)][56×{32'd0, I16, Q16}][num_eq×52×等化器输出]
  TSF 锁存于 pkt_header_valid_strobe；56 由 subcarrier_mask 门控产生（以 RTL 行为为准，别按 52 硬猜）；
  等化器非 HT 时在 48 数据子载波位置补 4 个 {32767,32767}
```
**IQ 模式**（`1+iq_len` 字/次）：
```
[TSF(64b)][iq_len×采样字]
  cfg bit0=0 时采样字 = {24b状态, 8b gpio_status, I16, Q16}
    （第3个16bit: gpio_status——bit7 AGC lock、bit6:0 增益；第4个16bit: rssi_half_db 低11位
      + b15 demod、b14 tx_rf、b13 fcs_ok；第2个16bit 的 b15 = ch_idle）
  cfg bit0=1 时采样字 = {iq1(I16,Q16), iq0(I16,Q16)} 双天线
  TSF 锁存于触发时刻
```
解析参照：`user_space/side_ch_ctl_src/side_info_display.py`（CSI）、`iq_capture.py:59-82`（IQ）。

### 2.6 关键尺寸与时序数字（推参数用，全部要背下来）

- 基带采样率 **20 MHz**（`sample_in_strobe` 每 5 个 100MHz clk 一次）→ 1 OFDM 符号 4 µs = **80 采样**。
- 前导码：L-STF 160 + L-LTF 160 + L-SIG 80 = **400 采样（20 µs）**；HT 混合模式再加 HT-SIG 160 + HT-STF 80 + HT-LTF 80×N（N=1/2/4）→ HT 前导终点 640+80N（最多 960）。
- L-LTF 内部：TGI(1.6µs=32 采样) + T1(3.2µs=64) + T2(3.2µs=64，T1 原样重复)。TGI 是双倍保护间隔，标准有意为之，使 LTF 时域波形呈 3.2µs 自循环。
- **`long_preamble_detected`（LTF 相关尖峰）只能落在 [采样 256, 320+流水] 内**。双边界：下界——相关器要把 64 点模板完整压在 T1 上，至少收完 T1（第 255 点）才能出尖峰；上界——L-SIG 开 FFT 窗（采样 320）之前必须已有精确定时，否则整个包解不了。区间内的具体点由相关器实现决定（对齐 T1 还是 T1+T2、流水几拍）。
- 因此"尖峰→包起点"的距离是**常数 C1 ≈ 256~320+流水**：与速率/包长无关（波形和电路都是定死的），定标一次（仿真或回环）写死为 pre_trigger_len。C1 随 FFT_WIN_SHIFT（openofdm_rx reg5）配置可挪几个采样——**定标与采集必须同配置**。
- E316 dpram/FIFO 深度 **4096 字 = 204.8 µs 历史**；UDP 单包上限 65507 B；`MAX_NUM_DMA_SYMBOL_UDP=min(4096,8188)`。
- MAC 头（FC/addr1/addr2）在包内约 28–50 µs（采样 560~1000，速率相关，HT 更晚）处才解码完——锁窗（≤~320）远早于它、环形缓冲（204.8µs）远大于它：**窗口内容等到 CSI 提交时必然完好**，这是两段式触发的成立根据。
- m_axis FIFO 空间检查在 `IQ_PREPARE_TO_M_AXIS`（`:632`）与 `PREPARE_TO_M_AXIS`（`:778`），不够则**整块丢弃**（不产生半条记录）。
- CSI 块推入 m_axis 只需 58~474 字 @100MHz ≈ 0.6–4.7 µs，SIFS 背靠背场景安全。

### 2.7 软件/内核链路细节

- netlink 协议（`side_ch.c:434-520`）：`NETLINK_USERSOCK`；用户发 4×u32 `{action, reg_type, reg_idx, reg_val}`；action：1=写寄存器、2=读寄存器、**3=取数据**。内核回包 = 原始 side_info 数据（字节数>4）或 4 字节错误码（-2=无数据）。
- `get_side_info(num_eq, iq_len)`（`side_ch.c:348-432`）：读 reg20 得 FIFO 字数 → `字数/per_trans` 向下取整 → 映射 DMA → 等完成 → 返回字节数。**per_trans 必须与 RTL 记录长度严格一致，否则整包错位**。
- probe 初始化（`side_ch.c:566-594`）：`MULTI_RST=4 → CONFIG=0x7001 → IQ_TRIGGER=10 → NUM_EQ → (IQ模式: IQ_CAPTURE=1, PRE_TRIGGER=8190, IQ_LEN, IQ_TRIGGER=0) → CONFIG=0x0001 → 复位脉冲序列`。**注意 probe 硬编码 PRE_TRIGGER=8190，E316 上会被截断，路线 B/A 都必须随后 `wh11d<正确值>` 覆盖（或改 probe）**。
- 模块参数：`num_eq_init`（0~8，默认 8）、`iq_len_init`（>0 即 IQ 模式；驱动上限钳到 8187，**E316 用户须自己传 ≤4095**）。
- `side_ch_ctl.c`：`g[N] [-s IP]` 每 N ms 轮询 + UDP 发送；**`:419` 有个发送门槛：`side_info_size ≥ 464`（58 字）才 sendto**——纯 IQ 模式若记录 <464B 会被静默丢弃（ iq_len≥57 即可避开，路线 A 记录更大无碍，但要知道这个坑）。
- `side_ch.ko` 不在 `driver/Makefile` 的 obj-m 里，`make_all.sh` 末尾走 `driver/side_ch/make_driver.sh`；上板时在 `wgd.sh` 之后**单独 `insmod side_ch.ko`**。

---

## 3. 技术路线 A：FPGA 联合模式（主路线）

### 3.0 目标报文格式（软件侧新契约）

```
每包一条定长记录（m_axis 字流顺序，也是 UDP 载荷切分单位）：
[ IQ块: TSF + iq_len×采样字 ][ CSI块: TSF + phase_offset + 56×CSI + num_eq×52×EQ ]
record_len = (1+iq_len) + (2+56+num_eq*52)   ← 必须在 RTL 空间检查 / 驱动 per_trans / 主机解析器 / tb golden 四处一致
```
推荐参数：`pre_trigger_len = C1`（§2.6 定标常数，≈256~320+流水；不再是"猜最坏值"）、`iq_len=440`（非 HT：盖住 400 采样前导+余量；HT 按 HT-LTF 数加大到 ~720–1000）、`num_eq_init=0`（要等化器再加）。记录 = (1+440)+(2+56) = 499 字 ≈ 4.0 KB，FIFO 可积压 ~8 条；busy 信道把轮询从 `g`(100ms) 提到 `g10`。

### 3.1 Step 0：前置坑——宏文件

`side_ch.v:3-5` include 的 `fpga_scale.v`、`has_side_ch_flag.v`、`side_ch_pre_def.v` **不在 `ip/side_ch/src/`**（该目录只有 8 个手写 .v），它们由 `boards/ip_repo_gen.tcl:87` 在板级构建时生成拷入。建独立仿真工程前必须先补齐：从上一次构建的 `boards/e310v2/ip_repo/` 拷，或手写（E316 内容即 `` `define SIDE_CH_LESS_BRAM 1 ``、`` `define HAS_SIDE_CH 1 `` + 空 pre_def）。**这三个文件是生成物，别提交手改版本**（下次构建会被覆盖，属正常行为）。

### 3.2 Step 1：RTL 改动（全部在 `side_ch_control.v` + `side_ch.v` 各一处）

**a) `side_ch.v`（:367 附近，1 行）**——把 reg3 bit1 引入 control：
```verilog
.iq_capture(slv_reg3[0]),
.iq_capture_cfg(slv_reg3[5:4]),
.csi_iq_combined(slv_reg3[1]),   // 新增端口
```
不改 `side_ch_s_axi.v`、不改地址表、不改 hw_def.h → 不触发"三处同步"、不动设备树。

**b) `side_ch_control.v` 端口**加 `input wire csi_iq_combined;`

**c) 输出仲裁（改 :313-314）**：
```verilog
assign side_info       = side_info_iq_valid ? side_info_iq : side_info_csi;
assign side_info_valid = side_info_iq_valid | side_info_csi_valid;
// 配合 e) 串行化，保证两者永不同时 valid
```

**d) 解除 CSI 门控（4 处，同一模式）**：
```verilog
// :704  .rst(pkt_begin_rst|ht_rst|(iq_capture&(~csi_iq_combined)))
// :707  .wr_en(side_info_fifo_wr_en&(((iq_capture==0)|csi_iq_combined)))
// :436/:479/:730  if ((iq_capture==0)|csi_iq_combined)
```

**e) 两段式触发（最关键的新逻辑）**——把"锁窗口"与"推数据"拆成两个事件。`csi_commit_pulse` 指 CSI 状态机 FC/addr 匹配通过、确定提交采集的时刻（进入 WAIT_FOR_CAPTURE_DONE 的跳变）：

```
采样:    0       160  192  256   320  400          480~1000          …包尾
         ├ L-STF ─┤TGI ├ T1 ──┼ T2 ──┤LSIG┤── 数据符号 ……
                               ↑━━━━┓               ↑                  ↑
                         ①锁窗:尖峰只能落在这段内   ② csi_commit→推IQ块  ③ 推CSI块
                         iq_raddr←iq_waddr-C1      从钉好的指针回读     last_ofdm_symbol_flag
                         锁TSF;只记指针,不读数      iq_len 个采样
```

```verilog
wire csi_commit_pulse = (side_ch_state_old!=WAIT_FOR_CAPTURE_DONE) &&
                        (side_ch_state==WAIT_FOR_CAPTURE_DONE);
// 覆盖两条进入路径：正常包 addr2 匹配（WAIT_FOR_CONDITION2→DONE），
// 短包（pkt_len<20，WAIT_FOR_CONDITION1→DONE，ACK 类 14 字节帧没有 addr2！）

// IQ FSM（:619-654）在 WAIT 与 PREPARE 之间插入 IQ_WINDOW_LOCKED（仅联合模式）。
// 注意 iq_state 现为 2bit/4 状态（:269），插第 5 个状态要放宽到 3bit：
IQ_WAIT_FOR_CONDITION:
    if (long_preamble_detected) begin             // ① LTF 尖峰（§2.6，~采样256-320）
        iq_raddr <= iq_waddr - pre_trigger_len;   //   尖峰位置-C1 → 钉在采样0(包起点)
        tsf_val_lock_by_iq_trigger <= tsf_runtime_val;
        iq_state <= IQ_WINDOW_LOCKED;             //   只记指针，不读数
    end
IQ_WINDOW_LOCKED:
    if (long_preamble_detected) begin             // 下一包重锁：旧窗口(未提交)无声丢弃
        iq_raddr <= iq_waddr - pre_trigger_len;
        tsf_val_lock_by_iq_trigger <= tsf_runtime_val;
    end else if (csi_commit_pulse && (MAX_NUM_DMA_SYMBOL_UDP-m_axis_data_count)>=record_len)
        iq_state <= IQ_PREPARE_TO_M_AXIS;         // ② 此刻才开始回读推送
```

四个设计保证：
- **前导精确对齐**：窗口在 LTF 尖峰锁定，尖峰→包起点距离是常数 C1（§2.6）——IQ 块首采样=包起点，主机免搜索；
- **1:1 配对 + 无孤儿**：推数等 csi_commit，每条 IQ 块必跟 CSI 块；未通过过滤的包窗口被下一包**重锁自然覆盖**，不产生残骸；
- **过滤条件与 CSI 完全同源**：csi_commit 本身就是 CSI 状态机的 FC/addr1/addr2 判定；
- **时序必然成立**：①≤~320 ≪ ②≥~480（锁窗必先于读出）；窗口终点 ~440 ≪ 环形 4096（内容等到 ② 必然完好，读指针也追不上写指针）。

外部触发路由（:583 case）在联合模式下整体旁路（保留给纯 IQ 模式用）。

**f) 串行化 + 空间检查（改 :778 与 :632）**：
```verilog
// PREPARE_TO_M_AXIS（CSI 侧）：
//   ① 空间阈值从 num_dma_symbol_per_trans 改为整条记录长度
//      localparam record_len = 1+iq_len_target + HEADER_LEN+CSI_LEN+num_eq*EQUALIZER_LEN
//   ② 追加条件 (iq_state==IQ_WAIT_FOR_CONDITION)||(iq_state==IQ_WINDOW_LOCKED)
//      （等 IQ 流完。仍必要：6Mbps 最短包 commit≈采样960、IQ 流 440 采样≈22µs →
//        流终点≈1400，可能跨过 last_ofdm_symbol_flag≈1280——CSI 想出库时 IQ 还在流）
// IQ 侧空间检查移到 ② csi_commit 时刻（见 e），阈值 = record_len
```

### 3.3 Step 2：仿真验证（综合前必须全绿）

**为什么**：改动全是状态机时序问题（块顺序/重叠/孤儿），上板后信号全在 PL 内部只能 ILA 猜，而一轮综合 1–3 h。

**建工程**：`side_ch` 是六个 IP 里唯一没有 `unit_test/` 的。照 `ip/xpu/unit_test/mv_avg/` 的模式（`tb.v` + `tb.tcl` + `test_vec/`，`$fscanf` 回放文本采样、`$dumpfile` 出 VCD、自动 `$finish`）新建 `ip/side_ch/unit_test/side_ch_control_tb/`。用 `ip/create_vivado_proj.sh $XILINX_DIR side_ch.tcl e310v2 100` 生成独立工程后把 tb 挂为 Simulation Sources（或直接 `vivado -source tb.tcl`）。

**testbench 要扮演的角色与时间轴**（例化顶层 `side_ch` 走 AXI-Lite 配置，或直接例化 `side_ch_control` 驱动内部 wire）：
1. 复位后按 probe 顺序写 reg3=3（bit0|bit1 联合模式）、reg4、reg11=C1（定标值）、reg12=440、reg6/7 目标地址、reg1 匹配开关；
2. 从 `test_vec/data_in.txt` 每 5 个 clk 给一个 `iq_strobe+iq0`（前 400 点用真实前导波形 L-STF/L-LTF/L-SIG——C1 对齐断言要用；可截取 openofdm_rx 子模块的 SAMPLE_FILE 采样文件）；
3. 按真实顺序"演"openofdm_rx 握手：`demod_is_ongoing` 拉高（包检测即拉高）→ **回放到采样 ~256–320 处**给 `long_preamble_detected` 脉冲（真实相关器在"T1 收完~LTF 结束"之间出尖峰，§2.6；脉冲给早了 = 测试无效）→ `csi_valid`+64 个特征值（如 1~64，LTF FFT 之后）→ `pkt_header_valid_strobe`（L-SIG 解完）→ `equalizer_valid`×52×num_eq（随数据符号）→ `FC_DI_valid`/`addr1_valid`/`addr2_valid`（与 reg6/7 匹配，采样 ~560–1000）→ `pkt_rate`/`pkt_len` → `ofdm_symbol_eq_out_pulse`×N → `fcs_in_strobe/fcs_ok`。

**自动断言（golden 文件比对 m_axis 64bit 序列）**：
- 序列 = `[TSF_iq][iq_len×IQ][TSF_csi][phase_offset][56×CSI][52×EQ...]`，总字数严格等于 record_len；
- **IQ 块首采样 == test_vec 的 L-STF 第 0 点**（验证 C1 对齐；把 pre_trigger_len 故意设错几个采样应看到断言失败——自检断言有效性）；
- CSI 的 64 个激励值确实出现（证明门控 d) 解开，不是全 0）；
- 两段 tvalid 波形不重叠（证明 e/f 串行化生效）；
- 锁窗到推数之间 `iq_raddr` 保持不动（验证"只记指针不读数"）；
- 预填 m_axis FIFO 至近满 → 整条记录丢弃而非半条。

**回归矩阵**：

| 用例 | 期望 |
|---|---|
| FC/addr 不匹配包，紧接一个正常包 | 零记录输出；正常包记录正确且 IQ 窗口归属后者（重锁覆盖验证） |
| HT 包（ht_flag=1） | 记录正常，CSI 块走 HT 分支 |
| 极短包（1 个 data 符号，IQ 流与 CSI 流时间撞车） | 顺序与长度仍正确 |
| 背靠背两包（含 SIFS 极限：上一包 IQ 流未完时下一包前导到达） | 两条完整记录，dpram 不串包；进行中的流不被锁窗事件破坏 |
| 14 字节短帧（无 addr2，从 CONDITION1 直接提交） | 触发照常（验证 csi_commit_pulse 两条路径） |

跑法：先 Vivado GUI Run Behavioral Simulation 看波形，稳定后转 xsim batch 跑回归（秒级）。可开 `` `define SIDE_CH_ENABLE_DBG ``（`side_ch.v:9-13`）让关键 reg 带 mark_debug，上板 ILA 复用同一批信号名。

### 3.4 Step 3：驱动改动（`driver/side_ch/`）

- `side_ch.h`：加 `CSI_BLK_LEN`/`record_len` 宏（与 RTL localparam 对齐）；
- `side_ch.c:373-376`：`num_dma_symbol_per_trans` 加联合分支 `(1+iq_len)+(HEADER_LEN+CSI_LEN+num_eq*EQUALIZER_LEN)`；
- `side_ch.c:574-584` probe：联合模式下 `IQ_CAPTURE 写 3`、`PRE_TRIGGER_LEN 写 C1`（做成模块参数 `pre_trigger_init`，与 iq_len_init 同级；顺手修掉硬编码 8190 的 E316 截断问题）、`IQ_LEN`、触发无需写（RTL 两段式内部触发）；新增模块参数 `combined_init`，用法 `insmod side_ch.ko iq_len_init=440 num_eq_init=0 pre_trigger_init=<C1> combined_init=1`；
- 编译：`make_all.sh $XILINX_DIR 32`（内含 side_ch 的 make_driver.sh），或单独跑 `driver/side_ch/make_driver.sh`。

### 3.5 Step 4：用户态与解析器

- `side_ch_ctl.c` 传输逻辑**不改**（`-s IP` + 4000 端口已具备）；只需知道 `:419` 的 464B 门槛在联合模式下天然满足；
- 新写 `csi_iq_display.py`：合并 `side_info_display.py`（CSI 解析）与 `iq_capture.py:59-82`（IQ 解析），按 record_len 切 datagram，输出双 TSF 配对校验 + CSI 频域图 + 前导 IQ 时域图；`num_eq` 与 `iq_len` 作为命令行参数（与 ko 对齐，csi.md "Config the num_eq" 的老规矩）。

### 3.6 Step 5：构建与部署（对齐二次开发指南场景 C，此处只列索引）

```
FPGA:  cd openwifi-hw/boards/e310v2 && ../create_ip_repo.sh $XILINX_DIR
       → ../sdk_update.sh e310v2 $OPENWIFI_HW_IMG_DIR
       → openwifi/user_space/boot_bin_gen.sh $XILINX_DIR e310v2 <新xsa>
驱动:  cd openwifi/driver && ./make_all.sh $XILINX_DIR 32
上板:  scp system_top.bit.bin + *.ko → 板上 ./wgd.sh → 单独 insmod side_ch.ko ... → ./side_ch_ctl g10 -s 192.168.10.1
```
不改寄存器地址/BD 拓扑 → **无需换 dtb/内核/烧 SD**，`wgd.sh` 热重载闭环（分钟到小时级）。
回归纪律：板上 `./sdrctl dev sdr0 get reg xpu 63` 必须 == 新 openwifi-hw commit 前 7 位（bitstream↔.ko 配套验证）。

### 3.7 Step 6：板级验证

1. `./monitor_ch.sh sdr0 <忙信道>` 或 AP 模式 + `./side_ch_ctl wh1h4001/wh7h<对端MAC>` 过滤；
2. 自发自收回环（`sdrctl dev sdr0 set reg xpu 1 1` 解静音 + `inject_80211`，参照 `doc/app_notes/radar-self-csi.md`、`packet-iq-self-loopback-test.md`）：回环 CSI 应平坦；
3. 对比联合记录 CSI vs 纯 CSI 模式 `side_info_display.py` 输出；前导 IQ 与已知 L-STF/L-LTF 互相关对齐；
4. 同时 `tcpdump`，用 TSF 把"包↔CSI↔IQ"三方对上（上游 discussion #344 方法）；
5. 压力：`g10` 轮询 + 高包率流量，观察 reg20 不长期逼近 4096（逼近=丢块，需提轮询频率或加过滤）。

### 3.8 设计决策记录：两段式触发取代了哪些备选（为什么）

| 备选 | 机制 | 被否原因 |
|---|---|---|
| A1：csi_commit 即刻回读，pre_trigger_len=1200 盲回溯 | 触发即读 | 配对好，但 commit 时刻→包起点距离随速率/包型漂移（数百采样），前导在窗口内位置不定，**主机必须搜索前导**，精度差 |
| A2：long_preamble_detected 即刻推送，pre=240 | 触发即推 | 对齐好，但 FC/addr 不匹配的包会吐**孤儿 IQ 块**（CSI 块永不到来），破坏定长成帧 |
| 改 openofdm_rx 直接发 CSI+IQ 给 side_ch | 子模块加端口/缓存 | CSI 本来就在发（system.bd:4530-4558）、IQ 是顶层分接的同源流（:4772-4789）——**数据流已存在 90%，缺的只是 side_ch 内部配对/窗口逻辑**；且 openofdm_rx 是 git 子模块（fork/长期 divergence 成本）、流式不留存（要发 IQ 得重建 dpram+回读 FSM，重复造轮子）、新端口牵动 system.bd+两版 openwifi_ip.tcl+每板+可能地址表，成本高一个量级 |

**两段式触发 = 用一根现成的 `long_preamble_detected` 线，同时拿到 A2 的对齐精度与 A1 的配对保证，openofdm_rx 零改动。**（若将来需要孤儿容错的通用解：每块头部加 type/len 标签字，代价是四处格式契约改动。）

---

## 4. 技术路线 B：零 FPGA 改动过渡方案

### 4.1 命令序列（板上）

```
# 全包抓（干净信道/实验场景）：触发=前导检出，回溯盖住 L-STF
insmod side_ch.ko iq_len_init=400
./side_ch_ctl wh3h01            # iq_capture=1, 源=rx, 单天线+状态字
./side_ch_ctl wh11d240          # 覆盖 probe 硬编码的 8190（E316 会截断！）
./side_ch_ctl wh8d8             # 触发 = long_preamble_detected
./side_ch_ctl g10 -s 192.168.10.1

# 定向抓（只抓指定对端）：触发=addr 匹配，回溯盖住包起点
./side_ch_ctl wh7h<对端MAC低32位> && ./side_ch_ctl wh6h<本机MAC低32位>
./side_ch_ctl wh1h6001          # 开 addr1+addr2 匹配
./side_ch_ctl wh8d25            # 触发 = addr 匹配
./side_ch_ctl wh11d1200
```

### 4.2 主机端脚本（新写，0.5–1 人日）

输入：UDP 4000 的 IQ 记录（`[TSF][400×{I,Q,agc,rssi}]`，解析照抄 `iq_capture.py:59-82`）。处理：
1. **定位 L-LTF**：与已知 L-LTF 时域波形互相关（或 STF 能量平台+计数定窗）；
2. **CFO 估计**：L-STF 双半相关（延迟 16 采样=0.8 µs）得粗频偏，L-LTF T1/T2（延迟 64 采样=3.2 µs）得细频偏，校正；
3. **CSI**：对 64 点 L-LTF 窗做 FFT，`CSI_k = FFT_k × conj(LTF_ref_k)`，取 -26..-1、1..26 共 52 子载波；
4. 输出：同一记录的"前导 IQ + 重算 CSI"呈现/落盘。

### 4.3 局限（向需求方明示）

- CSI 非 FPGA 原生值（差 CFO/FFT 窗相位/归一化因子，主机校正后幅相可用但与芯片输出有差异）；
- 无 num_eq 等化器输出；
- trigger=8 版本无地址过滤（垃圾包也触发），trigger=25 版本依赖 §2.4 的 quirk 位语义。

---

## 5. 风险与边界情况清单

| 风险 | 场景 | 对策 |
|---|---|---|
| 孤儿 IQ 块 | 仅剩"CSI 提交后接收中断"一条罕见路径（提交前的未匹配窗口由重锁覆盖，§3.2-e） | 主机端按 record_len+TSF 合理性重同步；必要时加 type 标签字（§3.8 通用解） |
| C1 定标漂移 | FFT_WIN_SHIFT 换配置、openofdm_rx 子模块升级改变相关器时序 | 定标与采集同配置；§7 含定标复核项 |
| FIFO 溢出丢块 | busy 信道 + 轮询太慢 | `g10`/`g1`；reg20 监控；收紧 FC/addr 过滤 |
| 短包重叠 | 6Mbps 最短包：CSI 块（last symbol ≈采样1280）早于 IQ 流结束（commit≈960 + 440 采样流 ≈1400） | Step 1-f 串行化（仿真用例 3 专测） |
| probe 硬编码 8190 | E316 pre_trigger 截断 | Step 3 顺手修正；路线 B 用 wh11d 覆盖 |
| bitstream↔.ko 不配套 | 只换其一 | xpu reg63 校验 + 同批打包（drv_and_fpga_package_gen.sh） |
| Viterbi 评估 license 2h | 长时间实验突然停摆 | 脚本看门狗：`sdrctl get reg rx 20` 停滞→重载（二次开发指南 §6.3.5） |
| `openwifi.tcl` 的 `git clean -dxf ./src/` | boards/e310v2/src 未提交改动被删 | 综合前先 commit |
| AGC 状态字跨变化 | 前导期间 AGC 已锁定（L-STF 内完成） | 无碍；状态字本身可用于校验 |

## 6. 工程纪律（AI agent 特别注意）

1. **生成文件绝不手改/不提交**：`driver/pre_def.h`、`driver/git_rev.h`、`boards/e310v2/ip_repo/*`、`boards/e310v2/ip_config/*_pre_def.v`、`ip/*/src/{board_def,clock_speed,fpga_scale,has_side_ch_flag,spi_command,openwifi_hw_git_rev}.v`。
2. **三处同步原则**（本任务因复用 reg3 bit1 不触发，但只要动了 `side_ch_s_axi.v` 地址就必须同步 `hw_def.h` 与驱动）。
3. record_len 契约在 **RTL / 驱动 / 解析器 / tb golden 四处**必须一致——这是本项目最容易出现"静默错位"的地方（现象是数据全错但不报错）。
4. 遵守 `openwifi/AGENTS.md`：Conventional Commits（`feat(side-ch): ...`），**未经明确要求不 commit/push**。
5. 构建全在 Linux x86_64；macOS 只做编辑与 scp。改 FPGA 后的完整回归清单见二次开发指南 §6.4。
6. 板上操作基线：`wgd.sh` 起底 → `monitor_ch.sh` 或 AP → **单独 `insmod side_ch.ko`** → `side_ch_ctl`。

## 7. 上板回归清单（路线 A 完成定义）

```
□ 仿真回归矩阵 5 用例全绿（§3.3，含重锁覆盖与 C1 对齐断言）
□ record_len 四处一致（RTL localparam / side_ch.c per_trans / csi_iq_display.py / tb golden）
□ C1 定标完成（回环或仿真一次），采集时 FFT_WIN_SHIFT 与定标同配置
□ create_ip_repo.sh 无报错；sdk_update.sh 已归档；板上 xpu reg63 == 新 hw commit
□ 纯 CSI 模式回归不破坏（不 insmod combined 参数时行为与上游一致）
□ 回环：CSI 平坦 + 前导 IQ 与 L-LTF 互相关峰在预期位置
□ 过滤：改 wh1/wh6/wh7 后只出目标包记录；不匹配包零输出
□ 压力：g10 + iperf3 流量 5 分钟无长期积压（reg20 不贴 4096）
□ tcpdump TSF ↔ 记录双 TSF 对齐抽查 20 包
```

## 8. 关键文件/行号索引

| 文件 | 内容 |
|---|---|
| `openwifi-hw/ip/side_ch/src/side_ch_control.v` | 全部改动主战场：mux:313 / 门控:436,479,704,707,730 / 触发表:583-617 / IQ FSM:619-654 / 空间检查:632,778 / CSI FSM:740-833 / dpram:488-499 |
| `openwifi-hw/ip/side_ch/src/side_ch.v` | SIDE_CH_LESS_BRAM:32-36 / 端口映射:367 / DBG 宏:9-13 |
| `openwifi-hw/ip/side_ch/src/side_ch_m_axis.v` | m_axis FIFO:181-223 / 流控:66-67 |
| `openwifi/driver/side_ch/side_ch.c` | get_side_info:348-432 / per_trans:373-376 / netlink:434-520 / probe:566-594 |
| `openwifi/driver/side_ch/side_ch.h` | 寄存器地址与 CSI_LEN/EQUALIZER_LEN/HEADER_LEN 契约 |
| `openwifi/user_space/side_ch_ctl_src/side_ch_ctl.c` | 参数解析:140-266 / UDP 发送:366,419-421 |
| `openwifi/user_space/side_ch_ctl_src/iq_capture.py` | IQ 记录解析:59-82 / UDP 4000 监听 |
| `openwifi/user_space/side_ch_ctl_src/side_info_display.py` | CSI 记录解析参照 |
| `openwifi-hw/boards/e310v2/src/system.bd` | 信号接线证据:4530-4558,4772-4789 |
| `openwifi/doc/app_notes/csi.md`、`iq.md`、`radar-self-csi.md` | 上游用法权威文档 |
| `openwifi-hw/ip/xpu/unit_test/mv_avg/` | testbench 模式样板 |
| `openwifi-hw/boards/ip_repo_gen.tcl` | 宏文件生成:25-35,87 |
