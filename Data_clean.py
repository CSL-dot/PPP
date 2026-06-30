import os
import shutil
import time
from datetime import datetime

# ====================== 配置区（只改这里） ======================
# 所有站点
STATIONS = [
    "BHJD", "CABU", "CHXL", "CJXQ",
    "DAGH", "DZDX", "HSJD", "HUSI",
    "JIKA", "JXAS", "JXZF", "LIKG",
    "MULA", "PALC", "QIJW", "QILZ",
    "SHXJ", "WHCD", "WHDH", "WHGI",
    "WHHN", "WHHP", "WHXC", "WHXZ",
    "XIAD", "XIKO", "XZPT", "YALU",
    "ZHDH", "ZHHY", "ZHSH", "ZRJD"
]

# 要清理的目录
TARGET_PATTERNS = [
    "./{STATION}/rinex/FinishOfile",
    "./{STATION}/rtcm/FinishOfile",
    "./{STATION}/temp",
]

# 保留天数：超过这个天数的文件会被删除
KEEP_DAYS = 0
# 专门清理 .stat 文件
STAT_FILE_PATTERN = "./{STATION}/out/{STATION}/*.stat"
# 专门清理 .pos 文件
POS_FILE_PATTERN = "./{STATION}/out/{STATION}/*.pos"

# 保留天数：超过这个天数的文件会被删除
KEEP_DAYS2 = 5
# 专门清理 .gz .CLK .SP3文件
GZ_CLK_SP3_path = "./SoluData"
# 专门清理 png文件
PNG_path = r"E:\PyProj\Precipitation\PLOT"


# ====================================================================


def clean_directory_by_age(directory_path: str, days: int = KEEP_DAYS):
    """清理目录中超时的文件/文件夹"""
    if not os.path.exists(directory_path):
        print(f"[{datetime.now()}] 目录不存在，跳过: {directory_path}")
        return

    now = time.time()
    cutoff = now - days * 86400
    print(f"[{datetime.now()}] 清理目录（超时{days}天）: {directory_path}")

    for item in os.listdir(directory_path):
        item_path = os.path.join(directory_path, item)
        try:
            mtime = os.path.getmtime(item_path)
            if mtime < cutoff:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                    print(f"  → 删除文件: {item_path}")
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    print(f"  → 删除目录: {item_path}")
        except Exception as e:
            print(f"[{datetime.now()}] 失败: {item_path} → {e}")


def clean_stat_pos_files(station: str, days: int = KEEP_DAYS):
    """专门清理 ./XX/out/XX/ 下的 *.stat  *.pos 文件（按时间）"""
    stat_path = STAT_FILE_PATTERN.format(STATION=station)
    folder_path = os.path.dirname(stat_path)
    
    if not os.path.exists(folder_path):
        return

    now = time.time()
    cutoff = now - days * 86400

    for f in os.listdir(folder_path):
        if f.endswith((".stat", ".pos")):
            full_file = os.path.join(folder_path, f)
            try:
                mtime = os.path.getmtime(full_file)
                if mtime < cutoff:
                    os.unlink(full_file)
                    print(f"  → 删除.stat .pos文件: {full_file}")
            except Exception as e:
                print(f"[{datetime.now()}] 失败: {full_file} → {e}")
       
        
        
def clean_gz_clk_sp3_files(days: int = KEEP_DAYS2):
    """专门清理 ./XX/out/XX/ 下的 *.gz  *.clk  *.sp3 文件（按时间）"""
    folder_path = GZ_CLK_SP3_path
    
    if not os.path.exists(folder_path):
        return

    now = time.time()
    cutoff = now - days * 86400

    for f in os.listdir(folder_path):
        if f.endswith((".gz", ".CLK", ".SP3", "p")):
            full_file = os.path.join(folder_path, f)
            try:
                mtime = os.path.getmtime(full_file)
                if mtime < cutoff:
                    os.unlink(full_file)
                    print(f"  → 删除.gz .CLK .SP3 *p文件: {full_file}")
            except Exception as e:
                print(f"[{datetime.now()}] 失败: {full_file} → {e}")
                

        
def clean_png_files(days: int = KEEP_DAYS2):
    """专门清理 png 文件（按时间）"""
    folder_path = PNG_path
    
    if not os.path.exists(folder_path):
        return

    now = time.time()
    cutoff = now - days * 86400

    for f in os.listdir(folder_path):
        if f.endswith((".png")):
            full_file = os.path.join(folder_path, f)
            try:
                mtime = os.path.getmtime(full_file)
                if mtime < cutoff:
                    os.unlink(full_file)
                    print(f"  → 删除.png文件: {full_file}")
            except Exception as e:
                print(f"[{datetime.now()}] 失败: {full_file} → {e}")


def clean_all_stations():
    print("=" * 70)
    print(f"[{datetime.now()}] 开始按时间清理所有站点")
    print("=" * 70)

    for station in STATIONS:
        print(f"\n===== 正在处理站点: {station} =====")
        
        # 清理3个目录
        for pattern in TARGET_PATTERNS:
            full_path = pattern.format(STATION=station)
            clean_directory_by_age(full_path)
        
        # 清理 .stat .pos文件
        clean_stat_pos_files(station)
    
    # 清理 .gz .CLK *SP3文件
    clean_gz_clk_sp3_files()
    clean_png_files()
        
    print(f"\n[{datetime.now()}] ✅ 所有清理任务完成！\n")


def run_scheduled(interval_hours: float = 1):
    """定时运行（24小时一次）"""
    interval_seconds = interval_hours * 3600
    print(f"[{datetime.now()}] 定时服务已启动，每{interval_hours}小时执行一次")
    while True:
        try:
            clean_all_stations()
        except Exception as e:
            print(f"任务异常: {e}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    # 立即执行一次
    #clean_all_stations()

    # 长期后台定时执行（打开即用）
    run_scheduled(1)