import sys, struct, zlib, lzma, time

path = sys.argv[1]
f = open(path, 'rb')

def read_vli(buf, pos):
    val = 0; shift = 0
    while True:
        b = buf[pos]; pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80): return val, pos
        shift += 7

f.seek(0, 2); size = f.tell()
f.seek(-12, 2)
crc_ft, back_raw, sflags, magic = struct.unpack('<II2s2s', f.read(12))
back = (back_raw + 1) * 4
f.seek(size - 12 - back)
idx = f.read(back)
body = idx[:-4]
n, p = read_vli(body, 1)

recs = []
comp = 12; uncomp = 0
for i in range(n):
    u, p = read_vli(body, p)
    un, p = read_vli(body, p)
    recs.append((comp, uncomp, u, un))
    comp += u + ((4 - u % 4) % 4)      # 块间 4 字节对齐补齐
    uncomp += un

with open('/tmp/recs.tsv', 'w') as out:
    out.write("blk\tcomp_off\tuncomp_off\tunpadded\tuncomp_size\tpadded_end\n")
    for i, (co, uo, u, un) in enumerate(recs, 1):
        out.write(f"{i}\t{co}\t{uo}\t{u}\t{un}\t{co + u + ((4 - u % 4) % 4)}\n")

hdr = open(path, 'rb').read(12)

print(f"共 {len(recs)} 块，逐块独立解码校验（用流头+单块，liblzma 会校验块内 CRC64）...")
t0 = time.time()
bad = []
for i, (co, uo, u, un) in enumerate(recs, 1):
    f.seek(co); blk = f.read(u)
    d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    try:
        out = d.decompress(hdr + blk)
    except lzma.LZMAError as e:
        bad.append((i, co, u, un, f"LZMAError: {e}"))
        continue
    except Exception as e:
        bad.append((i, co, u, un, f"{type(e).__name__}: {e}"))
        continue
    if len(out) != un:
        bad.append((i, co, u, un, f"输出长度不符 {len(out)} != {un}"))

dt = time.time() - t0
print(f"耗时 {dt:.0f} 秒")
print()
if not bad:
    print(">>> 全部 634 块校验通过（那问题就在索引之外的填充字节）")
else:
    print(f">>> 发现 {len(bad)} 个坏块：")
    print(f"{'块号':>5} {'压缩偏移':>14} {'压缩长度':>10} {'解压长度':>10}  原因")
    for i, co, u, un, why in bad:
        print(f"{i:>5} {co:>14,} {u:>10,} {un:>10,}  {why}")
