# OpenWiFi CSI + 前导码 IQ 联合采集 —— 部署文档

> 路线 A（FPGA 联合模式）实现后的构建、上板部署与验证手册。配套文档：
> 《OpenWiFi-CSI-IQ同时采集-开发文档.md》（原理/时序）、《OpenWiFi-CSI-IQ同时采集-路线A-实现计划与代码骨架.md》（代码改动对照）。
> 目标板：**AntSDR E316（BOARD_NAME=e310v2）**，Zynq-7020，Vivado/Vitis 2022.2，内核 ADI 2026_R1。

---

## 1. 本次代码改动清单（已提交工作区，未经要求未 commit）

### 1.1 FPGA（`openwifi-hw/ip/side_ch/src/`）

| 文件 | 位置 | 内容 |
|---|---|---|
| `side_ch.v` | :367 附近 | 新增 `.csi_iq_combined(slv_reg3[1])` 端口映射 |
| `side_ch_control.v` | 端口表 | 新增 `input wire csi_iq_combined` |
| `side_ch_control.v` | :326-327 | 输出仲裁改 `side_info_iq_valid ? side_info_iq : side_info_csi`（串行化后双 valid 互斥） |
| `side_ch_control.v` | :436/:479/:730 | 三处 `if (iq_capture==0)` → `if ((iq_capture==0)|csi_iq_combined)` |
| `side_ch_control.v` | :704/:707 | CSI 小 FIFO：`.rst(...|(iq_capture&(~csi_iq_combined)))`、`.wr_en(...&((iq_capture==0)|csi_iq_combined))` |
| `side_ch_control.v` | :295-303 | 新增 `record_len`（+1 位防回绕）与 `csi_commit_pulse` |
| `side_ch_control.v` | 状态表/`iq_state` | IQ 状态加到 5 个（`IQ_WINDOW_LOCKED` 新增），`iq_state` 2→3 bit |
| `side_ch_control.v` | IQ FSM | 联合分支：`long_preamble_detected` 锁窗（钉 `iq_raddr=iq_waddr-pre_trigger_len`），`csi_commit_pulse` 推数；纯 IQ 走原触发路径 |
| `side_ch_control.v` | CSI `PREPARE_TO_M_AXIS` | 联合分支：空间检查改用 `record_len` + 等待 `iq_state∈{WAIT,LOCKED}`（保序 `[IQ][CSI]`） |

> 未改：`side_ch_s_axi.v`、寄存器地址、BD、设备树、内核。因此**无需换 dtb/内核/烧 SD**。

### 1.2 驱动（`openwifi/driver/side_ch/`）

| 文件 | 内容 |
|---|---|
| `side_ch.h` | 新增 `CSI_BLK_LEN(num_eq)` / `RECORD_LEN(iq_len,num_eq)`（与 RTL `record_len` 对齐） |
| `side_ch.c` | 新增模块参数 `pre_trigger_init`(默认256) / `combined_init`(默认0) |
| `side_ch.c` | `get_side_info` per_trans 加 `combined_init` 分支（`RECORD_LEN`） |
| `side_ch.c` | probe 联合分支写 `IQ_CAPTURE=3`、`PRE_TRIGGER_LEN=pre_trigger_init`、`IQ_LEN`；**顺手修 E316 上 8190 硬编码截断**；E316 FIFO 4096 钳制 |

### 1.3 用户态 / 脚本

- 新增 `openwifi/user_space/side_ch_ctl_src/csi_iq_display.py`（合并 CSI+IQ 解析，按 record_len 切包、双 TSF 配对校验）。`side_ch_ctl` 无需改。
- 新增仿真 `openwifi-hw/ip/side_ch/unit_test/side_ch_control_tb/`（tb.v / tb.tcl / test_vec/）。

---

## 2. 本机验证结果（macOS，已完成）

| 项 | 工具 | 结果 |
|---|---|---|
| RTL 语法/lint | verilator 5.052 `--lint-only`（E316 宏、xpm stub） | `side_ch_control.v`、`side_ch` 全模块 exit 0 |
| 行为回归 | verilator `--binary` 跑 `side_ch_control_tb.v`（功能级 xpm 模型） | **PASS：7 条记录全绿** |
| 回归覆盖 | 同 tb | 匹配包×4、FC 不匹配零输出、重锁覆盖、14B 短帧（CONDITION1→DONE）、近满整块丢弃、**C1 定标自检**（pre_trigger_len 错 3 → 首采样移位到 gstart+3 且被断言抓到） |
| 驱动逻辑 | clang 独立片段 | RECORD_LEN(440,0)=499、E316 钳制 4037、纯 IQ 不钳 |
| 主机解析 | python 3.12 + 合成 UDP datagram round-trip | **PASS**：TSF 对、440 IQ 采样、AGC/RSSI、56 CSI、phase、多记录切包 |

> Linux/Vivado 侧仍未做：综合/实现、xsim 复核、`.ko` 交叉编译、板级回环。

---

## 3. Linux x86_64 构建

### 3.1 FPGA bitstream

```bash
cd openwifi-hw/boards/e310v2
../create_ip_repo.sh $XILINX_DIR          # 生成宏文件/ip_repo（含本任务改的 side_ch）
../sdk_update.sh e310v2 $OPENWIFI_HW_IMG_DIR   # 归档 xsa
openwifi/user_space/boot_bin_gen.sh $XILINX_DIR e310v2 <新xsa>
```
> 注意：`boards/e310v2/src` 或 `ip/*/src` 的生成性宏文件（`fpga_scale.v`/`has_side_ch_flag.v`/`side_ch_pre_def.v` 等）会被重新生成，**勿手改勿提交**。综合前先 `git commit` 源码改动（`openwifi.tcl` 的 `git clean` 会删未提交文件）。

### 3.2 驱动 `.ko`

```bash
cd openwifi/driver && ./make_all.sh $XILINX_DIR 32     # 内含 side_ch/make_driver.sh
```

### 3.3 仿真（可选复核）

```bash
source $XILINX_DIR/Vivado/2022.2/settings64.sh
cd openwifi-hw/ip/side_ch/unit_test/side_ch_control_tb
mkdir -p ../pre_def && printf '`define SIDE_CH_LESS_BRAM 1\n' > ../pre_def/side_ch_pre_def.v
vivado -mode batch -source side_ch_control_tb.tcl    # 输出应含 PASS
```

---

## 4. E316 上板部署

前置：板卡 `ssh root@192.168.10.122`（密码 openwifi），主机静态 IP `192.168.10.1`；已按标准步骤 `.xsa`/BOOT 更新过或只需热重载。

```bash
# 1) 传文件
scp system_top.bit.bin *.ko root@192.168.10.122:/root/
# 2) 起底 + 热重载 bitstream
ssh root@192.168.10.122
./wgd.sh            # 加载新 bitstream（不改 dtb/内核）
# 3) 单独加载 side_ch（必须在 wgd.sh 之后）
insmod /root/side_ch.ko iq_len_init=440 num_eq_init=0 pre_trigger_init=<C1> combined_init=1
#    <C1> = 定标常数（§5.1），首次可用 300 起步
# 4) 校验 bitstream ↔ .ko 配套
./sdrctl dev sdr0 get reg xpu 63        # 低 7 位必须 == 新 openwifi-hw commit
# 5) 配置过滤（可选）。默认 allow-all 即 reg1=0x0001（probe 已设）：
#    只抓指定对端：./side_ch_ctl wh1h6001 && ./side_ch_ctl wh7h<对端MAC低32位>（每条记录只需 addr 匹配）
# 6) UDP 推送（g10=100ms 轮询；busy 信道用 g1）
./side_ch_ctl g10 -s 192.168.10.1
```

### 4.1 主机端接收

```bash
# PC（192.168.10.1），先按 §3.1 确认有 -s 指向本机；监听 4000：
python3 csi_iq_display.py 0 440 --ip 192.168.10.1
#  参1 = num_eq（与 ko 一致，0=不带等化器）
#  参2 = iq_len（与 ko 一致，440=非HT前导400+余量；HT 建议 720~1000）
```

---

## 5. 定标（C1）与验证

### 5.1 C1 定标流程（一次性）

C1 = 从 `long_preamble_detected`（LTF 相关尖峰，采样 256~320）回退到包起点（L-STF 第 0 点）的采样数，与速率/包长无关，只随 `FFT_WIN_SHIFT`（openofdm_rx reg5）变化。

1. 回环定标：发射侧 `inject_80211` + `sdrctl dev sdr0 set reg xpu 1 1` 解静音；
2. 采集一帧（pre_trigger_init=`P`），解析出 IQ 块前导；
3. 与已知 L-STF/L-LTF 波形互相关，得到实际偏移 `d`；
4. 取正确 `C1 = P + d`；**采集与定标必须同一 FFT_WIN_SHIFT 配置**；
5. 若非默认 300，重新 `insmod ... pre_trigger_init=<C1> ...`。

### 5.2 板级复核清单（对拍开发文档 §7）

```
□ 回环 CSI 平坦（csi_iq_display 频域图接近常数）
□ 前导 IQ 与已知 L-STF/L-LTF 互相关峰在首采样处（对齐 = C1 正确）
□ 过滤：改 wh1/wh6/wh7 后只出目标包；不匹配包零输出（UDP 无孤儿块）
□ 双 TSF 配对：|tsf_iq - tsf_csi| ≤ 20 µs（脚本 WARNING 不出现）
□ 压力：./side_ch_ctl g1 + iperf3 5 分钟；sdrctl get reg = sdr0? wh20 读 reg20 不长期贴 4096
□ 与纯 CSI 模式对比：csi_iq_display 的 CSI 与 side_info_display.py 对同一包数值一致
□ tcpdump 主机侧 TSF ↔ 记录双 TSF 抽查 20 包
```

### 5.3 常见问题排查

| 现象 | 排查 |
|---|---|
| 完全无记录 | `wh20` 读 reg20=0；确认 `combined_init=1`、`iq_len_init>0`、dma 通道、`g10` 在跑 |
| 记录错位/长度异常 | record_len 四处一致性：RTL `record_len` / `RECORD_LEN` / `csi_iq_display` / tb golden 必须同值；`num_eq`/`iq_len` 与 ko 参数对齐 |
| 前导不在预期位置 | FFT_WIN_SHIFT 变了导致 C1 漂移 → 重定标 |
| 高包率丢块 | `g10`→`g1`；收紧 FC/addr 过滤；查 reg20 峰值 |
| bitstream 与 .ko 不配套 | `xpu reg63` 校验；同批打包 |

---

## 6. 回滚

- FPGA：`git checkout openwifi-hw -- ip/side_ch/src/` + 重跑 §3.1；
- 驱动：`git checkout openwifi -- driver/side_ch/` + 重编 `.ko`；
- 用户态：删除 `csi_iq_display.py` 即可（`side_ch_ctl` 未改）。
- 纯 CSI/纯 IQ 模式不受影响（不传 `combined_init` 即上游行为；probe 其余路径未动）。