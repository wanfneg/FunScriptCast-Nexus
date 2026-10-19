"""最小 mpv IPC 调试：启动 mpv -> 写一条命令 -> 原样打印读到的字节。"""
import subprocess, sys, time

MPV = r"C:\Program Files\MPV Player\mpv.exe"
PIPE = r"\\.\pipe\fsc-debug-ipc"
VIDEO = sys.argv[1] if len(sys.argv) > 1 else r"E:\testvideo\K1drsp.mp4"

proc = subprocess.Popen([MPV, VIDEO, f"--input-ipc-server={PIPE}",
                         "--no-config", "--keep-open=yes", "--pause=yes",
                         "--vo=null", "--ao=null"],
                        creationflags=subprocess.CREATE_NEW_CONSOLE)
print("mpv pid:", proc.pid)

fh_w = fh_r = None
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    try:
        fh_w = open(PIPE, "r+b", buffering=0)
        fh_r = fh_w
        break
    except (FileNotFoundError, OSError) as e:
        print("pipe 未就绪:", e)
        time.sleep(0.3)
if not fh_w:
    sys.exit("打不开管道")

print("管道已连接，发送 get_property mpv-version ...")
fh_w.write(b'{"command":["get_property","mpv-version"],"request_id":7}\n')
fh_r.timeout = 3.0  # FileIO 无 timeout 属性，这行会抛错的话就换 raw 阻塞读+线程
try:
    data = fh_r.read(65536)
    print("读到:", data[:500])
except Exception as e:
    print("读异常:", type(e).__name__, e)

proc.terminate()
