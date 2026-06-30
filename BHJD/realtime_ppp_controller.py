#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import io

# 设置控制台编码为UTF-8
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import os
import time
import threading
import subprocess
import logging
from datetime import datetime, timedelta
from pathlib import Path
import signal
import re
import queue
import uuid
import shutil
import argparse
import hashlib

# 导入站点路径配置
from path_config import get_paths, ensure_directories, format_size, format_time

# 获取本站点路径
PATHS = get_paths()
ensure_directories(PATHS)

# 配置日志
LOG_FILE = os.path.join(PATHS["LOGS_DIR"], f"{PATHS['STATION_NAME']}.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(f'RT-PPP-{PATHS["STATION_NAME"]}')

# 创建消息队列
rinex_queue = queue.Queue()

# ==================== 清理配置 ====================
FINISH_OFILE_DIR = "FinishOfile"  # 已完成文件目录

# ==================== 负载控制配置 ====================
# 关键目的：避免 32 个站点在星历/SP3/CLK就绪后同时启动 PPP，导致 CPU 瞬时拉满。
# 这些值可通过环境变量修改，不改代码也能调参：
#   set PPP_MAX_CONCURRENT=4
#   set PPP_STAGGER_SECONDS=120
#   set SLICE_STAGGER_SECONDS=60
GLOBAL_PPP_MAX_CONCURRENT = int(os.environ.get("PPP_MAX_CONCURRENT", "4"))
PPP_STAGGER_SECONDS = int(os.environ.get("PPP_STAGGER_SECONDS", "120"))
SLICE_STAGGER_SECONDS = int(os.environ.get("SLICE_STAGGER_SECONDS", "60"))
PPP_LOCK_STALE_SECONDS = int(os.environ.get("PPP_LOCK_STALE_SECONDS", "1800"))
PPP_LOCK_DIR = Path(PATHS["SOLU_DATA_DIR"]) / "_ppp_global_locks"


def station_stagger_seconds(station_name, max_seconds):
    """根据站点名生成稳定错峰秒数，避免所有站点同秒触发。"""
    if max_seconds <= 0:
        return 0
    h = hashlib.md5(station_name.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % max_seconds


class GlobalPPPSlot:
    """
    跨进程 PPP 全局并发锁。
    用目录 mkdir 的原子性实现，不依赖第三方库，Windows/Linux 均可用。
    最多允许 GLOBAL_PPP_MAX_CONCURRENT 个 RTKLIB_demo.exe 同时运行。
    """

    def __init__(self, station_name, max_concurrent=GLOBAL_PPP_MAX_CONCURRENT):
        self.station_name = station_name
        self.max_concurrent = max(1, int(max_concurrent))
        self.lock_dir = PPP_LOCK_DIR
        self.slot_path = None

    def _cleanup_stale_slot(self, slot):
        try:
            if not slot.exists():
                return
            age = time.time() - slot.stat().st_mtime
            if age > PPP_LOCK_STALE_SECONDS:
                shutil.rmtree(slot, ignore_errors=True)
                logger.warning(f"[{self.station_name}] 清理过期PPP锁: {slot.name}, age={age:.0f}s")
        except Exception as e:
            logger.debug(f"[{self.station_name}] 清理PPP锁失败: {e}")

    def acquire(self):
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        wait_logged = False
        while True:
            for i in range(self.max_concurrent):
                slot = self.lock_dir / f"slot_{i:02d}"
                self._cleanup_stale_slot(slot)
                try:
                    slot.mkdir()  # 原子操作：成功即拿到该槽位
                    self.slot_path = slot
                    owner = slot / "owner.txt"
                    owner.write_text(
                        f"station={self.station_name}\n"
                        f"pid={os.getpid()}\n"
                        f"time={datetime.now().isoformat()}\n",
                        encoding="utf-8"
                    )
                    logger.info(f"[{self.station_name}] 获得PPP全局槽位: {slot.name} ({i+1}/{self.max_concurrent})")
                    return
                except FileExistsError:
                    continue
                except Exception as e:
                    logger.debug(f"[{self.station_name}] 获取PPP槽位异常: {e}")

            if not wait_logged:
                logger.info(f"[{self.station_name}] PPP全局并发已满，等待空闲槽位... max={self.max_concurrent}")
                wait_logged = True
            time.sleep(2)

    def release(self):
        if self.slot_path:
            try:
                shutil.rmtree(self.slot_path, ignore_errors=True)
                logger.info(f"[{self.station_name}] 释放PPP全局槽位: {self.slot_path.name}")
            finally:
                self.slot_path = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


class DailyCleaner(threading.Thread):
    """每日0点清空 FinishOfile 文件夹（递归删除所有内容）"""
    
    def __init__(self, station_name, station_root):
        """
        station_name: 站点名称
        station_root: 站点根目录（如 D:/WUHAN/ZHDH）
        """
        threading.Thread.__init__(self)
        self.station_name = station_name
        self.station_root = Path(station_root)
        
        # 两个完成目录（基于站点根目录）
        self.rtcm_finish_dir = self.station_root / "rtcm" / FINISH_OFILE_DIR
        self.rinex_finish_dir = self.station_root / "rinex" / FINISH_OFILE_DIR
        
        self.running = True
        self.daemon = True
        
    def run(self):
        logger.info(f"[{self.station_name}] 启动每日清理线程")
        logger.info(f"  清理时间: 每天 00:00")
        logger.info(f"  清理方式: 递归删除目录中的所有内容")
        logger.info(f"  RTCM完成目录: {self.rtcm_finish_dir}")
        logger.info(f"  RINEX完成目录: {self.rinex_finish_dir}")
        
        while self.running:
            try:
                # 计算到下一个0点的时间
                now = datetime.now()
                # 设置今天18:30
                next_cleanup = now.replace(hour=0, minute=0, second=0, microsecond=0)

                # 如果今天18:30已经过了，就改为明天18:30
                if now >= next_cleanup:
                    next_cleanup += timedelta(days=1)

                wait_seconds = (next_cleanup - now).total_seconds()
                
                logger.info(f"[{self.station_name}] 距离下次清理还有 {wait_seconds/3600:.1f} 小时")
                time.sleep(wait_seconds)
                
                # 执行清理
                self.do_cleanup()
                
            except Exception as e:
                logger.error(f"[{self.station_name}] 清理线程错误: {e}")
                time.sleep(3600)
    
    def cleanup_directory_recursive(self, dir_path):
        """
        递归删除目录中的所有内容（文件和子目录）
        返回: (删除文件数, 释放字节数)
        """
        if not dir_path.exists():
            return 0, 0
        
        deleted_files = 0
        freed_bytes = 0
        
        # 遍历目录中的所有项
        for item in dir_path.iterdir():
            try:
                if item.is_file():
                    # 删除文件
                    file_size = item.stat().st_size
                    item.unlink()
                    deleted_files += 1
                    freed_bytes += file_size
                    logger.debug(f"[{self.station_name}] 删除文件: {item.relative_to(self.station_root)} ({format_size(file_size)})")
                    
                elif item.is_dir():
                    # 递归删除子目录
                    sub_files, sub_bytes = self.cleanup_directory_recursive(item)
                    deleted_files += sub_files
                    freed_bytes += sub_bytes
                    
                    # 删除空目录
                    try:
                        item.rmdir()
                        logger.debug(f"[{self.station_name}] 删除目录: {item.relative_to(self.station_root)}")
                    except:
                        pass
                        
            except Exception as e:
                logger.warning(f"[{self.station_name}] 删除失败: {item} - {e}")
        
        return deleted_files, freed_bytes
    
    def do_cleanup(self):
        """执行清理 - 递归删除两个完成目录中的所有内容"""
        logger.info(f"[{self.station_name}] ========== 开始每日清理 ==========")
        
        total_files = 0
        total_bytes = 0
        
        # 清理 RTCM 完成目录
        if self.rtcm_finish_dir.exists():
            files, bytes_freed = self.cleanup_directory_recursive(self.rtcm_finish_dir)
            total_files += files
            total_bytes += bytes_freed
            logger.info(f"[{self.station_name}] RTCM完成目录: 删除 {files} 个文件, 释放 {format_size(bytes_freed)}")
            # 确保目录存在（清理后重建）
            self.rtcm_finish_dir.mkdir(parents=True, exist_ok=True)
        else:
            logger.info(f"[{self.station_name}] RTCM完成目录不存在，创建: {self.rtcm_finish_dir}")
            self.rtcm_finish_dir.mkdir(parents=True, exist_ok=True)
        
        # 清理 RINEX 完成目录
        if self.rinex_finish_dir.exists():
            files, bytes_freed = self.cleanup_directory_recursive(self.rinex_finish_dir)
            total_files += files
            total_bytes += bytes_freed
            logger.info(f"[{self.station_name}] RINEX完成目录: 删除 {files} 个文件, 释放 {format_size(bytes_freed)}")
            # 确保目录存在（清理后重建）
            self.rinex_finish_dir.mkdir(parents=True, exist_ok=True)
        else:
            logger.info(f"[{self.station_name}] RINEX完成目录不存在，创建: {self.rinex_finish_dir}")
            self.rinex_finish_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"[{self.station_name}] 清理完成: 共删除 {total_files} 个文件, 释放 {format_size(total_bytes)}")
        logger.info(f"[{self.station_name}] ========== 清理结束 ==========")
    
    def stop(self):
        self.running = False


class BRDMMonitor(threading.Thread):
    """
    监控BRDM文件更新（从公共SoluData目录）
    当检测到BRDM文件内容变化时，触发本站点的RTCM切片
    不负责下载BRDM文件
    """
    
    def __init__(self, brdm_dir, slice_script, station_name, check_interval=60):
        threading.Thread.__init__(self)
        self.brdm_dir = Path(brdm_dir)
        self.slice_script = slice_script
        self.station_name = station_name
        self.check_interval = check_interval
        self.running = True
        self.daemon = True
        self.last_brdm_info = {}
        self.slice_in_progress = False
        self.rtcm_source_file = None
        
        # 查找本站点的RTCM源文件
        self._find_rtcm_source()
        
    def _find_rtcm_source(self):
        """查找本站点的RTCM源文件"""
        rtcm_source_dir = PATHS["RTCM_SOURCE_DIR"]
        if os.path.exists(rtcm_source_dir):
            rtcm_files = list(Path(rtcm_source_dir).glob('*.rtcm3'))
            if rtcm_files:
                # 优先选择以本站点开头的文件
                for f in rtcm_files:
                    if f.name.startswith(self.station_name):
                        self.rtcm_source_file = str(f)
                        logger.info(f"找到RTCM源文件: {self.rtcm_source_file}")
                        return
                # 否则取第一个
                self.rtcm_source_file = str(rtcm_files[0])
                logger.info(f"找到RTCM源文件: {self.rtcm_source_file}")
            else:
                logger.warning(f"未找到RTCM源文件: {rtcm_source_dir}")
    
    def run(self):
        logger.info(f"[{self.station_name}] 启动BRDM文件监控 - 公共目录: {self.brdm_dir}")
        
        while self.running:
            try:
                brdm_files = list(self.brdm_dir.glob('BRDM*.26p'))
                
                if brdm_files:
                    current_info = {}
                    for f in brdm_files:
                        current_info[f.name] = {
                            'size': f.stat().st_size,
                            'mtime': f.stat().st_mtime,
                            'path': f
                        }
                    
                    self._check_for_content_updates(current_info)
                    self.last_brdm_info = current_info
                
                time.sleep(self.check_interval)
                
            except Exception as e:
                logger.error(f"[{self.station_name}] BRDM监控错误: {e}")
                time.sleep(self.check_interval)
    
    def _check_for_content_updates(self, current_info):
        """检查文件内容是否有更新（通过文件大小）"""
        try:
            for filename, info in current_info.items():
                if filename not in self.last_brdm_info:
                    logger.info(f"[{self.station_name}] 检测到新BRDM文件: {filename}")
                    self.trigger_slice_once()
                    return
            
            for filename, info in current_info.items():
                if filename in self.last_brdm_info:
                    old_info = self.last_brdm_info[filename]
                    
                    if info['size'] != old_info['size']:
                        logger.info(f"[{self.station_name}] 检测到BRDM文件内容更新: {filename}")
                        size_diff = info['size'] - old_info['size']
                        if size_diff > 0:
                            size_diff_str = format_size(size_diff)
                            logger.info(f"   大小变化: +{size_diff_str}")
                        self.trigger_slice_once()
                        return
                        
        except Exception as e:
            logger.error(f"[{self.station_name}] 检查内容更新时出错: {e}")
    
    def trigger_slice_once(self):
        """调用切片脚本 - 为本站点执行增量切片"""
        if self.slice_in_progress:
            logger.debug(f"[{self.station_name}] 切片正在进行中，跳过")
            return
        
        if not self.rtcm_source_file:
            logger.warning(f"[{self.station_name}] 未找到RTCM源文件，跳过切片")
            self._find_rtcm_source()
            return
        
        self.slice_in_progress = True
    
        try:
            # BRDM更新会被所有站点同时看到，这里按站点名错峰，避免32个切片/解码瞬时并发。
            delay = station_stagger_seconds(self.station_name, SLICE_STAGGER_SECONDS)
            if delay > 0:
                logger.info(f"[{self.station_name}] BRDM触发切片错峰等待 {delay}s")
                time.sleep(delay)

            logger.info("="*60)
            logger.info(f"[{self.station_name}] BRDM内容更新触发增量切片")
        
            if not os.path.exists(self.slice_script):
                logger.error(f"[{self.station_name}] 切片脚本不存在: {self.slice_script}")
                return
            
            cmd = [
                sys.executable, 
                self.slice_script, 
                "--once",
                "--station", self.station_name,
                "--input", self.rtcm_source_file
            ]
            logger.info(f"[{self.station_name}] 执行切片: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=os.path.dirname(self.slice_script), encoding='utf-8', errors='ignore')
            
            if result.returncode == 0:
                logger.info(f"[{self.station_name}] 增量切片成功")
                if result.stdout:
                    for line in result.stdout.split('\n'):
                        if line.strip():
                            logger.info(f"   {line}")
            else:
                logger.error(f"[{self.station_name}] 增量切片失败, 返回码: {result.returncode}")
                if result.stderr:
                    logger.error(f"   错误: {result.stderr}")
        
        except subprocess.TimeoutExpired:
            logger.error(f"[{self.station_name}] 切片超时")
        except Exception as e:
            logger.error(f"[{self.station_name}] 切片过程出错: {e}")
        finally:
            self.slice_in_progress = False
            logger.info("="*60)
    
    def stop(self):
        self.running = False


class RTCMFileMonitor(threading.Thread):
    """监控RTCM切片文件并触发解码"""
    
    def __init__(self, station_name, watch_dir, decode_exe, config_file, rinex_queue, interval=1):
        threading.Thread.__init__(self)
        self.station_name = station_name
        self.watch_dir = Path(watch_dir)
        self.decode_exe = decode_exe
        self.config_file = config_file
        self.rinex_queue = rinex_queue
        self.interval = interval
        self.processed_files = set()
        self.processing_files = set()
        self.file_start_time = {}
        self.running = True
        self.daemon = True
        self.rinex_dir = None
        self.last_processed_time = None
        
        # 创建完成文件目录
        self.finish_dir = self.watch_dir.parent / FINISH_OFILE_DIR
        if not self.finish_dir.exists():
            self.finish_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.station_name}] 创建完成文件目录: {self.finish_dir}")
        
        logger.info(f"[{self.station_name}] RTCM监控初始化:")
        logger.info(f"  - 监控目录: {self.watch_dir}")
        logger.info(f"  - 完成目录: {self.finish_dir}")
        logger.info(f"  - 解码程序: {self.decode_exe}")
        
    def run(self):
        logger.info(f"[{self.station_name}] 启动RTCM文件监控: {self.watch_dir}")
        
        # 从配置文件读取RINEX输出目录
        self._get_rinex_output_dir()
        
        while self.running:
            try:
                all_rtcm_files = list(self.watch_dir.glob('*.rtcm3'))
                
                for rtcm_file in all_rtcm_files:
                    if not rtcm_file.name.startswith(self.station_name):
                        continue
                    
                    # 跳过已完成目录中的文件
                    if FINISH_OFILE_DIR in str(rtcm_file):
                        continue
                        
                    if rtcm_file.name in self.processed_files:
                        continue
                        
                    if rtcm_file.name in self.processing_files:
                        if self._is_processing_timeout(rtcm_file.name):
                            logger.warning(f"[{self.station_name}] 文件处理超时: {rtcm_file.name}")
                            self.processing_files.remove(rtcm_file.name)
                            if rtcm_file.name in self.file_start_time:
                                del self.file_start_time[rtcm_file.name]
                        continue
                    
                    self.processing_files.add(rtcm_file.name)
                    self.file_start_time[rtcm_file.name] = time.time()
                    self._process_single_file(rtcm_file)
                
                self._cleanup_processing_records()
                time.sleep(self.interval)
                
            except Exception as e:
                logger.error(f"[{self.station_name}] 监控线程错误: {e}")
                time.sleep(self.interval)
    
    def _extract_file_time(self, filename):
        """从RTCM文件名中提取时间"""
        match = re.search(r'[A-Z]{4}(\d{4})(\d{3})_(\d{4})\.rtcm3$', filename)
        if match:
            year = int(match.group(1))
            doy = int(match.group(2))
            hm = match.group(3)
            hour = int(hm[:2])
            minute = int(hm[2:])
            try:
                date = datetime(year, 1, 1) + timedelta(days=doy - 1)
                return datetime(year, date.month, date.day, hour, minute)
            except:
                return None
        return None
    
    def _extract_first_obs_time(self, o_file_path):
        """从O文件头中提取第一个观测时间"""
        try:
            with open(o_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if 'END OF HEADER' in line:
                        break
                    if 'TIME OF FIRST OBS' in line:
                        stripped = line.lstrip()
                        if stripped and stripped[0].isdigit():
                            parts = stripped.split()
                            if len(parts) >= 6:
                                year = int(parts[0])
                                month = int(parts[1])
                                day = int(parts[2])
                                hour = int(parts[3])
                                minute = int(parts[4])
                                second = float(parts[5])
                                return datetime(year, month, day, hour, minute, int(second))
        except Exception as e:
            logger.error(f"[{self.station_name}] 解析O文件失败: {e}")
        return None
    
    def _generate_gps_filename(self, gps_time):
        """根据GPST生成文件名"""
        year = gps_time.year
        doy = gps_time.timetuple().tm_yday
        hm = gps_time.strftime("%H%M")
        return f"{self.station_name}{year}{doy:03d}_{hm}.o"
    
    def _process_single_file(self, rtcm_file):
        """处理单个RTCM文件"""
        try:
            logger.info(f"[{self.station_name}] 开始处理: {rtcm_file.name}")
            
            file_time = self._extract_file_time(rtcm_file.name)
            if file_time and self.last_processed_time:
                time_diff = file_time - self.last_processed_time
                if time_diff.total_seconds() > 660:
                    logger.warning(f"[{self.station_name}] 文件时间间隔过大: {time_diff.total_seconds():.1f}秒")
            
            process_id = str(uuid.uuid4())[:8]
            temp_work_dir = os.path.join(PATHS["TEMP_DIR"], f"{self.station_name}_{process_id}")
            os.makedirs(temp_work_dir, exist_ok=True)
            
            temp_config = os.path.join(temp_work_dir, "jiema.conf")
            shutil.copy2(self.config_file, temp_config)
            
            with open(temp_config, 'r', encoding='utf-8') as f:
                config_content = f.read()
            
            fixed_output_dir = temp_work_dir.replace("\\", "/")
            config_content = re.sub(
                r'output_dir\s*=\s*.+',
                f'output_dir = {fixed_output_dir}',
                config_content
            )
            
            with open(temp_config, 'w', encoding='utf-8') as f:
                f.write(config_content)
            
            cmd = [self.decode_exe, temp_config]
            logger.info(f"[{self.station_name}] 执行解码")
            
            work_dir = os.path.dirname(self.decode_exe)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=work_dir, encoding='utf-8', errors='ignore')
            
            if result.returncode == 0:
                time.sleep(3)
                
                if os.path.exists(temp_work_dir):
                    temp_files = list(Path(temp_work_dir).glob('*.o'))
                    
                    if temp_files:
                        logger.info(f"[{self.station_name}] 生成RINEX文件: {[f.name for f in temp_files]}")
                        self.processed_files.add(rtcm_file.name)
                        
                        if file_time:
                            self.last_processed_time = file_time
                        else:
                            self.last_processed_time = datetime.now()
                        
                        for temp_file in temp_files:
                            gps_time = self._extract_first_obs_time(temp_file)
                            
                            if gps_time is None:
                                logger.error(f"[{self.station_name}] 无法从O文件提取GPST时间")
                                try:
                                    temp_file.unlink()
                                except:
                                    pass
                                continue
                            
                            new_name = self._generate_gps_filename(gps_time)
                            logger.info(f"[{self.station_name}] 新文件名: {new_name}")
                            
                            target_path = os.path.join(self.rinex_dir, new_name)
                            
                            if os.path.exists(target_path):
                                logger.warning(f"[{self.station_name}] 目标文件已存在，覆盖: {new_name}")
                                os.remove(target_path)
                            
                            shutil.move(str(temp_file), target_path)
                            logger.info(f"[{self.station_name}] 移动到: {new_name}")
                            self.rinex_queue.put(new_name)
                        
                        process_time = time.time() - self.file_start_time.get(rtcm_file.name, time.time())
                        logger.info(f"[{self.station_name}] 处理完成: {rtcm_file.name} ({process_time:.1f}秒)")
                        

                    else:
                        logger.warning(f"[{self.station_name}] 解码成功但未检测到新RINEX文件")
                        self.rinex_queue.put("SCAN_NOW")
            else:
                logger.error(f"[{self.station_name}] 解码失败: {rtcm_file.name}")
                if result.stderr:
                    logger.error(f"错误信息: {result.stderr}")
            
            try:
                shutil.rmtree(temp_work_dir)
            except:
                pass
                
        except subprocess.TimeoutExpired:
            logger.error(f"[{self.station_name}] 解码超时: {rtcm_file.name}")
        except Exception as e:
            logger.error(f"[{self.station_name}] 处理文件出错: {e}")
        finally:
            if rtcm_file.name in self.processing_files:
                self.processing_files.remove(rtcm_file.name)
            if rtcm_file.name in self.file_start_time:
                del self.file_start_time[rtcm_file.name]
    
    def _is_processing_timeout(self, filename, timeout=300):
        start_time = self.file_start_time.get(filename)
        if start_time and (time.time() - start_time) > timeout:
            return True
        return False
    
    def _cleanup_processing_records(self):
        if len(self.processed_files) > 1000:
            self.processed_files = set(list(self.processed_files)[-1000:])
    
    def _get_rinex_output_dir(self):
        try:
            config_path = Path(self.config_file)
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                match = re.search(r'output_dir\s*=\s*(.+)', content)
                if match:
                    self.rinex_dir = Path(match.group(1).strip())
                    logger.info(f"[{self.station_name}] RINEX输出目录: {self.rinex_dir}")
        except Exception as e:
            logger.error(f"[{self.station_name}] 读取配置文件失败: {e}")
    
    def stop(self):
        self.running = False


class RINEXFileMonitor(threading.Thread):
    """监控RINEX文件并触发PPP解算 - 先处理累积O文件，再继续处理新O文件"""
    
    def __init__(self, station_name, watch_dir, ppp_exe, config_file, rinex_queue, sp3_dir, interval=1):
        threading.Thread.__init__(self)
        self.station_name = station_name
        self.watch_dir = Path(watch_dir)
        self.ppp_exe = ppp_exe
        self.config_file = config_file
        self.rinex_queue = rinex_queue
        self.sp3_dir = Path(sp3_dir)
        self.interval = interval
        self.processed_files = set()  # 本次程序运行期间已成功/已隔离的文件名
        self.running = True
        self.daemon = True
        self.station_stagger = station_stagger_seconds(self.station_name, PPP_STAGGER_SECONDS)
        
        # 按“年+年积日”分组，避免跨年或历史文件被当前年份误判
        # 结构: {(year, doy): [file1, file2, ...]}
        self.pending_by_day = {}
        # 记录正在等待星历/钟差或等待重试的日期键: {(year, doy), ...}
        self.waiting_days = set()
        
        # 创建完成文件目录
        self.finish_dir = self.watch_dir.parent / FINISH_OFILE_DIR
        if not self.finish_dir.exists():
            self.finish_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.station_name}] 创建完成文件目录: {self.finish_dir}")

        # 异常O文件隔离目录：某个坏文件失败后移走，保证后续O文件继续解算
        self.failed_dir = self.watch_dir.parent / "FailedOfile"
        self.failed_dir.mkdir(parents=True, exist_ok=True)

        # 每个O文件的失败计数，超过阈值后移到 FailedOfile
        self.fail_counts = {}
        self.max_fail_count = int(os.environ.get("PPP_MAX_FAIL_COUNT", "2"))

        # 周期性补扫 rinex/119，避免队列通知丢失或手动放入历史O文件后无人处理
        self.rescan_interval_seconds = int(os.environ.get("RINEX_RESCAN_SECONDS", "60"))
    
    def _get_doy_from_filename(self, filename):
        """从RINEX文件名提取年积日，返回整数（如 92）"""
        try:
            match = re.search(r'[A-Z]{4}(\d{4})(\d{3})_', filename)
            if match:
                return int(match.group(2))
        except:
            pass
        return None
    
    def _get_year_from_filename(self, filename):
        """从RINEX文件名提取年份"""
        try:
            match = re.search(r'[A-Z]{4}(\d{4})(\d{3})_', filename)
            if match:
                return int(match.group(1))
        except:
            pass
        return None

    def _get_day_key_from_filename(self, filename):
        """从文件名返回日期键 (year, doy)。"""
        year = self._get_year_from_filename(filename)
        doy = self._get_doy_from_filename(filename)
        if year is None or doy is None:
            return None
        return (year, doy)

    def _date_from_year_doy(self, year, doy):
        """year + doy 转日期；允许 doy=0，用于自动处理跨年前一天。"""
        return datetime(year, 1, 1) + timedelta(days=doy - 1)

    def _day_key_text(self, key):
        year, doy = key
        return f"{year}年第{doy:03d}天"
    
    def _check_sp3_clk_exists(self, year, doy):
        """
        检查指定年积日的 SP3 和 CLK 文件是否都存在。
        允许 doy=0 或 doy>年天数，会自动换算到实际日期，解决跨年/历史文件问题。
        ✅ 模糊匹配：仅识别文件名中被下划线包围的 YYYYDOY0000 日期字段
        ✅ 忽略所有前缀、时长、采样间隔等可变部分
        ✅ 支持大小写不敏感扩展名（.SP3/.sp3/.CLK/.clk）
        ✅ 基于“同一天仅存在一个机构文件”的前提，无需优先级判断
        """
        product_date = self._date_from_year_doy(year, doy)
        product_year = product_date.year
        product_doy = product_date.timetuple().tm_yday
        
        # 唯一匹配条件：被下划线包围的标准日期字段
        date_key = f"{product_year}{product_doy:03d}0000"
        glob_date = f"*_{date_key}_*"
        
        # 匹配SP3文件（大小写不敏感）
        sp3_files = list(self.sp3_dir.glob(f"{glob_date}.SP3")) + list(self.sp3_dir.glob(f"{glob_date}.sp3"))
        # 过滤：仅保留存在且非空的文件
        sp3_valid = [f for f in sp3_files if f.is_file() and f.stat().st_size > 0]
        
        # 匹配CLK文件（大小写不敏感）
        clk_files = list(self.sp3_dir.glob(f"{glob_date}.CLK")) + list(self.sp3_dir.glob(f"{glob_date}.clk"))
        clk_valid = [f for f in clk_files if f.is_file() and f.stat().st_size > 0]
        
        # 生成返回值（完全兼容原格式）
        sp3_exists = bool(sp3_valid)
        clk_exists = bool(clk_valid)
        
        # 找到则返回实际文件名，未找到返回原GRG0标准格式（与原代码行为完全一致）
        sp3_pattern = sp3_valid[0].name if sp3_exists else f"GRG0OPSULT_{date_key}_02D_05M_ORB.SP3"
        clk_pattern = clk_valid[0].name if clk_exists else f"GRG0OPSULT_{date_key}_02D_05M_CLK.CLK"
        
        return sp3_exists, clk_exists, sp3_pattern, clk_pattern
    
    def _check_and_process_day(self, target_doy, year):
        """
        检查目标日期前一天的SP3/CLK是否存在；若存在，则处理该日期全部等待O文件。
        关键逻辑：失败文件只记录/隔离，不break，不影响后续O文件。
        返回: (是否处理了文件, 是否星历已就绪)
        """
        key = (year, target_doy)
        target_date = self._date_from_year_doy(year, target_doy)
        required_date = target_date - timedelta(days=1)
        required_year = required_date.year
        required_doy = required_date.timetuple().tm_yday
        
        sp3_exists, clk_exists, sp3_name, clk_name = self._check_sp3_clk_exists(required_year, required_doy)
        
        if sp3_exists and clk_exists:
            pending_files = self.pending_by_day.get(key, [])
            if pending_files:
                logger.info(f"[{self.station_name}] ========== 星历已就绪 ==========")
                logger.info(f"[{self.station_name}] O文件日期: {self._day_key_text(key)}")
                logger.info(f"[{self.station_name}] 使用前一天产品: {required_year}年第{required_doy:03d}天")
                logger.info(f"[{self.station_name}] SP3: {sp3_name}")
                logger.info(f"[{self.station_name}] CLK: {clk_name}")
                logger.info(f"[{self.station_name}] 开始批量解算 {len(pending_files)} 个O文件")
                
                # 按时间顺序处理（文件名已包含时间，排序即可）
                files_to_process = sorted(pending_files)

                # 失败文件暂存到 retry_later；超过阈值后移到 FailedOfile，不阻断后续文件
                retry_later = []

                for filename in files_to_process:
                    file_path = self.watch_dir / filename

                    if not file_path.exists():
                        # exe成功处理后会自动移走O文件；这里视为已处理
                        logger.info(f"[{self.station_name}] O文件已不存在，视为已处理: {filename}")
                        self.processed_files.add(filename)
                        self.fail_counts.pop(filename, None)
                        continue

                    if not self._is_file_stable(file_path):
                        logger.warning(f"[{self.station_name}] 文件仍在写入，本轮跳过，后续重试: {filename}")
                        retry_later.append(filename)
                        continue

                    logger.info(
                        f"[{self.station_name}] 解算: {filename} "
                        f"(O={self._day_key_text(key)}, 产品={required_year}年第{required_doy:03d}天)"
                    )
                    success = self._run_ppp(file_path, self.config_file)

                    if success:
                        self.processed_files.add(filename)
                        self.fail_counts.pop(filename, None)
                    else:
                        self.fail_counts[filename] = self.fail_counts.get(filename, 0) + 1
                        fail_count = self.fail_counts[filename]
                        logger.error(
                            f"[{self.station_name}] 解算未确认成功: {filename}, "
                            f"fail_count={fail_count}/{self.max_fail_count}"
                        )

                        if fail_count >= self.max_fail_count:
                            self._move_to_failed(file_path, reason=f"PPP failed {fail_count} times")
                            self.processed_files.add(filename)
                            self.fail_counts.pop(filename, None)
                        else:
                            retry_later.append(filename)

                # 更新该天队列；只保留需要后续重试的文件
                if retry_later:
                    self.pending_by_day[key] = retry_later
                    self.waiting_days.add(key)
                    logger.warning(
                        f"[{self.station_name}] {self._day_key_text(key)} 仍有 {len(retry_later)} 个O文件待重试；"
                        f"本轮已继续处理后续文件"
                    )
                else:
                    self.pending_by_day.pop(key, None)
                    self.waiting_days.discard(key)
                    logger.info(f"[{self.station_name}] {self._day_key_text(key)} 队列已处理完成")

                logger.info(f"[{self.station_name}] ========== {self._day_key_text(key)} 解算检查完成 ==========")
                return True, True
            else:
                self.pending_by_day.pop(key, None)
                self.waiting_days.discard(key)
                return False, True
        else:
            missing = []
            if not sp3_exists:
                missing.append(f"SP3 ({sp3_name})")
            if not clk_exists:
                missing.append(f"CLK ({clk_name})")
            self.waiting_days.add(key)
            logger.debug(
                f"[{self.station_name}] {self._day_key_text(key)} 等待前一天产品: "
                f"{required_year}年第{required_doy:03d}天 {', '.join(missing)}"
            )
            return False, False
    
    def _add_to_pending(self, filename, try_process=True):
        """
        将文件加入等待队列。
        try_process=True 时会立即检查产品并尝试解算，适合新文件通知；
        try_process=False 时只入队，适合启动/周期扫描后统一批量处理。
        """
        key = self._get_day_key_from_filename(filename)
        if key is None:
            logger.warning(f"[{self.station_name}] 无法解析O文件日期: {filename}")
            return False

        year, doy = key
        if key not in self.pending_by_day:
            self.pending_by_day[key] = []
        
        if filename not in self.pending_by_day[key]:
            self.pending_by_day[key].append(filename)
            self.waiting_days.add(key)
            logger.info(f"[{self.station_name}] 加入等待队列: {filename} ({self._day_key_text(key)})")
        else:
            return False

        if try_process:
            processed, ready = self._check_and_process_day(doy, year)
            if not ready:
                target_date = self._date_from_year_doy(year, doy)
                required_date = target_date - timedelta(days=1)
                logger.info(
                    f"[{self.station_name}] {self._day_key_text(key)} 进入等待队列，"
                    f"等待前一天产品: {required_date.year}年第{required_date.timetuple().tm_yday:03d}天"
                )
        return True
    
    def _scan_existing_files(self):
        """扫描 rinex/119 中现有的、未处理的O文件；用于启动处理历史堆积和运行中补扫。"""
        added_keys = set()
        scanned_count = 0

        for f in sorted(self.watch_dir.glob('*.o')):
            if not f.name.startswith(self.station_name):
                continue
            if FINISH_OFILE_DIR in str(f):
                continue
            if f.name in self.processed_files:
                continue
            
            key = self._get_day_key_from_filename(f.name)
            if key is None:
                logger.warning(f"[{self.station_name}] 文件名不符合O文件规则，跳过: {f.name}")
                continue

            if key in self.pending_by_day and f.name in self.pending_by_day[key]:
                continue
            
            if self._add_to_pending(f.name, try_process=False):
                added_keys.add(key)
                scanned_count += 1

        if scanned_count > 0:
            logger.info(f"[{self.station_name}] 扫描到 {scanned_count} 个待处理O文件，按日期批量检查星历")
            for year, doy in sorted(added_keys):
                self._check_and_process_day(doy, year)

        return scanned_count
    
    def _check_all_pending_days(self):
        """定期检查所有等待中的日期，看星历是否已出现。"""
        for year, doy in sorted(list(self.waiting_days)):
            self._check_and_process_day(doy, year)
    
    def _move_to_finish(self, file_path):
        """将解算成功的文件移动到完成目录。通常由RTKLIB_demo.exe自动移动，这里保留备用。"""
        try:
            rel_path = file_path.relative_to(self.watch_dir)
            finish_path = self.finish_dir / rel_path
            finish_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(finish_path))
            logger.debug(f"[{self.station_name}] 移动完成文件: {file_path.name}")
        except Exception as e:
            logger.warning(f"[{self.station_name}] 移动文件失败: {e}")
    
    def _move_to_failed(self, file_path, reason="unknown"):
        """将异常O文件移到 FailedOfile，避免阻塞后续解算。"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                return

            failed_path = self.failed_dir / file_path.name

            # 避免重名覆盖
            if failed_path.exists():
                stem = failed_path.stem
                suffix = failed_path.suffix
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                failed_path = self.failed_dir / f"{stem}_{timestamp}{suffix}"

            shutil.move(str(file_path), str(failed_path))
            logger.warning(
                f"[{self.station_name}] 异常O文件已移到FailedOfile: {file_path.name}, reason={reason}"
            )
        except Exception as e:
            logger.error(f"[{self.station_name}] 移动异常O文件失败: {file_path} - {e}")

    def _is_file_stable(self, file_path, check_interval=1, check_count=2):
        """检查文件是否稳定（写入完成）。"""
        try:
            if not file_path.exists():
                return False
            size1 = file_path.stat().st_size
            time.sleep(check_interval)
            if not file_path.exists():
                return False
            size2 = file_path.stat().st_size
            return size1 == size2 and size1 > 0
        except:
            return False
    
    def _safe_move_to_finish_after_ppp(self, file_path):
        """PPP 确认成功后，将原始 O 文件移入 FinishOfile，避免重复解算。"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                return True

            finish_path = self.finish_dir / file_path.name
            if finish_path.exists():
                stem = finish_path.stem
                suffix = finish_path.suffix
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                finish_path = self.finish_dir / f"{stem}_{timestamp}{suffix}"

            finish_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(finish_path))
            logger.info(f"[{self.station_name}] PPP确认成功，原始O文件已移入FinishOfile: {file_path.name}")
            return True
        except Exception as e:
            logger.error(f"[{self.station_name}] PPP成功后移动原始O文件失败: {file_path} - {e}")
            return False

    def _conf_dir_value(self, path_obj):
        """生成 RTKLIB 配置文件中使用的目录字符串，统一用 / 并保留末尾 /。"""
        s = str(Path(path_obj).resolve()).replace('\\', '/')
        if not s.endswith('/'):
            s += '/'
        return s

    def _write_single_file_ppp_config(self, base_config, temp_config, temp_input_dir, temp_finish_dir):
        """为单个 O 文件生成临时 PPP 配置，只改输入目录和完成目录。"""
        base_config = Path(base_config)
        temp_config = Path(temp_config)

        with open(base_config, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        replacements = {
            'file-indir': self._conf_dir_value(temp_input_dir),
            'file-FinishFile': self._conf_dir_value(temp_finish_dir),
        }

        seen = set()
        out_lines = []
        for line in lines:
            stripped = line.lstrip()
            replaced = False
            for key, value in replacements.items():
                if re.match(r'^' + re.escape(key) + r'\s*=', stripped, flags=re.IGNORECASE):
                    out_lines.append(f"{key:<20} ={value}\n")
                    seen.add(key)
                    replaced = True
                    break
            if not replaced:
                out_lines.append(line)

        for key, value in replacements.items():
            if key not in seen:
                out_lines.append(f"{key:<20} ={value}\n")

        temp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_config, 'w', encoding='utf-8') as f:
            f.writelines(out_lines)

    def _run_ppp(self, rinex_file, config_file):
        """执行 PPP 解算。

        原目录单文件隔离版本：
        RTKLIB_demo.exe 实际只认 chushihua1.conf 中固定的 file-indir，
        不可靠地接受临时配置中的 file-indir。

        因此这里不再生成临时配置，也不改 file-indir。
        做法是：在原始 rinex/119 目录内只保留当前要解算的一个 O 文件，
        其它 O 文件临时移到 hold 目录；运行原始 chushihua1.conf 后再恢复。
        这样仍然使用你手动验证能成功的命令：RTKLIB_demo.exe chushihua1.conf。
        """
        rinex_file = Path(rinex_file)
        if not rinex_file.exists():
            logger.info(f"[{self.station_name}] O文件已不存在，视为已处理: {rinex_file.name}")
            return True

        process_id = str(uuid.uuid4())[:8]
        temp_root = Path(PATHS["TEMP_DIR"]) / "ppp_original_indir_single" / f"{rinex_file.stem}_{process_id}"
        hold_dir = temp_root / "hold"
        hold_dir.mkdir(parents=True, exist_ok=True)

        moved_to_hold = []

        def _unique_path(dst_dir, name):
            dst = Path(dst_dir) / name
            if not dst.exists():
                return dst
            stem = Path(name).stem
            suffix = Path(name).suffix
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            return Path(dst_dir) / f"{stem}_{timestamp}{suffix}"

        try:
            # 关键：保持原始 file-indir 不变，只改变原目录里可见的 O 文件集合。
            # 在运行 exe 前，把 rinex/119 里除当前目标外的 O 文件都暂时移走。
            for other in sorted(self.watch_dir.glob('*.o')):
                if other.name == rinex_file.name:
                    continue
                if not other.name.startswith(self.station_name):
                    continue
                try:
                    dst = _unique_path(hold_dir, other.name)
                    shutil.move(str(other), str(dst))
                    moved_to_hold.append((dst, other.name))
                except Exception as e:
                    logger.warning(f"[{self.station_name}] 暂存其它O文件失败，跳过: {other.name} - {e}")

            logger.info(f"[{self.station_name}] 执行PPP解算: {rinex_file.name}")
            logger.info(f"[{self.station_name}] 原目录单文件隔离：rinex/119 当前仅保留目标O文件，暂存 {len(moved_to_hold)} 个其它O文件")

            cmd = [self.ppp_exe, config_file]
            work_dir = os.path.dirname(self.ppp_exe)

            with GlobalPPPSlot(self.station_name):
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=work_dir,
                    encoding='utf-8',
                    errors='ignore'
                )

            time.sleep(1)

            if result.stdout:
                logger.info(f"   stdout: {result.stdout[:2000]}")
            if result.stderr:
                logger.warning(f"   stderr: {result.stderr[:2000]}")

            if result.returncode != 0:
                logger.error(f"[{self.station_name}] PPP程序返回非0，解算失败: {rinex_file.name}, returncode={result.returncode}")
                return False

            # 成功标志：原始目标 O 文件被 exe 移走/消费，或进入 FinishOfile。
            finish_path = self.finish_dir / rinex_file.name
            consumed = (not rinex_file.exists()) or finish_path.exists()

            if consumed:
                logger.info(f"[{self.station_name}] PPP确认成功：目标O文件已被RTKLIB消费: {rinex_file.name}")
                return True

            logger.warning(
                f"[{self.station_name}] PPP程序返回0，但目标O文件仍存在，不能确认成功: {rinex_file.name}"
            )
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"[{self.station_name}] PPP解算超时: {rinex_file.name}")
            return False
        except Exception as e:
            logger.error(f"[{self.station_name}] PPP解算出错: {e}")
            return False
        finally:
            # 无论成功失败，都把暂存 O 文件移回 rinex/119。
            for held_path, original_name in moved_to_hold:
                try:
                    held_path = Path(held_path)
                    if not held_path.exists():
                        continue
                    dst = self.watch_dir / original_name
                    if dst.exists():
                        # 若运行期间又生成了同名文件，避免覆盖，追加时间戳。
                        stem = Path(original_name).stem
                        suffix = Path(original_name).suffix
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        dst = self.watch_dir / f"{stem}_{timestamp}{suffix}"
                    shutil.move(str(held_path), str(dst))
                except Exception as e:
                    logger.error(f"[{self.station_name}] 恢复暂存O文件失败: {original_name} - {e}")
            try:
                shutil.rmtree(temp_root, ignore_errors=True)
            except:
                pass

    def _check_queue(self):
        """从队列接收新文件通知。"""
        try:
            while True:
                msg = self.rinex_queue.get_nowait()
                if msg == "SCAN_NOW":
                    logger.info(f"[{self.station_name}] 收到扫描指令")
                    self._scan_existing_files()
                elif msg.endswith('.o'):
                    if msg.startswith(self.station_name):
                        logger.info(f"[{self.station_name}] 收到新文件通知: {msg}")
                        self._add_to_pending(msg, try_process=True)
        except queue.Empty:
            pass
    
    def run(self):
        logger.info(f"[{self.station_name}] 启动RINEX文件监控 - 累积O文件优先入队，新O文件继续入队")
        logger.info(f"[{self.station_name}]   监控目录: {self.watch_dir}")
        logger.info(f"[{self.station_name}]   完成目录: {self.finish_dir}")
        logger.info(f"[{self.station_name}]   异常目录: {self.failed_dir}")
        logger.info(f"[{self.station_name}]   星历目录: {self.sp3_dir}")
        logger.info(f"[{self.station_name}]   等待策略: 解算第N天需要第N-1天的SP3+CLK")
        logger.info(f"[{self.station_name}]   检查间隔: 5分钟；补扫间隔: {self.rescan_interval_seconds}秒")
        logger.info(f"[{self.station_name}]   PPP错峰延迟: {self.station_stagger}s，全局并发上限: {GLOBAL_PPP_MAX_CONCURRENT}")
        logger.info(f"[{self.station_name}]   单文件失败阈值: {self.max_fail_count} 次，超过后移入 FailedOfile")
        
        # 启动/星历就绪检查前按站点名错峰，避免所有站点同秒扫描并启动PPP。
        if self.station_stagger > 0:
            time.sleep(self.station_stagger)

        # 启动时立即扫描历史累积O文件；这是“先解算累积文件”的入口
        self._scan_existing_files()
        
        last_check_time = time.time()
        last_rescan_time = time.time()
        check_interval_seconds = 5 * 60  # 5分钟
        
        while self.running:
            try:
                # 1. 处理实时解码产生的新O文件通知；这是“继续新的解算”的入口
                self._check_queue()
                
                current_time = time.time()

                # 2. 周期性补扫 rinex/119，防止历史/新文件漏入队
                if current_time - last_rescan_time >= self.rescan_interval_seconds:
                    self._scan_existing_files()
                    last_rescan_time = current_time

                # 3. 定期检查等待中的日期：星历/钟差一出现就继续解算
                if current_time - last_check_time >= check_interval_seconds:
                    if self.waiting_days:
                        logger.info(f"[{self.station_name}] 定期检查等待队列（{len(self.waiting_days)} 个日期在等待）")
                        self._check_all_pending_days()
                    last_check_time = current_time
                
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"[{self.station_name}] RINEX监控错误: {e}")
                time.sleep(5)
    
    def stop(self):
        self.running = False

def signal_handler(signum, frame):
    logger.info("接收到退出信号，正在停止...")
    global running
    running = False


def main():
    global running
    
    parser = argparse.ArgumentParser(description='实时PPP处理控制器 - 站点版本')
    parser.add_argument('--station', type=str, default=PATHS["STATION_NAME"], help=f'站点名称')
    args = parser.parse_args()
    
    station_name = args.station
    running = True
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("="*60)
    logger.info(f"实时PPP处理系统启动 - 站点: {station_name}")
    logger.info(f"站点目录: {PATHS['STATION_ROOT']}")
    logger.info(f"公共数据目录: {PATHS['SOLU_DATA_DIR']} (BRDM/SP3从这里读取)")
    logger.info(f"RTCM切片目录: {PATHS['RTCM_WATCH_DIR']}")
    logger.info(f"RINEX目录: {PATHS['RINEX_WATCH_DIR']}")
    logger.info(f"输出目录: {PATHS['OUT_DIR']}")
    logger.info("="*60)
    logger.info("数据来源说明:")
    logger.info("  - BRDM文件: 从公共目录读取 (由brdm_downloader.py下载)")
    logger.info("  - SP3/CLK文件: 从公共目录读取 (由sp3_downloader.py下载)")
    logger.info("  - RTCM源文件: 从本站点rtknavi/读取")
    logger.info("  - PPP配置文件: chushihua1.conf (单个配置文件)")
    logger.info("  - 本程序不启动任何下载线程")
    logger.info("="*60)
    logger.info("文件处理策略:")
    logger.info(f"  - 解码成功后: RTCM文件移动到 {FINISH_OFILE_DIR}/")
    logger.info(f"  - PPP解算成功后: RINEX文件移动到 {FINISH_OFILE_DIR}/")
    logger.info(f"  - 每日清理: 每天0点清空 {FINISH_OFILE_DIR}/ 目录")
    logger.info("="*60)
    
    # 检查必要的文件
    required_files = {
        "RTCM切片脚本": os.path.join(PATHS["STATION_ROOT"], "rtcm_slice.py"),
        "RTCM解码程序": PATHS["RTCM_DECODE_EXE"],
        "解码配置文件": PATHS["RTCM_CONFIG"],
        "PPP解算程序": PATHS["PPP_EXE"],
        "PPP配置文件": PATHS["PPP_CONFIG1"],
    }
    
    for name, path in required_files.items():
        if os.path.exists(path):
            logger.info(f"  {name}: {path}")
        else:
            logger.warning(f"  {name} 不存在: {path}")
    
    # 创建线程列表
    threads = []
    
    # 1. BRDM监控线程
    brdm_monitor = BRDMMonitor(
        brdm_dir=PATHS["SOLU_DATA_DIR"],
        slice_script=os.path.join(PATHS["STATION_ROOT"], "rtcm_slice.py"),
        station_name=station_name,
        check_interval=60
    )
    threads.append(brdm_monitor)
    
    # 2. RTCM解码监控线程
    rtcm_monitor = RTCMFileMonitor(
        station_name=station_name,
        watch_dir=PATHS["RTCM_WATCH_DIR"],
        decode_exe=PATHS["RTCM_DECODE_EXE"],
        config_file=PATHS["RTCM_CONFIG"],
        rinex_queue=rinex_queue,
        interval=1
    )
    threads.append(rtcm_monitor)
    
    # 3. RINEX监控线程
    rinex_monitor = RINEXFileMonitor(
        station_name=station_name,
        watch_dir=PATHS["RINEX_WATCH_DIR"],
        ppp_exe=PATHS["PPP_EXE"],
        config_file=PATHS["PPP_CONFIG1"],
        rinex_queue=rinex_queue,
        sp3_dir=PATHS["SOLU_DATA_DIR"],
        interval=1
    )
    threads.append(rinex_monitor)
    
    # 4. 每日清理线程
    daily_cleaner = DailyCleaner(
        station_name=station_name,
        station_root=PATHS["STATION_ROOT"]  # 传入站点根目录
    )
    threads.append(daily_cleaner)
    
    # 启动所有线程
    logger.info(f"\n启动 {len(threads)} 个线程...")
    for thread in threads:
        thread.start()
        logger.info(f"启动线程: {thread.__class__.__name__}")
    
    logger.info("\n" + "="*60)
    logger.info(f"站点 {station_name} 监控服务已启动！")
    logger.info("保持此窗口打开")
    logger.info("按 Ctrl+C 停止")
    logger.info("="*60)
    
    try:
        while running:
            time.sleep(1)
            
            # 检查线程状态，自动重启
            for i, thread in enumerate(threads):
                if not thread.is_alive():
                    logger.warning(f"线程 {thread.__class__.__name__} 已停止，尝试重启...")
                    
                    if isinstance(thread, BRDMMonitor):
                        new_thread = BRDMMonitor(
                            brdm_dir=PATHS["SOLU_DATA_DIR"],
                            slice_script=os.path.join(PATHS["STATION_ROOT"], "rtcm_slice.py"),
                            station_name=station_name,
                            check_interval=60
                        )
                    elif isinstance(thread, RTCMFileMonitor):
                        new_thread = RTCMFileMonitor(
                            station_name=station_name,
                            watch_dir=PATHS["RTCM_WATCH_DIR"],
                            decode_exe=PATHS["RTCM_DECODE_EXE"],
                            config_file=PATHS["RTCM_CONFIG"],
                            rinex_queue=rinex_queue,
                            interval=1
                        )
                    elif isinstance(thread, RINEXFileMonitor):
                        new_thread = RINEXFileMonitor(
                            station_name=station_name,
                            watch_dir=PATHS["RINEX_WATCH_DIR"],
                            ppp_exe=PATHS["PPP_EXE"],
                            config_file=PATHS["PPP_CONFIG1"],
                            rinex_queue=rinex_queue,
                            sp3_dir=PATHS["SOLU_DATA_DIR"],
                            interval=1
                        )
                    else:  # DailyCleaner
                        new_thread = DailyCleaner(
                            station_name=station_name,
                            station_root=PATHS["STATION_ROOT"]
                        )
                    
                    new_thread.start()
                    threads[i] = new_thread
                    logger.info(f"线程已重启")
    
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        logger.info("正在停止所有线程...")
        for thread in threads:
            thread.stop()
        for thread in threads:
            thread.join(timeout=5)
        logger.info("系统已停止")


if __name__ == "__main__":
    main()