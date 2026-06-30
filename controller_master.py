#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
总开关 - 一键启动所有服务

功能：
- 启动 BRDM 下载器（1个进程）
- 启动 SP3 下载器（1个进程）
- 自动扫描所有站点目录，为每个站点启动 PPP 控制器
- 监控所有进程，自动重启崩溃的进程
- 支持动态添加新站点

运行方式：
    cd WUHAN
    python controller_master.py
"""

import os
import sys
import time
import signal
import subprocess
import threading
from pathlib import Path
from datetime import datetime

# ==================== 配置 ====================
# 要排除的目录（不当作站点处理）
EXCLUDE_DIRS = {"SoluData", "__pycache__", ".git"}

# 进程状态检查间隔（秒）
CHECK_INTERVAL = 30

# 站点目录扫描间隔（秒）
SCAN_INTERVAL = 60

# BRDM和SP3下载器配置
BRDM_SCRIPT = "brdm_downloader.py"
SP3_SCRIPT = "sp3_downloader.py"

# ==================== 窗口/日志配置 ====================
# 是否显示子进程输出到控制台（调试用）
SHOW_SUBPROCESS_OUTPUT = True

# 是否弹出独立窗口
# - None: 使用系统默认（通常不弹窗，输出合并到主控台）
# - "new_console": 弹出新控制台窗口
# - "hide": 完全隐藏窗口（Windows后台运行）
POPUP_WINDOW = {
    "BRDM": "new_console",      # BRDM下载器：弹窗
    "SP3": "new_console",       # SP3下载器：弹窗
    "PPP": None,                # PPP控制器：不弹窗（输出到日志）
}
# ==================================================

# 日志目录（存放站点子进程的日志文件）
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


class ServiceManager:
    """服务管理器 - 管理所有进程（BRDM、SP3、各站点PPP控制器）"""
    
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir)
        
        # 进程字典：{服务名: subprocess.Popen}
        self.processes = {}
        
        # 服务类型标记
        self.SERVICE_BRDM = "BRDM下载器"
        self.SERVICE_SP3 = "SP3下载器"
        self.SERVICE_PPP = "PPP控制器"
        
        self.running = True
        self.lock = threading.Lock()
        
        # 创建日志目录
        os.makedirs(LOG_DIR, exist_ok=True)
        
    def get_stations(self):
        """扫描所有站点目录"""
        stations = []
        for item in self.root_dir.iterdir():
            if not item.is_dir():
                continue
            if item.name in EXCLUDE_DIRS:
                continue
            controller_script = item / "realtime_ppp_controller.py"
            if controller_script.exists():
                stations.append(item.name)
        return stations
    
    def _get_process_creation_flags(self, service_type, log_file=None):
        """
        获取进程创建标志
        service_type: "BRDM", "SP3", "PPP"
        返回: (creationflags, stdout/stderr设置)
        """
        if sys.platform != 'win32':
            # Linux/Mac 不需要特殊处理
            return 0, None, None
        
        popup_setting = POPUP_WINDOW.get(service_type, None)
        
        if popup_setting == "new_console":
            # 弹出新控制台窗口
            return subprocess.CREATE_NEW_CONSOLE, None, None
        elif popup_setting == "hide":
            # 完全隐藏窗口（后台运行）
            return subprocess.CREATE_NO_WINDOW, subprocess.DEVNULL, subprocess.DEVNULL
        else:
            # 不弹窗，输出重定向到文件或主控台
            if SHOW_SUBPROCESS_OUTPUT and log_file is None:
                # 输出到主控台（不弹窗）
                return 0, None, None
            else:
                # 输出重定向到日志文件
                if log_file:
                    log_f = open(log_file, 'a', encoding='utf-8')
                    return 0, log_f, log_f
                else:
                    return 0, subprocess.DEVNULL, subprocess.DEVNULL
    
    def _get_log_file(self, service_name):
        """获取服务的日志文件路径"""
        # 清理服务名中的特殊字符
        safe_name = service_name.replace(" ", "_").replace("-", "_")
        timestamp = datetime.now().strftime("%Y%m%d")
        log_path = os.path.join(LOG_DIR, f"{safe_name}_{timestamp}.log")
        return log_path
    
    def start_brdm_downloader(self):
        """启动BRDM下载器"""
        script_path = self.root_dir / BRDM_SCRIPT
        if not script_path.exists():
            print(f"[错误] BRDM下载脚本不存在: {script_path}")
            return None
        
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [启动] {self.SERVICE_BRDM}")
            
            creationflags, stdout, stderr = self._get_process_creation_flags("BRDM")
            
            process = subprocess.Popen(
                [sys.executable, str(script_path)],
                cwd=str(self.root_dir),
                creationflags=creationflags,
                stdout=stdout,
                stderr=stderr
            )
            return process
        except Exception as e:
            print(f"[错误] 启动BRDM下载器失败: {e}")
            return None
    
    def start_sp3_downloader(self):
        """启动SP3下载器"""
        script_path = self.root_dir / SP3_SCRIPT
        if not script_path.exists():
            print(f"[错误] SP3下载脚本不存在: {script_path}")
            return None
        
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [启动] {self.SERVICE_SP3}")
            
            creationflags, stdout, stderr = self._get_process_creation_flags("SP3")
            
            process = subprocess.Popen(
                [sys.executable, str(script_path)],
                cwd=str(self.root_dir),
                creationflags=creationflags,
                stdout=stdout,
                stderr=stderr
            )
            return process
        except Exception as e:
            print(f"[错误] 启动SP3下载器失败: {e}")
            return None
    
    def start_ppp_controller(self, station_name):
        """启动一个站点的PPP控制器"""
        station_dir = self.root_dir / station_name
        controller_script = station_dir / "realtime_ppp_controller.py"
        
        if not controller_script.exists():
            print(f"[错误] 站点 {station_name} 缺少 realtime_ppp_controller.py")
            return None
        
        try:
            service_name = f"{self.SERVICE_PPP}-{station_name}"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [启动] {service_name}")
            
            # 获取日志文件
            log_file = self._get_log_file(service_name)
            
            creationflags, stdout, stderr = self._get_process_creation_flags("PPP", log_file)
            
            # 如果stdout是文件句柄，写入日志头
            if stdout is not None and hasattr(stdout, 'write'):
                header = f"\n{'='*60}\n"
                header += f"站点 {station_name} PPP控制器启动\n"
                header += f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                header += f"{'='*60}\n\n"
                stdout.write(header)
                stdout.flush()
            
            process = subprocess.Popen(
                [sys.executable, str(controller_script), "--station", station_name],
                cwd=str(station_dir),
                creationflags=creationflags,
                stdout=stdout,
                stderr=stderr
            )
            
            # 记录进程和对应的日志文件（用于后续清理）
            if hasattr(process, 'log_file'):
                process.log_file = log_file
            
            return process
        except Exception as e:
            print(f"[错误] 启动站点 {station_name} 失败: {e}")
            return None
    
    def stop_service(self, service_name):
        """停止一个服务"""
        with self.lock:
            if service_name in self.processes:
                process = self.processes[service_name]
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [停止] {service_name}")
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                except Exception as e:
                    print(f"[错误] 停止 {service_name} 时出错: {e}")
                finally:
                    # 如果有日志文件句柄，关闭它
                    if hasattr(process, 'stdout') and process.stdout and hasattr(process.stdout, 'close'):
                        try:
                            process.stdout.close()
                        except:
                            pass
                    del self.processes[service_name]
    
    def check_processes(self):
        """检查所有进程状态，重启崩溃的"""
        with self.lock:
            for service_name, process in list(self.processes.items()):
                if process.poll() is not None:
                    exit_code = process.returncode
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [崩溃] {service_name} 已退出 (退出码: {exit_code})，正在重启...")
                    
                    # 根据服务类型重启
                    if service_name == self.SERVICE_BRDM:
                        new_process = self.start_brdm_downloader()
                    elif service_name == self.SERVICE_SP3:
                        new_process = self.start_sp3_downloader()
                    elif service_name.startswith(self.SERVICE_PPP):
                        station_name = service_name.replace(f"{self.SERVICE_PPP}-", "")
                        new_process = self.start_ppp_controller(station_name)
                    else:
                        new_process = None
                    
                    if new_process:
                        self.processes[service_name] = new_process
                    else:
                        del self.processes[service_name]
    
    def sync_stations(self):
        """同步站点列表：启动新站点，停止已删除的站点"""
        current_stations = set(self.get_stations())
        
        # 找出所有PPP控制器服务名
        running_ppp_services = set()
        for service_name in self.processes.keys():
            if service_name.startswith(self.SERVICE_PPP):
                station = service_name.replace(f"{self.SERVICE_PPP}-", "")
                running_ppp_services.add(station)
        
        # 停止已删除的站点
        for station in running_ppp_services - current_stations:
            service_name = f"{self.SERVICE_PPP}-{station}"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [移除] 站点目录已删除: {station}")
            self.stop_service(service_name)
        
        # 启动新站点
        for station in current_stations - running_ppp_services:
            service_name = f"{self.SERVICE_PPP}-{station}"
            process = self.start_ppp_controller(station)
            if process:
                with self.lock:
                    self.processes[service_name] = process
    
    def start_all_services(self):
        """启动所有服务（BRDM、SP3、所有站点PPP控制器）"""
        print("\n" + "="*60)
        print("正在启动所有服务...")
        print("="*60)
        
        # 1. 启动BRDM下载器
        brdm_process = self.start_brdm_downloader()
        if brdm_process:
            self.processes[self.SERVICE_BRDM] = brdm_process
        
        time.sleep(2)
        
        # 2. 启动SP3下载器
        sp3_process = self.start_sp3_downloader()
        if sp3_process:
            self.processes[self.SERVICE_SP3] = sp3_process
        
        time.sleep(2)
        
        # 3. 启动所有站点的PPP控制器
        stations = self.get_stations()
        if stations:
            print(f"\n发现 {len(stations)} 个站点: {stations}")
            for station in stations:
                process = self.start_ppp_controller(station)
                if process:
                    self.processes[f"{self.SERVICE_PPP}-{station}"] = process
                time.sleep(1)
        else:
            print("\n[警告] 未发现任何站点目录")
            print("请确保每个站点目录下有 realtime_ppp_controller.py")
        
        print("\n" + "="*60)
        print(f"所有服务已启动，共 {len(self.processes)} 个进程")
        print("\n窗口模式配置:")
        print(f"  - BRDM下载器: {POPUP_WINDOW.get('BRDM', '默认')}")
        print(f"  - SP3下载器: {POPUP_WINDOW.get('SP3', '默认')}")
        print(f"  - PPP控制器: {POPUP_WINDOW.get('PPP', '默认')}")
        if POPUP_WINDOW.get('PPP') is None:
            print(f"  - PPP日志目录: {LOG_DIR}")
        print("="*60)
    
    def stop_all(self):
        """停止所有服务"""
        print("\n[停止] 正在停止所有服务...")
        for service_name in list(self.processes.keys()):
            self.stop_service(service_name)
    
    def print_status(self):
        """打印当前状态"""
        with self.lock:
            running = list(self.processes.keys())
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [状态] 当前运行 {len(running)} 个服务")
    
    def run(self):
        """主运行循环"""
        print("="*60)
        print("总开关 - 一键启动所有服务")
        print(f"根目录: {self.root_dir}")
        print("="*60)
        print("将启动以下服务:")
        print("  1. BRDM下载器 (brdm_downloader.py)")
        print("  2. SP3下载器 (sp3_downloader.py)")
        print("  3. 所有站点的PPP控制器 (自动扫描)")
        print("="*60)
        print(f"进程检查间隔: {CHECK_INTERVAL}秒")
        print(f"站点扫描间隔: {SCAN_INTERVAL}秒")
        print("="*60)
        
        # 启动所有服务
        self.start_all_services()
        
        last_scan_time = time.time()
        
        try:
            while self.running:
                self.check_processes()
                
                current_time = time.time()
                if current_time - last_scan_time >= SCAN_INTERVAL:
                    self.sync_stations()
                    last_scan_time = current_time
                    #self.print_status()
                
                time.sleep(CHECK_INTERVAL)
                
        except KeyboardInterrupt:
            print("\n[中断] 收到退出信号")
        finally:
            self.stop_all()
            print("[完成] 所有服务已停止")


def signal_handler(signum, frame):
    """信号处理"""
    print("\n[信号] 收到退出信号")
    global manager
    if manager:
        manager.running = False


def main():
    global manager
    
    root_dir = os.path.dirname(os.path.abspath(__file__))
    
    brdm_script = os.path.join(root_dir, "brdm_downloader.py")
    sp3_script = os.path.join(root_dir, "sp3_downloader.py")
    
    if not os.path.exists(brdm_script):
        print(f"[警告] BRDM下载脚本不存在: {brdm_script}")
    
    if not os.path.exists(sp3_script):
        print(f"[警告] SP3下载脚本不存在: {sp3_script}")
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    manager = ServiceManager(root_dir)
    manager.run()


if __name__ == "__main__":
    main()