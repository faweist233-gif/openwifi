# 路线 A（FPGA 联合模式）实现计划与代码骨架

> 对应《OpenWiFi-CSI-IQ同时采集-开发文档.md》§3。本文件只给**每步的验收点**与**关键代码骨架**（关键 RTL/驱动逻辑给出可贴入的真实代码，重复/样板部分给 `…` 占位），供逐文件确认后落地。
> 所有行号均已对照当前工作区源码核对（2026-09 起笔时的状态）。
> 重要纪律：record_len 契约在 **RTL localparam / 驱动 per_trans / 主机解析器 / tb golden 四处一致**；生成文件（`fpga_scale.v` 等三个宏文件）绝不手改/提交。

---

## 0. 总体路径与验收主线

```
Step 0  宏文件补齐（仅准备，不改代码）
Step 1  RTL：side_ch.v + side_ch_control.v（一次改完 5 类改动）
Step 2  仿真：side_ch_control_tb（回放+握手+断言，综合前必须绿）
Step 3  驱动：side_ch.h + side_ch.c（combined_init / pre_trigger_init）
Step 4  主机解析：csi_iq_display.py
Step 5  构建部署（Linux，索引）
Step 6  板级验证（索引）
         └─ 每步完成定义见 §7 回归清单
```

本任务**失败模式的全在"静默错位"**：`record_len` 数值在四处不一致时，UDP 数据看起来"全对但全错位"，不报错。Step 1 落地后先跑 Step 2 仿真断言，再进驱动。

---

## 1. Step 0：宏文件（前置坑，只读）

`side_ch.v:3-5` 与 `side_ch_control.v:4` include 的 `fpga_scale.v` / `has_side_ch_flag.v` / `side_ch_pre_def.v` 不在 `ip/side_ch/src/`（该目录仅 8 个手写 .v），由 `boards/ip_repo_gen.tcl:87` 板级构建时生成拷入。

| 文件 | E316 内容 |
|---|---|
| `has_side_ch_flag.v` | `` `define HAS_SIDE_CH 1 `` |
| `side_ch_pre_def.v` | `` `define SIDE_CH_LESS_BRAM 1 `` |
| `fpga_scale.v` | (SIDE_CH 未用，保留空/照抄上次构建的) |

**动作**：建独立仿真工程前，从上次构建的 `boards/e310v2/ip_repo/` 拷入 `unit_test` 工作目录，或用上面内容手写一份**放在 tb 工作目录，不进 git**。
**验收**：独立工程能 `elaborate`/`simulate` 通过（不报 missing include）。

---

## 2. Step 1：RTL（`side_ch_control.v` + `side_ch.v` 各一处前缀 + 五类改动）

### 2.1 `side_ch.v`（1 行）

`:367` 处，把 reg3 bit1 引入 control（不改 `side_ch_s_axi.v`、不改地址表、不动设备树）：

```verilog
  .iq_capture(slv_reg3[0]),
  .iq_capture_cfg(slv_reg3[5:4]),
  .csi_iq_combined(slv_reg3[1]),   // 新增端口
```

### 2.2 `side_ch_control.v` 端口（1 行）

`iq_capture_cfg` 之后追加：

```verilog
  `DEBUG_PREFIX input wire csi_iq_combined,
```

### 2.3 输出仲裁（改 `:313-314`）

```verilog
  assign side_info       = side_info_iq_valid ? side_info_iq : side_info_csi;
  assign side_info_valid = side_info_iq_valid | side_info_csi_valid;
  // 配合 2.5/2.6 的串行化，保证 side_info_iq_valid 与 side_info_csi_valid 永不同时有效
```

### 2.4 解除 CSI 门控（4 处同款）

```verilog
  // :436  ofdm_rx 状态机
  if ((iq_capture==0)|csi_iq_combined) begin
  // :479  capture_src_flag 切换
  if ((iq_capture==0)|csi_iq_combined) begin
  // :730  CSI 状态机
  if ((iq_capture==0)|csi_iq_combined) begin
  // :704 / :707  CSI 小 FIFO 复位/写使能
  .rst(pkt_begin_rst|ht_rst|(iq_capture&(~csi_iq_combined))),   // :704
  .wr_en(side_info_fifo_wr_en&((iq_capture==0)|csi_iq_combined)), // :707
```

### 2.5 新增 record_len 契约信号 + csi_commit_pulse

放在 `:290`（`num_dma_symbol_per_trans` assign）附近：

```verilog
  // 联合模式整条记录长度（64bit 字数）。E316 上 IQ 块以 iq_len_target 为准（cfg bit0=0，单天线+状态字）
  wire [MAX_BIT_NUM_DMA_SYMBOL-1:0] record_len;
  assign record_len = (1+iq_len_target) + (HEADER_LEN+CSI_LEN+num_eq*EQUALIZER_LEN);

  // CSI 状态机"确定提交采集"的跳变（进入 WAIT_FOR_CAPTURE_DONE）。
  // 覆盖两条进入路径：正常包 addr2 匹配(WAIT_FOR_CONDITION2→DONE)；
  // 短包 pkt_len<20 (WAIT_FOR_CONDITION1→DONE，ACK 类 14B 帧没有 addr2)。
  wire csi_commit_pulse =
        (side_ch_state_old != WAIT_FOR_CAPTURE_DONE) && (side_ch_state == WAIT_FOR_CAPTURE_DONE);
```

### 2.6 两段式触发（IQ FSM，`:619-654` 内做分支；`iq_state` 加宽 2→3 bit）

状态表改为（位宽放宽到 `reg [2:0] iq_state;`，`iq_count` 等不动）：

```verilog
  localparam [2:0]   IQ_WAIT_FOR_CONDITION =      3'b000,
                     IQ_WINDOW_LOCKED      =      3'b001,   // 新增：锁窗完成，待 csi_commit
                     IQ_PREPARE_TO_M_AXIS  =      3'b010,
                     IQ_HEADER_TO_M_AXIS   =      3'b011,
                     IQ_INFO_TO_M_AXIS     =      3'b100;
```

case 体（`if (iq_capture)` 伞形块内）改为：

```verilog
  IQ_WAIT_FOR_CONDITION: begin
    side_info_iq <= 0;
    side_info_iq_valid <= 0;
    iq_count <= 0;
    if (csi_iq_combined) begin
      if (long_preamble_detected) begin     // ① 锁窗：LTF 尖峰（§2.6，~采样256-320）
        iq_raddr <= iq_waddr - pre_trigger_len;
        tsf_val_lock_by_iq_trigger <= tsf_runtime_val;
        iq_state <= IQ_WINDOW_LOCKED;       // 只钉指针，不读数
      end
    end else if (iq_trigger) begin          // 纯 IQ 模式，行为与上游一致
      iq_raddr <= iq_waddr - pre_trigger_len;
      tsf_val_lock_by_iq_trigger <= tsf_runtime_val;
      iq_state <= IQ_PREPARE_TO_M_AXIS;
    end
  end

  IQ_WINDOW_LOCKED: begin
    side_info_iq <= 0;
    side_info_iq_valid <= 0;
    iq_count <= 0;
    if (long_preamble_detected) begin       // 下一包重锁：旧窗口(未提交)无声丢弃
      iq_raddr <= iq_waddr - pre_trigger_len;
      tsf_val_lock_by_iq_trigger <= tsf_runtime_val;   // iq_state 保持 IQ_WINDOW_LOCKED
    end else if (csi_commit_pulse && ((MAX_NUM_DMA_SYMBOL_UDP-m_axis_data_count)>=record_len)) begin
      iq_state <= IQ_PREPARE_TO_M_AXIS;     // ② csi_commit 才回读推送
    end
  end
```

> 设计保证（对拍 §3.2-e 四处）：① 前导精确对齐（C1 常量）② 1:1 配对无孤儿（推数只发生在 csi_commit 后；未匹配包的窗口被下一包重锁覆盖）③ 过滤与 CSI 完全同源 ④ 时序必然成立（①≤~320 ≪ ②≥~480；窗口终点 ~440 ≪ 环形 4096）。
> 外部触发路由（`:583` case）在联合模式下整体旁路（保留给纯 IQ 模式）。

### 2.7 串行化 + 整条记录空间检查

**CSI 侧 `PREPARE_TO_M_AXIS`（改 `:778`）**：

```verilog
  PREPARE_TO_M_AXIS: begin
    if (csi_iq_combined)
      side_ch_state <= ( ((MAX_NUM_DMA_SYMBOL_UDP-m_axis_data_count)>=record_len)
                          && (iq_state==IQ_WAIT_FOR_CONDITION || iq_state==IQ_WINDOW_LOCKED)
                       ) ? HEADER_TO_M_AXIS : WAIT_FOR_CONDITION;
    else
      side_ch_state <= ((MAX_NUM_DMA_SYMBOL_UDP-m_axis_data_count)>=num_dma_symbol_per_trans)?HEADER_TO_M_AXIS:WAIT_FOR_CONDITION;
  end
```
> 追加 `iq_state` 条件：等 IQ 流完。6Mbps 最短包 commit≈采样960、IQ 流 440 采样≈22µs → 流终点≈1400，可能跨过 `last_ofdm_symbol_flag`≈1280——CSI 想出库时 IQ 还在流，必须等它回 WAIT/LOCKED 再推 CSI，保序 `[IQ块][CSI块]`。

**IQ 侧空间检查已在 2.6 的 `csi_commit_pulse` 时刻完成（阈值=record_len）**，`IQ_PREPARE_TO_M_AXIS`（`:632`）本体不需改（纯 IQ 保持上游行为）。

### Step 1 验收

- [ ] `side_ch_control.v`/`side_ch.v` 语法检查通过（`vivado -mode batch -source <synth_tcl>` 或至少在 Linux 上 `xvlog` 过一遍）；
- [ ] 三处门控 + FIFO 复位/写使能 = 5 处，一处不漏；
- [ ] `record_len` 与驱动/解析器/tb 的契约数字一致（ID: (1+iq_len) + (2+56+num_eq*52)）；
- [ ] 纯模式行为完全不破坏（`combined=0` 时所有分支按原码路径走）。

---

## 3. Step 2：仿真（`ip/side_ch/unit_test/side_ch_control_tb/` 新建）

### 3.1 骨架目录

```
unit_test/side_ch_control_tb/
├── side_ch_control_tb.v      # 例化 side_ch（走 AXI-Lite 配置）或直接例化 side_ch_control
├── side_ch_control_tb.tcl    # Vivado 建独立工程 + 挂 tb + xsim batch 跑法
├── test_vec/
│   ├── data_in.txt           # 20MHz 复基带采样文本（回放样本）
│   └── gen_preamble.py       # 生成 L-STF/L-LTF/L-SIG 已知波形（真实波形保 C1 断言有效）
└── pre_def/                  # Step0 宏文件工作副本（三个 .v，不入 git）
    ├── fpga_scale.v   has_side_ch_flag.v   side_ch_pre_def.v
```

### 3.2 tb 骨架要点（`side_ch_control_tb.v`）

驱动结构与 `mv_avg_tb.v` 同款（`$fscanf` 回放、`#5 clock` 100MHz、`$dumpvars`、`$finish`）。按真实顺序"演"openofdm_rx 握手（时间轴与文档 §3.3 一致）：

```verilog
// —— 回放：每 5 clk 给一个采样（20MHz），前 400 点为真实前导 ——
//  iq_strobe <= 1'b1;
//  iq0_inner <= $fscanf 读到的样本;           (i=0..N)
//  sample0_in_lo/hi = {Q, I}

// —— 演解调握手（乱序给会让仿真自证失败，是特性不是 bug）——
//  #~ sample 256~320 处:  long_preamble_detected <= 1'b1;   // 真实相关器长相
//  之后:                 csi_valid + 64 个特征值(如 1~64)
//                        pkt_header_valid_strobe(解完 L-SIG)
//                        equalizer_valid ×52×num_eq
//  采样~560~1000:        FC_DI_valid / addr1_valid / addr2_valid(与 reg6/7 匹配)
//                        pkt_rate / pkt_len
//                        ofdm_symbol_eq_out_pulse × N
//                        fcs_in_strobe / fcs_ok
```

**自动断言（用 m_axis 前 `data_to_ps_valid` 采样到的 64bit 词流做 golden 比对）**：

```verilog
// 1) 每条记录总字数 == record_len；字的序列 == [TSF_iq][iq_len×IQ][TSF_csi][phase][56×CSI][...]
// 2) IQ 块首采样(首个 IQ 字的 I/Q 16bit) == test_vec 的 L-STF 第 0 点 → 证明 C1 对齐
//    (把 pre_trigger_len 故意设错几个采样 → 断言必须失败，自检断言有效性)
// 3) CSI 的 64 个激励值确实出现 → 证明门控 2.4 解开，不是全 0
// 4) 记录内 tvalid 无重叠；iq_raddr 在 锁窗→推数 之间保持不动
// 5) 预填 m_axis FIFO 至近满 → 整条记录丢弃（不是半条）
```

**回归矩阵（5+1 用例，分段跑 xsim batch）**：

| 用例 | 期望 |
|---|---|
| 不匹配包紧接正常包 | 匹配包无记录；正常包记录正确且 IQ 窗口归属后者（重锁覆盖） |
| HT 包（ht_flag=1） | 记录正常，CSI 走 HT 分支 |
| 极短包（1 个 data 符号，IQ 流与 CSI 流时间碰撞） | 顺序与长度正确 |
| 背靠背两包（含 SIFS：上包 IQ 流未完下包前导到） | 两条完整记录，dpram 不串包 |
| 14B 短帧（无 addr2，CONDITION1 直接 DONE） | 触发照常（验证 csi_commit_pulse 双路径） |

### Step 2 验收

- [ ] 5 用例 + C1 对齐断言全绿（含"故意写错 pre_trigger_len 断言失败"的自检）；
- [ ] `SIDE_CH_ENABLE_DBG` 宏打开后关键 reg 带 mark_debug，与上板 ILA 同一批信号名。

---

## 4. Step 3：驱动（`driver/side_ch/`）

### 4.1 `side_ch.h`（追加宏）

```c
// 联合模式记录长度 = RTL record_len（与 side_ch_control.v localparam 对齐）
// record_len(64bit 字) = (1+iq_len) + (HEADER_LEN+CSI_LEN+num_eq*EQUALIZER_LEN)
#define RECORD_LEN(iq_len, num_eq) ((1+(iq_len)) + (HEADER_LEN + CSI_LEN + (num_eq)*EQUALIZER_LEN))
#define CSI_BLK_LEN(num_eq) (HEADER_LEN + CSI_LEN + (num_eq)*EQUALIZER_LEN)
```

### 4.2 `side_ch.c` 模块参数（`:33-40` 追加）

```c
static int num_eq_init = 8;    // 0~8
static int iq_len_init = 0;    // >0 即 IQ 模式
static int pre_trigger_init = 256;   // C1 定标值，联合模式直接写 reg11（替换硬编码 8190）
static int combined_init = 0;        // 联合模式开关（=1 时 iq_capture 写 3）

module_param(pre_trigger_init, int, 0);
MODULE_PARM_DESC(pre_trigger_init, "pre_trigger_len in samples (C1 calibration constant). Written to reg11. Default 256.");
module_param(combined_init, int, 0);
MODULE_PARM_DESC(combined_init, "combined_init. 1 = CSI+IQ combined capture mode (iq_capture bit0|bit1).");
```

### 4.3 `get_side_info` per_trans（改 `:373-376`）

```c
  if (combined_init)
      num_dma_symbol_per_trans = RECORD_LEN(iq_len, num_eq);
  else if (iq_len>0)
      num_dma_symbol_per_trans = 1+iq_len;
  else
      num_dma_symbol_per_trans = HEADER_LEN + CSI_LEN + num_eq*EQUALIZER_LEN;
```

### 4.4 probe（改 `:574-584`，顺手修硬编码 8190）

```c
  if (iq_len_init>0) {//initialize the side channel into iq capture mode
    if (iq_len_init>8187) { ... clamp 到 8187 ... }
    if (combined_init) {
      // E316 约束：整条记录必须 ≤ MAX_NUM_DMA_SYMBOL(4096)，否则空间检查永不满足
      if (iq_len_init > (4096 - CSI_BLK_LEN(num_eq_init) - 1)) {
        iq_len_init = 4096 - CSI_BLK_LEN(num_eq_init) - 1;
        printk("%s dev_probe: limit iq_len_init to %d for combined mode on 4096-symbol FIFO!\n", ..., iq_len_init);
      }
      SIDE_CH_REG_IQ_CAPTURE_write(3);              // bit0|bit1 = 联合模式
      SIDE_CH_REG_PRE_TRIGGER_LEN_write(pre_trigger_init);
      SIDE_CH_REG_IQ_LEN_write(iq_len_init);
      // 不写 IQ_TRIGGER：RTL 两段式内部触发（长前导锁窗 + csi_commit 推数）
    } else {
      SIDE_CH_REG_IQ_CAPTURE_write(1);
      SIDE_CH_REG_PRE_TRIGGER_LEN_write(pre_trigger_init);   // 原 8190 硬编码 → 参数
      SIDE_CH_REG_IQ_LEN_write(iq_len_init);
      SIDE_CH_REG_IQ_TRIGGER_write(0);
    }
  }
```

用法：`insmod side_ch.ko iq_len_init=440 num_eq_init=0 pre_trigger_init=<C1> combined_init=1`

### Step 3 验收

- [ ] 不传 `combined_init` 时行为与上游完全一致（`insmod side_ch.ko` 纯 CSI / `iq_len_init>0` 纯 IQ 回归）；
- [ ] `get_side_info` 的 per_trans 与 RTL record_len 严格一致；
- [ ] E316 上 `pre_trigger_init` 覆盖掉 probe 里 E316 会截断的 8190。

---

## 5. Step 4：主机解析（新写 `user_space/side_ch_ctl_src/csi_iq_display.py`）

合并 `side_info_display.py`（CSI 解析）+ `iq_capture.py:59-82`（IQ 解析），按 record_len 切 datagram：

```python
# record_len(64bit)= (1+iq_len)+(HEADER_LEN+CSI_LEN+num_eq*EQUALIZER_LEN)
# 命令行参数：num_eq 与 iq_len（与 .ko 对齐）
import argparse, socket, numpy as np
parser = argparse.ArgumentParser()
parser.add_argument('num_eq', type=int, default=0)
parser.add_argument('iq_len', type=int, default=440)
args = parser.parse_args()

RECORD_LEN = (1+args.iq_len) + (2+56+args.num_eq*52)
MAX_NUM_DMA_SYMBOL = 8192
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('192.168.10.1', 4000))

while True:
    data, addr = sock.recvfrom(MAX_NUM_DMA_SYMBOL*8)
    # 校验 len(data) % (8*RECORD_LEN) == 0，否则打印 Abnormal length
    u16 = np.frombuffer(data, dtype='uint16')
    n = len(u16)//(RECORD_LEN*4)
    u16 = u16.reshape(n, RECORD_LEN*4)

    # --- IQ 块（前 1+iq_len 个 64bit）---
    tsf_iq   = (u16[:,0] | u16[:,1]<<16 | u16[:,2]<<32 | u16[:,3]<<48).astype(np.uint64)
    iq_block = u16[:, 4 : (1+args.iq_len)*4]                       # 每采样 {I16,Q16,gpio_status,rssi}
    iq_c     = np.int16(iq_block[:,0::4]) + 1j*np.int16(iq_block[:,1::4])
    agc      = iq_block[:,2::4] & 0xFF
    rssi     = iq_block[:,3::4] & 0x7FF

    # --- CSI 块（(1+iq_len)*4 之后）---  结构与 side_info_display.py 完全相同
    csi_off = (1+args.iq_len)*4
    tsf_csi = (u16[:,csi_off+0] | ... <<48).astype(np.uint64)
    freq_offset = (20e6*np.int16(u16[:,csi_off+4])/512)/(2*np.pi)
    tmp  = np.int16(u16[:,csi_off+8::4]) + 1j*np.int16(u16[:,csi_off+9::4])
    csi  = np.concatenate([tmp[:,28:56], tmp[:,0:28]], axis=1)   # 56 子载波重排（同 side_info_display）
    # (num_eq>0 时从 tmp[:,56:56+num_eq*52] 取 equalizer)

    # 双 TSF 配对校验：|tsf_iq - tsf_csi| 换算 µs 后应 ≤ 20µs，超限打印 warning
    # 输出 csi 频域图 + 前导 iq 时域图（照搬 display_* 画图函数）
```

### Step 4 验收

- [ ] 能按 record_len 切分多记录 datagram，无 "Abnormal length"；
- [ ] CSI 部分输出与 `side_info_display.py` 对同一包数值一致（回归项）；
- [ ] IQ 部分输出与 `iq_capture.py` 对同一记录的解析一致。

---

## 6. Step 5/6：构建与板级（Linux 侧，索引见开发文档 §3.6/§3.7）

```
FPGA:  cd openwifi-hw/boards/e310v2 && ../create_ip_repo.sh $XILINX_DIR
       → ../sdk_update.sh e310v2 $OPENWIFI_HW_IMG_DIR
       → openwifi/user_space/boot_bin_gen.sh $XILINX_DIR e310v2 <新xsa>
驱动:  cd openwifi/driver && ./make_all.sh $XILINX_DIR 32
上板:  scp system_top.bit.bin + *.ko → ./wgd.sh → 单独 insmod side_ch.ko ... 
       → ./side_ch_ctl g10 -s 192.168.10.1
自检:  sdrctl dev sdr0 get reg xpu 63 == 新 openwifi-hw commit 前 7 位
```

板级验证动作（自发自收、回环平坦 CSI、前导 IQ 与 L-LTF 互相关、tcpdump TSF 三方对齐、g10 压力 reg20 不贴 4096）见开发文档 §3.7 / §7，此处不重复。

---

## 7. 风险与对拍（本方案落地时逐条勾）

| 风险 | 对策（代码位置） |
|---|---|
| record_len 四处静默错位 | RTL `record_len`(2.5) / 驱动 `RECORD_LEN`(4.1) / 解析器(5) / tb golden(3) 对齐；tb 断言专门抓错位 |
| E316 FIFO 4096 装不下整条记录 | 驱动 combined 分支把 iq_len 钳到 `4096-CSI_BLK_LEN-1`（4.4） |
| 短包 IQ 流未结束 CSI 想先出库 | CSI PREPARE 追加 `iq_state in {WAIT, LOCKED}` 条件（2.7） |
| C1 定标漂移（FFT_WIN_SHIFT 换配/子模块升级） | 定标与采集同配置；`pre_trigger_init` 参数化；回归清单含定标复核 |
| 纯模式回归破坏 | 所有新逻辑都以 `csi_iq_combined` 分支包裹，combined=0 走原路径（2.4-2.7） |
| 孤儿 IQ 块（仅剩"CSI 提交后接收中断"罕见路径） | 主机按 record_len+TSF 合理性重同步；必要时加 type 标签字（开发文档 §3.8 通用解） |
| 生成宏文件被误提交 | Step0 文件放 tb 工作目录，不入 git（§1） |

---

## 8. 完成定义（对齐开发文档 §7）

- [ ] 仿真回归 5 用例全绿（含重锁覆盖与 C1 对齐断言，且断言自检有效）
- [ ] record_len 四处一致
- [ ] C1 定标完成，采集时 FFT_WIN_SHIFT 与定标同配置
- [ ] 纯 CSI 模式回归不破坏
- [ ] 板级：回环 CSI 平坦 + 前导 IQ 与 L-LTF 互相关峰在预期位置
- [ ] 过滤：改 wh1/wh6/wh7 后只出目标包；不匹配包零输出
- [ ] 压力：g10 + iperf3 5 分钟 reg20 不长期逼近 4096
- [ ] tcpdump TSF ↔ 记录双 TSF 抽查 20 包