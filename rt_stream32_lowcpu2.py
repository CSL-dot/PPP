
import subprocess
import time
import os
import gc
import ctypes
import sys

# ===================== 【可修改配置】 =====================
stations = [
    "WHGI",
    "WHHN",
    "ZHDH", 
    "BHJD",
    "CHXL", "MULA", "QIJW", "PALC", "XIAD","CJXQ", "WHHP",
    "WHXZ", "XZPT", "YALU", "CABU", "DAGH", "LIKG",  "QILZ",
    "WHCD", "ZRJD", "ZHSH", "JXZF", "JXAS", "HUSI",  "JIKA",
    "XIKO", "WHDH", "DZDX", "HSJD", "ZHHY", "SHXJ", "WHXC"
]

SERVER = "116.211.238.25:2101"
MOUNT = "FIXEDGW-CGCS2000"
SAVE_DIR_TEMPLATE = "./{station}/rtknavi"  

BATCH_START_DELAY = 0.05
MONITOR_INTERVAL = 10
ENABLE_MONITOR = False
RECONNECT_INTERVAL = 5000  # 断连后5秒重连

# =====================================================

process_list = []
job_handle = None  # 作业对象

# Windows 作业对象：父进程退出 → 所有子进程自动退出
if sys.platform == 'win32':
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    # 显式声明 API 函数签名，防止 ctypes 自动转换时发生类型错误
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p

    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetInformationJobObject.restype = ctypes.c_int

    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int

    # 创建 Job 对象
    job_handle = kernel32.CreateJobObjectW(None, None)

    # 标准定义 Win32 API 内部结构体以获取准确的内存偏移
    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # 初始化限制配置并设置 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x00002000)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000  

    # 应用属性至作业
    res = kernel32.SetInformationJobObject(
        job_handle,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(info),
        ctypes.sizeof(info)
    )
    if not res:
        print(f"⚠️ 警告：设置作业限制失败，错误代码: {kernel32.GetLastError()}")

# 内存优化
if sys.platform == 'win32':
    ctypes.windll.kernel32.SetProcessWorkingSetSize(
        ctypes.windll.kernel32.GetCurrentProcess(), -1, -1)
    ctypes.windll.kernel32.SetPriorityClass(
        ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)


def minimize_memory():
    gc.collect()
    if sys.platform == 'win32':
        ctypes.windll.kernel32.SetProcessWorkingSetSize(
            ctypes.windll.kernel32.GetCurrentProcess(), -1, -1)


def start_station(sta, index):
    save_dir = SAVE_DIR_TEMPLATE.format(station=sta)
    os.makedirs(save_dir, exist_ok=True)
    out_path = os.path.join(save_dir, f"{sta}.rtcm3")
    
    cmd = [
        "str2str.exe",
        "-in", f"ntrip://PPP-{sta}:{PASSWORD}@{SERVER}/{MOUNT}",
        "-out", out_path,
        "-t", "0",
        "-r", str(RECONNECT_INTERVAL)
    ]

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0

    p = subprocess.Popen(
        cmd,
        startupinfo=startupinfo,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        shell=False
    )

    # 绑定子进程到作业对象，注意使用 int() 将 Python 的句柄对象转换为整数句柄传递
    if sys.platform == 'win32' and job_handle:
        kernel32.AssignProcessToJobObject(job_handle, int(p._handle))

    return p


def monitor_processes():
    if not ENABLE_MONITOR:
        return
    dead = []
    for i, p in enumerate(process_list):
        if p.poll() is not None:
            dead.append((i, stations[i], p.returncode))
    if dead:
        print(f"\n⚠️  {len(dead)} 个进程已退出")
        for i, sta, code in dead:
            print(f"   - [{i:2d}] {sta} → 退出码: {code}")


if __name__ == "__main__":
    print("=" * 60)
    print("  32个CORS站批量接收工具（子进程自动清理版）")
    print("  退出方式：Ctrl + C → 所有 str2str.exe 自动关闭")
    print("=" * 60)

    minimize_memory()

    for i, sta in enumerate(stations, 1):
        p = start_station(sta, i)
        process_list.append(p)
        save_dir = SAVE_DIR_TEMPLATE.format(station=sta)
        print(f"[{i:2d}] {sta} → 运行中（保存路径：{save_dir}/{sta}.rtcm3）")
        time.sleep(BATCH_START_DELAY)
        if i % 8 == 0:
            minimize_memory()

    minimize_memory()
    print("\n✅ 全部启动成功！")
    print("\n按 Ctrl + C 停止所有进程")

    try:
        while True:
            monitor_processes()
            time.sleep(MONITOR_INTERVAL)
    except KeyboardInterrupt:
        print("\n🛑 正在停止所有进程...")

        # 双重保险：手动终止
        for p in process_list:
            try:
                p.terminate()
            except Exception:
                pass

        time.sleep(0.5)

        for p in process_list:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass

        process_list.clear()
        minimize_memory()
        print("✅ 所有 str2str.exe 已全部关闭！")
