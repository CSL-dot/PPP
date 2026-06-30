#!/usr/bin/env python3
"""
GNSS数据智能下载系统 - 下载WUM精密星历(SP3)和钟差文件(CLK)
基于UTC时间运行，每天9:40唤醒，下载UTC前一天的完整数据。当WUM不可用时自动切换为GRG数据。
"""

import os
import gzip
import shutil
from datetime import datetime, timedelta, timezone
from ftplib import FTP, error_temp, error_perm
import time
import json
import re
from pathlib import Path

# ==================== 配置 ====================
WUM_HOST = "igs.gnsswhu.cn"
GRG_HOST = "gdc.cddis.eosdis.nasa.gov"

BASE_PATH = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(BASE_PATH, "SoluData")
EXCLUDE_DIRS = {"SoluData", "__pycache__", ".git", "logs"}

# 唤醒时间配置（北京时间）
WAKEUP_HOUR = 9
WAKEUP_MINUTE = 40

# WUM文件模板
WUM_FILES = [
    {
        "type": "SP3",
        "pattern": "WUM0MGXNRT_{year}{doy}0000_02D_05M_ORB.SP3.gz",
        "description": "精密星历",
        "source": "WUM"
    },
    {
        "type": "CLK",
        "pattern": "WUM0MGXNRT_{year}{doy}0000_02D_30S_CLK.CLK.gz",
        "description": "钟差文件",
        "source": "WUM"
    }
]

# GRG文件模板
GRG_FILES = [
    {
        "type": "SP3",
        "pattern": "GRG0OPSULT_{year}{doy}0000_02D_05M_ORB.SP3.gz",
        "description": "精密星历",
        "source": "GRG"
    },
    {
        "type": "CLK",
        "pattern": "GRG0OPSULT_{year}{doy}0000_02D_05M_CLK.CLK.gz",
        "description": "钟差文件",
        "source": "GRG"
    }
]

# 唤醒后检查间隔（秒）- 3分钟
CHECK_INTERVAL = 180

# 最大重试次数
MAX_RETRY_COUNT = 3
RETRY_DELAY = 30

# 状态文件
STATUS_FILE = os.path.join(LOCAL_DIR, "sp3_status.json")
LOG_FILE = os.path.join(LOCAL_DIR, "sp3_log.txt")

# 确保目录存在
os.makedirs(LOCAL_DIR, exist_ok=True)


class WUMDownloader:
    def __init__(self):
        self.ftp = None
        self.current_host = None
        self.status = {}
        self.load_status()
        
    def load_status(self):
        """加载下载状态"""
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, 'r') as f:
                    self.status = json.load(f)
            except Exception:
                self.status = {}
        else:
            self.status = {}
    
    def save_status(self):
        """保存下载状态"""
        try:
            with open(STATUS_FILE, 'w') as f:
                json.dump(self.status, f, indent=2)
        except Exception as e:
            self.log(f"保存状态文件失败: {e}")
    
    def log(self, message):
        """记录日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {message}")
        
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass
    
    def get_utc_time(self):
        """获取当前UTC时间（兼容Python 3.12+弃用utcnow的情况）"""
        return datetime.now(timezone.utc).replace(tzinfo=None)
    
    def get_target_utc_date(self):
        """
        获取需要下载的目标UTC日期
        策略：下载UTC昨天的数据
        """
        utc_now = self.get_utc_time()
        target_date = utc_now - timedelta(days=1)
        return target_date
    
    def connect(self, host):
        """连接指定的FTP服务器 (WUM使用标准FTP，CDDIS使用FTPS)"""
        try:
            self.log(f"连接 {host}...")
            if host == GRG_HOST:
                # CDDIS 必须使用 FTPS (FTP_TLS) 并提供 Email 作为匿名登录密码
                from ftplib import FTP_TLS
                self.ftp = FTP_TLS(host)
                self.ftp.login(user='anonymous', passwd='anonymous@example.com')  
                self.ftp.prot_p()  # 保护数据连接，FTPS必需
            else:
                # WUM 使用普通 FTP
                self.ftp = FTP(host)
                self.ftp.login()  # 匿名登录
                
            self.ftp.set_pasv(True)
            self.current_host = host
            self.log(f"✓ 连接 {host} 成功")
            return True
        except Exception as e:
            self.log(f"✗ 连接 {host} 失败: {e}")
            self.ftp = None
            self.current_host = None
            return False
    
    def disconnect(self):
        """断开连接"""
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
            self.ftp = None
            self.current_host = None
    
    def ensure_connection(self, host):
        """确保到指定主机的连接有效"""
        # 如果要连接的主机与当前连接的主机不同，先断开，避免遗留悬挂Socket
        if self.ftp and getattr(self, 'current_host', None) != host:
            self.log(f"🔄 检测到主机切换：从 {self.current_host} 切换至 {host}，先断开当前连接...")
            self.disconnect()

        try:
            if self.ftp and getattr(self, 'current_host', None) == host:
                self.ftp.voidcmd("NOOP")
                return True
        except Exception:
            self.log(f"⚠ {host} 连接已断开，重新连接...")
            self.disconnect()
        
        return self.connect(host)
    
    def get_gps_week(self, date):
        """获取GPS周"""
        gps_epoch = datetime(1980, 1, 6)
        delta = date - gps_epoch
        gps_week = delta.days // 7
        return gps_week
    
    def get_remote_path(self, target_date, file_info):
        """获取远程文件路径"""
        year = target_date.strftime("%Y")
        doy = target_date.strftime("%j")
        gps_week = self.get_gps_week(target_date)
        
        filename = file_info["pattern"].format(year=year, doy=doy)
        source = file_info.get("source", "WUM")
        
        if source == "GRG":
            remote_path = f"/pub/gps/products/{gps_week:04d}/{filename}"
        else:
            remote_path = f"/pub/gnss/products/mgex/{gps_week:04d}/{filename}"
        
        return remote_path, filename
    
    def check_remote_file_exists(self, remote_path, host):
        """检查远程文件是否存在"""
        try:
            if not self.ensure_connection(host):
                return None
            
            self.ftp.size(remote_path)
            return True
        except error_perm:
            return False
        except Exception as e:
            self.log(f"  检查文件出错: {e}")
            return None
    
    def download_file(self, remote_path, local_path, host, remote_size=None):
        """下载文件"""
        try:
            os.makedirs(LOCAL_DIR, exist_ok=True)
            
            if not self.ensure_connection(host):
                return False
            
            # 获取远程文件大小
            if remote_size is None:
                remote_size = self.ftp.size(remote_path)
            
            self.log(f"  ↓ 开始下载 ({remote_size:,} 字节)")
            
            with open(local_path, 'wb') as f:
                self.ftp.retrbinary(f'RETR {remote_path}', f.write, blocksize=262144)
            
            # 验证下载
            if os.path.exists(local_path):
                local_size = os.path.getsize(local_path)
                if local_size == remote_size:
                    self.log(f"  ✓ 下载完成 ({local_size:,} 字节)")
                    return True
                else:
                    self.log(f"  ✗ 下载不完整 ({local_size}/{remote_size})")
                    try:
                        os.remove(local_path)
                    except Exception:
                        pass
            
            return False
            
        except Exception as e:
            self.log(f"  ✗ 下载出错: {e}")
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            return False
    
    def extract_gz(self, gz_path):
        """解压.gz文件"""
        if not gz_path.endswith('.gz'):
            return None
        
        extract_path = gz_path[:-3]
        
        if os.path.exists(extract_path) and os.path.getsize(extract_path) > 0:
            return extract_path
        
        try:
            gz_size = os.path.getsize(gz_path)
            self.log(f"  📦 解压: {os.path.basename(gz_path)} ({gz_size:,} 字节)")
            
            with gzip.open(gz_path, 'rb') as f_in:
                with open(extract_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1024*1024)
            
            if os.path.exists(extract_path):
                extract_size = os.path.getsize(extract_path)
                self.log(f"  ✓ 解压完成: {os.path.basename(extract_path)} ({extract_size:,} 字节)")
                
                # 删除压缩包
                os.remove(gz_path)
                self.log(f"  🗑️ 已删除压缩包")
                
                return extract_path
            
        except Exception as e:
            self.log(f"  ✗ 解压出错: {e}")
        
        return None
    
    def check_local_completed(self, target_date, source="any"):
        """检查本地是否已有完整的文件（SP3和CLK都存在且有效）
        source: "WUM", "GRG", 或 "any"
        """
        year = target_date.strftime("%Y")
        doy = target_date.strftime("%j")
        
        wum_sp3 = os.path.join(LOCAL_DIR, f"WUM0MGXNRT_{year}{doy}0000_02D_05M_ORB.SP3")
        wum_clk = os.path.join(LOCAL_DIR, f"WUM0MGXNRT_{year}{doy}0000_02D_30S_CLK.CLK")
        wum_ok = (os.path.exists(wum_sp3) and os.path.getsize(wum_sp3) > 0 and 
                  os.path.exists(wum_clk) and os.path.getsize(wum_clk) > 0)
        
        grg_sp3 = os.path.join(LOCAL_DIR, f"GRG0OPSULT_{year}{doy}0000_02D_05M_ORB.SP3")
        grg_clk = os.path.join(LOCAL_DIR, f"GRG0OPSULT_{year}{doy}0000_02D_05M_CLK.CLK")
        grg_ok = (os.path.exists(grg_sp3) and os.path.getsize(grg_sp3) > 0 and 
                  os.path.exists(grg_clk) and os.path.getsize(grg_clk) > 0)
        
        if source == "WUM":
            return wum_ok
        elif source == "GRG":
            return grg_ok
        else:
            return wum_ok or grg_ok

    def download_day(self, target_date):
        """下载指定日期的数据"""
        self.log(f"--- 开始处理日期: {target_date.strftime('%Y-%m-%d')} ---")
        
        # 1. 优先尝试从 WUM 下载
        if self.check_local_completed(target_date, source="WUM"):
            self.log("本地已有完整的 WUM 数据，无需重复下载。")
            return True
            
        self.log("尝试从武汉大学 (WUM) 下载星历和钟差...")
        wum_success = True
        for file_info in WUM_FILES:
            remote_path, filename = self.get_remote_path(target_date, file_info)
            local_gz_path = os.path.join(LOCAL_DIR, filename)
            local_extracted_path = local_gz_path[:-3]
            
            if os.path.exists(local_extracted_path) and os.path.getsize(local_extracted_path) > 0:
                continue
                
            exists = self.check_remote_file_exists(remote_path, WUM_HOST)
            if exists is False:
                self.log(f"  ✗ WUM 远程文件不存在: {remote_path}")
                wum_success = False
                break
            elif exists is None:
                self.log(f"  ✗ 连接服务器失败或检查出错，暂时无法确认文件状态")
                wum_success = False
                break
                
            downloaded = False
            for retry in range(MAX_RETRY_COUNT):
                if self.download_file(remote_path, local_gz_path, WUM_HOST):
                    self.extract_gz(local_gz_path)
                    downloaded = True
                    break
                else:
                    self.log(f"  重试下载 ({retry+1}/{MAX_RETRY_COUNT})...")
                    time.sleep(RETRY_DELAY)
            
            if not downloaded:
                self.log(f"  ✗ 达到最大重试次数，WUM 下载失败")
                wum_success = False
                break
                
        if wum_success and self.check_local_completed(target_date, source="WUM"):
            self.log("✓ WUM 数据下载并解压成功！")
            return True
            
        # 2. 如果 WUM 失败，尝试 GRG (CDDIS)
        self.log("WUM 不可用或不完整，切换至 GRG (CDDIS) 备用源...")
        if self.check_local_completed(target_date, source="GRG"):
            self.log("本地已有完整的 GRG 数据，无需重复下载。")
            return True
            
        grg_success = True
        for file_info in GRG_FILES:
            remote_path, filename = self.get_remote_path(target_date, file_info)
            local_gz_path = os.path.join(LOCAL_DIR, filename)
            local_extracted_path = local_gz_path[:-3]
            
            if os.path.exists(local_extracted_path) and os.path.getsize(local_extracted_path) > 0:
                continue

               
            exists = self.check_remote_file_exists(remote_path, GRG_HOST)
            if exists is False:
                self.log(f"  ✗ GRG 远程文件不存在: {remote_path}")
                grg_success = False
                break
            elif exists is None:
                self.log(f"  ✗ 连接服务器失败或检查出错，暂时无法确认文件状态")
                grg_success = False
                break
                
            downloaded = False
            for retry in range(MAX_RETRY_COUNT):
                if self.download_file(remote_path, local_gz_path, GRG_HOST):
                    self.extract_gz(local_gz_path)
                    downloaded = True
                    break
                else:
                    self.log(f"  重试下载 ({retry+1}/{MAX_RETRY_COUNT})...")
                    time.sleep(RETRY_DELAY)
            
            if not downloaded:
                self.log(f"  ✗ 达到最大重试次数，GRG 下载失败")
                grg_success = False
                break
                
        if grg_success and self.check_local_completed(target_date, source="GRG"):
            self.log("✓ GRG 数据下载并解压成功！")
            return True
            
        self.log("✗ 两个源均无法获取完整的数据。")
        return False

    def sleep_split(self, seconds):
        """分片休眠，便于随时响应系统终止信号"""
        if seconds <= 0:
            return
        chunk = 10
        elapsed = 0
        while elapsed < seconds:
            time.sleep(min(chunk, seconds - elapsed))
            elapsed += chunk

    def run(self):
        """主循环：保持常驻进程运行"""
        self.log("==========================================")
        self.log("WUM/GRG SP3 下载后台服务已启动 (守护进程)")
        self.log(f"计划唤醒时间: 每天北京时间 {WAKEUP_HOUR:02d}:{WAKEUP_MINUTE:02d}")
        self.log("==========================================")
        
        while True:
            try:
                now_cst = datetime.now()
                target_date = self.get_target_utc_date()
                target_date_str = target_date.strftime("%Y-%m-%d")
                
                # 检查本地是否已经拥有这一天完整的文件
                completed = self.check_local_completed(target_date)
                
                # 判断当前北京时间是否已经到了 9:40 之后
                is_wakeup_time = (now_cst.hour > WAKEUP_HOUR) or (now_cst.hour == WAKEUP_HOUR and now_cst.minute >= WAKEUP_MINUTE)
                
                if completed:
                    self.log(f"📅 目标日期 {target_date_str} 的数据在本地已存在，跳过本次下载。")
                    # 计算下一次 9:40 的时间并休眠
                    next_wakeup = datetime(now_cst.year, now_cst.month, now_cst.day, WAKEUP_HOUR, WAKEUP_MINUTE)
                    if next_wakeup <= now_cst:
                        next_wakeup += timedelta(days=1)
                    
                    sleep_seconds = int((next_wakeup - now_cst).total_seconds())
                    self.log(f"💤 任务已完成。程序将挂起，于 {next_wakeup.strftime('%Y-%m-%d %H:%M:%S')} 重新唤醒。")
                    self.sleep_split(sleep_seconds)
                else:
                    if is_wakeup_time:
                        self.log(f"⏰ 触发定时下载任务 (当前时间: {now_cst.strftime('%H:%M:%S')})...")
                        success = self.download_day(target_date)
                        
                        self.status[target_date_str] = {
                            "status": "success" if success else "failed",
                            "last_attempt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        self.save_status()
                        
                        if not success:
                            self.log(f"⚠ 下载目标日期 {target_date_str} 失败，将在 {CHECK_INTERVAL} 秒后尝试重新下载...")
                            time.sleep(CHECK_INTERVAL)
                    else:
                        # 还没到 9:40，等待到 9:40 
                        next_wakeup = datetime(now_cst.year, now_cst.month, now_cst.day, WAKEUP_HOUR, WAKEUP_MINUTE)
                        sleep_seconds = int((next_wakeup - now_cst).total_seconds())
                        self.log(f"💤 未到触发时刻 (当前时间: {now_cst.strftime('%H:%M:%S')})，预计休眠 {sleep_seconds:,} 秒...")
                        self.sleep_split(sleep_seconds)
                        
            except Exception as e:
                self.log(f"💥 主循环遇到异常: {e}")
                time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        downloader = WUMDownloader()
        downloader.run()
    except KeyboardInterrupt:
        print("\n服务收到中断信号，正常退出。")