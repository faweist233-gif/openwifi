#
# openwifi CSI + preamble IQ combined-mode receive / display tool (route A).
# Receives UDP datagrams on :4000 from side_ch_ctl (g <period> -s <this-IP>) and parses
# the fixed-length combined records produced by the FPGA (csi_iq_combined=1):
#
#   record_len (64bit words) = (1+iq_len) + (2 + 56 + num_eq*52)
#   [IQ block]  TSF(64b) + iq_len * {I16, Q16, gpio_status16, rssi16}
#   [CSI block] TSF(64b) + phase_offset(64b) + 56*CSI(64b) + num_eq*52*EQ(64b)
#
# Reuses the CSI parsing of side_info_display.py and the IQ parsing of iq_capture.py.
# num_eq and iq_len must match the side_ch.ko module parameters used on the board.
#
# Xianjun jiao. putaoshu@msn.com; xianjun.jiao@imec.be (structure origin)
#
import os
import socket
import argparse
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt

UDP_IP = "192.168.10.1"   # Local IP to listen
UDP_PORT = 4000            # Local port to listen

# align with side_ch_control.v and all related user space / driver / remote files
MAX_NUM_DMA_SYMBOL = 8192
CSI_LEN = 56
EQUALIZER_LEN = (56-4)
HEADER_LEN = 2

def parse_record(u16, num_eq, iq_len):
    """Parse one combined record from a uint16 array of length record_len*4."""
    record_len = (1 + iq_len) + (HEADER_LEN + CSI_LEN + num_eq*EQUALIZER_LEN)
    num_int16 = record_len*4

    # ---- IQ block (first 1+iq_len words) ----
    tsf_iq = (u16[0] | np.left_shift(u16[1], 16) | np.left_shift(u16[2], 32) | np.left_shift(u16[3], 48)).astype(np.uint64)
    iq_block = u16[4:(1+iq_len)*4]
    iq_c = np.int16(iq_block[0::4]) + np.int16(iq_block[1::4])*1j
    agc_gain = np.bitwise_and(iq_block[2::4], np.uint16(0xFF))
    rssi_half_db = np.bitwise_and(iq_block[3::4], np.uint16(0x7FF))

    # ---- CSI block (after the IQ block) ----
    csi_off = (1+iq_len)*4
    tsf_csi = (u16[csi_off+0] | np.left_shift(u16[csi_off+1], 16) |
               np.left_shift(u16[csi_off+2], 32) | np.left_shift(u16[csi_off+3], 48)).astype(np.uint64)
    freq_offset = (20e6*np.int16(u16[csi_off+4])/512)/(2*np.pi)

    tmp_vec_i = np.int16(u16[csi_off+8:(num_int16-1):4])
    tmp_vec_q = np.int16(u16[csi_off+9:(num_int16-1):4])
    tmp_vec = tmp_vec_i + tmp_vec_q*1j
    CSI_LEN_HALF = CSI_LEN//2
    csi = np.zeros(CSI_LEN, dtype=complex)
    csi[0:CSI_LEN_HALF] = tmp_vec[CSI_LEN_HALF:CSI_LEN]
    csi[CSI_LEN_HALF:] = tmp_vec[0:CSI_LEN_HALF]
    equalizer = np.zeros(0, dtype=complex)
    if num_eq > 0:
        equalizer = tmp_vec[CSI_LEN:(CSI_LEN+num_eq*EQUALIZER_LEN)]

    return tsf_iq, iq_c, agc_gain, rssi_half_db, tsf_csi, freq_offset, csi, equalizer

def display_record(tsf_iq, iq_c, rssi_half_db, tsf_csi, freq_offset, csi, equalizer):
    tsf_gap_us = abs(int(tsf_iq) - int(tsf_csi))
    if tsf_gap_us > 20:
        print(f"WARNING: IQ/CSI TSF gap {tsf_gap_us} us > 20 us (pairing violated)")

    fig_iq = plt.figure(0)
    fig_iq.clf()
    ax = fig_iq.subplots(2, 1)
    ax[0].plot(iq_c.real, 'b', label='I')
    ax[0].plot(iq_c.imag, 'r', label='Q')
    ax[0].set_title(f"Preamble IQ  tsf_iq={int(tsf_iq)} us  tsf_csi={int(tsf_csi)} us  (gap {tsf_gap_us} us)")
    ax[0].legend()
    ax[1].plot(rssi_half_db)
    ax[1].set_ylabel("RSSI half-dB")
    ax[0].grid(); ax[1].grid()
    fig_iq.canvas.flush_events()

    fig_csi = plt.figure(1)
    fig_csi.clf()
    axc = fig_csi.add_subplot(211)
    axc.set_title(f"CSI  freq_offset={freq_offset:+.0f} Hz")
    axc.stem(np.arange(-28, 28), np.abs(np.roll(csi, CSI_LEN//2)))
    axc.set_ylabel("abs")
    axp = fig_csi.add_subplot(212)
    axp.plot(np.unwrap(np.angle(np.roll(csi, CSI_LEN//2))))
    axp.set_ylabel("phase (rad)")
    axp.set_xlabel("subcarrier idx")
    fig_csi.canvas.flush_events()

    if len(equalizer) > 0:
        fig_eq = plt.figure(2)
        fig_eq.clf()
        plt.scatter(equalizer.real, equalizer.imag)
        plt.title("equalizer (I,Q)")
        fig_eq.canvas.flush_events()

def main():
    parser = argparse.ArgumentParser(description='openwifi CSI+IQ combined capture display tool')
    parser.add_argument('num_eq', nargs='?', type=int, default=0,
                        help='Number of equalizer outputs (default: 0)')
    parser.add_argument('iq_len', nargs='?', type=int, default=440,
                        help='Number of preamble IQ samples per record (default: 440)')
    parser.add_argument('--ip', default=UDP_IP,
                        help=f'Local IP to listen on (default: {UDP_IP})')
    args = parser.parse_args()

    num_eq = args.num_eq
    iq_len = args.iq_len
    record_len = (1 + iq_len) + (HEADER_LEN + CSI_LEN + num_eq*EQUALIZER_LEN)
    num_byte_per_record = 8*record_len
    num_dma_symbol_per_trans = record_len

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.ip, UDP_PORT))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 464)

    print(f"record_len={record_len} words ({num_byte_per_record} B)  num_eq={num_eq} iq_len={iq_len}")
    print(f"Listening on {args.ip}:{UDP_PORT} (Ctrl-C to stop)")

    if os.path.exists("csi_iq.txt"):
        os.remove("csi_iq.txt")
    fd = open('csi_iq.txt', 'a')

    plt.ion()
    while True:
        try:
            data, addr = sock.recvfrom(MAX_NUM_DMA_SYMBOL*8)
            if len(data) % num_byte_per_record != 0:
                print(f"Abnormal length {len(data)} (not multiple of {num_byte_per_record})")
            u16 = np.frombuffer(data, dtype='uint16')
            np.savetxt(fd, u16)
            num_rec = len(u16) // (record_len*4)
            u16 = u16.reshape(num_rec, record_len*4)
            for i in range(num_rec):
                tsf_iq, iq_c, agc_gain, rssi_half_db, tsf_csi, freq_offset, csi, equalizer = \
                    parse_record(u16[i], num_eq, iq_len)
                display_record(tsf_iq, iq_c, rssi_half_db, tsf_csi, freq_offset, csi, equalizer)
        except KeyboardInterrupt:
            print('User quit')
            break

    fd.close()
    sock.close()

if __name__ == '__main__':
    main()