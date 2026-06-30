#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BRDM文件智能下载系统（UTC模式）- 备用星历源集成版
BKG FTP匿名下载 & CDDIS FTPS匿名下载

优化版策略：
1. 启动时扫描各站点 rinex/119/*.o 和 rinex/FailedOfile/*.o；
2. 历史/昨日星历（Yesterday & Backlog）：
   - BKG 优先下载最终发布的 DLR 多系统星历：BRDM00DLR_S_{year}{doy}0000_01D_MN.rnx.gz
   - CDDIS 备用下载 IGS 多系统星历最终版：BRDC00IGS_R_{year}{doy}0000_01D_MN.rnx.gz
3. 当天实时星历（Today）：
   - BKG 优先下载 WRD_S。若其大小无变化，智能切换为 WRD_R 备份；若 WRD_S 大小恢复变化，则切回 WRD_S。
   - CDDIS 备用下载 brdc{doy}0.{yy}n.gz 作为 GPS 单系统兜底。
"""

import os
import sys
import re
import json
import time
import gzip
import shutil
from datetime import datetime, timedelta, timezone

# ==================== 配置 ====================
FTP_HOST = "igs-ftp.bkg.bund.de"
REMOTE_PATH_TEMPLATE = "/IGS/BRDC/{year}/{doy}/{filename}"

FTP_HOST2 = "gdc.cddis.eosdis.nasa.gov"
REMOTE_PATH_TEMPLATE_2 = "/pub/gnss/data/daily/{year}/brdc/{filename}"

BASE_PATH = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(BASE_PATH, "SoluData")
LOG_DIR = os.path.join(LOCAL_DIR, "brdm")

CHECK_INTERVAL = int(os.environ.get("BRDM_CHECK_INTERVAL", "600"))
MAX_RETRY_COUNT = int(os.environ.get("BRDM_MAX_RETRY", "3"))
RETRY_DELAY = int(os.environ.get("BRDM_RETRY_DELAY", "30"))
FTP_TIMEOUT = int(os.environ.get("BRDM_FTP_TIMEOUT", "60"))
KEEP_LOG_DAYS = int(os.environ.get("BRDM_KEEP_LOG_DAYS", "7"))

# 单轮最多补多少个历史日期
HISTORY_MAX_DATES_PER_RUN = int(os.environ.get("BRDM_HISTORY_MAX_DATES_PER_RUN", "60"))
STATUS_FILE = os.path.join(LOG_DIR, "brdm_status.json")
EXCLUDE_DIRS = {"SoluData", "logs", "__pycache__", ".git"}

RINEX_SCAN_REL_DIRS = [
    os.path.join("rinex", "119"),
    os.path.join("rinex", "FailedOfile"),
]

os.makedirs(LOCAL_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


class BRDMBKGDownloader:

    def __init__(self):
        self.ftp = None
        self.load_status()

    # ==================== 状态 ====================

    def load_status(self):
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                    self.status = json.load(f)
            except Exception:
                self.status = {}
        else:
            self.status = {}

    def save_status(self):
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.status, f, indent=2, ensure_ascii=False)

    # ==================== 日志 ====================

    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_date = datetime.now().strftime("%Y%m%d")
        log_file = os.path.join(LOG_DIR, f"brdm_{log_date}.log")
        text = f"[{timestamp}] {message}"
        print(text)
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(text + "\n")
        except Exception:
            pass

    def clean_old_logs(self, keep_days=KEEP_LOG_DAYS):
        try:
            now = datetime.now()
            deleted_count = 0
            for filename in os.listdir(LOG_DIR):
                match = re.search(r'brdm_(\d{8})\.log', filename)
                if not match:
                    continue
                try:
                    log_date = datetime.strptime(match.group(1), "%Y%m%d")
                    if (now - log_date).days > keep_days:
                        file_path = os.path.join(LOG_DIR, filename)
                        file_size = os.path.getsize(file_path)
                        os.remove(file_path)
                        deleted_count += 1
                        self.log(f"🧹 清理旧日志: {filename} ({self.format_size(file_size)})")
                except Exception:
                    pass
            if deleted_count > 0:
                self.log(f"✅ 已清理 {deleted_count} 个旧日志文件")
        except Exception as e:
            self.log(f"清理日志失败: {e}")

    # ==================== UTC / 日期 ====================

    def get_utc_time(self):
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def date_from_year_doy(self, year, doy):
        return datetime(int(year), 1, 1) + timedelta(days=int(doy) - 1)

    def local_brdm_name(self, utc_date):
        doy = utc_date.strftime("%j")
        yy = utc_date.strftime("%y")
        return f"BRDM{doy}0.{yy}p"

    def local_brdm_path(self, utc_date):
        return os.path.join(LOCAL_DIR, self.local_brdm_name(utc_date))

    def local_brdm_exists(self, utc_date):
        path = self.local_brdm_path(utc_date)
        return os.path.exists(path) and os.path.getsize(path) > 0

    def parse_o_file_date(self, filename):
        match = re.search(r'^[A-Za-z0-9]{4}(\d{4})(\d{3})_\d{4}\.o$', filename, re.IGNORECASE)
        if not match:
            return None
        year = int(match.group(1))
        doy = int(match.group(2))
        return self.date_from_year_doy(year, doy)

    # ==================== FTP 连接（即用即断） ====================

    def connect(self, host, use_ssl=False):
        try:
            from ftplib import FTP, FTP_TLS
            if use_ssl:
                self.log(f"连接 {host} (TLS安全模式)...")
                self.ftp = FTP_TLS(host, timeout=FTP_TIMEOUT)
                self.ftp.login(user="anonymous", passwd="anonymous@example.com")
                self.ftp.prot_p()
            else:
                self.log(f"连接 {host} (常规模式)...")
                self.ftp = FTP(host, timeout=FTP_TIMEOUT)
                self.ftp.login()

            self.ftp.set_pasv(True)
            self.log(f"✓ {host} 连接成功")
            return True
        except Exception as e:
            self.log(f"✗ {host} 连接失败: {e}")
            self.ftp = None
            return False

    def disconnect(self):
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
            self.ftp = None
            self.log("🔌 已主动断开 FTP 连接")

    # ==================== 远程文件 ====================

    def get_remote_path(self, utc_date, source):
        """解析标准源配置对应的远程文件名和路径"""
        year = utc_date.strftime("%Y")
        doy = utc_date.strftime("%j")
        yy = utc_date.strftime("%y")

        pattern = source["pattern"]
        template = source["path_template"]

        filename = pattern.format(year=year, doy=doy, yy=yy)
        remote_path = template.format(year=year, doy=doy, yy=yy, filename=filename)
        return remote_path, filename

    def get_remote_path_custom(self, utc_date, source, pattern_template):
        """解析自定义文件名格式对应的路径，避免二次格式化问题"""
        year = utc_date.strftime("%Y")
        doy = utc_date.strftime("%j")
        yy = utc_date.strftime("%y")
        filename = pattern_template.format(year=year, doy=doy, yy=yy)
        remote_path = source["path_template"].format(year=year, doy=doy, yy=yy, filename=filename)
        return remote_path, filename

    def check_remote_file_exists(self, remote_path):
        from ftplib import error_perm
        try:
            if not self.ftp:
                return None
            self.ftp.size(remote_path)
            return True
        except error_perm:
            return False
        except Exception as e:
            self.log(f"检查远程文件失败: {e}")
            return None

    # ==================== 下载 ====================

    def download_file(self, remote_path, local_gz_path, remote_size):
        try:
            self.log(f"↓ 开始下载 ({self.format_size(remote_size)})")
            with open(local_gz_path, 'wb') as f:
                self.ftp.retrbinary(f'RETR {remote_path}', f.write, blocksize=262144)

            if not os.path.exists(local_gz_path):
                return False

            local_size = os.path.getsize(local_gz_path)
            if local_size != remote_size:
                self.log(f"✗ 下载不完整 ({local_size}/{remote_size})")
                os.remove(local_gz_path)
                return False

            self.log(f"✓ 下载完成 ({self.format_size(local_size)})")
            return True
        except Exception as e:
            self.log(f"✗ 下载失败: {e}")
            if os.path.exists(local_gz_path):
                os.remove(local_gz_path)
            return False

    # ==================== 解压与转换 ====================

    def extract_gz(self, gz_path):
        if not gz_path.endswith('.gz'):
            return None
        extract_path = gz_path[:-3]
        try:
            with gzip.open(gz_path, 'rb') as f_in:
                with open(extract_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
            if not os.path.exists(extract_path):
                return None
            os.remove(gz_path)
            return extract_path
        except Exception as e:
            self.log(f"✗ 解压失败: {e}")
            return None

    def convert_to_brdm(self, decompressed_path, utc_date, remote_size, source_name, 
                        active_format=None, remote_size_S=0, remote_size_R=0):
        try:
            year = utc_date.strftime("%Y")
            doy = utc_date.strftime("%j")
            yy = utc_date.strftime("%y")

            target_filename = self.local_brdm_name(utc_date)
            target_path = self.local_brdm_path(utc_date)

            if os.path.exists(target_path):
                os.remove(target_path)

            shutil.move(decompressed_path, target_path)
            current_time = time.time()
            os.utime(target_path, (current_time, current_time))

            file_size = os.path.getsize(target_path)
            self.log(f"✅ 已保存: {target_filename} ({self.format_size(file_size)})")

            status_key = f"{year}{doy}"
            status_entry = {
                "doy": doy,
                "year": year,
                "yy": yy,
                "source": source_name,
                "remote_size": remote_size,
                "file_size": file_size,
                "download_time": datetime.now().isoformat(),
                "utc_time": utc_date.isoformat()
            }
            # 如果是今日的 BKG 源，保存双流格式的特定大小，方便后续比对是否停滞
            if active_format:
                status_entry["active_format"] = active_format
                status_entry["remote_size_S"] = remote_size_S
                status_entry["remote_size_R"] = remote_size_R

            self.status[status_key] = status_entry
            self.save_status()
            return True
        except Exception as e:
            self.log(f"✗ 转换失败: {e}")
            return False

    def clean_old_files(self, current_doy=None):
        self.log("📁 历史解算模式：不自动清理旧BRDM文件")

    def format_size(self, size_bytes):
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        return f"{size_bytes / (1024 * 1024):.1f}MB"

    # ==================== 指定日期下载 ====================

    def download_date(self, utc_date, verify_remote_update=False, reason=""):
        """
        下载指定UTC日期的BRDM。
        1. 针对当天和历史动态调整 BKG / CDDIS 模式。
        2. BKG当天启用 WRD_S 与 WRD_R 智能双流大小变化切换策略。
        """
        utc_date = datetime(utc_date.year, utc_date.month, utc_date.day)
        year = utc_date.strftime("%Y")
        doy = utc_date.strftime("%j")
        target_name = self.local_brdm_name(utc_date)
        target_path = self.local_brdm_path(utc_date)
        status_key = f"{year}{doy}"

        prefix = f"{reason}: " if reason else ""
        self.log("=" * 60)
        self.log(f"{prefix}检查BRDM {target_name} (UTC {year}-{doy})")

        local_exists = os.path.exists(target_path) and os.path.getsize(target_path) > 0
        if local_exists and not verify_remote_update:
            self.log(f"✅ 本地已存在，跳过: {target_name} ({self.format_size(os.path.getsize(target_path))})")
            return True

        # 判断是否是当天
        is_today = (utc_date.date() == self.get_utc_time().date())

        # 动态指定下载模式
        if is_today:
            bkg_pattern = "BRDC00WRD_S_{year}{doy}0000_01D_MN.rnx.gz"
            cddis_pattern = "brdc{doy}0.{yy}n.gz"  # 今日兜底用GPS单系统
        else:
            bkg_pattern = "BRDM00DLR_S_{year}{doy}0000_01D_MN.rnx.gz"  # 历史使用高质量混合星历
            cddis_pattern = "BRDC00IGS_R_{year}{doy}0000_01D_MN.rnx.gz"  # 历史使用多系统合并版

        sources = [
            {
                "name": "BKG",
                "host": FTP_HOST,
                "use_ssl": False,
                "pattern": bkg_pattern,
                "path_template": REMOTE_PATH_TEMPLATE
            },
            {
                "name": "CDDIS",
                "host": FTP_HOST2,
                "use_ssl": True,
                "pattern": cddis_pattern,
                "path_template": REMOTE_PATH_TEMPLATE_2
            }
        ]

        # 读取上一次的下载状态
        status_data = self.status.get(status_key, {})
        saved_source = status_data.get("source")
        saved_remote_size = status_data.get("remote_size")
        saved_remote_size_S = status_data.get("remote_size_S", 0)
        saved_remote_size_R = status_data.get("remote_size_R", 0)

        for source in sources:
            source_name = source["name"]
            self.log(f"📡 尝试获取星历源: {source_name}")

            try:
                if not self.connect(source["host"], use_ssl=source["use_ssl"]):
                    self.log(f"⚠ 连接 {source_name} 失败，准备尝试下一个备用源...")
                    continue

                # ==========================================
                # 分支 A：BKG 当天实时的 WRD_S vs WRD_R 智能切换
                # ==========================================
                if source_name == "BKG" and is_today:
                    pattern_S = "BRDC00WRD_S_{year}{doy}0000_01D_MN.rnx.gz"
                    pattern_R = "BRDC00WRD_R_{year}{doy}0000_01D_MN.rnx.gz"

                    remote_path_S, filename_S = self.get_remote_path_custom(utc_date, source, pattern_S)
                    remote_path_R, filename_R = self.get_remote_path_custom(utc_date, source, pattern_R)

                    exists_S = self.check_remote_file_exists(remote_path_S)
                    exists_R = self.check_remote_file_exists(remote_path_R)

                    if not exists_S and not exists_R:
                        self.log("⏳ BKG 当天的 WRD_S 和 WRD_R 远程文件均未生成")
                        self.disconnect()
                        continue

                    # 安全读取文件大小，防止临时异常导致崩盘
                    remote_size_S = 0
                    if exists_S:
                        try:
                            remote_size_S = self.ftp.size(remote_path_S)
                        except Exception:
                            exists_S = False

                    remote_size_R = 0
                    if exists_R:
                        try:
                            remote_size_R = self.ftp.size(remote_path_R)
                        except Exception:
                            exists_R = False

                    choose_format = None
                    target_remote_path = None
                    target_filename = None
                    target_remote_size = 0

                    # 强制下载标记（本地没有或上次下载的源不是BKG）
                    force_download = not local_exists or saved_source != "BKG"

                    # 决策下载格式
                    if exists_S and (remote_size_S != saved_remote_size_S or force_download):
                        # WRD_S 存在且处于变化中，或者无本地文件，优先选用 S
                        self.log(f"⚡ BKG 的 WRD_S 处于更新/活跃状态 ({self.format_size(saved_remote_size_S)} -> {self.format_size(remote_size_S)})，下载 S 文件。")
                        choose_format = "S"
                        target_remote_path = remote_path_S
                        target_filename = filename_S
                        target_remote_size = remote_size_S
                    elif exists_R and (remote_size_R != saved_remote_size_R or force_download):
                        # 到了这里代表 S 停止更新（大小相同），而 R 有所变化或者强制下载，切换到 R 备份
                        self.log(f"💤 BKG 的 WRD_S 大小未变或不可用，切换下载 WRD_R 文件 ({self.format_size(saved_remote_size_R)} -> {self.format_size(remote_size_R)})。")
                        choose_format = "R"
                        target_remote_path = remote_path_R
                        target_filename = filename_R
                        target_remote_size = remote_size_R
                    else:
                        self.log(f"⏸️ 本地已是 BKG 今日最新版 (WRD_S: {self.format_size(remote_size_S)}, WRD_R: {self.format_size(remote_size_R)})")
                        self.disconnect()
                        return True

                    local_gz_path = os.path.join(LOCAL_DIR, target_filename)
                    download_success = False

                    for attempt in range(MAX_RETRY_COUNT):
                        if not self.ftp:
                            if not self.connect(source["host"], use_ssl=source["use_ssl"]):
                                if attempt < MAX_RETRY_COUNT - 1:
                                    time.sleep(RETRY_DELAY)
                                continue

                        if self.download_file(target_remote_path, local_gz_path, target_remote_size):
                            download_success = True
                            break

                        if attempt < MAX_RETRY_COUNT - 1:
                            self.log(f"⚠ 下载异常，{RETRY_DELAY} 秒后执行第 {attempt + 2} 次重试...")
                            self.disconnect()
                            time.sleep(RETRY_DELAY)
                        else:
                            self.log("✗ 下载异常，已达到最大重试次数")
                            self.disconnect()

                    if not download_success:
                        self.log("✗ BKG 实时星历下载失败，即将尝试 CDDIS 备用源...")
                        continue

                    decompressed_path = self.extract_gz(local_gz_path)
                    if not decompressed_path:
                        self.disconnect()
                        continue

                    if self.convert_to_brdm(decompressed_path, utc_date, target_remote_size, "BKG", 
                                            active_format=choose_format, remote_size_S=remote_size_S, remote_size_R=remote_size_R):
                        self.log(f"✅ 今日 BRDM 下载成功 (BKG - WRD_{choose_format}): {target_name}")
                        self.disconnect()
                        return True

                # ==========================================
                # 分支 B：常规策略（BKG历史/DLR 或 CDDIS当天/历史）
                # ==========================================
                else:
                    remote_path, filename = self.get_remote_path(utc_date, source)
                    exists = self.check_remote_file_exists(remote_path)

                    if exists is None:
                        self.log(f"⚠ 无法确定 {source_name} 远程文件状态: {filename}")
                        self.disconnect()
                        continue
                    if not exists:
                        self.log(f"⏳ {source_name} 远程文件尚未更新: {filename}")
                        self.disconnect()
                        continue

                    remote_size = self.ftp.size(remote_path)
                    self.log(f"[{source_name}] 远程文件大小: {self.format_size(remote_size)}")

                    # 本地是否已是最新的校验
                    if local_exists and saved_source == source_name and saved_remote_size == remote_size:
                        self.log(f"⏸️ 本地已是 {source_name} 最新发布版: {target_name}")
                        self.disconnect()
                        return True

                    if local_exists:
                        if saved_source != source_name:
                            self.log(f"🔄 检测到源变更 (从 {saved_source} 变更为 {source_name})，重写: {target_name}")
                        elif saved_remote_size != remote_size:
                            self.log(f"🔄 远程文件更新，重下: {target_name} ({self.format_size(saved_remote_size)} -> {self.format_size(remote_size)})")
                    else:
                        self.log(f"🔄 本地缺失，准备下载: {target_name}")

                    local_gz_path = os.path.join(LOCAL_DIR, filename)
                    download_success = False

                    for attempt in range(MAX_RETRY_COUNT):
                        if not self.ftp:
                            if not self.connect(source["host"], use_ssl=source["use_ssl"]):
                                if attempt < MAX_RETRY_COUNT - 1:
                                    time.sleep(RETRY_DELAY)
                                continue

                        if self.download_file(remote_path, local_gz_path, remote_size):
                            download_success = True
                            break

                        if attempt < MAX_RETRY_COUNT - 1:
                            self.log(f"⚠ 下载异常，{RETRY_DELAY} 秒后执行第 {attempt + 2} 次重试...")
                            self.disconnect()
                            time.sleep(RETRY_DELAY)
                        else:
                            self.log("✗ 下载异常，已达到最大重试次数")
                            self.disconnect()

                    if not download_success:
                        self.disconnect()
                        self.log(f"✗ 尝试 {source_name} 资源下载全部失败，准备跳转至下一个备用源...")
                        continue

                    decompressed_path = self.extract_gz(local_gz_path)
                    if not decompressed_path:
                        self.disconnect()
                        continue

                    if self.convert_to_brdm(decompressed_path, utc_date, remote_size, source_name):
                        self.log(f"✅ BRDM 下载并转换成功 (来源: {source_name}): {target_name}")
                        self.disconnect()
                        return True

            except Exception as e:
                self.log(f"❌ 尝试源 {source_name} 下载异常: {e}")
                self.disconnect()
            finally:
                self.disconnect()

        self.log(f"❌ 错误: 所有的星历服务器源在当前轮次均不可用，无法更新 {target_name}")
        return False

    # ==================== 历史O文件联动 ====================

    def discover_required_brdm_dates_from_rinex_backlog(self):
        required_dates = set()
        station_file_count = 0

        if not os.path.isdir(BASE_PATH):
            return required_dates

        for item in os.listdir(BASE_PATH):
            station_root = os.path.join(BASE_PATH, item)
            if not os.path.isdir(station_root):
                continue
            if item in EXCLUDE_DIRS:
                continue

            for rel_dir in RINEX_SCAN_REL_DIRS:
                rinex_dir = os.path.join(station_root, rel_dir)
                if not os.path.isdir(rinex_dir):
                    continue

                try:
                    for filename in os.listdir(rinex_dir):
                        if not filename.lower().endswith('.o'):
                            continue
                        dt = self.parse_o_file_date(filename)
                        if dt is None:
                            continue
                        required_dates.add(datetime(dt.year, dt.month, dt.day))
                        station_file_count += 1
                except Exception as e:
                    self.log(f"扫描O文件目录失败: {rinex_dir}, error={e}")

        if station_file_count > 0:
            self.log(f"📁 扫描到 {station_file_count} 个O文件，涉及 {len(required_dates)} 个BRDM日期")
        return required_dates

    def download_backlog_required_brdm(self):
        dates = sorted(self.discover_required_brdm_dates_from_rinex_backlog())
        if not dates:
            self.log("📁 未发现需要补齐BRDM的历史/待处理O文件")
            return True

        utc_now = self.get_utc_time()
        missing_or_recent_dates = []
        for dt in dates:
            is_recent = (utc_now - dt).days <= 2
            if is_recent or not self.local_brdm_exists(dt):
                missing_or_recent_dates.append(dt)

        if not missing_or_recent_dates:
            self.log(f"✅ 历史/待处理O文件所需BRDM均已存在，共 {len(dates)} 个日期")
            return True

        if len(missing_or_recent_dates) > HISTORY_MAX_DATES_PER_RUN:
            self.log(
                f"⚠ 待处理BRDM日期较多: {len(missing_or_recent_dates)}，"
                f"本轮只处理前 {HISTORY_MAX_DATES_PER_RUN} 个"
            )
            missing_or_recent_dates = missing_or_recent_dates[:HISTORY_MAX_DATES_PER_RUN]

        ok_all = True
        self.log(f"🧩 发现 {len(missing_or_recent_dates)} 个需补充/校验的BRDM日期，开始处理")
        for dt in missing_or_recent_dates:
            is_recent = (utc_now - dt).days <= 2
            reason = "历史O文件近2日补齐(强制核对大小)" if is_recent else "历史O文件补齐(本地缺失)"
            ok = self.download_date(dt, verify_remote_update=is_recent, reason=reason)
            ok_all = ok_all and ok
            time.sleep(1)
        return ok_all

    # ==================== 主检查 ====================

    def check_and_download(self):
        utc_time = self.get_utc_time()
        yesterday = utc_time - timedelta(days=1)

        # 1. 优先验证和补齐“昨天”（可下载 DLR 高质量多系统混合星历最终版）
        self.log("🕒 校验前一天 (Yesterday) 的星历更新...")
        ok_yesterday = self.download_date(yesterday, verify_remote_update=True, reason="昨天UTC更新")

        # 2. 验证和下载“今天”的星历追加（可在 WRD_S 与 WRD_R 间自动切换）
        self.log("🕒 校验当前 (Today) 的星历更新...")
        ok_today = self.download_date(utc_time, verify_remote_update=True, reason="当前UTC更新")

        return ok_yesterday and ok_today

    # ==================== 主循环 ====================

    def run(self):
        try:
            self.log("=" * 60)
            self.log("BRDM智能下载服务启动 - 历史O文件补齐 + BKG/CDDIS多源智能切换")
            self.log("=" * 60)
            self.log(f"根目录: {BASE_PATH}")
            self.log(f"下载目录: {LOCAL_DIR}")
            self.log(f"检查间隔: {CHECK_INTERVAL // 60} 分钟")
            self.log("文件策略: 保留历史BRDM，不自动删除")

            self.clean_old_logs()
            self.clean_old_files()

            self.download_backlog_required_brdm()
            self.check_and_download()

            self.log(f"✅ 服务进入监控状态 (每 {CHECK_INTERVAL // 60} 分钟检查一次)")

            while True:
                try:
                    time.sleep(CHECK_INTERVAL)
                    self.download_backlog_required_brdm()
                    self.check_and_download()
                except Exception as e:
                    self.log(f"主循环异常: {e}")
                    self.disconnect()
                    sys.exit(1)

        except KeyboardInterrupt:
            self.log("👋 收到用户中止信号，程序已退出")
            self.disconnect()
        except Exception as e:
            self.log(f"致命错误: {e}")
            self.disconnect()
            sys.exit(1)


def main():
    downloader = BRDMBKGDownloader()
    downloader.run()


if __name__ == "__main__":
    main()