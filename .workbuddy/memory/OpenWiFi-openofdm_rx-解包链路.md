# openofdm_rx 解包链路（源码级笔记）

子模块 pin `2bb3ad1a1f0023bfd15168db4e196ebf0d56d76c`。本地 `openwifi-hw/ip/openofdm_rx/` 是**空目录**，这些结论来自已拉取的拷贝，重查需先 `git submodule update`。

- `openofdm_rx.v` 只是壳（AXI-Lite reg0~31 + `signal_watchdog` + 例化 `dot11`）；逻辑全在 `dot11.v`（1190 行 = 数据通路 + 17 态 FSM，状态号见 `common_params.v:29-45`）。
- 流水线：功率门（`rssi_half_db>=power_thres`，仅粗开关）→ `sync_short`（L-STF 16 样本延迟自相关，门限=能量均值×1.5/1.25，plateau 计数防单音）→ `sync_long`（32 抽头与已知 LTS 前 16 样本互相关，前 63 样本取首峰 `addr1`，再等 64 样本取第二峰；CORDIC 相关角 ÷64 = 每样本 CFO `ltf_phase_offset`，单位 2π/1024；读地址回退 `addr1+22-32`）→ 64 点 FFT（`S_FFT` 持续装点，符号间 `in_raddr += gi_skip` 跳 CP）→ `rot_after_fft` → `equalizer`（`S_FIRST_LTS` 存、`S_SECOND_LTS` 均值×参考 = H[k]；导频 ±7/±21 估 CPE 逐符号纠正、可选平滑、出 `noise_var`）→ `ofdm_decoder` 五级：`demodulate`（按 `{rate[7],rate[3:0]}` 选 BPSK/QPSK/16QAM/64QAM，LLR 权重 = `csi²/noise_var`）→ `deinterleave`(LUT) → `viterbi`(Xilinx v7.0, K=7 r1/2) → `descramble`(LFSR x⁷+x⁴+1，前 7 bit 种种子) → `bits_to_bytes`+`crc32`(`EXPECTED_FCS=0xc704dd7b`)。
- HT 识别不靠 L-SIG rate=0b1011（那只是"下一符号是 HT-SIG"），而是均衡输出转 90° 后统计 `|Q|>=|I|` >8（QBPSK），再 HT-SIG 6 字节过 CRC8 + 逐项否决 mcs>7/cbw/stbc/fec/num_ext/tail。A-MPDU 在 `S_MPDU_DELIM` 按 4 字节 delimiter（签名 `0x4e`+CRC8）循环切 MPDU。
- 长度：`num_bits_to_decode = 22 + len×8`；`phy_len_calculation.v` 的 `n_dbps` 表（6M=24…54M=216；MCS0=26…MCS7=260）算 `n_ofdm_sym`。SIGNAL/HT-SIG 不扰码，只有数据段 `do_descramble=1` 且跳过前 9 bit 服务域。
- sync_long / rot_after_fft / equalizer **复用同一块 `rot_lut` ROM**（`dot11.v:194-204` 双口地址仲裁）；FFT 持续循环而非每包重启。`signal_watchdog`（`openofdm_rx.v:150,168`）在 `S_DECODE_SIGNAL` 期间监控信号长度/DC 游走/星座点过小/相偏超阈，拉 `receiver_rst` 复位 dot11。
- CSI 具体出处：`equalizer.v` 对 L-LTF 的频域估计 `H[k]=(Y1[k]+Y2[k])/2×L_LTF[k]`（`calc_mean.v`：±1 参考即取反）；`csi_valid` 在非 HT 第 1 / HT 第 5 个符号输出（`:217,219`），早于 FCS 判定 → **FCS 失败包也有 CSI**。段长 56 项 = `HT_SUBCARRIER_MASK`（±1..±28），HT 与非 HT 都用（FIFO 恒 64 项按掩码取 56，`:726`）；非 HT `SUBCARRIER_MASK`=52 仅 equalizer 内部用。措辞："CSI 不是从 side_ch 抓的那份 IQ 算的"，而不是"CSI 与 IQ 无关"。
