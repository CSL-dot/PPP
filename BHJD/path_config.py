#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一路径配置 - 站点版本
自动识别站点目录和公共目录
"""

import os
import sys

def get_paths():
    """
    获取本站点的所有路径
    返回字典，包含所有需要的路径
    """
    # 当前脚本所在目录就是站点根目录 (如 WUHAN/ZHDH/)
    station_root = os.path.dirname(os.path.abspath(__file__))
    
    # WUHAN根目录是站点目录的父目录
    wuhan_root = os.path.dirname(station_root)
    
    # 站点名称就是目录名
    station_name = os.path.basename(station_root)
    
    # 打印配置信息
    print(f"[路径] WUHAN根目录: {wuhan_root}")
    print(f"[路径] 站点名称: {station_name}")
    print(f"[路径] 站点目录: {station_root}")
    
    # ==================== 返回所有路径 ====================
    paths = {
        # 公共数据目录（所有站共享，BRDM和SP3文件放这里）
        "SOLU_DATA_DIR": os.path.join(wuhan_root, "SoluData"),
        
        # 本站点目录
        "STATION_ROOT": station_root,
        "STATION_NAME": station_name,
        
        # 数据子目录
        "RTCM_WATCH_DIR": os.path.join(station_root, "rtcm", "119"),
        "RINEX_WATCH_DIR": os.path.join(station_root, "rinex", "119"),
        "OUT_DIR": os.path.join(station_root, "out"),
        "LOGS_DIR": os.path.join(station_root, "logs"),
        "TEMP_DIR": os.path.join(station_root, "temp"),
        
        # 程序文件（本站点目录下）
        "EDGE_DRIVER_PATH": os.path.join(station_root, "msedgedriver.exe"),
        "RTCM_DECODE_EXE": os.path.join(station_root, "rtcm_decode_obs.exe"),
        "PPP_EXE": os.path.join(station_root, "RTKLIB_demo.exe"),
        
        # 配置文件（本站点目录下）
        "PPP_CONFIG1": os.path.join(station_root, "chushihua1.conf"),
        "PPP_CONFIG2": os.path.join(station_root, "chushihua2.conf"),
        "RTCM_CONFIG": os.path.join(station_root, "jiema.conf"),
        
        # RTCM源文件目录（需要用户把.rtcm3文件放这里）
        "RTCM_SOURCE_DIR": os.path.join(station_root, "rtknavi"),
    }
    
    return paths

def ensure_directories(paths):
    """确保所有必要的目录存在"""
    dirs = [
        paths["RTCM_WATCH_DIR"],
        paths["RINEX_WATCH_DIR"],
        paths["OUT_DIR"],
        paths["LOGS_DIR"],
        paths["TEMP_DIR"],
        paths["RTCM_SOURCE_DIR"],
    ]
    
    for dir_path in dirs:
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)
            print(f"[创建] 目录: {dir_path}")

def format_size(size_bytes):
    """格式化文件大小显示"""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.1f}KB"
    else:
        return f"{size_bytes/(1024*1024):.1f}MB"

def format_time(timestamp):
    """格式化时间戳显示"""
    if timestamp:
        from datetime import datetime
        return datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
    return "未知"

# 测试代码
if __name__ == "__main__":
    paths = get_paths()
    print("\n" + "="*60)
    print("路径配置信息:")
    print("="*60)
    for key, value in paths.items():
        print(f"  {key}: {value}")
    print("="*60)