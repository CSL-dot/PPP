# <center>RealTime-PPP软件说明1.0</center>

本软件基于Python代码开发，主要用途为实时接收RTCM数据流进行PPP解算得到实时PWV。

## 1. 文件介绍
### 根目录文件
1. `brdm_downloader.py`：下载实时BKG、IGS广播星历文件
2. `sp3_downloader.py`：下载WUM、GRG近实时精密星历文件，预报一天
3. `rt_stream32_lowcpu2.py`：实时数据流接收
4. `str2str.exe`：数据流接收程序
5. `controller_master.py`：进程总控
6. `Data_clean.py`：数据动态清理
7. `start_end PPP.bat`：一键起算、结束脚本

### 子目录文件
1. `jiema/chushihua1.conf`：解算配置文件
2. `path_config`：路径配置文件
3. `readtime_ppp_controller.py`：PPP解算代码
4. `rtcm_slice.py`：切分rtcm文件
5. `rtcm_decode.exe`：rtcm解码程序
6. `RTKLIB_demo`：PPP解算程序

### 文件目录结构
- 根目录：
  - `BHJD`：测站子目录
  - `logs`：日志目录
  - `SoluData`：广播星历、精密星历存储目录

- `BHJD` 测站子目录内部：
  - `rtknavi`：存储实时rtcm3原始文件
  - `rtcm`：存储切片后的rtcm文件
  - `rinex`：存储解码后的观测obs文件
  - `out`：存储PPP解算结果文件
  - `logs`：单站解算日志

![本地路径](<Directory Structure.PNG>)

## 2. 使用方法
1. 修改测站目录下 `conf` 配置文件；
2. 根据需求调整 `rt_stream32_lowcpu2.py` 内站点参数；
3. 双击运行 `start_end PPP.bat` 一键启动解算流程。
