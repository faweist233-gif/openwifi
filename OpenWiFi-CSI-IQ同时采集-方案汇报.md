# CSI 与前导码 IQ 同时采集 —— 方案汇报

> 需求：openwifi（AntSDR E316，BOARD_NAME=e310v2）上，把每包的 **CSI** 与该包**前导码（L-STF/L-LTF/L-SIG）对应的原始 IQ 采样**同时发给目标 IP。
>
> 结论：**UDP 传输链路已具备，无需新开发**（`side_ch_ctl g -s <IP>` 已把数据发往目标 IP 的 4000 端口）。真正的卡点在 FPGA：`side_ch` IP 内 CSI 模式与 IQ 抓取硬互斥。
---

## 1. 现状：为什么现在不能"同时"

- CSI 与 IQ 的数据源都已接入 `side_ch`（`openwifi-hw/boards/e310v2/src/system.bd`）：CSI 来自 `openofdm_rx` 的信道估计器；IQ 就是解调器正在解析的同一路 20 MHz 基带采样（即"解析出的前导码对应的原始 IQ"）。
- 但 `openwifi-hw/ip/side_ch/src/side_ch_control.v` 中 `iq_capture`（寄存器 3 的 bit0）一置 1：
  - `:313` 输出 mux 二选一；
  - `:704/:707` CSI 小 FIFO 被置复位、写使能被关死；
  - `:436/:479/:730` CSI 状态机三处被门住。
- 驱动同步二选一（`driver/side_ch/side_ch.c:34` 注释明说 "iq capture enabled, csi disabled"）。
- UDP 链路现状：内核 `side_ch.ko` 经 netlink 把 DMA 到 DDR 的数据回给 `side_ch_ctl`，后者原样 `sendto()` 到目标 IP:4000（`side_ch_ctl.c:419-421`）。

---

## 2. 方案 A：FPGA 增加"CSI + 前导 IQ 联合模式"

**原理**：在 `side_ch_control.v` 增加一个联合模式位（复用寄存器 3 的空闲 bit1），

1. **解除 CSI 采集门控**——CSI 与 IQ 两条采集路径并行工作；
2. **IQ 采集改为两段式触发**：`long_preamble_detected`（接收机 LTF 匹配尖峰，openofdm_rx 现成输出；尖峰→包起点距离是与速率无关的常数）时刻**锁定回读窗口**——前导码精确对齐、免主机搜索；CSI 状态机"FC/addr 匹配通过、CSI 确定提交"时刻回读推送——每条 IQ 块必跟一条 CSI 块，未通过过滤的包窗口被下一包重锁自然覆盖，孤儿块在硬件层面消失；
3. **两块串行化 + 整条记录的空间检查**（短包时 IQ 流与 CSI 块可能时间重叠，CSI 数据安全停在小 FIFO 里，延迟无害）。

每包在 m_axis 流中形成一条**定长联合记录**，一个 UDP datagram 可含多条完整记录：

```
│ IQ 块: TSF(64b) + iq_len×{I16,Q16,AGC状态16,RSSI16} │ CSI 块: TSF(64b) + 频偏(64b) + 56×CSI(64b) + num_eq×52×等化器(64b) │
     record_len = (1+iq_len) + (2+56+num_eq×52) 个 64bit 字
```

前导码 = L-STF(160)+L-LTF(160)+L-SIG(80) = 400 采样 @20 MHz（20 µs），全在 IQ 块内。

**开销**（按环节标注平台要求：**Linux x86_64** 指必须在一台 Linux 机器上装 Vivado/交叉编译环境；**E316 板上** 指直接在板端 shell 操作即可）

| 项 | 量 | 平台/环境 |
|---|---|---|
| RTL 改动 | 集中在 `side_ch_control.v` + `side_ch.v`；**不改寄存器地址表/BD/设备树/内核** | 改 `.v` 源码任意平台；仿真验证须 Linux x86_64 + Vivado |
| 综合/实现 | 每轮 Vivado 1–3 h | **必须 Linux x86_64 + Vivado** |
| 驱动 | 驱动源码零改动；若需重编 `.ko` | 沿用现有 `side_ch.ko` 则无需任何环境；重编须 Linux x86_64（`make_driver.sh` 交叉编译） |
| 主机端脚本 | 解析联合记录格式（每 datagram 多条） | 任意平台（PC 上 Python） |
| 板级部署 | bitstream + 配套 `.ko` 用 `wgd.sh` 热重载；**不烧 SD、全程无需重建镜像**（BOOT.BIN/BD/设备树未动） | **E316 板上直接操作**（scp 上传文件 + 跑脚本即可） |
| 板级验证 | 0.5–1 人日 | E316 + PC（PC 收 UDP:4000） |
| 资源 | **不新增 BRAM**（复用现有 dpram + FIFO，仅放开门控） | — |
| 联合记录 | num_eq=0、iq_len=440 时 ≈ 499 字（4.0 KB）；FIFO 可积压 ~8 条 | — |
| 人力 | RTL+仿真 1–2 人日，驱动+脚本 1 人日，板级验证 0.5–1 人日，**共 2–4 人日** | — |

**产出**：FPGA 原生 CSI（含 num_eq 个等化器输出）+ 前导原始 IQ（每采样带 AGC/RSSI 状态），同一 datagram、同一时间基准（双 TSF 互验）。

---

## 3. 方案 B：零 FPGA 改动

**原理**：现有 IQ 模式本身就能抓到前导码——`insmod side_ch.ko iq_len_init=400` + 触发条件选 `long_preamble_detected`（或 addr 匹配触发）+ `pre_trigger_len` 回退盖住包起点，UDP 里即有"从包起点开始的前导原始 IQ"。**CSI 由主机端从同一段 IQ 中的 L-LTF 重算**（64 点 FFT × 已知 L-LTF 共轭；先用 L-STF 双半相关估 CFO 并校正），一个 datagram 同时承载两种数据。

**开销**：0 FPGA 改动、0 驱动改动；写一个主机端解析/重算脚本（参照 `iq_capture.py` + `side_info_display.py` 改造）。
**局限**（是否可接受取决于研究目标）：
- CSI 数值非 FPGA 原生值（与芯片输出差一个 CFO/归一化因子，需主机端校正）；
- 拿不到 `num_eq` 等化器输出；
- 触发条件语义与 CSI 过滤条件不完全等价（可选 addr 匹配触发近似对齐）。

---

