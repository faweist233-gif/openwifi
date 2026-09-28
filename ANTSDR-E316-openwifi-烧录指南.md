# AntSDR E316 烧录 openwifi 操作指南

> 适用硬件：MicroPhase AntSDR E316（Zynq-7020 + AD9361，2x2 MIMO）
> 目标：把 SD 卡做成 openwifi 启动盘，板上跑起 802.11 AP（`sdr0`）

---

## 0. 先确认最关键的一件事

**AntSDR E316 就是 E310V2。** 微相官方两处文档都写死了这一点：

- 文档中心首页产品列表：`E series includes E310, E200, E310V2 (E316)`
- `antsdr-fw-patch` 编译说明：`generate the firmware for ANTSDR E310 or ANTSDR E200 or ANTSDR E316(E310V2)`，对应 `export TARGET=e310v2`

所以 openwifi 侧使用 **`BOARD_NAME=e310v2`**，官方仓库里已有这个板级目录，**不需要改任何代码**：

- `openwifi/kernel_boot/boards/e310v2/`（devicetree + u-boot.elf）
- `openwifi-hw/boards/e310v2/`（FPGA 工程）
- `openwifi-hw-img/boards/e310v2/sdk/system_top.xsa`（预编译硬件，免 Vivado 授权）

⚠️ **不要用 `antsdr`**。那是老 E310 的板名；E310V2 的设备树 model 是 `ANTSDR-E310V2`，`setup_once.sh` 靠这个字符串选板，选错会加载错误的 bitstream / 驱动。

Zynq-7020 属于小器件，**该板卡在 openwifi 官方列表里标注 Vivado License "NO need"**。

---

## 1. 两条路线，先选一条

| | 路线 A：官方预编译镜像 | 路线 B：Buildroot 自建镜像 |
|---|---|---|
| 主机要求 | macOS / Windows / Linux 都行 | **Ubuntu / Debian x86_64**（Buildroot 限制） |
| 下载量 | 3.3 GB（解压后 15.9 GB） | 源码 + Buildroot，构建数小时 |
| SD 卡 | **≥16 GB**（随板附赠 32 GB 可用） | 169 MB，任意卡 |
| 需要 Vivado | 首次不需要 | 不需要（从 XSA 提 bitstream 和 ps7_init） |
| 版本 | openwifi 1.5.0（2025-08） | 当前 master |
| 手工步骤 | 需覆盖 3 个 BOOT 文件 | 零手工，烧完即用 |

- **想最快跑通 → 路线 A。**
- **手上有 Linux 机器、想要干净且最新的系统 → 路线 B。**
- 路线 C（`user_space/update_sdcard.sh` 从源码完整造 Kuiper 大镜像）需要 Vivado/Vitis + 本地交叉编译内核，仅在你必须定制系统时才走，本文不展开。

---

## 2. 路线 A：官方预编译镜像

### A1. 物料

- AntSDR E316 + 附赠 32 GB SD 卡（或自备 ≥16 GB）
- USB 线（UART 串口调试用）
- 网线 + **千兆**网口（板子 100M 不通）
- 天线 **2 根**：一根接 `TX/RX1`（发），一根接 `RX1`（收）；详见 §A4 的天线接线表

> 建议先把原卡数据备份或另找一张卡。E316 出厂时 QSPI 和 SD 卡里都是 **Pluto 固件**，烧 openwifi 会把原固件冲掉。

### A2. 下载并写入 SD 卡（macOS 命令）

```bash
curl -L -O https://users.ugent.be/~xjiao/openwifi-1.5.0-shahecheng.img.xz   # 3.3 GB
xz -dk openwifi-1.5.0-shahecheng.img.xz                                    # 解压出 ~15.9 GB

diskutil list                       # 找到 SD 卡，例如 /dev/disk4（按容量辨认）
diskutil unmountDisk /dev/disk4
sudo dd if=openwifi-1.5.0-shahecheng.img of=/dev/rdisk4 bs=1m
sync
```

- 用 `/dev/rdisk4`（裸设备）而不是 `/dev/disk4`，快数倍。
- 目标必须是**整盘设备**，不是分区（`/dev/disk4s1`）。
- 别的平台：Ubuntu 用 `Startup Disk Creator` / `gnome-disks`；Windows 用 `balenaEtcher`。

### A3. 覆盖 e310v2 板级文件（仍在电脑上做）

镜像烧完会出现两个分区：

- `BOOT`（FAT32）—— macOS 会自动挂载到 `/Volumes/BOOT`
- `rootfs`（ext4）—— macOS **挂不上，这是正常的**，不用管

把 E316 对应的那套启动文件从子目录提到 BOOT 根目录：

```bash
cp /Volumes/BOOT/openwifi/e310v2/* /Volumes/BOOT/
```

覆盖的是这 3 个文件：`BOOT.BIN`、`devicetree.dtb`、`system_top.bit.bin`。（`uImage` 是 32 位 Zynq 板共用，位置本来就在根目录，不用动。）

然后弹出：

```bash
diskutil eject /dev/disk4
```

> 官方 README 还让你删 `rootfs/root/kernel_modules` 和 `rootfs/etc/network/interfaces.new`。这两个都在 ext4 分区上，macOS 删不了 —— **可以跳过**：前者 `setup_once.sh` 开头就会 `rm -rf` 重建；后者只在 SSH 连不上时才需要处理，届时用串口进板子删即可（见排错第 2 条）。

### A4. 拨开关、接线、上电

1. **启动模式 DIP**：开关在网口下方，丝印 `BOOT / QSPI / SD`，拨到 **SD**。
2. **网线**接板上 RJ45，另一端接 PC 的千兆口。
3. **PC 网卡设静态 IP**（macOS，网卡名换成你自己的，`ifconfig` 查）：
   ```bash
   sudo ifconfig en7 inet 192.168.10.1 netmask 255.255.255.0
   ```
4. **天线 2 根**：`TX/RX1` 一根、`RX1` 一根（见下方天线接线表）。
5. 上电，等约 40 秒。

**天线接线表**——四个 SMA 的丝印从左到右是 `TX/RX1`、`RX1`、`RX2`、`TX/RX2`：

| SMA 口 | 内部连到 | 怎么用 |
|---|---|---|
| `TX/RX1` | TX1A 经 balun 进 SP2T；该开关的另一个位置通向 `RX1` 那条支路 | **必接**：openwifi 的发射从这里出去 |
| `RX1` | RX1A 经 balun 进另一个 SP2T（两个 SP2T 交叉相连） | **必接**：openwifi 的接收默认从这个口进来 |
| `RX2` | RX2A（通道 2 的接收输入） | 可选：用第二路接收（`side_ch` 监测 / dual-antenna capture）时接 |
| `TX/RX2` | TX2A（通道 2 的发射） | 可选：2×2 MIMO 时接 |

- **默认用 2 根天线**（`TX/RX1` 发 + `RX1` 收），不要指望一根通吃，原因见下。

**"TX/RX1 一根通吃"为什么不成立**

四个口接哪个不是固定接线，而是由**开关**决定的：每个通道的 `TX_nA` 和 `RX_nA` 各经一个 balun 进一个 SP2T，两个 SP2T 交叉相连（openwifi 板级框图 `kernel_boot/boards/e310v2/README.assets/struct.png`）。

这组开关由 **AD9361 的 `CTRL_OUT` 引脚**驱动，而 openwifi 驱动里确实在管它：`openwifi_set_antenna()` 选天线时会写 `ad9361_ctrl_outs_setup(index=AD9361_CTRL_OUT_INDEX_ANT0/ANT1)`（`driver/sdr.h` 里这两个值是 `0x16` / `0x17`），同时改 FPGA 的 `TX_INTF_REG_ANT_SEL` / `RX_INTF_REG_ANT_SEL`。

而 openwifi 的 AD9361 跑的是 **FDD**（设备树有 `adi,frequency-division-duplex-mode-enable`）：TX 和 RX **同时在线**——自发时只是把 RX **基带**静音（`./sdrctl dev sdr0 set reg xpu 1 1` 可以解静音），射频接收通路一直开着。同一天线无法同时收发，所以默认配置必然是分口的：**TX → `TX/RX1`，RX → `RX1`**。

旁证：ADI 的 USRP B210 是同一套前端思路（`TX/RX` 共口 + 独立 `RX2` 收口），UHD 里 RX 默认走 `RX2`；AntSDR 系列在 UHD 下的输出同样是 `Antennas: TX/RX, RX2`。只有把开关切到 **TDD 共口**（RX 也从 `TX/RX1` 进）时一根天线才能既发又收，那是半双工用法，与 openwifi 的 FDD 默认冲突。

- 附赠天线：2 根胶棒 + 1 根**吸盘天线（那是 GPS 天线）**。吸盘天线接板子左缘的两个小同轴口之一（另一个丝印 `PPS/10M`，是外部参考输入），**不是 SMA**。
- 频率：默认 AP 是 **5 GHz**（`hw_mode=a`、`channel=36`），天线要能覆盖 5 GHz；手上只有 2.4 GHz 天线就把 `hostapd-openwifi.conf` 改成 `hw_mode=g` + 2.4G 信道再重跑 `fosdem.sh`。
- 别让发射口空载：起 AP（`fosdem.sh`）前先把 `TX/RX1` 的天线接上。

**30 秒实测确认接受口在哪**（不确定就做这一步）：先只把天线插 `TX/RX1`，读 RSSI；再把天线换到 `RX1` 读同一个值。值明显高（比如 −40 对 −90）的那一侧就是当前生效的接收口。

```bash
cat /sys/bus/iio/devices/iio:device0/in_voltage0_rssi    # 或板上 ./rssi_openwifi_show.sh
dmesg | grep openwifi_set_antenna                        # 会打印 tx_ant/rx_ant 和 ctrl_out 索引
```

**自检小技巧**：如果 `ping 192.168.1.10` 有响应、`ssh` 进去是 `root/analog` —— 那说明它还在跑 **Pluto 固件**，DIP 拨错了或没生效，回到第 1 步。

### A5. 首次登录与初始化

```bash
ping 192.168.10.122
ssh root@192.168.10.122          # 密码 openwifi
```

板上执行（**只做一次**）：

```bash
raspi-config --expand-rootfs     # 仅在 SD > 16 GB 时需要，做完先 reboot
/root/openwifi/setup_once.sh     # 按 /proc/device-tree/model 自动识别 e310v2
reboot
```

`setup_once.sh` 干的事：把对应架构（32 位）的内核模块和 openwifi 二进制就位、建立 `/lib/modules/$(uname -r)` 软链、把 `/root/openwifi_BOOT/e310v2/system_top.bit.bin` 拷到 openwifi 目录、现场编译 `sdrctl` / `side_ch_ctl` / `inject_80211`。

### A6. 起 AP 并验证

```bash
cd /root/openwifi
./wgd.sh                 # 加载 system_top.bit.bin + 驱动，生成 sdr0 接口
./fosdem.sh              # 启动 hostapd，SSID: openwifi
iw dev                   # 应能看到 sdr0
```

让手机/笔记本搜 `openwifi` 并连上，会自动拿到 `192.168.13.x`，浏览器开 `192.168.13.1` 能看到板上网页。

常用变体：

- `./wgd.sh 1` —— 打开实验性 AMPDU 聚合（11n 吞吐更高）
- `./fosdem-11ag.sh` —— 强制 11a/g 模式
- 默认信道是 5 GHz ch36（`hw_mode=a`），终端不支持 5 GHz 就改板上的 `hostapd-openwifi.conf` 再重跑 `fosdem.sh`

给客户端通外网（**在 PC 上配置**，不是在板子上）：

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -o <上网网卡> -j MASQUERADE
sudo ip route add 192.168.13.0/24 via 192.168.10.122 dev <连板网卡>
```

### A7.（可选，推荐）把 FPGA + 驱动升到最新

1.5.0 镜像里的 bitstream / 内核模块是 2025-08 的。要跟 master 同步，需要一台 **Linux x86_64 主机 + Vivado/Vitis 2021.1**（只用其中的 `bootgen`，不需要重新综合 FPGA）：

```bash
git clone https://github.com/open-sdr/openwifi-hw-img
export XILINX_DIR=/opt/Xilinx                 # 必须含 Vitis 目录，不是 Vitis_HLS
export OPENWIFI_HW_IMG_DIR=~/openwifi-hw-img

# 1) 由 XSA 生成 system_top.bit.bin（板子侧 FPGA Manager 用）
cd openwifi/user_space
./boot_bin_gen.sh $XILINX_DIR e310v2 $OPENWIFI_HW_IMG_DIR/boards/e310v2/sdk/system_top.xsa
scp system_top.bit.bin root@192.168.10.122:/root/openwifi/

# 2) 编最新驱动（首次要先 prepare_kernel.sh，仅需跑一次）
sudo apt install flex bison libssl-dev device-tree-compiler u-boot-tools -y
./prepare_kernel.sh $XILINX_DIR 32            # Zynq-7000 用 32
cd ../driver && ./make_all.sh $XILINX_DIR 32
scp `find ./ -name \*.ko` root@192.168.10.122:/root/openwifi/
```

回板上 `cd /root/openwifi && ./wgd.sh`，它会发现同目录下的 `system_top.bit.bin` 并先重新加载 FPGA。

---

## 3. 路线 B：Buildroot 小镜像（169 MB）

在 Ubuntu/Debian x86_64 上：

```bash
sudo apt install build-essential git rsync cpio unzip bc file wget curl python3 libncurses-dev

git clone https://github.com/open-sdr/openwifi
cd openwifi
git submodule update --init buildroot
git clone https://github.com/open-sdr/openwifi-hw-img ../openwifi-hw-img

./buildroot-build.sh e310v2 build
```

产物：

```
output/e310v2/images/openwifi-e310v2-sdcard.img      # 完整 SD 镜像，约 169 MB
output/e310v2/images/openwifi-e310v2-system.frm      # 完整系统升级包，约 20 MB
```

烧卡：

```bash
lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINTS
sudo umount /dev/sdX1 /dev/sdX2 2>/dev/null || true
sudo dd if=output/e310v2/images/openwifi-e310v2-sdcard.img of=/dev/sdX bs=4M conv=fsync status=progress
sync
```

首次构建的 common 系统（工具链 + Linux 6.12 + rootfs）最慢，之后换板只重编 U-Boot 和最终产物。

上板（串口 115200 8N1 或 SSH，`root` / `openwifi`）：

```bash
cat /etc/openwifi-board      # 应为 e310v2
openwifi-start 0             # 等价于 /root/openwifi/wgd.sh 0
ip link show sdr0
iw dev
```

要点：

- **不需要手工覆盖 BOOT 文件**，构建流程会从 XSA 里生成 bitstream 和 ps7_init。
- 这是 **single-rootfs、无 A/B 回滚**的设计，整机升级（`host-tools/openwifi_fw_update.py`）写入时不能断电。
- 默认 `eth0 = 192.168.10.122/24`，和路线 A 一致；两张板同时在线要改一个的 IP。
- 正常使用建议让 U-Boot 去加载 FPGA（`OPENWIFI_RELOAD_FPGA=0`），运行时重载只用于开发。

---

## 4. 排错清单

| 现象 | 原因 / 处理 |
|---|---|
| `ping 192.168.10.122` 不通，但 `192.168.1.10` 通 | 还在跑 Pluto 固件 → DIP 拨到 SD；`ssh root@192.168.1.10` 密码是 `analog` 可确认 |
| 网口灯不亮 / 连不上 | 板子**只支持 1000M**，换千兆口/换网线；PC 网卡确认协商到 1000 |
| 能 ping 但 SSH 不通 | 串口登进去 `rm -f /etc/network/interfaces.new` 后 reboot（官方 known issue） |
| 首次启动报 `EXT4-fs error (device mmcblk0p2)` | 烧录不完整，换工具重烧（macOS 用 `dd` 到 `/dev/rdiskX` / `balenaEtcher`；Linux 用 gnome-disks） |
| 干脆起不来 / 卡在 U-Boot | 板上 SPI flash 里的旧环境变量作祟：串口打断进 U-Boot，执行 `env default -a` 然后 `saveenv` |
| 加载 `.ko` 报 symbol/version 错 | 镜像内核对不上新驱动：把 `prepare_kernel.sh` 生成的 `adi-linux/arch/arm/boot/uImage` 拷回 BOOT 分区 |
| 手机搜不到 SSID | 默认 5 GHz ch36，换支持 5G 的终端，或改 `hostapd-openwifi.conf` 后重跑 `fosdem.sh` |
| 客户端拿不到 IP | 板上 `service isc-dhcp-server restart` 后重连 |
| 跑约 2 小时后收不到包 | Xilinx Viterbi 解码器评估授权超时。判定：`./sdrctl dev sdr0 get reg rx 20` 输出一直不变。处理：重跑 `wgd.sh` 重载 bitstream，或断电重启 |
| ping 通了但丢包严重且延迟忽大忽小 | 对端 COTS 设备的省电策略：`iw dev wlan0 set power_save off` |

---

## 5. 参数速查

| 项 | 值 |
|---|---|
| openwifi 板名 | `e310v2` |
| 设备树 model | `ANTSDR-E310V2` |
| 预编译镜像 | `openwifi-1.5.0-shahecheng.img.xz`，3.3 GB → 解压 15.9 GB |
| SD 卡要求 | ≥16 GB（路线 A）/ 任意（路线 B） |
| 需覆盖的 BOOT 文件 | `BOOT.BIN`、`devicetree.dtb`、`system_top.bit.bin` |
| 板子登录 | `root` / `openwifi`，管理口 `192.168.10.122/24` |
| PC 侧管理 IP | `192.168.10.1` |
| AP 网段 / 网页 | `192.168.13.0/24` / `192.168.13.1` |
| 串口 | 115200 8N1 |
| 内核架构位宽 `ARCH_BIT` | `32`（Zynq-7000） |
| Vivado 授权 | 不需要 |

---

## 6. 安全与法规

openwifi 是真实发射的 Wi-Fi 基带，**不是仿真**。请遵守当地频谱法规：优先用同轴 + 衰减器做回环测试，或调到合法 ISM 频段并控制发射功率；不要在不合规频点上长时间开 AP。
