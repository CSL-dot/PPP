#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RTCM切片服务 - 原地切割模式
支持正在写入的文件进行切割（通过重试机制处理文件锁定）
"""

import os
import time
import sys
import argparse
from datetime import datetime, timedelta
import struct

# 设置控制台编码为UTF-8（Windows）
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 导入站点路径配置
from path_config import get_paths, ensure_directories

# 获取本站点路径
PATHS = get_paths()
ensure_directories(PATHS)

# ==================== 配置参数 ====================
SEGMENT_MINUTES = 10
SAVE_DIR = PATHS["RTCM_WATCH_DIR"]
RTCM_SOURCE_DIR = PATHS["RTCM_SOURCE_DIR"]
STATION_NAME = PATHS["STATION_NAME"]

# 确保保存目录存在
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)


def find_rtcm_file():
    """自动查找RTCM源文件"""
    if os.path.exists(RTCM_SOURCE_DIR):
        rtcm_files = [f for f in os.listdir(RTCM_SOURCE_DIR) if f.endswith('.rtcm3')]
        if rtcm_files:
            # 优先选择以本站点名开头的文件
            for f in rtcm_files:
                if f.startswith(STATION_NAME):
                    return os.path.join(RTCM_SOURCE_DIR, f)
            # 否则返回最新修改的文件
            rtcm_files.sort(key=lambda x: os.path.getmtime(os.path.join(RTCM_SOURCE_DIR, x)), reverse=True)
            return os.path.join(RTCM_SOURCE_DIR, rtcm_files[0])
    return None


def parse_rtcm_time(data):
    """从RTCM数据中提取时间信息"""
    pos = 0
    data_len = len(data)
    
    while pos < data_len - 8:
        if data[pos] != 0xD3:
            pos += 1
            continue
        
        if pos + 3 >= data_len:
            break
        
        msg_len = struct.unpack('>H', data[pos+1:pos+3])[0] & 0x3FF
        if msg_len < 6:
            pos += 1
            continue
        
        msg_end = pos + msg_len + 3
        if msg_end > data_len:
            break
        
        msg_type = ((data[pos+3] & 0xFC) >> 2)
        
        if msg_type in [1004, 1005, 1006, 1007, 1008, 1074, 1084, 1094, 1124]:
            if msg_end >= pos + 11:
                tow_ms = struct.unpack('>I', data[pos+4:pos+8])[0]
                gps_tow = tow_ms / 1000.0
                gps_week = struct.unpack('>H', data[pos+8:pos+10])[0]
                return gps_week, gps_tow
        
        pos = msg_end
    
    return None, None


def gps_week_to_date(gps_week, gps_tow):
    """GPS周和秒转日期"""
    gps_start = datetime(1980, 1, 6)
    utc_time = gps_start + timedelta(weeks=gps_week, seconds=gps_tow)
    leap_seconds = 18
    utc_time -= timedelta(seconds=leap_seconds)
    return utc_time


def format_filename(station_name, utc_time):
    """生成文件名"""
    year_str = utc_time.strftime("%Y")
    doy = utc_time.timetuple().tm_yday
    hm_str = utc_time.strftime("%H%M")
    return f"{station_name}{year_str}{doy:03d}_{hm_str}.rtcm3"


def wait_for_file_unlock(file_path, max_wait=5):
    """
    等待文件解锁
    返回: 是否成功获取文件
    """
    for attempt in range(max_wait):
        try:
            # 尝试以共享模式打开文件（只读）
            with open(file_path, 'rb') as f:
                f.read(1)  # 尝试读取1字节
            return True
        except (PermissionError, IOError) as e:
            print(f"  文件被占用，等待中... ({attempt+1}/{max_wait})")
            time.sleep(1)
    return False


def copy_and_clear_file(src_file, dst_dir, station_name):
    """
    复制文件内容到切片目录，然后清空原文件
    处理文件锁定问题
    """
    if not os.path.exists(src_file):
        print(f"文件不存在: {src_file}")
        return False, None
    
    # 等待文件解锁
    if not wait_for_file_unlock(src_file, max_wait=5):
        print(f"无法获取文件，可能被长时间占用: {src_file}")
        return False, None
    
    file_size = os.path.getsize(src_file)
    if file_size == 0:
        print("文件为空，跳过")
        return False, None
    
    # 文件太小可能数据不完整
    if file_size < 100:
        print(f"文件太小 ({file_size} 字节)，跳过")
        return False, None
    
    try:
        # 读取全部数据
        with open(src_file, 'rb') as f:
            data = f.read()
        
        if not data:
            return False, None
        
        print(f"读取数据: {len(data)} 字节")
        
        # 解析时间
        gps_week, gps_tow = parse_rtcm_time(data)
        
        if gps_week is not None and gps_tow is not None:
            utc_time = gps_week_to_date(gps_week, gps_tow)
            new_name = format_filename(station_name, utc_time)
            print(f"使用GPST命名: {utc_time}")
        else:
            current_time = datetime.now()
            new_name = format_filename(station_name, current_time)
            print(f"无法解析时间，使用系统时间: {current_time}")
        
        new_path = os.path.join(dst_dir, new_name)
        
        # 保存切片
        with open(new_path, 'wb') as dst:
            dst.write(data)
        
        print(f"切片完成: {new_name} ({len(data)/1024:.1f} KB)")
        
        # 清空原文件
        # 再次等待文件解锁（写入程序可能正在写）
        if not wait_for_file_unlock(src_file, max_wait=3):
            print(f"警告: 无法清空文件（被占用），但切片已保存")
            return True, new_name
        
        # 清空文件
        with open(src_file, 'wb') as f:
            pass  # 清空
        
        print(f"[清空] 原文件已清空")  # 使用普通字符，不用特殊符号
        
        return True, new_name
        
    except Exception as e:
        print(f"处理失败: {e}")
        import traceback
        traceback.print_exc()
        return False, None


def do_slice_cut(rtcm_file, station_name, segment_minutes=SEGMENT_MINUTES):
    """
    原地切割：读取全部数据，保存为切片，清空原文件
    """
    print(f"切割文件: {rtcm_file}")
    
    success, filename = copy_and_clear_file(rtcm_file, SAVE_DIR, station_name)
    
    return success, [filename] if filename else []


def run_continuous(rtcm_file, station_name, segment_minutes=SEGMENT_MINUTES):
    """连续运行模式 - 原地切割"""
    print("="*60)
    print("RTCM切片服务 - 原地切割模式")
    print(f"站点: {station_name}")
    print(f"源文件: {rtcm_file}")
    print(f"切片间隔: {segment_minutes}分钟")
    print(f"保存目录: {SAVE_DIR}")
    print("模式: 读取后清空原文件，处理文件锁定")
    print("="*60)
    
    last_cut_time = datetime.now()
    
    while True:
        time.sleep(1)
        
        if not os.path.isfile(rtcm_file):
            continue

        current_time = datetime.now()
        if (current_time - last_cut_time).total_seconds() >= segment_minutes * 60:
            print(f"\n{'='*50}")
            print(f"开始切割 - {current_time.strftime('%H:%M:%S')}")
            
            success, files = do_slice_cut(rtcm_file, station_name, segment_minutes)
            
            if success and files:
                last_cut_time = current_time
                print(f"切割完成: {', '.join(files)}")
            elif success:
                last_cut_time = current_time
                print("无数据，跳过")
            else:
                print("切割失败")
                # 失败后等待更长时间再试
                time.sleep(30)


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='RTCM切片服务 - 原地切割模式')
    parser.add_argument('--once', action='store_true', help='单次执行模式')
    parser.add_argument('--station', type=str, default=STATION_NAME, help=f'站点名称')
    parser.add_argument('--input', type=str, default=None, help='输入RTCM文件')
    parser.add_argument('--interval', type=int, default=SEGMENT_MINUTES, help=f'切片间隔分钟数')
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    station_name = args.station
    
    rtcm_file = args.input
    if not rtcm_file or not os.path.exists(rtcm_file):
        rtcm_file = find_rtcm_file()
        if not rtcm_file:
            print(f"错误: 未找到RTCM源文件，请将.rtcm3文件放入: {RTCM_SOURCE_DIR}")
            sys.exit(1)
    
    print(f"使用RTCM源文件: {rtcm_file}")
    
    if args.once:
        print("="*50)
        print("RTCM切片服务 - 单次模式")
        print(f"站点: {station_name}")
        print("="*50)
        success, files = do_slice_cut(rtcm_file, station_name, args.interval)
        if success:
            print(f"生成文件: {files}")
        else:
            print("切片失败")
            sys.exit(1)
    else:
        run_continuous(rtcm_file, station_name, args.interval)